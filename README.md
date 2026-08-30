# Song Metadata Fixer

Fix incorrect / missing audio metadata (mp3 & m4a) by looking up the correct
title, artists, album, album artist and year — and optionally embedding the
local cover art and lyrics — from Chinese music platforms in priority order:

1. **网易云音乐 (NetEase Cloud Music)**
2. **QQ音乐 (QQ Music)**
3. **酷狗音乐 (Kugou)**
4. **哔哩哔哩 (Bilibili)**

## Why?

Many files downloaded from Chinese mirrors carry wrong tags: the uploader /
label name ("索尼音乐中国", "JLRS日落fm", "云端音乐铺"…) is written as the
*artist*, the *album* is empty (`?`), or the whole tag set is missing.

This tool searches the platforms, matches the result against the file name
(`Title_Artist.ext`), and writes clean metadata back into the file.

## Features

- Searches NetEase → QQ Music → Kugou → Bilibili in order, falling back when no reliable match is found.
- Fuzzy title/artist matching (Unicode-normalized), tolerant of extra annotations like `(为你而战)`, `【洛天依原创】`, mojibake-safe.
- Filters out junk versions (karaoke / 伴奏 / 网友改编 / 变速 / 拼接 / Live …).
- Merges artists from the filename when the platform result truncates them.
- Writes `Title / Artist / Album / AlbumArtist / Year`:
  - **mp3** → ID3v2 (`TIT2/TPE1/TALB/TPE2/TDRC`)
  - **m4a** → iTunes atoms (`©nam/©ART/©alb/aART/©day`)
- Embeds local **cover** (`Covers/<name>-xxxx.jpg`) and **lyrics** (`Lyrics/<name>.lrc`) when present.
- **Dry-run mode** — preview every decision without touching files.
- Automatically **skips** files with no reliable match and reports them.
- Backs up the original tags before writing, and caches search results on disk.

## Requirements

- Python 3.8+
- [`mutagen`](https://pypi.org/project/mutagen/) and [`requests`](https://pypi.org/project/requests/)

```bash
pip install mutagen requests
```

## Usage

File name convention: `Song Title_Artist.ext` (multiple artists separated by `_` or ` _ `).

```bash
# show metadata to be written (writes nothing)
python fix_metadata.py --dry

# write metadata (default folder: ~/Desktop/music)
python fix_metadata.py

# another folder
python fix_metadata.py --dir "D:\Music\Songs"

# only some files
python fix_metadata.py --files "Animals_Maroon 5.m4a"

# dry-run on a subset
python fix_metadata.py --dry --dir "D:\Music" --files "A.mp3" "B.m4a"
```

### Folder layout (optional)

```
music/
├── Covers/     # cover images, named <audio-basename>-anything.jpg
├── Lyrics/     # .lrc files, named <audio-basename>.lrc
└── *.mp3 / *.m4a
```

## How it works

1. Parse `Title_Artist` from each file name.
2. For each platform in order, search `"Title Artist"` (then `"Title"` alone).
3. Score candidates by fuzzy title + artist similarity; penalize junk versions.
4. Take the first reliable match; otherwise move to the next platform.
5. Bilibili is the fallback for tracks that only exist there (Chinese VOCALOID originals, dance covers…).
6. Write tags; embed local cover/lyrics if available.

## Outputs

| File | Location | Purpose |
|---|---|---|
| `music_metadata_backup.json` | `%TEMP%` | original tags before writing (rollback if needed) |
| `music_fix_report.json` | `%TEMP%` | per-file decisions (match/skip/error) |
| `music_search_cache.json` | `%TEMP%` | cached platform search results (re-runs are fast) |

## Caveats

- Platform search APIs are unofficial and rate-limited; NetEase may intermittently
  return nothing, in which case the tool falls through to the next platform.
  Re-running the tool is safe (previous searches are cached).
- Files with no reliable match are **skipped and reported**, never guessed.
- The tool has no Bilibili album data (it's a video platform), so Bilibili-only
  tracks get title/artist only.

## License

[MIT](LICENSE)