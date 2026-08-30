# -*- coding: utf-8 -*-
"""Song Metadata Fixer — fix audio tags using NetEase / QQ Music / Kugou / Bilibili."""

import argparse
import datetime
import difflib
import glob
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlencode

import requests
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, APIC, USLT, TDRC
from mutagen.mp4 import MP4, MP4Cover

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

log = logging.getLogger("song-fixer")


# ─── helpers ────────────────────────────────────────────────────────────────


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


# ─── MetadataFixer ─────────────────────────────────────────────────────────


class MetadataFixer:
    """Core class: search platforms, match, and write tags."""

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
        key = f"{platform}|{query}"
        return self.cache.get(key)

    def _set_cached(self, platform: str, query: str, results: list[dict]) -> None:
        key = f"{platform}|{query}"
        self.cache[key] = results
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
            log.debug("Bilibili WBI key refreshed")
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
            log.debug("Netease CSRF token obtained")
        except Exception as exc:
            log.warning("Failed to get Netease CSRF: %s", exc)
            self._netease_csrf = ""

    def _search_netease(self, query: str) -> list[dict]:
        self._ensure_netease_csrf()
        url = "https://music.163.com/api/search/get/web"
        params = {"s": query, "type": 1, "offset": 0, "limit": 15}
        if self._netease_csrf:
            params["csrf_token"] = self._netease_csrf
        try:
            r = self.session.get(
                url, params=params, headers={"Referer": "https://music.163.com"}, timeout=15
            )
            data = r.json()
            songs = ((data.get("result") or {}).get("songs")) or []
            if not songs:
                log.warning(
                    "[netease] empty results (code=%s, msg=%s)",
                    data.get("code"), data.get("message"),
                )
            out = []
            for s in songs:
                arts = [a.get("name", "") for a in s.get("artists", [])]
                al = s.get("album") or {}
                year = None
                pt = al.get("publishTime")
                if pt:
                    year = datetime.datetime.fromtimestamp(pt / 1000).year
                out.append({
                    "source": "netease",
                    "title": s.get("name", ""),
                    "artists": arts,
                    "artist": " / ".join(a for a in arts if a),
                    "album": al.get("name", ""),
                    "year": year,
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
            r = self.session.post(
                url,
                data={"format": "json", "data": json.dumps(payload)},
                headers={"Referer": "https://y.qq.com"},
                timeout=15,
            )
            j = r.json()
            body = ((j.get("req_0") or {}).get("data") or {}).get("body") or {}
            songs = (body.get("song") or {}).get("list") or []
            if not songs:
                code = (j.get("req_0") or {}).get("code")
                log.warning("[qqmusic] empty results (code=%s)", code)
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
                    "source": "qqmusic",
                    "title": s.get("songname", ""),
                    "artists": arts,
                    "artist": " / ".join(a for a in arts if a),
                    "album": s.get("albumname", ""),
                    "year": year,
                    "sid": s.get("songmid"),
                    "cover_url": f"https://y.gtimg.cn/music/photo_new/T002R800x800M000{mid}.jpg"
                    if mid
                    else None,
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
                "source": source,
                "title": s.get("SongName", ""),
                "artists": arts,
                "artist": " / ".join(arts),
                "album": s.get("AlbumName", ""),
                "year": year,
                "sid": s.get("FileHash"),
                "cover_url": None,
            })
        return out

    def _search_kugou(self, query: str) -> list[dict]:
        # primary endpoint
        try:
            r = self.session.get(
                "https://songsearch.kugou.com/song_search_v2",
                params={
                    "keyword": query, "page": 1, "pagesize": 15,
                    "platform": "WebFilter", "userid": -1, "clientver": 2000,
                    "iscorrection": 1, "privilege_filter": 0, "filter": 10,
                },
                headers={"Referer": "https://www.kugou.com"},
                timeout=15,
            )
            j = r.json()
            if j.get("status") != 1:
                log.warning("[kugou] primary status=%s, falling back", j.get("status"))
                return self._search_kugou_fallback(query)
            lists = (j.get("data") or {}).get("lists") or []
            if not lists:
                log.warning("[kugou] primary returned 0 results for %r", query)
            return self._parse_kugou_items(lists)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("[kugou] primary not JSON: %s", exc)
            return self._search_kugou_fallback(query)
        except Exception as exc:
            log.warning("[kugou] primary failed: %s", exc)
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
            if not items:
                log.warning("[kugou-fallback] 0 results for %r", query)
            return self._parse_kugou_items(items, source="kugou")
        except Exception as exc:
            log.warning("[kugou-fallback] query=%r failed: %s", query, exc)
            return []

    # ── Bilibili ───────────────────────────────────────────────────────────

    def _search_bilibili(self, query: str) -> list[dict]:
        for attempt in range(2):
            try:
                self.session.get("https://www.bilibili.com", timeout=8)
                params = self._bili_sign({"search_type": "video", "keyword": query, "page": 1})
                r = self.session.get(
                    "https://api.bilibili.com/x/web-interface/wbi/search/type",
                    params=params,
                    headers={"Referer": "https://www.bilibili.com"},
                    timeout=15,
                )
                if r.status_code == 412:
                    log.warning("[bilibili] 412 rate limit, refreshing cookie (attempt %d)", attempt + 1)
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
                        "source": "bilibili",
                        "title": title,
                        "artists": [s.get("author", "")],
                        "artist": s.get("author", ""),
                        "album": "",
                        "year": None,
                        "sid": s.get("bvid"),
                        "cover_url": None,
                    })
                if not out:
                    log.warning("[bilibili] 0 results for %r", query)
                return out
            except Exception as exc:
                log.warning("[bilibili] attempt %d failed: %s", attempt + 1, exc)
        return []

    # ── matching ───────────────────────────────────────────────────────────

    @staticmethod
    def _best_match(
        q_title: str,
        q_artist: str,
        results: list[dict],
        threshold: float = 0.85,
    ) -> tuple[dict, float] | None:
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
    def _merge_artist(
        q_artist: str | None, res_artist: str | None, n_artists: int
    ) -> str | None:
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
        fallback: list[tuple[dict, float, str]] = []
        for pname, _method_name in self.PLATFORMS:
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
                            "source": "bilibili",
                            "title": q_title,
                            "artist": q_artist,
                            "artists": [q_artist],
                            "album": "",
                            "year": None,
                            "sid": b.get("sid"),
                            "cover_url": None,
                            "score": sc,
                            "query": q,
                        }
                else:
                    m = self._best_match(q_title, q_artist, results)
                    if m:
                        b, sc = m
                        if _title_sim(q_title, b["title"]) >= 0.72:
                            out = dict(b)
                            out["artist"] = self._merge_artist(
                                q_artist, out["artist"], len(out.get("artists") or [])
                            )
                            out["score"] = sc
                            out["query"] = q
                            return out
                        fallback.append((dict(b), sc, q))
                time.sleep(0.08)
        return None

    # ── parse filename ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_fname(fname: str) -> tuple[str, str, str, bool]:
        base = Path(fname).stem
        # try delimiters: underscore, dash, space+dash
        for delim in (" - ", "_", " -"):
            parts = base.split(delim)
            if len(parts) >= 2:
                title = parts[0].strip()
                artist = delim.join(parts[1:]).strip()
                artist = re.sub(r"\s+", " ", artist)
                return base, title, artist, True
        # no delimiter found — heuristic: first CJK/latin word block = title, rest = artist
        m = re.match(r"^(\S+)\s+(.+)$", base)
        if m:
            return base, m.group(1).strip(), m.group(2).strip(), False
        return base, base, "", False

    # ── tag diff ───────────────────────────────────────────────────────────

    @staticmethod
    def _tag_diff(old: dict, new: dict) -> dict[str, tuple[str, str]]:
        changes = {}
        for k, v in new.items():
            ov = old.get(k, "")
            if ov != v:
                changes[k] = (ov, v)
        return changes

    # ── tag I/O ────────────────────────────────────────────────────────────

    @staticmethod
    def _read_tags(path: Path) -> dict:
        ext = path.suffix.lower()
        tags: dict[str, str] = {}
        try:
            if ext == ".mp3":
                t = ID3(path)
                for frame_id in ("TIT2", "TPE1", "TALB", "TPE2", "TDRC"):
                    frame = t.get(frame_id)
                    if frame:
                        tags[frame_id] = str(frame.text[0])
                if "APIC:" in t:
                    tags["APIC:"] = f"<{len(t['APIC:'].data)} bytes>"
                uslt = t.get("USLT::'zho'")
                if uslt:
                    tags["USLT::'zho'"] = f"<{len(str(uslt.text))} chars>"
            elif ext == ".m4a":
                m = MP4(path)
                tag_map = {"\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album",
                           "aART": "albumartist", "\xa9day": "year"}
                for atom, name in tag_map.items():
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
        p = self.lyr_dir / f"{basename}.lrc"
        if p.exists():
            return p
        p2 = self.lyr_dir / f"{basename}_trans.lrc"
        if p2.exists():
            return p2
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

    def _write_tags(self, path: Path, info: dict, basename: str) -> bool:
        title = _sanitize(info.get("title") or basename)
        artist = _sanitize(info.get("artist") or "")
        album = _sanitize(info.get("album") or "")
        albumartist = _sanitize(info.get("albumartist") or artist)
        year = info.get("year")

        cover_data = None
        cov_path = self._find_local_cover(basename)
        if cov_path:
            cover_data = cov_path.read_bytes()
        elif info.get("cover_url"):
            cover_data = self._download_cover(info["cover_url"])

        lyr_path = self._find_local_lyrics(basename)
        lyrics = _load_text(lyr_path) if lyr_path else None

        ext = path.suffix.lower()

        if ext == ".mp3":
            tags = ID3(path)
            old = {}
            for fid in ("TIT2", "TPE1", "TALB", "TPE2", "TDRC"):
                frame = tags.get(fid)
                if frame:
                    old[fid] = str(frame.text[0])
            if "APIC:" in tags:
                old["APIC:"] = f"<{len(tags['APIC:'].data)} bytes>"
                uslt = tags.get("USLT::'zho'")
                if uslt:
                    old["USLT::'zho'"] = f"<{len(str(uslt.text))} chars>"

            new = {"TIT2": title, "TPE1": artist}
            if album:
                new["TALB"] = album
            if albumartist:
                new["TPE2"] = albumartist
            if year:
                new["TDRC"] = str(year)

            for fid, val in new.items():
                tags.delall(fid)
                tags.add(
                    {
                        "TIT2": TIT2(encoding=3, text=[val]),
                        "TPE1": TPE1(encoding=3, text=[val]),
                        "TALB": TALB(encoding=3, text=[val]),
                        "TPE2": TPE2(encoding=3, text=[val]),
                        "TDRC": TDRC(encoding=3, text=[val]),
                    }[fid]
                )

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
            old = {}
            atom_map = {"\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album",
                        "aART": "albumartist", "\xa9day": "year"}
            for atom, name in atom_map.items():
                if atom in mp4:
                    old[name] = mp4[atom][0] if mp4[atom] else ""
            if "covr" in mp4:
                old["covr"] = f"<{len(mp4['covr'][0])} bytes>"
            if "\xa9lyr" in mp4:
                old["\xa9lyr"] = f"<{len(mp4['\xa9lyr'][0])} chars>"

            mp4["\xa9nam"] = [title]
            mp4["\xa9ART"] = [artist]
            if album:
                mp4["\xa9alb"] = [album]
            if albumartist:
                mp4["aART"] = [albumartist]
            if year:
                mp4["\xa9day"] = [str(year)]
            if cover_data:
                fmt = MP4Cover.FORMAT_PNG if cover_data[:8] == b"\x89PNG\r\n\x1a\n" else MP4Cover.FORMAT_JPEG
                mp4["covr"] = [MP4Cover(cover_data, imageformat=fmt)]
            if lyrics:
                mp4["\xa9lyr"] = [lyrics]

            mp4.save(path)

        else:
            log.warning("[write] unsupported format %s", ext)
            return False

        new_tags = self._read_tags(path)
        diff = self._tag_diff(old, new_tags)
        if diff:
            for field, (ov, nv) in diff.items():
                log.info("   %s: %r → %r", field, ov, nv)
        else:
            log.info("   (no changes)")
        return True

    # ── main loop ──────────────────────────────────────────────────────────

    def run(self, dry: bool = False, only_files: list[str] | None = None) -> None:
        if not self.base_dir.is_dir():
            log.error("Directory does not exist: %s", self.base_dir)
            return

        audio_exts = {".mp3", ".m4a"}
        files = sorted(
            f.name for f in self.base_dir.iterdir()
            if f.suffix.lower() in audio_exts and f.is_file()
        )
        if only_files:
            files = [f for f in files if f in only_files]
        if not files:
            log.warning("No audio files found in %s", self.base_dir)
            return

        # backup
        backup: dict[str, dict] = {}
        if self.backup_file.exists():
            try:
                backup = json.loads(self.backup_file.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Failed to load backup: %s", exc)

        report: list[dict] = []
        ok = skip = fail = 0

        for i, fname in enumerate(files, 1):
            path = self.base_dir / fname
            base, q_title, q_artist, confidence = self._parse_fname(fname)
            log.info("[%d/%d] %s", i, len(files), fname)

            if not confidence:
                log.info("   -> SKIP (low confidence filename, no clear delimiter)")
                report.append({"file": fname, "status": "skipped", "reason": "low_confidence"})
                skip += 1
                continue

            if base not in backup:
                backup[base] = self._read_tags(path)

            if dry:
                try:
                    info = self._find_metadata(q_title, q_artist)
                except Exception as exc:
                    log.info("   -> ERROR: %s", exc)
                    report.append({"file": fname, "status": "error", "error": str(exc)})
                    fail += 1
                    continue
                if info:
                    log.info(
                        "   -> [%s] %s / %s / %s  (score=%.2f)",
                        info["source"], info["title"], info["artist"],
                        info.get("album") or "-", info.get("score", 0),
                    )
                    report.append({"file": fname, "status": "match", "result": info})
                    ok += 1
                else:
                    log.info("   -> NO MATCH")
                    report.append({"file": fname, "status": "nomatch"})
                    skip += 1
                continue

            # write mode
            try:
                info = self._find_metadata(q_title, q_artist)
                if not info:
                    log.info("   -> NO MATCH, skipped")
                    report.append({"file": fname, "status": "skipped", "reason": "no match"})
                    skip += 1
                    continue
                self._write_tags(path, info, base)
                log.info(
                    "   -> WROTE [%s] %s / %s / %s",
                    info["source"], info["title"], info["artist"],
                    info.get("album") or "-",
                )
                report.append({"file": fname, "status": "fixed", "result": info})
                ok += 1
            except Exception as exc:
                log.info("   -> ERROR: %s", exc)
                report.append({"file": fname, "status": "error", "error": str(exc)})
                fail += 1
            time.sleep(0.1)

        # save report
        try:
            self.report_file.parent.mkdir(parents=True, exist_ok=True)
            self.report_file.write_text(
                json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except Exception as exc:
            log.warning("Failed to write report: %s", exc)

        # save backup (non-dry only)
        if not dry:
            try:
                self.backup_file.parent.mkdir(parents=True, exist_ok=True)
                self.backup_file.write_text(
                    json.dumps(backup, ensure_ascii=False, indent=1), encoding="utf-8"
                )
            except Exception as exc:
                log.warning("Failed to write backup: %s", exc)

        log.info("")
        log.info("=== done: fixed=%d skipped=%d failed=%d ===", ok, skip, fail)
        log.info("report: %s", self.report_file)


# ─── entry point ───────────────────────────────────────────────────────────


def main() -> None:
    default_music = str(Path.home() / "Desktop" / "music")

    ap = argparse.ArgumentParser(
        description="Fix audio tags using NetEase / QQ Music / Kugou / Bilibili"
    )
    ap.add_argument(
        "--dir", default=default_music,
        help="music folder (default: ~/Desktop/music)",
    )
    ap.add_argument(
        "--dry", action="store_true",
        help="look up metadata only, do not write to files",
    )
    ap.add_argument(
        "--files", nargs="*", default=None,
        help="only process these file names",
    )
    ap.add_argument(
        "--verbose", "-v", action="store_true",
        help="enable debug logging",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    fixer = MetadataFixer(args.dir)
    fixer.run(dry=args.dry, only_files=args.files)


if __name__ == "__main__":
    main()
