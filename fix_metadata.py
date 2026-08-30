# -*- coding: utf-8 -*-
import os
import re
import sys
import json
import time
import unicodedata
import hashlib
import difflib
import datetime
import argparse
from urllib.parse import urlencode

import requests
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, APIC, USLT, TDRC
from mutagen.mp4 import MP4, MP4Cover

DEFAULT_MUSIC = os.path.join(os.path.expanduser('~'), 'Desktop', 'music')
BASE = DEFAULT_MUSIC
LYR_DIR = os.path.join(BASE, 'Lyrics')
COV_DIR = os.path.join(BASE, 'Covers')
BACKUP = os.path.join(os.environ.get('TEMP', r'C:\Users\Robin\AppData\Local\Temp\opencode'), 'music_metadata_backup.json')
REPORT = os.path.join(os.environ.get('TEMP', r'C:\Users\Robin\AppData\Local\Temp\opencode'), 'music_fix_report.json')

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': UA})
CACHE = {}
CACHE_FILE = os.path.join(os.environ.get('TEMP', r'C:\Users\Robin\AppData\Local\Temp\opencode'), 'music_search_cache.json')
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            CACHE = {tuple(k): v for k, v in json.load(f).items()}
    except Exception:
        CACHE = {}

def save_cache():
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump({list(k): v for k, v in CACHE.items()}, f, ensure_ascii=False)
    except Exception:
        pass

# ---------------- helpers ----------------

def log(msg):
    print(msg, flush=True)

def norm(s):
    s = unicodedata.normalize('NFKC', s)
    s = s.lower()
    s = s.replace('&', ' and ')
    s = s.replace('／', '/').replace('｜', '|')
    s = re.sub(r'[\u200e\u200f\u202a-\u202e\ufeff]', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()

def strip_parenthetical(s):
    s = re.sub(r'（[^（）]*）', '', s)
    s = re.sub(r'\([^()]*\)', '', s)
    s = re.sub(r'【[^】]*】', '', s)
    s = re.sub(r'\[[^\]]*\]', '', s)
    return s

def bare(s):
    s = strip_parenthetical(s)
    s = unicodedata.normalize('NFKC', s)
    s = re.sub(r'[^\w\u4e00-\u9fff]+', '', s)
    return s.lower()

def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()

def title_sim(q, r):
    qn, rn = norm(q), norm(r)
    vals = [ratio(qn, rn)]
    qb, rb = bare(q), bare(r)
    if qb and rb:
        vals.append(ratio(qb, rb))
        vals.append(float(qb == rb))
    vals.append(float(qn == rn))
    return max(vals)

def artist_overlap(qa, ra):
    if not qa or not ra:
        return False
    qa, ra = norm(qa), norm(ra)
    if qa == ra:
        return True
    q_toks = set(t for t in re.split(r'[\s/|,]+', qa) if len(t) > 1)
    r_toks = set(t for t in re.split(r'[\s/|,]+', ra) if len(t) > 1)
    if q_toks & r_toks:
        return True
    qb, rb = bare(qa), bare(ra)
    return (bool(qb) and qb in rb) or (bool(rb) and rb in qa)

def artist_sim(qa, ra):
    if not qa and not ra:
        return 1.0
    if not qa or not ra:
        return 0.0
    return max(ratio(norm(qa), norm(ra)), artist_overlap(qa, ra) * 0.9, 0.0)

# ---------------- platform search ----------------

def netease_search(query):
    url = 'https://music.163.com/api/search/get/web'
    params = {'s': query, 'type': 1, 'offset': 0, 'limit': 15}
    def run():
        r = SESSION.get(url, params=params, headers={'Referer': 'https://music.163.com'}, timeout=20)
        songs = ((r.json().get('result') or {}).get('songs')) or []
        out = []
        for s in songs:
            arts = [a.get('name', '') for a in s.get('artists', [])]
            al = s.get('album') or {}
            year = None
            pt = al.get('publishTime')
            if pt:
                year = datetime.datetime.fromtimestamp(pt / 1000).year
            out.append({
                'source': 'netease',
                'title': s.get('name', ''),
                'artists': arts,
                'artist': ' / '.join(a for a in arts if a),
                'album': al.get('name', ''),
                'year': year,
                'sid': s.get('id'),
                'cover_url': al.get('picUrl') or al.get('blurPicUrl'),
            })
        return out
    out = run()
    if not out:
        time.sleep(1.5)
        out = run()
    return out

def qq_search(query):
    url = 'https://c.y.quux.qq.com/soso/fcgi-bin/client_search_cp'
    url = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
    payload = {
        'req_0': {
            'module': 'music.search.SearchCgiService',
            'method': 'DoSearchForQQMusicDesktop',
            'param': {'query': query, 'num_per_page': 15, 'page_num': 1, 'search_type': 0},
        },
        'comm': {'uin': 0},
    }
    r = SESSION.post(url, data={'format': 'json', 'data': json.dumps(payload)}, headers={'Referer': 'https://y.qq.com'}, timeout=20)
    j = r.json()
    songs = ((j.get('req_0') or {}).get('data') or {}).get('body') or {}
    songs = songs.get('song') or {}
    songs = songs.get('list') or []
    if not songs:
        time.sleep(1.5)
        r = SESSION.post(url, data={'format': 'json', 'data': json.dumps(payload)}, headers={'Referer': 'https://y.qq.com'}, timeout=20)
        j = r.json()
        songs = ((j.get('req_0') or {}).get('data') or {}).get('body') or {}
        songs = songs.get('song') or {}
        songs = songs.get('list') or []
    out = []
    for s in songs:
        arts = [a.get('name', '') for a in s.get('singer', [])]
        year = None
        if s.get('pubtime'):
            year = datetime.datetime.fromtimestamp(s.get('pubtime')).year
        out.append({
            'source': 'qqmusic',
            'title': s.get('songname', ''),
            'artists': arts,
            'artist': ' / '.join(a for a in arts if a),
            'album': s.get('albumname', ''),
            'year': year,
            'sid': s.get('songmid'),
            'cover_url': 'https://y.gtimg.cn/music/photo_new/T002R800x800M000%s.jpg' % s.get('albummid', '') if s.get('albummid') else None,
        })
    return out

def kugou_search(query):
    url = 'https://songsearch.kugou.com/song_search_v2'
    params = {'keyword': query, 'page': 1, 'pagesize': 15, 'platform': 'WebFilter',
              'userid': -1, 'clientver': 2000, 'iscorrection': 1, 'privilege_filter': 0, 'filter': 10}
    r = SESSION.get(url, params=params, headers={'Referer': 'https://www.kugou.com'}, timeout=15)
    j = r.json()
    lists = ((j.get('data') or {}).get('lists')) or []
    out = []
    for s in lists:
        arts = [a.strip() for a in re.split(r'[、,，&]', s.get('SingerName', '')) if a.strip()]
        album = s.get('AlbumName', '')
        year = None
        rd = s.get('ReleaseDate')
        if rd:
            m = re.search(r'(\d{4})', str(rd))
            if m:
                year = int(m.group(1))
        out.append({
            'source': 'kugou',
            'title': s.get('SongName', ''),
            'artists': arts,
            'artist': ' / '.join(arts),
            'album': album,
            'year': year,
            'sid': s.get('FileHash'),
            'cover_url': None,
        })
    return out

BILI_MIX_TAB = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]
BILI_KEY = None

def bili_sign(params):
    global BILI_KEY
    if BILI_KEY is None:
        j = SESSION.get('https://api.bilibili.com/x/web-interface/nav', timeout=15).json()
        img = j['data']['wbi_img']
        def key(u):
            return u.split('/')[-1].split('.')[0]
        raw = key(img['img_url']) + key(img['sub_url'])
        BILI_KEY = ''.join(raw[i] for i in BILI_MIX_TAB)[:32]
    p = dict(params)
    p['wts'] = int(time.time())
    q = urlencode(sorted(p.items()))
    p['w_rid'] = hashlib.md5((q + BILI_KEY).encode()).hexdigest()
    return p

def bilibili_search(query):
    try:
        SESSION.get('https://www.bilibili.com', timeout=6)
    except Exception:
        pass
    params = bili_sign({'search_type': 'video', 'keyword': query, 'page': 1})
    r = SESSION.get('https://api.bilibili.com/x/web-interface/wbi/search/type', params=params,
                    headers={'Referer': 'https://www.bilibili.com'}, timeout=12)
    j = r.json()
    res = ((j.get('data') or {}).get('result')) or []
    out = []
    for s in res:
        if s.get('type') not in ('video', 'bili_user'):
            continue
        title = re.sub(r'<[^>]+>', '', s.get('title', ''))
        out.append({
            'source': 'bilibili',
            'title': title,
            'artists': [s.get('author', '')],
            'artist': s.get('author', ''),
            'album': '',
            'year': None,
            'sid': s.get('bvid'),
            'cover_url': None,
        })
    return out

PLATFORMS = [('netease', netease_search), ('qqmusic', qq_search), ('kugou', kugou_search), ('bilibili', bilibili_search)]

def search_platform(name, fn, q):
    ck = (name, q)
    if ck not in CACHE:
        try:
            CACHE[ck] = fn(q)
            save_cache()
        except Exception as e:
            CACHE[ck] = []
    return CACHE[ck]

JUNK_HINTS = ('karaoke', 'kareoke', '伴奏', '网友改编', 'originallyperformed', 'completeversion',
              '现场', '(live)', '（live）', '翻奏', '拼接', '变速', '纯音乐')

def junk_level(it):
    t = unicodedata.normalize('NFKC', (it.get('title') or '')).lower()
    if any(x in t for x in JUNK_HINTS):
        return 0.35
    a = unicodedata.normalize('NFKC', (it.get('album') or '')).lower()
    if any(x in a for x in JUNK_HINTS):
        return 0.2
    return 0.0

def merge_artist(q_artist, res_artist, n_artists):
    if not q_artist or res_artist is None or n_artists >= 6:
        return res_artist
    have = bare(res_artist) or ''
    add = []
    toks = [t.strip() for t in re.split(r'[,，、/|&\s]+', q_artist) if t.strip()]
    for t in toks:
        tb = bare(t)
        if not tb:
            continue
        if tb in have:
            continue
        if any(ratio(tb, bare(x)) >= 0.85 for x in re.split(r'[,，、/|&]+', res_artist)):
            continue
        if add and t.lower() in ('feat', 'ft', 'remix', 'radio', 'edit', 'slowed', 'sped up'):
            continue
        add.append(t)
    if not add:
        return res_artist
    return (res_artist + ' / ' + ' / '.join(add)) if res_artist else ' / '.join(add)

def best_match(q_title, q_artist, results):
    best = None
    for it in results:
        ts = title_sim(q_title, it['title'])
        if not ts:
            continue
        ra = it.get('artist') or ''
        aov = artist_overlap(q_artist, ra)
        asim = artist_sim(q_artist, ra)
        if q_artist:
            score = ts * 0.6 + asim * 0.4
            if aov:
                score += 0.05
            if score > 1.0:
                score = 1.0
        else:
            score = ts
        score = max(0.0, score - junk_level(it))
        if best is None or score > best[1]:
            best = (it, score)
    return best

def accept(title_score):
    return title_score[0] >= 0.85

def find_metadata(q_title, q_artist):
    fallback = []
    for pname, fn in PLATFORMS:
        queries = []
        q_all = (q_title + ' ' + q_artist).strip()
        queries.append(q_all)
        if q_title != q_all:
            queries.append(q_title)
        for q in queries:
            res = search_platform(pname, fn, q)
            if not res:
                continue
            if pname == 'bilibili':
                m = best_match(q_title, q_artist, res)
                if m and m[1] >= 0.72:
                    b, sc = m
                    return {'source': 'bilibili', 'title': q_title, 'artist': q_artist,
                            'artists': [q_artist], 'album': '', 'year': None,
                            'sid': b.get('sid'), 'cover_url': None, 'score': sc, 'query': q}
            else:
                m = best_match(q_title, q_artist, res)
                if m:
                    b, sc = m
                    if sc >= 0.8 and title_sim(q_title, b['title']) >= 0.72:
                        out = dict(b)
                        out['artist'] = merge_artist(q_artist, out['artist'], len(out.get('artists') or []))
                        out['score'] = sc
                        out['query'] = q
                        return out
                    fallback.append((dict(b), sc, q))
            time.sleep(0.08)
    return None

# ---------------- tag writing ----------------

def find_local_cover(basename):
    for f in os.listdir(COV_DIR):
        if f.lower().endswith(('.jpg', '.jpeg', '.png')) and f.rsplit('-', 1)[0] == basename:
            return os.path.join(COV_DIR, f)
    return None

def find_local_lyrics(basename):
    p = os.path.join(LYR_DIR, basename + '.lrc')
    if os.path.exists(p):
        return p
    p2 = os.path.join(LYR_DIR, basename + '_trans.lrc')
    if os.path.exists(p2):
        return p2
    return None

def load_text(p):
    for enc in ('utf-8', 'gb18030', 'big5'):
        try:
            with open(p, 'r', encoding=enc) as f:
                return f.read()
        except Exception:
            continue
    return None

def download_cover(url):
    try:
        r = SESSION.get(url, timeout=15)
        if r.status_code == 200 and r.content[:3] in (b'\xff\xd8\xff', b'\x89PN'):
            return r.content
    except Exception:
        pass
    return None

def sanitize(s):
    if not s:
        return s
    s = unicodedata.normalize('NFKC', s)
    s = re.sub(r'[\u200e\u200f\u202a-\u202e\ufeff\u00a0]', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()

def write_tags(path, info, basename):
    title = sanitize(info.get('title') or basename)
    artist = sanitize(info.get('artist') or '')
    album = sanitize(info.get('album') or '')
    albumartist = sanitize(info.get('albumartist') or artist)
    year = info.get('year')

    cov_path = find_local_cover(basename)
    cover_data = None
    if cov_path:
        with open(cov_path, 'rb') as f:
            cover_data = f.read()
    elif info.get('cover_url'):
        cover_data = download_cover(info['cover_url'])

    lyr_path = find_local_lyrics(basename)
    lyrics = load_text(lyr_path) if lyr_path else None

    if path.lower().endswith('.mp3'):
        tags = ID3(path)
        tags.delete()
        tags.add(TIT2(encoding=3, text=[title]))
        tags.add(TPE1(encoding=3, text=[artist]))
        if album:
            tags.add(TALB(encoding=3, text=[album]))
        if albumartist:
            tags.add(TPE2(encoding=3, text=[albumartist]))
        if year:
            tags.add(TDRC(encoding=3, text=[str(year)]))
        if cover_data:
            mime = 'image/png' if cover_data[:8] == b'\x89PNG\r\n\x1a\n' else 'image/jpeg'
            tags.add(APIC(encoding=3, mime=mime, type=3, desc='Cover', data=cover_data))
        if lyrics:
            tags.add(USLT(encoding=3, lang='zho', desc='Lyrics', text=lyrics))
        tags.save(path)
    else:
        mp4 = MP4(path)
        mp4.clear()
        mp4['\xa9nam'] = [title]
        mp4['\xa9ART'] = [artist]
        if album:
            mp4['\xa9alb'] = [album]
        if albumartist:
            mp4['aART'] = [albumartist]
        if year:
            mp4['\xa9day'] = [str(year)]
        if cover_data:
            fmt = MP4Cover.FORMAT_PNG if cover_data[:8] == b'\x89PNG\r\n\x1a\n' else MP4Cover.FORMAT_JPEG
            mp4['covr'] = [MP4Cover(cover_data, imageformat=fmt)]
        if lyrics:
            mp4['\xa9lyr'] = [lyrics]
        mp4.save(path)

def old_tags(path):
    from mutagen import File
    try:
        m = File(path, easy=True)
        d = {}
        if m and m.tags:
            for k, v in dict(m.tags).items():
                d[k] = [str(x) for x in v]
        return d
    except Exception:
        return {}

# ---------------- main ----------------

def parse_fname(fname):
    base = os.path.splitext(fname)[0]
    parts = base.split('_')
    title = parts[0].strip()
    artist = '_'.join(parts[1:]).replace('_', ' ').strip()
    artist = re.sub(r'\s+', ' ', artist)
    return base, title, artist

def main():
    global BASE, LYR_DIR, COV_DIR
    ap = argparse.ArgumentParser(description='Fix audio tags using NetEase / QQ Music / Kugou / Bilibili')
    ap.add_argument('--dir', default=DEFAULT_MUSIC,
                    help='music folder (default: ~/Desktop/music)')
    ap.add_argument('--dry', action='store_true',
                    help='look up metadata only, do not write to files')
    ap.add_argument('--files', nargs='*', default=None,
                    help='only process these file names')
    args = ap.parse_args()

    BASE = args.dir
    LYR_DIR = os.path.join(BASE, 'Lyrics')
    COV_DIR = os.path.join(BASE, 'Covers')

    files = [f for f in os.listdir(BASE) if f.lower().endswith(('.mp3', '.m4a'))] if os.path.isdir(BASE) else []
    if args.files:
        files = [f for f in files if f in args.files]
    if not files:
        log('no audio files found in %r' % BASE)
        return

    backup = {}
    if os.path.exists(BACKUP):
        with open(BACKUP, 'r', encoding='utf-8') as f:
            backup = json.load(f)

    report = []
    ok = skip = fail = 0
    for i, f in enumerate(sorted(files), 1):
        path = os.path.join(BASE, f)
        base, q_title, q_artist = parse_fname(f)
        log('[%d/%d] %s' % (i, len(files), f))
        if base not in backup:
            backup[base] = old_tags(path)
        if args.dry:
            try:
                info = find_metadata(q_title, q_artist)
            except Exception as e:
                log('   -> ERROR %r' % e)
                report.append({'file': f, 'status': 'error', 'error': repr(e)})
                fail += 1
                continue
            if info:
                log('   -> [%s] %s / %s / %s  (score=%.2f)' % (info['source'], info['title'], info['artist'], info.get('album') or '-', info.get('score', 0)))
                report.append({'file': f, 'status': 'match', 'result': info})
                ok += 1
            else:
                log('   -> NO MATCH')
                report.append({'file': f, 'status': 'nomatch'})
                skip += 1
            continue
        try:
            info = find_metadata(q_title, q_artist)
            if not info:
                log('   -> NO MATCH, skipped')
                report.append({'file': f, 'status': 'skipped', 'reason': 'no match'})
                skip += 1
                continue
            write_tags(path, info, base)
            log('   -> WROTE [%s] %s / %s / %s' % (info['source'], info['title'], info['artist'], info.get('album') or '-'))
            report.append({'file': f, 'status': 'fixed', 'result': info})
            ok += 1
        except Exception as e:
            log('   -> ERROR %r' % e)
            report.append({'file': f, 'status': 'error', 'error': repr(e)})
            fail += 1
        time.sleep(0.1)

    with open(REPORT, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    if not args.dry:
        with open(BACKUP, 'w', encoding='utf-8') as f:
            json.dump(backup, f, ensure_ascii=False, indent=1)

    log('')
    log('=== done: fixed=%d skipped=%d failed=%d ===' % (ok, skip, fail))
    log('report: %s' % REPORT)

if __name__ == '__main__':
    main()