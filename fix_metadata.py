# -*- coding: utf-8 -*-
"""Song Metadata Fixer — TUI & CLI for fixing audio tags via NetEase / QQ Music / Kugou / Bilibili."""

from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import json
import logging
import re
import tempfile
import time
import traceback
import unicodedata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import requests
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, APIC, USLT, TDRC
from mutagen.mp4 import MP4, MP4Cover

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    Log,
    ProgressBar,
    RadioButton,
    RadioSet,
    Static,
)

# ═══════════════════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════════════════

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

JUNK_HINTS = (
    "karaoke", "kareoke", "伴奏", "网友改编", "originallyperformed",
    "completeversion", "现场", "(live)", "（live）", "翻奏", "拼接",
    "变速", "纯音乐",
)

BILI_MIX_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

AUDIO_EXTS = {".mp3", ".m4a"}

DELIM_STRATEGIES = [
    ("_", "歌名_歌手", lambda s: s.split("_")),
    (" - ", "歌名 - 歌手", lambda s: s.split(" - ")),
    (" -", "歌名-歌手", lambda s: s.split(" -")),
    ("&", "歌名&歌手", lambda s: s.split("&")),
    ("、", "歌名、歌手", lambda s: s.split("、")),
    (",", "歌名,歌手", lambda s: s.split(",")),
]

log = logging.getLogger("song-fixer")


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.replace("&", " and ").replace("／", "/").replace("｜", "|")
    s = re.sub(r"[\u200e\u200f\u202a-\u202e\ufeff]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _strip_paren(s: str) -> str:
    for pat in (r"（[^（）]*）", r"\([^()]*\)", r"【[^】]*】", r"\[[^\]]*\]"):
        s = re.sub(pat, "", s)
    return s


def _bare(s: str) -> str:
    s = _strip_paren(s)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", s).lower()


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _title_sim(q: str, r: str) -> float:
    qn, rn = _norm(q), _norm(r)
    vals = [_ratio(qn, rn)]
    qb, rb = _bare(q), _bare(r)
    if qb and rb:
        vals.append(_ratio(qb, rb))
        vals.append(float(qb == rb))
    vals.append(float(qn == rn))
    return max(vals)


def _artist_overlap(qa: str, ra: str) -> bool:
    if not qa or not ra:
        return False
    qa_n, ra_n = _norm(qa), _norm(ra)
    if qa_n == ra_n:
        return True
    q_toks = {t for t in re.split(r"[\s/|,]+", qa_n) if len(t) > 1}
    r_toks = {t for t in re.split(r"[\s/|,]+", ra_n) if len(t) > 1}
    if q_toks & r_toks:
        return True
    qb, rb = _bare(qa), _bare(ra)
    return (bool(qb) and qb in rb) or (bool(rb) and rb in qa_n)


def _artist_sim(qa: str, ra: str) -> float:
    if not qa and not ra:
        return 1.0
    if not qa or not ra:
        return 0.0
    return max(
        _ratio(_norm(qa), _norm(ra)),
        _artist_overlap(qa, ra) * 0.9,
        0.0,
    )


def _junk_level(it: dict) -> float:
    t = _norm(it.get("title") or "")
    if any(x in t for x in JUNK_HINTS):
        return 0.35
    a = _norm(it.get("album") or "")
    if any(x in a for x in JUNK_HINTS):
        return 0.2
    return 0.0


def _sanitize(s: str) -> str:
    if not s:
        return s
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\u200e\u200f\u202a-\u202e\ufeff\u00a0]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _load_text(p: Path) -> str | None:
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return p.read_text(encoding=enc)
        except Exception:
            continue
    return None


def _safe_name(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", s)


# ═══════════════════════════════════════════════════════════════════════════
#  Filename parsing — multi-strategy with confidence
# ═══════════════════════════════════════════════════════════════════════════

def parse_fname_multi(fname: str, strategy: str | None = None) -> tuple[str, str, str, bool]:
    """Parse filename. strategy=None means auto-detect. Returns (base, title, artist, confidence)."""
    base = Path(fname).stem
    if strategy is not None and strategy != "auto":
        delim = strategy
        parts = base.split(delim, 1)
        if len(parts) == 2:
            t, a = parts[0].strip(), parts[1].strip()
            t = re.sub(r"\s+", " ", t)
            a = re.sub(r"\s+", " ", a)
            return base, t, a, True
        return base, base, "", True

    # auto: try each delimiter
    for delim, _name, _fn in DELIM_STRATEGIES:
        parts = base.split(delim, 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            t = re.sub(r"\s+", " ", parts[0].strip())
            a = re.sub(r"\s+", " ", parts[1].strip())
            return base, t, a, True

    # no delimiter — title only, confidence=True
    return base, base, "", True


def detect_best_strategy(files: list[str]) -> list[tuple[str, str, int]]:
    """Return list of (strategy_key, display_name, match_count) sorted by count desc."""
    results: list[tuple[str, str, int]] = []
    for delim, display, _fn in DELIM_STRATEGIES:
        count = 0
        for f in files:
            stem = Path(f).stem
            parts = stem.split(delim, 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                count += 1
        results.append((delim, display, count))
    results.append(("auto", "自动适配每首歌", len(files)))
    results.sort(key=lambda x: x[2], reverse=True)
    return results


# ═══════════════════════════════════════════════════════════════════════════
#  MetadataFixer core
# ═══════════════════════════════════════════════════════════════════════════

class MetadataFixer:
    PLATFORMS = [
        ("netease", "_search_netease"),
        ("qqmusic", "_search_qqmusic"),
        ("kugou", "_search_kugou"),
        ("bilibili", "_search_bilibili"),
    ]

    def __init__(self, music_dir: str | Path):
        self.base_dir = Path(music_dir).resolve()
        self.lyr_dir = self.base_dir / "Lyrics"
        self.cov_dir = self.base_dir / "Covers"
        tmp = Path(tempfile.gettempdir())
        self.cache_file = tmp / "song-fixer" / "search_cache.json"
        self.report_file = tmp / "song-fixer" / "fix_report.json"
        self.backup_file = tmp / "song-fixer" / "metadata_backup.json"
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8))
        self.cache: dict[str, list[dict]] = {}
        self._load_cache()
        self._bili_key: str | None = None
        self._bili_key_ts: float = 0.0
        self._netease_csrf: str | None = None

    # ── cache ──────────────────────────────────────────────────────────────

    def _load_cache(self) -> None:
        if self.cache_file.exists():
            try:
                self.cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Failed to load cache: %s", exc)
                self.cache = {}

    def _save_cache(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as exc:
            log.warning("Failed to save cache: %s", exc)

    def _get_cached(self, platform: str, query: str) -> list[dict] | None:
        return self.cache.get(f"{platform}|{query}")

    def _set_cached(self, platform: str, query: str, results: list[dict]) -> None:
        self.cache[f"{platform}|{query}"] = results
        self._save_cache()

    def _fetch_platform(self, platform: str, query: str) -> list[dict]:
        cached = self._get_cached(platform, query)
        if cached is not None:
            return cached
        method_name = next(m for name, m in self.PLATFORMS if name == platform)
        method = getattr(self, method_name)
        try:
            results = method(query)
        except Exception as exc:
            log.warning("[%s] query=%r failed: %s", platform, query, exc)
            results = []
        self._set_cached(platform, query, results)
        return results

    # ── Bilibili WBI ───────────────────────────────────────────────────────

    def _refresh_bili_key(self) -> None:
        if self._bili_key and (time.time() - self._bili_key_ts) < 5400:
            return
        try:
            j = self.session.get("https://api.bilibili.com/x/web-interface/nav", timeout=15).json()
            img = j["data"]["wbi_img"]
            def _key(u: str) -> str:
                return u.split("/")[-1].split(".")[0]
            raw = _key(img["img_url"]) + _key(img["sub_url"])
            self._bili_key = "".join(raw[i] for i in BILI_MIX_TAB)[:32]
            self._bili_key_ts = time.time()
        except Exception as exc:
            log.warning("Failed to refresh Bilibili WBI key: %s", exc)

    def _bili_sign(self, params: dict) -> dict:
        self._refresh_bili_key()
        if not self._bili_key:
            raise RuntimeError("Bilibili WBI key unavailable")
        p = dict(params)
        p["wts"] = int(time.time())
        q = urlencode(sorted(p.items()))
        p["w_rid"] = hashlib.md5((q + self._bili_key).encode()).hexdigest()
        return p

    # ── Netease ────────────────────────────────────────────────────────────

    def _ensure_netease_csrf(self) -> None:
        if self._netease_csrf:
            return
        try:
            r = self.session.get("https://music.163.com", timeout=15)
            self._netease_csrf = r.cookies.get("__csrf", "")
        except Exception as exc:
            log.warning("Failed to get Netease CSRF: %s", exc)
            self._netease_csrf = ""

    def _search_netease(self, query: str) -> list[dict]:
        self._ensure_netease_csrf()
        url = "https://music.163.com/api/search/get/web"
        params: dict[str, Any] = {"s": query, "type": 1, "offset": 0, "limit": 15}
        if self._netease_csrf:
            params["csrf_token"] = self._netease_csrf
        try:
            r = self.session.get(url, params=params, headers={"Referer": "https://music.163.com"}, timeout=15)
            data = r.json()
            songs = ((data.get("result") or {}).get("songs")) or []
            if not songs:
                log.warning("[netease] empty (code=%s, msg=%s)", data.get("code"), data.get("message"))
            out = []
            for s in songs:
                arts = [a.get("name", "") for a in s.get("artists", [])]
                al = s.get("album") or {}
                year = None
                pt = al.get("publishTime")
                if pt:
                    year = datetime.datetime.fromtimestamp(pt / 1000).year
                out.append({
                    "source": "netease", "title": s.get("name", ""),
                    "artists": arts, "artist": " / ".join(a for a in arts if a),
                    "album": al.get("name", ""), "year": year,
                    "sid": s.get("id"),
                    "cover_url": al.get("picUrl") or al.get("blurPicUrl"),
                })
            return out
        except Exception as exc:
            log.warning("[netease] query=%r failed: %s", query, exc)
            return []

    # ── QQ Music ───────────────────────────────────────────────────────────

    def _search_qqmusic(self, query: str) -> list[dict]:
        url = "https://u.y.qq.com/cgi-bin/musicu.fcg"
        payload = {
            "req_0": {
                "module": "music.search.SearchCgiService",
                "method": "DoSearchForQQMusicDesktop",
                "param": {"query": query, "num_per_page": 15, "page_num": 1, "search_type": 0},
            },
            "comm": {"uin": 0},
        }
        try:
            r = self.session.post(url, data={"format": "json", "data": json.dumps(payload)},
                                  headers={"Referer": "https://y.qq.com"}, timeout=15)
            j = r.json()
            body = ((j.get("req_0") or {}).get("data") or {}).get("body") or {}
            songs = (body.get("song") or {}).get("list") or []
            if not songs:
                code = (j.get("req_0") or {}).get("code")
                log.warning("[qqmusic] empty (code=%s)", code)
                if code and code != 0:
                    log.warning("[qqmusic] possible sign error, code=%s", code)
            out = []
            for s in songs:
                arts = [a.get("name", "") for a in s.get("singer", [])]
                year = None
                if s.get("pubtime"):
                    year = datetime.datetime.fromtimestamp(s["pubtime"]).year
                mid = s.get("albummid", "")
                out.append({
                    "source": "qqmusic", "title": s.get("songname", ""),
                    "artists": arts, "artist": " / ".join(a for a in arts if a),
                    "album": s.get("albumname", ""), "year": year,
                    "sid": s.get("songmid"),
                    "cover_url": f"https://y.gtimg.cn/music/photo_new/T002R800x800M000{mid}.jpg" if mid else None,
                })
            return out
        except Exception as exc:
            log.warning("[qqmusic] query=%r failed: %s", query, exc)
            return []

    # ── Kugou ──────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_kugou_items(items: list[dict], source: str = "kugou") -> list[dict]:
        out = []
        for s in items:
            arts = [a.strip() for a in re.split(r"[、,，&]", s.get("SingerName", "")) if a.strip()]
            year = None
            rd = s.get("ReleaseDate")
            if rd:
                m = re.search(r"(\d{4})", str(rd))
                if m:
                    year = int(m.group(1))
            out.append({
                "source": source, "title": s.get("SongName", ""),
                "artists": arts, "artist": " / ".join(arts),
                "album": s.get("AlbumName", ""), "year": year,
                "sid": s.get("FileHash"), "cover_url": None,
            })
        return out

    def _search_kugou(self, query: str) -> list[dict]:
        try:
            r = self.session.get(
                "https://songsearch.kugou.com/song_search_v2",
                params={"keyword": query, "page": 1, "pagesize": 15, "platform": "WebFilter",
                        "userid": -1, "clientver": 2000, "iscorrection": 1, "privilege_filter": 0, "filter": 10},
                headers={"Referer": "https://www.kugou.com"}, timeout=15,
            )
            j = r.json()
            if j.get("status") != 1:
                log.warning("[kugou] primary status=%s, fallback", j.get("status"))
                return self._search_kugou_fallback(query)
            lists = (j.get("data") or {}).get("lists") or []
            return self._parse_kugou_items(lists)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("[kugou] not JSON: %s", exc)
            return self._search_kugou_fallback(query)
        except Exception as exc:
            log.warning("[kugou] failed: %s", exc)
            return self._search_kugou_fallback(query)

    def _search_kugou_fallback(self, query: str) -> list[dict]:
        try:
            r = self.session.get(
                "https://mobilecdn.kugou.com/api/v3/search/song",
                params={"format": "json", "keyword": query, "page": 1, "pagesize": 15},
                headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10) Mobile Safari/537.36"},
                timeout=15,
            )
            j = r.json()
            items = (j.get("data") or {}).get("info") or []
            return self._parse_kugou_items(items, source="kugou")
        except Exception as exc:
            log.warning("[kugou-fallback] failed: %s", exc)
            return []

    # ── Bilibili ───────────────────────────────────────────────────────────

    def _search_bilibili(self, query: str) -> list[dict]:
        for attempt in range(2):
            try:
                self.session.get("https://www.bilibili.com", timeout=8)
                params = self._bili_sign({"search_type": "video", "keyword": query, "page": 1})
                r = self.session.get(
                    "https://api.bilibili.com/x/web-interface/wbi/search/type",
                    params=params, headers={"Referer": "https://www.bilibili.com"}, timeout=15,
                )
                if r.status_code == 412:
                    log.warning("[bilibili] 412 rate limit (attempt %d)", attempt + 1)
                    try:
                        self.session.get("https://www.bilibili.com", timeout=8)
                    except Exception:
                        pass
                    time.sleep(1.0)
                    continue
                j = r.json()
                res = (j.get("data") or {}).get("result") or []
                out = []
                for s in res:
                    if s.get("type") not in ("video", "bili_user"):
                        continue
                    title = re.sub(r"<[^>]+>", "", s.get("title", ""))
                    out.append({
                        "source": "bilibili", "title": title,
                        "artists": [s.get("author", "")], "artist": s.get("author", ""),
                        "album": "", "year": None, "sid": s.get("bvid"), "cover_url": None,
                    })
                return out
            except Exception as exc:
                log.warning("[bilibili] attempt %d failed: %s", attempt + 1, exc)
        return []

    # ── matching ───────────────────────────────────────────────────────────

    @staticmethod
    def _best_match(q_title: str, q_artist: str, results: list[dict],
                    threshold: float = 0.85) -> tuple[dict, float] | None:
        best: tuple[dict, float] | None = None
        for it in results:
            ts = _title_sim(q_title, it["title"])
            if not ts:
                continue
            ra = it.get("artist") or ""
            asim = _artist_sim(q_artist, ra)
            aov = _artist_overlap(q_artist, ra)
            if q_artist:
                score = ts * 0.6 + asim * 0.4
                if aov:
                    score += 0.05
                score = min(score, 1.0)
            else:
                score = ts
            score = max(0.0, score - _junk_level(it))
            if score < threshold:
                continue
            if best is None or score > best[1]:
                best = (it, score)
        return best

    @staticmethod
    def _merge_artist(q_artist: str | None, res_artist: str | None, n_artists: int) -> str | None:
        if not q_artist or res_artist is None or n_artists >= 6:
            return res_artist
        have = _bare(res_artist) or ""
        add: list[str] = []
        toks = [t.strip() for t in re.split(r"[，、/|&\s]+", q_artist) if t.strip()]
        for t in toks:
            tb = _bare(t)
            if not tb or tb in have:
                continue
            if any(_ratio(tb, _bare(x)) >= 0.85 for x in re.split(r"[，、/|&]+", res_artist)):
                continue
            if add and t.lower() in ("feat", "ft", "remix", "radio", "edit", "slowed", "sped up"):
                continue
            add.append(t)
        if not add:
            return res_artist
        joined = " / ".join(add)
        return f"{res_artist} / {joined}" if res_artist else joined

    def _find_metadata(self, q_title: str, q_artist: str) -> dict | None:
        for pname, _ in self.PLATFORMS:
            queries: list[str] = []
            q_all = f"{q_title} {q_artist}".strip()
            queries.append(q_all)
            if q_title != q_all:
                queries.append(q_title)
            for q in queries:
                results = self._fetch_platform(pname, q)
                if not results:
                    continue
                if pname == "bilibili":
                    m = self._best_match(q_title, q_artist, results, threshold=0.72)
                    if m:
                        b, sc = m
                        return {
                            "source": "bilibili", "title": q_title, "artist": q_artist,
                            "artists": [q_artist], "album": "", "year": None,
                            "sid": b.get("sid"), "cover_url": None, "score": sc, "query": q,
                        }
                else:
                    m = self._best_match(q_title, q_artist, results)
                    if m:
                        b, sc = m
                        if _title_sim(q_title, b["title"]) >= 0.72:
                            out = dict(b)
                            out["artist"] = self._merge_artist(q_artist, out["artist"], len(out.get("artists") or []))
                            out["score"] = sc
                            out["query"] = q
                            return out
                time.sleep(0.08)
        return None

    # ── tag I/O ────────────────────────────────────────────────────────────

    @staticmethod
    def _read_tags(path: Path) -> dict:
        ext = path.suffix.lower()
        tags: dict[str, str] = {}
        try:
            if ext == ".mp3":
                t = ID3(path)
                for fid in ("TIT2", "TPE1", "TALB", "TPE2", "TDRC"):
                    frame = t.get(fid)
                    if frame:
                        tags[fid] = str(frame.text[0])
                if "APIC:" in t:
                    tags["APIC:"] = f"<{len(t['APIC:'].data)} bytes>"
                uslt = t.get("USLT::'zho'")
                if uslt:
                    tags["USLT::'zho'"] = f"<{len(str(uslt.text))} chars>"
            elif ext == ".m4a":
                m = MP4(path)
                amap = {"\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album",
                         "aART": "albumartist", "\xa9day": "year"}
                for atom, name in amap.items():
                    if atom in m:
                        tags[name] = m[atom][0] if m[atom] else ""
                if "covr" in m:
                    tags["covr"] = f"<{len(m['covr'][0])} bytes>"
                if "\xa9lyr" in m:
                    tags["\xa9lyr"] = f"<{len(m['\xa9lyr'][0])} chars>"
        except Exception:
            pass
        return tags

    def _find_local_cover(self, basename: str) -> Path | None:
        if not self.cov_dir.exists():
            return None
        for f in self.cov_dir.iterdir():
            if f.suffix.lower() in (".jpg", ".jpeg", ".png") and f.name.rsplit("-", 1)[0] == basename:
                return f
        return None

    def _find_local_lyrics(self, basename: str) -> Path | None:
        for suffix in (".lrc", "_trans.lrc"):
            p = self.lyr_dir / f"{basename}{suffix}"
            if p.exists():
                return p
        return None

    def _download_cover(self, url: str) -> bytes | None:
        try:
            r = self.session.get(url, timeout=15)
            if r.status_code == 200 and r.content[:3] in (b"\xff\xd8\xff", b"\x89PN"):
                return r.content
            log.warning("[cover] HTTP %d for %s", r.status_code, url)
        except Exception as exc:
            log.warning("[cover] download failed %s: %s", url, exc)
        return None

    @staticmethod
    def _tag_diff(old: dict, new: dict) -> dict[str, tuple[str, str]]:
        return {k: (old.get(k, ""), v) for k, v in new.items() if old.get(k, "") != v}

    def _write_tags(self, path: Path, info: dict, basename: str,
                    fields: dict[str, bool] | None = None,
                    cover_mode: str = "local",
                    lyrics_mode: str = "local") -> bool:
        if fields is None:
            fields = {"title": True, "artist": True, "album": True, "albumartist": True, "year": True}

        title = _sanitize(info.get("title") or basename) if fields.get("title") else None
        artist = _sanitize(info.get("artist") or "") if fields.get("artist") else None
        album = _sanitize(info.get("album") or "") if fields.get("album") else None
        albumartist = _sanitize(info.get("albumartist") or (artist or "")) if fields.get("albumartist") else None
        year = info.get("year") if fields.get("year") else None

        cover_data = None
        if cover_mode != "none":
            cov_path = self._find_local_cover(basename)
            if cov_path:
                cover_data = cov_path.read_bytes()
            elif cover_mode == "download" and info.get("cover_url"):
                cover_data = self._download_cover(info["cover_url"])

        lyrics = None
        if lyrics_mode != "none":
            lyr_path = self._find_local_lyrics(basename)
            if lyr_path:
                lyrics = _load_text(lyr_path)

        ext = path.suffix.lower()

        if ext == ".mp3":
            tags = ID3(path)
            old: dict[str, str] = {}
            for fid in ("TIT2", "TPE1", "TALB", "TPE2", "TDRC"):
                frame = tags.get(fid)
                if frame:
                    old[fid] = str(frame.text[0])
            if "APIC:" in tags:
                old["APIC:"] = f"<{len(tags['APIC:'].data)} bytes>"
            uslt = tags.get("USLT::'zho'")
            if uslt:
                old["USLT::'zho'"] = f"<{len(str(uslt.text))} chars>"

            new: dict[str, str] = {}
            if title is not None:
                new["TIT2"] = title
            if artist is not None:
                new["TPE1"] = artist
            if album is not None and album:
                new["TALB"] = album
            if albumartist is not None and albumartist:
                new["TPE2"] = albumartist
            if year is not None:
                new["TDRC"] = str(year)

            for fid, val in new.items():
                tags.delall(fid)
                tags.add({
                    "TIT2": TIT2(encoding=3, text=[val]),
                    "TPE1": TPE1(encoding=3, text=[val]),
                    "TALB": TALB(encoding=3, text=[val]),
                    "TPE2": TPE2(encoding=3, text=[val]),
                    "TDRC": TDRC(encoding=3, text=[val]),
                }[fid])

            if cover_data:
                tags.delall("APIC:")
                mime = "image/png" if cover_data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_data))
            if lyrics:
                tags.delall("USLT:")
                tags.add(USLT(encoding=3, lang="zho", desc="Lyrics", text=lyrics))
            tags.save(path)

        elif ext == ".m4a":
            mp4 = MP4(path)
            old_m: dict[str, str] = {}
            amap = {"\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album",
                     "aART": "albumartist", "\xa9day": "year"}
            for atom, name in amap.items():
                if atom in mp4:
                    old_m[name] = mp4[atom][0] if mp4[atom] else ""
            if "covr" in mp4:
                old_m["covr"] = f"<{len(mp4['covr'][0])} bytes>"
            if "\xa9lyr" in mp4:
                old_m["\xa9lyr"] = f"<{len(mp4['\xa9lyr'][0])} chars>"
            old = old_m

            if title is not None:
                mp4["\xa9nam"] = [title]
            if artist is not None:
                mp4["\xa9ART"] = [artist]
            if album is not None and album:
                mp4["\xa9alb"] = [album]
            if albumartist is not None and albumartist:
                mp4["aART"] = [albumartist]
            if year is not None:
                mp4["\xa9day"] = [str(year)]
            if cover_data:
                fmt = MP4Cover.FORMAT_PNG if cover_data[:8] == b"\x89PNG\r\n\x1a\n" else MP4Cover.FORMAT_JPEG
                mp4["covr"] = [MP4Cover(cover_data, imageformat=fmt)]
            if lyrics:
                mp4["\xa9lyr"] = [lyrics]
            mp4.save(path)
        else:
            log.warning("[write] unsupported %s", ext)
            return False

        new_tags = self._read_tags(path)
        diff = self._tag_diff(old, new_tags)
        for field, (ov, nv) in diff.items():
            log.info("   %s: %r → %r", field, ov, nv)
        if not diff:
            log.info("   (no changes)")
        return True

    def _restore_tags(self, path: Path, old_tags: dict[str, str]) -> bool:
        ext = path.suffix.lower()
        try:
            if ext == ".mp3":
                tags = ID3(path)
                for fid, val in old_tags.items():
                    if fid.startswith("APIC"):
                        continue
                    if fid.startswith("USLT"):
                        tags.delall("USLT:")
                        tags.add(USLT(encoding=3, lang="zho", desc="Lyrics", text=val))
                    elif fid in ("TIT2", "TPE1", "TALB", "TPE2", "TDRC"):
                        tags.delall(fid)
                        tags.add({
                            "TIT2": TIT2(encoding=3, text=[val]),
                            "TPE1": TPE1(encoding=3, text=[val]),
                            "TALB": TALB(encoding=3, text=[val]),
                            "TPE2": TPE2(encoding=3, text=[val]),
                            "TDRC": TDRC(encoding=3, text=[val]),
                        }[fid])
                tags.save(path)
            elif ext == ".m4a":
                mp4 = MP4(path)
                amap = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
                         "albumartist": "aART", "year": "\xa9day"}
                for name, atom in amap.items():
                    if name in old_tags:
                        mp4[atom] = [old_tags[name]]
                mp4.save(path)
            return True
        except Exception as exc:
            log.warning("Restore failed for %s: %s", path.name, exc)
            return False

    # ── scan ───────────────────────────────────────────────────────────────

    def scan_files(self) -> list[str]:
        if not self.base_dir.is_dir():
            return []
        return sorted(
            f.name for f in self.base_dir.iterdir()
            if f.suffix.lower() in AUDIO_EXTS and f.is_file()
        )

    def load_backup(self) -> dict[str, dict]:
        if self.backup_file.exists():
            try:
                return json.loads(self.backup_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def save_backup(self, data: dict[str, dict]) -> None:
        try:
            self.backup_file.parent.mkdir(parents=True, exist_ok=True)
            self.backup_file.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:
            log.warning("Failed to save backup: %s", exc)

    def save_report(self, report: list[dict]) -> None:
        try:
            self.report_file.parent.mkdir(parents=True, exist_ok=True)
            self.report_file.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:
            log.warning("Failed to save report: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
#  TUI — App state
# ═══════════════════════════════════════════════════════════════════════════

class FixerState:
    def __init__(self) -> None:
        self.music_dir: Path = Path.home() / "Desktop" / "music"
        self.files: list[str] = []
        self.strategy: str = "auto"
        self.scope: str = "all"
        self.range_start: int = 0
        self.range_end: int = 0
        self.fields: dict[str, bool] = {
            "title": True, "artist": True, "album": True,
            "albumartist": True, "year": True,
        }
        self.cover_mode: str = "local"
        self.lyrics_mode: str = "local"
        self.dry: bool = False
        self.verbose: bool = False


STATE = FixerState()


# ═══════════════════════════════════════════════════════════════════════════
#  TUI — Screens
# ═══════════════════════════════════════════════════════════════════════════


class FolderPickerScreen(Screen):
    """Folder picker using DirectoryTree."""

    CSS = """
    Screen { align: center middle; }
    #picker_box { width: 80; height: 85%; padding: 1 2; border: solid $accent; }
    #picker_title { text-align: center; text-style: bold; margin-bottom: 1; }
    #dir_tree { height: 1fr; border: solid $surface; }
    #path_bar { height: 3; margin: 1 0; padding: 0 1; background: $surface; }
    #picker_btns { margin-top: 1; align: center middle; }
    #picker_btns Button { margin: 0 1; }
    """

    def __init__(self, on_select: Callable[[Path], None] | None = None) -> None:
        super().__init__()
        self._on_select = on_select

    def compose(self) -> ComposeResult:
        with Vertical(id="picker_box"):
            yield Static("选择音乐文件夹", id="picker_title")
            yield Static(str(STATE.music_dir), id="path_bar")
            yield DirectoryTree(str(STATE.music_dir), id="dir_tree")
            with Horizontal(id="picker_btns"):
                yield Button("选择当前文件夹", id="pick", variant="primary")
                yield Button("b 返回", id="back")
                yield Button("q 退出", id="quit")

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        self.query_one("#path_bar", Static).update(str(event.path))

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        p = Path(event.path)
        self.query_one("#path_bar", Static).update(str(p.parent))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pick":
            path_str = self.query_one("#path_bar", Static).renderable
            selected = Path(str(path_str))
            if not selected.is_dir():
                selected = selected.parent
            STATE.music_dir = selected
            if self._on_select:
                self._on_select(selected)
            self.app.pop_screen()
        elif event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "quit":
            self.app.exit()

class ScanScreen(Screen):
    CSS = """
    Screen { align: center middle; }
    #box { width: 72; height: auto; padding: 1 2; border: solid $accent; }
    #title { text-align: center; text-style: bold; margin-bottom: 1; }
    .row { height: auto; }
    #btn_row { margin-top: 1; align: center middle; }
    #btn_row Button { margin: 0 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._strategies: list[tuple[str, str, int]] = []
        self._recommended_idx: int = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("扫描结果 & 文件名解析", id="title")
            yield Label(f"当前目录：{STATE.music_dir}", id="path_info")
            yield Label("", id="info")
            yield RadioSet(id="strat_radio")
            with Horizontal(id="btn_row"):
                yield Button("选择文件夹", id="pick_dir", variant="default")
                yield Button("确认", id="confirm", variant="primary")
                yield Button("q 退出", id="quit")

    def on_mount(self) -> None:
        from textual.widgets import Button
        STATE.files = MetadataFixer(STATE.music_dir).scan_files()
        total = len(STATE.files)
        self.query_one("#info", Label).update(f"扫描到 {total} 个音频文件")
        self._strategies = detect_best_strategy(STATE.files)
        radio = self.query_one("#strat_radio", RadioSet)
        radio.clear_options()
        for i, (key, display, count) in enumerate(self._strategies):
            label = f"{display}  (匹配 {count} 个)"
            if i == 0 and count > 0:
                label += "  ← 推荐"
                self._recommended_idx = i
            radio.append_option(RadioButton(label))
        if self._strategies:
            radio._selected = self._recommended_idx

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            radio = self.query_one("#strat_radio", RadioSet)
            idx = radio.pressed_index or 0
            STATE.strategy = self._strategies[idx][0]
            self.app.push_screen(ScopeScreen())
        elif event.button.id == "pick_dir":
            self.app.push_screen(FolderPickerScreen(on_select=self._on_dir_picked))
        elif event.button.id == "quit":
            self.app.exit()

    def _on_dir_picked(self, path: Path) -> None:
        STATE.files = MetadataFixer(STATE.music_dir).scan_files()
        total = len(STATE.files)
        self.query_one("#path_info", Label).update(f"当前目录：{STATE.music_dir}")
        self.query_one("#info", Label).update(f"扫描到 {total} 个音频文件")
        self._strategies = detect_best_strategy(STATE.files)
        radio = self.query_one("#strat_radio", RadioSet)
        radio.clear_options()
        self._recommended_idx = 0
        for i, (_key, display, count) in enumerate(self._strategies):
            label = f"{display}  (匹配 {count} 个)"
            if i == 0 and count > 0:
                label += "  ← 推荐"
                self._recommended_idx = i
            radio.append_option(RadioButton(label))
        if self._strategies:
            radio._selected = self._recommended_idx


class ScopeScreen(Screen):
    CSS = """
    Screen { align: center middle; }
    #box { width: 72; height: auto; padding: 1 2; border: solid $accent; }
    #title { text-align: center; text-style: bold; margin-bottom: 1; }
    #btn_row { margin-top: 1; align: center middle; }
    #btn_row Button { margin: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("处理范围", id="title")
            yield RadioSet(
                RadioButton(f"全部文件 ({len(STATE.files)})"),
                RadioButton("仅未匹配过的文件"),
                RadioButton("指定序号范围"),
                RadioButton("恢复上次备份的元数据（回滚）"),
                id="scope_radio",
            )
            yield Label('范围（仅"指定序号"时有效，格式：1-5 或 1,3,5）：', id="range_label")
            yield Static("输入框：（请用 textual Input widget）", id="range_input_placeholder")
            with Horizontal(id="btn_row"):
                yield Button("确认", id="confirm", variant="primary")
                yield Button("b 返回", id="back")
                yield Button("q 退出", id="quit")

    def on_mount(self) -> None:
        self.query_one("#range_label", Label).visible = False
        # placeholder — we'll handle range input via message

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        idx = event.radio_set.pressed_index
        show_range = idx == 2
        self.query_one("#range_label", Label).visible = show_range

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            radio = self.query_one("#scope_radio", RadioSet)
            idx = radio.pressed_index or 0
            STATE.scope = ["all", "unmatched", "range", "restore"][idx]
            if STATE.scope == "restore":
                self.app.push_screen(RestoreScreen())
            elif STATE.scope == "range":
                self.app.push_screen(RangeInputScreen())
            else:
                self.app.push_screen(FieldScreen())
        elif event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "quit":
            self.app.exit()


class RangeInputScreen(Screen):
    CSS = """
    Screen { align: center middle; }
    #box { width: 60; height: auto; padding: 1 2; border: solid $accent; }
    #title { text-align: center; text-style: bold; margin-bottom: 1; }
    #btn_row { margin-top: 1; align: center middle; }
    #btn_row Button { margin: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("输入序号范围", id="title")
            yield Label(f"共 {len(STATE.files)} 个文件，格式：1-5 或 1,3,5")
            from textual.widgets import Input
            yield Input(placeholder="例如: 1-10", id="range_input")
            with Horizontal(id="btn_row"):
                yield Button("确认", id="confirm", variant="primary")
                yield Button("b 返回", id="back")

    @on(Input.Submitted, "#range_input")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._parse_range(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            val = self.query_one("#range_input", Input).value
            self._parse_range(val)
        elif event.button.id == "back":
            self.app.pop_screen()

    def _parse_range(self, text: str) -> None:
        from textual.widgets import Input
        text = text.strip()
        if not text:
            return
        try:
            nums: list[int] = []
            for part in text.split(","):
                part = part.strip()
                if "-" in part:
                    a, b = part.split("-", 1)
                    nums.extend(range(int(a), int(b) + 1))
                else:
                    nums.append(int(part))
            STATE.range_start = min(nums) - 1
            STATE.range_end = max(nums)
            self.app.push_screen(FieldScreen())
        except ValueError:
            self.query_one("#range_input", Input).border_title = "格式错误，请重试"


class FieldScreen(Screen):
    CSS = """
    Screen { align: center middle; }
    #box { width: 72; height: auto; padding: 1 2; border: solid $accent; }
    #title { text-align: center; text-style: bold; margin-bottom: 1; }
    .sep { height: 1; background: $surface; margin: 0 0; }
    #btn_row { margin-top: 1; align: center middle; }
    #btn_row Button { margin: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("要写入的元数据字段", id="title")
            yield Checkbox("标题 (TIT2 / ©nam)", id="f_title", value=True)
            yield Checkbox("歌手 (TPE1 / ©ART)", id="f_artist", value=True)
            yield Checkbox("专辑 (TALB / ©alb)", id="f_album", value=True)
            yield Checkbox("专辑歌手 (TPE2 / aART)", id="f_albumartist", value=True)
            yield Checkbox("年份 (TDRC / ©day)", id="f_year", value=True)
            yield Static("", classes="sep")
            yield Label("封面来源：")
            yield RadioSet(
                RadioButton("不嵌入封面"),
                RadioButton("本地 Covers 文件夹", value=True),
                RadioButton("网络下载 + 本地"),
                id="cover_radio",
            )
            yield Static("", classes="sep")
            yield Label("歌词来源：")
            yield RadioSet(
                RadioButton("不嵌入歌词"),
                RadioButton("本地 Lyrics 文件夹", value=True),
                id="lyrics_radio",
            )
            with Horizontal(id="btn_row"):
                yield Button("确认", id="confirm", variant="primary")
                yield Button("b 返回", id="back")
                yield Button("q 退出", id="quit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            STATE.fields["title"] = self.query_one("#f_title").value
            STATE.fields["artist"] = self.query_one("#f_artist").value
            STATE.fields["album"] = self.query_one("#f_album").value
            STATE.fields["albumartist"] = self.query_one("#f_albumartist").value
            STATE.fields["year"] = self.query_one("#f_year").value
            cover_idx = self.query_one("#cover_radio", RadioSet).pressed_index or 1
            STATE.cover_mode = ["none", "local", "download"][cover_idx]
            lyrics_idx = self.query_one("#lyrics_radio", RadioSet).pressed_index or 1
            STATE.lyrics_mode = ["none", "local"][lyrics_idx]
            self.app.push_screen(ProgressScreen())
        elif event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "quit":
            self.app.exit()


class ProgressScreen(Screen):
    CSS = """
    Screen { align: center middle; }
    #box { width: 80; height: 85%; padding: 1 2; border: solid $accent; }
    #title { text-align: center; text-style: bold; margin-bottom: 1; }
    #progress_row { height: 3; margin: 1 0; }
    #log_box { height: 1fr; overflow-y: auto; border: solid $surface; }
    #btn_row { margin-top: 1; align: center middle; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("进度", id="title")
            with Horizontal(id="progress_row"):
                yield ProgressBar(id="pbar", total=100, show_eta=True)
                yield Static("0%", id="pct_label")
            yield Log(id="log_box", highlight=True)
            with Horizontal(id="btn_row"):
                yield Button("q 安全退出", id="quit", variant="warning")

    def on_mount(self) -> None:
        self.run_worker(self._run_fixer, exclusive=True)

    def _run_fixer(self) -> None:
        import sys
        fixer = MetadataFixer(STATE.music_dir)
        files = STATE.files[:]
        if STATE.scope == "range":
            files = files[STATE.range_start:STATE.range_end]
        total = len(files)
        if total == 0:
            self._log("没有要处理的文件")
            return

        backup = fixer.load_backup()
        report: list[dict] = []
        ok = skip = fail = 0

        log_handler = _TUILogHandler(self)
        log.addHandler(log_handler)
        try:
            for i, fname in enumerate(files, 1):
                path = fixer.base_dir / fname
                base, q_title, q_artist, confidence = parse_fname_multi(fname, STATE.strategy)
                log.info("[%d/%d] %s", i, total, fname)

                if not confidence:
                    log.info("   -> SKIP (low confidence)")
                    report.append({"file": fname, "status": "skipped", "reason": "low_confidence"})
                    skip += 1
                    self._update_progress(i, total)
                    continue

                if base not in backup:
                    backup[base] = MetadataFixer._read_tags(path)

                if STATE.dry:
                    try:
                        info = fixer._find_metadata(q_title, q_artist)
                    except Exception as exc:
                        log.info("   -> ERROR: %s", exc)
                        report.append({"file": fname, "status": "error", "error": str(exc)})
                        fail += 1
                        self._update_progress(i, total)
                        continue
                    if info:
                        log.info("   -> [%s] %s / %s / %s  (score=%.2f)",
                                 info["source"], info["title"], info["artist"],
                                 info.get("album") or "-", info.get("score", 0))
                        report.append({"file": fname, "status": "match", "result": info})
                        ok += 1
                    else:
                        log.info("   -> NO MATCH")
                        report.append({"file": fname, "status": "nomatch"})
                        skip += 1
                    self._update_progress(i, total)
                    continue

                try:
                    info = fixer._find_metadata(q_title, q_artist)
                    if not info:
                        log.info("   -> NO MATCH, skipped")
                        report.append({"file": fname, "status": "skipped", "reason": "no match"})
                        skip += 1
                        self._update_progress(i, total)
                        continue
                    fixer._write_tags(path, info, base, fields=STATE.fields,
                                       cover_mode=STATE.cover_mode, lyrics_mode=STATE.lyrics_mode)
                    log.info("   -> WROTE [%s] %s / %s / %s",
                             info["source"], info["title"], info["artist"], info.get("album") or "-")
                    report.append({"file": fname, "status": "fixed", "result": info})
                    ok += 1
                except Exception as exc:
                    log.info("   -> ERROR: %s", exc)
                    report.append({"file": fname, "status": "error", "error": str(exc)})
                    fail += 1
                self._update_progress(i, total)
                time.sleep(0.1)
        finally:
            log.removeHandler(log_handler)

        fixer.save_report(report)
        if not STATE.dry:
            fixer.save_backup(backup)

        log.info("")
        log.info("=== done: fixed=%d skipped=%d failed=%d ===", ok, skip, fail)
        log.info("report: %s", fixer.report_file)

    def _update_progress(self, current: int, total: int) -> None:
        pct = int(current / total * 100) if total else 0
        self.call_from_thread(self._set_progress, pct, current, total)

    def _set_progress(self, pct: int, current: int, total: int) -> None:
        try:
            self.query_one("#pbar", ProgressBar).progress = pct
            self.query_one("#pct_label", Static).update(f"{pct}% ({current}/{total})")
        except Exception:
            pass

    def _log(self, msg: str) -> None:
        self.call_from_thread(self._append_log, msg)

    def _append_log(self, msg: str) -> None:
        try:
            self.query_one("#log_box", Log).write_line(msg)
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit":
            self.app.exit()


class RestoreScreen(Screen):
    CSS = """
    Screen { align: center middle; }
    #box { width: 80; height: 85%; padding: 1 2; border: solid $accent; }
    #title { text-align: center; text-style: bold; margin-bottom: 1; }
    #table_box { height: 1fr; }
    #btn_row { margin-top: 1; align: center middle; }
    #btn_row Button { margin: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("恢复上次备份的元数据", id="title")
            with Vertical(id="table_box"):
                yield DataTable(id="restore_table")
            with Horizontal(id="btn_row"):
                yield Button("全选", id="select_all", variant="default")
                yield Button("恢复选中", id="restore", variant="primary")
                yield Button("b 返回", id="back")
                yield Button("q 退出", id="quit")

    def on_mount(self) -> None:
        table = self.query_one("#restore_table", DataTable)
        table.add_columns("序号", "文件名", "标题", "歌手", "状态")
        fixer = MetadataFixer(STATE.music_dir)
        backup = fixer.load_backup()
        if not backup:
            table.add_row("-", "-", "-", "-", "无备份数据")
            return
        files = fixer.scan_files()
        for i, fname in enumerate(files, 1):
            base = Path(fname).stem
            if base in backup:
                tags = backup[base]
                t = tags.get("TIT2") or tags.get("title") or "-"
                a = tags.get("TPE1") or tags.get("artist") or "-"
                table.add_row(str(i), fname, t, a, "可恢复")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "select_all":
            table = self.query_one("#restore_table", DataTable)
            for row_idx in range(table.row_count):
                table.cursor_type = "row"
                table.move_cursor(row=row_idx)
        elif event.button.id == "restore":
            self._do_restore()
        elif event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "quit":
            self.app.exit()

    def _do_restore(self) -> None:
        table = self.query_one("#restore_table", DataTable)
        fixer = MetadataFixer(STATE.music_dir)
        backup = fixer.load_backup()
        restored = 0
        for row_idx in range(table.row_count):
            try:
                cursor = table.get_cursor()
                if cursor and cursor.row == row_idx:
                    fname_cell = table.get_cell_at((row_idx, 1))
                    if fname_cell and fname_cell != "-":
                        path = fixer.base_dir / str(fname_cell)
                        base = path.stem
                        if base in backup:
                            if fixer._restore_tags(path, backup[base]):
                                restored += 1
                                log.info("Restored: %s", fname_cell)
            except Exception:
                continue
        log.info("Restored %d files", restored)
        self.app.pop_screen()


class DoneScreen(ModalScreen):
    CSS = """
    Screen { align: center middle; }
    #dialog { width: 40; height: auto; padding: 1 2; border: solid $success; text-align: center; }
    """

    def __init__(self, msg: str) -> None:
        super().__init__()
        self._msg = msg

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._msg)
            yield Button("确定", id="ok", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


# ═══════════════════════════════════════════════════════════════════════════
#  TUI — App
# ═══════════════════════════════════════════════════════════════════════════

class SongFixerApp(App):
    TITLE = "歌曲元数据修复工具"
    SUB_TITLE = "NetEase / QQ Music / Kugou / Bilibili"
    CSS = """
    Screen { background: $background; }
    """
    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("ctrl+c", "quit", "安全退出"),
    ]

    def on_mount(self) -> None:
        self.push_screen(ScanScreen())


# ═══════════════════════════════════════════════════════════════════════════
#  TUI log handler
# ═══════════════════════════════════════════════════════════════════════════

class _TUILogHandler(logging.Handler):
    def __init__(self, screen: ProgressScreen) -> None:
        super().__init__()
        self._screen = screen

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self._screen._log(msg)


# ═══════════════════════════════════════════════════════════════════════════
#  CLI mode (backward compatible)
# ═══════════════════════════════════════════════════════════════════════════

def run_cli(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    fixer = MetadataFixer(args.dir)

    only_files: list[str] | None = None
    if args.files:
        only_files = []
        for f in args.files:
            p = Path(f)
            if p.is_file():
                only_files.append(p.name)
                if not args.dir or args.dir == str(Path.home() / "Desktop" / "music"):
                    STATE.music_dir = p.parent
                    fixer = MetadataFixer(STATE.music_dir)
            elif p.name:
                only_files.append(p.name)

    files = fixer.scan_files()
    if only_files:
        files = [f for f in files if f in only_files]
    if not files:
        log.warning("No audio files found in %s", fixer.base_dir)
        return

    STATE.files = files
    STATE.strategy = "auto"
    STATE.dry = args.dry
    STATE.verbose = args.verbose
    STATE.music_dir = fixer.base_dir

    backup = fixer.load_backup()
    report: list[dict] = []
    ok = skip = fail = 0

    for i, fname in enumerate(files, 1):
        path = fixer.base_dir / fname
        base, q_title, q_artist, confidence = parse_fname_multi(fname, STATE.strategy)
        log.info("[%d/%d] %s", i, len(files), fname)

        if not confidence:
            log.info("   -> SKIP (low confidence)")
            report.append({"file": fname, "status": "skipped", "reason": "low_confidence"})
            skip += 1
            continue

        if base not in backup:
            backup[base] = MetadataFixer._read_tags(path)

        if args.dry:
            try:
                info = fixer._find_metadata(q_title, q_artist)
            except Exception as exc:
                log.info("   -> ERROR: %s", exc)
                report.append({"file": fname, "status": "error", "error": str(exc)})
                fail += 1
                continue
            if info:
                log.info("   -> [%s] %s / %s / %s  (score=%.2f)",
                         info["source"], info["title"], info["artist"],
                         info.get("album") or "-", info.get("score", 0))
                report.append({"file": fname, "status": "match", "result": info})
                ok += 1
            else:
                log.info("   -> NO MATCH")
                report.append({"file": fname, "status": "nomatch"})
                skip += 1
            continue

        try:
            info = fixer._find_metadata(q_title, q_artist)
            if not info:
                log.info("   -> NO MATCH, skipped")
                report.append({"file": fname, "status": "skipped", "reason": "no match"})
                skip += 1
                continue
            fixer._write_tags(path, info, base)
            log.info("   -> WROTE [%s] %s / %s / %s",
                     info["source"], info["title"], info["artist"], info.get("album") or "-")
            report.append({"file": fname, "status": "fixed", "result": info})
            ok += 1
        except Exception as exc:
            log.info("   -> ERROR: %s", exc)
            report.append({"file": fname, "status": "error", "error": str(exc)})
            fail += 1
        time.sleep(0.1)

    fixer.save_report(report)
    if not args.dry:
        fixer.save_backup(backup)

    log.info("")
    log.info("=== done: fixed=%d skipped=%d failed=%d ===", ok, skip, fail)
    log.info("report: %s", fixer.report_file)


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser(description="Song Metadata Fixer — TUI & CLI")
    ap.add_argument("--dir", default=None, help="music folder (default: ~/Desktop/music)")
    ap.add_argument("--dry", action="store_true", help="preview only, do not write")
    ap.add_argument("--files", nargs="*", default=None, help="process only these files")
    ap.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    args = ap.parse_args()

    has_args = any([args.dir, args.dry, args.files, args.verbose])
    if has_args:
        if args.dir:
            STATE.music_dir = Path(args.dir)
        run_cli(args)
    else:
        SongFixerApp().run()


if __name__ == "__main__":
    main()
