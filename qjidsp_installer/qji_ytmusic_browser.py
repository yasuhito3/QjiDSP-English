#!/usr/bin/env python3
"""
qji_ytmusic_browser.py  —  YouTube Music ブラウザUI  (ポート 8082)

機能:
  ・曲 / アルバム / プレイリスト 検索
  ・アルバム詳細（トラック一覧）
  ・お気に入り（qji_ytmusic_favorites.json）
  ・ブラウザ上でトラック選択 → ターミナルで即再生
"""

import json, time, threading, subprocess, sys, re, os, stat, tempfile
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, urljoin
from typing import Optional, List

PORT         = 8082
FAVS_PATH      = Path.home() / '.config' / 'qji_ytmusic_favorites.json'
ALBUM_FAVS_PATH = Path.home() / '.config' / 'qji_ytmusic_album_favs.json'
AUTH_PATH    = Path.home() / '.config' / 'qji_ytmusic_auth.json'


def _private_dir() -> Path:
    """自分専用(0700)のディレクトリ。XDG_RUNTIME_DIR/qji を優先する。"""
    uid = os.getuid()
    cands = []
    base = os.environ.get('XDG_RUNTIME_DIR')
    if base and os.path.isdir(base):
        cands.append(Path(base) / 'qji')
    cands.append(Path(tempfile.gettempdir()) / f'qji-{uid}')
    for d in cands:
        try:
            d.mkdir(mode=0o700, exist_ok=True)
            st = os.lstat(d)
            if (stat.S_ISDIR(st.st_mode) and st.st_uid == uid
                    and not (st.st_mode & 0o077)):
                return d
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix='qji-'))


def _write_private(path: Path, text: str) -> None:
    """権限 0600・シンボリックリンク不追従・原子的(replace)に書き込む。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


REQUEST_PATH = _private_dir() / 'qji_ytmusic_request.json'

FILTER_PRESET_LABELS = {
    'musikverein': '🎻 Musikverein', 'piano': '🎹 Piano',
    'chamber': '🏠 Chamber',        'vocal': '🎙 Vocal',
    'jazz':    '🎷 Jazz',            'radio': '📻 Radio',
}
GAIN_PRESETS_DB = {
    'classical': 0.0, 'general': -1.5, 'jazz_pop': -3.5, 'loud': -5.0,
}

_server        = None
_server_thread = None
_CLOSE_SIGNAL  = threading.Event()
_fav_lock      = threading.Lock()

_MAX_BODY   = 2_000_000      # POST ボディ上限(バイト)
_MAX_TRACKS = 1000           # 1リクエストの曲数上限
_MAX_IMG    = 8_000_000      # 画像1枚の上限(バイト)
_VID_RE  = re.compile(r'^[A-Za-z0-9_-]{11}$')
_BID_RE  = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_CTRL_RE = re.compile('[\x00-\x1f\x7f-\x9f\u2028\u2029]')
_IMG_HOST_SUFFIXES = ('ytimg.com', 'ggpht.com', 'googleusercontent.com')


def _valid_vid(v) -> bool:
    return isinstance(v, str) and bool(_VID_RE.match(v))


def _clip(v, n: int) -> str:
    """文字列のみ受け付け、制御文字を除去して n 文字に切る。"""
    if not isinstance(v, str):
        return ''
    return _CTRL_RE.sub('', v)[:n]


def _to_int(v, lo: int, hi: int, default: int = 0) -> int:
    if isinstance(v, bool):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, n))


def _https_host(u, maxlen: int = 1000):
    if not isinstance(u, str) or not u or len(u) > maxlen:
        return None
    if _CTRL_RE.search(u) or ' ' in u:
        return None
    try:
        pu = urlparse(u)
        if pu.scheme != 'https' or pu.username or pu.password:
            return None
        if pu.port not in (None, 443):
            return None
        return (pu.hostname or '').lower() or None
    except ValueError:
        return None


def _host_in(host: str, suffixes) -> bool:
    return any(host == x or host.endswith('.' + x) for x in suffixes)


def _is_media_url(u) -> bool:
    h = _https_host(u, 500)
    return bool(h) and (h == 'youtu.be' or _host_in(h, ('youtube.com',)))


def _is_image_url(u) -> bool:
    h = _https_host(u, 1000)
    return bool(h) and _host_in(h, _IMG_HOST_SUFFIXES)


def _sniff_img(b: bytes) -> str:
    if b[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if b[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if b[:4] == b'RIFF' and b[8:12] == b'WEBP':
        return 'image/webp'
    if b[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    return ''


def _clean_track(t):
    if not isinstance(t, dict) or not _is_media_url(t.get('url')):
        return None
    cover = t.get('cover_url') or t.get('art') or ''
    return {
        'url':       t['url'],
        'title':     _clip(t.get('title'), 300),
        'artist':    _clip(t.get('artist'), 300),
        'album':     _clip(t.get('album'), 300),
        'cover_url': cover if _is_image_url(cover) else '',
        'duration':  _to_int(t.get('duration'), 0, 172800, 0),
    }


def _clean_tracks(raw) -> list:
    if not isinstance(raw, list):
        return []
    out = []
    for t in raw[:_MAX_TRACKS]:
        c = _clean_track(t)
        if c:
            out.append(c)
    return out


def _clean_audio(a) -> dict:
    if not isinstance(a, dict):
        return {}
    try:
        if len(json.dumps(a)) > 4096:
            return {}
    except (TypeError, ValueError):
        return {}
    return {str(k)[:64]: v for k, v in a.items()}


def _bad_const(c):
    raise ValueError(c)      # NaN / Infinity を JSON として受け付けない


# ═══════════════════════════════════════════════════════════════════════════
# ytmusicapi ラッパー
# ═══════════════════════════════════════════════════════════════════════════

def _get_ytmusic():
    try:
        from ytmusicapi import YTMusic
        if AUTH_PATH.exists():
            return YTMusic(str(AUTH_PATH))
        return YTMusic()
    except Exception:
        return None


def _best_thumbnail(thumbs) -> str:
    if not thumbs:
        return ''
    if isinstance(thumbs, str):
        return thumbs
    try:
        best = max(thumbs, key=lambda t: (t.get('width') or 0) * (t.get('height') or 0))
        url  = best.get('url', '')
    except Exception:
        return ''
    url = re.sub(r'=w\d+-h\d+[^&\s]*', '=w500-h500-l90-rj', url)
    url = re.sub(r'=s\d+', '=s500', url)
    return url


def _artists_str(artists) -> str:
    if not artists:
        return ''
    if isinstance(artists, list):
        return ', '.join(a.get('name', '') for a in artists if a.get('name'))
    return str(artists)


def _parse_duration(d) -> int:
    if isinstance(d, (int, float)):
        return int(d)
    if isinstance(d, str):
        try:
            parts = d.strip().split(':')
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except Exception:
            pass
    return 0


def search_songs(query: str, limit: int = 100) -> dict:
    """
    曲を検索する。ytmusicapi + yt-dlp 補完方式。
    ytmusicapi は内部で continuations を処理するが YouTube Music 側の
    上限（通常 40〜50 件）を超えられないため、不足分を yt-dlp で補完する。
    """
    seen: set = set()
    items: list = []

    def _ytm_to_item(r) -> dict | None:
        vid = r.get('videoId', '')
        if not _valid_vid(vid) or vid in seen:
            return None
        seen.add(vid)
        dur_sec = r.get('duration_seconds') or _parse_duration(r.get('duration', 0))
        return {
            'url':      f'https://music.youtube.com/watch?v={vid}',
            'id':       vid,
            'title':    r.get('title', ''),
            'artist':   _artists_str(r.get('artists', [])),
            'album':    (r.get('album') or {}).get('name', ''),
            'art':      _best_thumbnail(r.get('thumbnails', [])),
            'duration': dur_sec,
            'plays':    r.get('views', 0),
        }

    engine = 'yt-dlp'
    yt = _get_ytmusic()
    if yt:
        try:
            results = yt.search(query, filter='songs', limit=limit)
            for r in results:
                it = _ytm_to_item(r)
                if it:
                    items.append(it)
            if items:
                engine = 'ytmusicapi'
        except Exception:
            pass

    # 不足分を yt-dlp で補完
    remaining = limit - len(items)
    if remaining > 0:
        fb = _search_ytdlp(query, remaining * 2)
        for it in fb['items']:
            if it['id'] not in seen:
                seen.add(it['id'])
                items.append(it)
                if len(items) >= limit:
                    break

    return {'items': items[:limit], 'engine': engine}


def search_videos(query: str, limit: int = 100) -> dict:
    """ビデオ（MV等）を検索する。ytmusicapi + yt-dlp 補完方式。"""
    seen: set = set()
    items: list = []

    def _ytm_to_item(r) -> dict | None:
        vid = r.get('videoId', '')
        if not _valid_vid(vid) or vid in seen:
            return None
        seen.add(vid)
        dur_sec = r.get('duration_seconds') or _parse_duration(r.get('duration', 0))
        return {
            'url':      f'https://music.youtube.com/watch?v={vid}',
            'id':       vid,
            'title':    r.get('title', ''),
            'artist':   _artists_str(r.get('artists', [])),
            'album':    '',
            'art':      _best_thumbnail(r.get('thumbnails', [])),
            'duration': dur_sec,
            'plays':    r.get('views', 0),
        }

    engine = 'yt-dlp'
    yt = _get_ytmusic()
    if yt:
        try:
            results = yt.search(query, filter='videos', limit=limit)
            for r in results:
                it = _ytm_to_item(r)
                if it:
                    items.append(it)
            if items:
                engine = 'ytmusicapi'
        except Exception:
            pass

    remaining = limit - len(items)
    if remaining > 0:
        fb = _search_ytdlp(query, remaining * 2)
        for it in fb['items']:
            if it['id'] not in seen:
                seen.add(it['id'])
                items.append(it)
                if len(items) >= limit:
                    break

    return {'items': items[:limit], 'engine': engine}


def search_albums(query: str, limit: int = 16) -> dict:
    yt = _get_ytmusic()
    if not yt:
        return {'items': [], 'engine': 'none'}
    try:
        results = yt.search(query, filter='albums', limit=limit)
        items = []
        for r in results:
            bid = r.get('browseId', '')
            if not bid:
                continue
            items.append({
                'browse_id':   bid,
                'title':       r.get('title', ''),
                'artist':      _artists_str(r.get('artists', [])),
                'year':        str(r.get('year', '')),
                'art':         _best_thumbnail(r.get('thumbnails', [])),
                'track_count': r.get('trackCount', 0),
                'type':        r.get('type', 'Album'),
            })
        return {'items': items, 'engine': 'ytmusicapi'}
    except Exception:
        return {'items': [], 'engine': 'error'}


def get_album_detail(browse_id: str) -> Optional[dict]:
    yt = _get_ytmusic()
    if not yt:
        return None
    try:
        album = yt.get_album(browse_id)
        if not album:
            return None
        tracks_raw = album.get('tracks', [])
        tracks = []
        for t in tracks_raw:
            vid = t.get('videoId', '')
            if not _valid_vid(vid):
                continue
            artists = _artists_str(t.get('artists') or album.get('artists', []))
            thumbs  = t.get('thumbnails') or album.get('thumbnails', [])
            art     = _best_thumbnail(thumbs)
            dur_sec = t.get('duration_seconds') or _parse_duration(t.get('duration', 0))
            tracks.append({
                'url':      f'https://music.youtube.com/watch?v={vid}',
                'id':       vid,
                'title':    t.get('title', ''),
                'artist':   artists,
                'album':    album.get('title', ''),
                'art':      art,
                'duration': dur_sec,
            })
        return {
            'title':       album.get('title', ''),
            'artist':      _artists_str(album.get('artists', [])),
            'year':        str(album.get('year', '')),
            'art':         _best_thumbnail(album.get('thumbnails', [])),
            'track_count': len(tracks),
            'tracks':      tracks,
        }
    except Exception as e:
        print(f'  ⚠ Album detail fetch error: {e}')
        return None


def get_playlist_detail(playlist_url: str) -> Optional[dict]:
    if not _is_media_url(playlist_url):
        return None
    cmd = ['yt-dlp', '--flat-playlist', '--dump-json',
           '--no-warnings', '--quiet', '--', playlist_url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        tracks = []
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                vid  = item.get('id', '')
                if not _valid_vid(vid):
                    continue
                thumb = item.get('thumbnail', '')
                tracks.append({
                    'url':      f'https://music.youtube.com/watch?v={vid}',
                    'id':       vid,
                    'title':    item.get('title', ''),
                    'artist':   item.get('uploader', item.get('channel', '')),
                    'album':    item.get('album', ''),
                    'art':      thumb,
                    'duration': int(item.get('duration') or 0),
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return {'tracks': tracks, 'url': playlist_url} if tracks else None
    except Exception:
        return None


def _search_ytdlp(query: str, limit: int = 80) -> dict:
    cmd = ['yt-dlp', f'ytsearch{limit}:{query}',
           '--dump-json', '--flat-playlist', '--no-warnings', '--quiet']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        items = []
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                vid  = item.get('id', '')
                if not _valid_vid(vid):
                    continue
                items.append({
                    'url':      f'https://music.youtube.com/watch?v={vid}',
                    'id':       vid,
                    'title':    item.get('title', ''),
                    'artist':   item.get('uploader', item.get('channel', '')),
                    'album':    '',
                    'art':      item.get('thumbnail', ''),
                    'duration': int(item.get('duration') or 0),
                    'plays':    item.get('view_count', 0),
                })
            except Exception:
                continue
        return {'items': items, 'engine': 'yt-dlp'}
    except Exception:
        return {'items': [], 'engine': 'error'}


def _load_json_list(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    except Exception:
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _strs(x: dict, keys) -> None:
    for k in keys:
        if not isinstance(x.get(k), str):
            x[k] = ''


def load_favorites() -> List[dict]:
    out = []
    for x in _load_json_list(FAVS_PATH):
        if not isinstance(x.get('url'), str):
            continue
        _strs(x, ('title', 'artist', 'album', 'cover_url'))
        if not isinstance(x.get('audio_settings'), dict):
            x['audio_settings'] = {}
        out.append(x)
    return out


def load_album_favorites() -> list:
    out = []
    for x in _load_json_list(ALBUM_FAVS_PATH):
        if not isinstance(x.get('browse_id'), str):
            continue
        _strs(x, ('title', 'artist', 'year', 'cover_url'))
        tr = x.get('tracks')
        x['tracks'] = [t for t in tr if isinstance(t, dict) and isinstance(t.get('url'), str)] \
            if isinstance(tr, list) else []
        for t in x['tracks']:
            _strs(t, ('title', 'artist'))
        out.append(x)
    return out


def save_album_favorite(browse_id: str, title: str, artist: str,
                        year: str, cover_url: str, tracks: list) -> dict:
    with _fav_lock:
        favs = load_album_favorites()
        nos  = [f.get('no', 0) for f in favs if isinstance(f.get('no'), int)]
        no   = (max(nos) + 1) if nos else 1
        existing = next((i for i, f in enumerate(favs)
                         if f.get('browse_id') == browse_id), None)
        entry = {
            'no':          no if existing is None else favs[existing].get('no', no),
            'browse_id':   browse_id,
            'title':       title,
            'artist':      artist,
            'year':        year,
            'cover_url':   cover_url,
            'track_count': len(tracks),
            'tracks':      tracks,
            'saved_at':    time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        if existing is not None:
            favs[existing] = entry
        else:
            favs.append(entry)
        _write_private(ALBUM_FAVS_PATH, json.dumps(favs, ensure_ascii=False, indent=2))
    return entry


def fetch_cover(url: str) -> bytes:
    """YouTube 系の画像ホストだけを取得する。リダイレクトは1ホップずつ検証。"""
    if not _is_image_url(url):
        return b''
    try:
        import requests
    except Exception:
        return b''
    headers = {
        'User-Agent':
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://music.youtube.com/',
        'Accept':  'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'ja,en-US;q=0.9',
    }

    def _get(u: str) -> bytes:
        cur = u
        for _ in range(4):
            if not _is_image_url(cur):
                return b''
            r = requests.get(cur, timeout=10, headers=headers,
                             allow_redirects=False, stream=True)
            try:
                if r.status_code in (301, 302, 303, 307, 308):
                    cur = urljoin(cur, r.headers.get('Location', ''))
                    continue
                if r.status_code != 200:
                    return b''
                if not r.headers.get('Content-Type', '').lower().startswith('image/'):
                    return b''
                buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf += chunk
                    if len(buf) > _MAX_IMG:
                        return b''
                return bytes(buf)
            finally:
                r.close()
        return b''

    try:
        data = _get(url)
        if data:
            return data
        # 403 等 → yt thumbnail の代替 URL を試みる
        if 'ytimg.com' in url:
            for quality in ('maxresdefault', 'hqdefault', 'mqdefault', 'default'):
                alt = re.sub(
                    r'/(maxresdefault|hqdefault|mqdefault|sddefault|default)(\.jpg)',
                    f'/{quality}\\2', url)
                if alt == url:
                    break
                data = _get(alt)
                if data:
                    return data
        return b''
    except Exception:
        return b''


# ═══════════════════════════════════════════════════════════════════════════
# HTML/CSS
# ═══════════════════════════════════════════════════════════════════════════

def _esc(s: str) -> str:
    return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

def _js(s: str) -> str:
    # 旧版。onclick 属性内では安全でないため使用しない（_jsarg を使う）
    return _esc(str(s)).replace("'","&#39;")

def _jsarg(v) -> str:
    """HTML 属性(ダブルクォート)内の JS 引数。JSON 文字列として出力し、属性用にエスケープ。"""
    return _esc(json.dumps(v, ensure_ascii=True))

def _json_in_script(obj) -> str:
    """<script type="application/json"> 内に安全に埋め込める JSON。"""
    return (json.dumps(obj, ensure_ascii=False)
            .replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
            .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))

def _di(v) -> int:
    return _to_int(v, 0, 10**9, 0)

def _no3(v) -> str:
    try:
        return f'{int(v):03d}'
    except (TypeError, ValueError, OverflowError):
        return '000'

def _dur_str(sec: int) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError, OverflowError):
        return ''
    if not sec: return ''
    if sec >= 3600:
        return f'{sec//3600}:{(sec%3600)//60:02d}:{sec%60:02d}'
    return f'{sec//60}:{sec%60:02d}'

def _num_fmt(n) -> str:
    try: n = int(n)
    except: return ''
    if n >= 1_000_000: return f'{n/1_000_000:.1f}M'
    if n >= 1_000:     return f'{n/1_000:.1f}K'
    return str(n)


CSS = """
:root{--bg:#0f0f0f;--surf:#161616;--card:#1f1f1f;--acc:#ff4458;--acc2:#ff6b7a;
--text:#e8edf2;--muted:#607080;--border:#2a2a2a;--hover:#252525;
--gold:#f5a623;--red:#e05555;--ytred:#ff0033}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
font-family:'Helvetica Neue','Hiragino Kaku Gothic ProN',sans-serif;min-height:100vh}
header{background:var(--surf);border-bottom:1px solid var(--border);
padding:11px 18px;display:flex;align-items:center;gap:10px;
position:sticky;top:0;z-index:200;flex-wrap:wrap}
.logo{font-size:1.05rem;font-weight:700;color:var(--ytred);white-space:nowrap}
.logo span{color:var(--muted);font-weight:400;font-size:.85rem}
.nav{display:flex;gap:5px;flex-wrap:wrap}
.nav a{padding:4px 11px;border-radius:16px;border:1px solid var(--border);
color:var(--muted);text-decoration:none;font-size:.76rem;white-space:nowrap;transition:all .15s}
.nav a:hover,.nav a.active{border-color:var(--acc);color:var(--acc2);background:rgba(255,68,88,.1)}
.sf{display:flex;gap:5px;margin-left:auto;flex:1;max-width:480px}
.sf input{background:var(--card);border:1px solid var(--border);border-radius:7px;
padding:5px 12px;color:var(--text);font-size:.82rem;flex:1;outline:none}
.sf input:focus{border-color:var(--acc)}
.sf button{background:var(--acc);color:#fff;border:none;border-radius:7px;
padding:5px 15px;cursor:pointer;font-size:.82rem;font-weight:600;white-space:nowrap}
.sf button:hover{background:var(--acc2)}
.stabs{display:flex;gap:4px;padding:0 18px 0 0}
.stab{padding:3px 10px;border-radius:12px;border:1px solid var(--border);
color:var(--muted);cursor:pointer;font-size:.74rem;transition:all .15s;background:none}
.stab.active,.stab:hover{border-color:var(--acc);color:var(--acc2);background:rgba(255,68,88,.1)}
.gw{padding:18px}
.gi{font-size:.76rem;color:var(--muted);margin-bottom:12px}
.engine-badge{display:inline-block;padding:2px 8px;border-radius:10px;
font-size:.65rem;background:rgba(255,68,88,.12);color:var(--acc2);
border:1px solid rgba(255,68,88,.25);margin-left:8px;vertical-align:middle}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:9px;
overflow:hidden;cursor:pointer;text-decoration:none;color:inherit;display:block;
transition:transform .15s,border-color .15s,box-shadow .15s;position:relative}
.card:hover{transform:translateY(-3px);border-color:var(--acc);
box-shadow:0 5px 20px rgba(255,68,88,.15)}
.cov{width:100%;aspect-ratio:1;background:var(--surf);position:relative;overflow:hidden}
.cov img{width:100%;height:100%;object-fit:cover;display:block}
.ni{width:100%;height:100%;display:flex;align-items:center;justify-content:center;
font-size:2.5rem;color:var(--border)}
.pbadge{position:absolute;bottom:5px;right:5px;background:rgba(8,10,12,.85);
border:1px solid var(--border);border-radius:3px;padding:1px 5px;
font-size:.58rem;color:var(--acc2);font-weight:600}
.type-badge{position:absolute;top:5px;left:5px;background:var(--acc);
border-radius:3px;padding:1px 6px;font-size:.58rem;color:#fff;font-weight:700}
.ci{padding:9px}
.ci .ttl{font-size:.8rem;font-weight:600;line-height:1.3;margin-bottom:2px;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ci .art{font-size:.7rem;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ci .alb{font-size:.65rem;color:var(--muted);overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;font-style:italic;margin-top:1px}
.ci .meta{margin-top:5px;font-size:.62rem;color:var(--muted);display:flex;gap:8px;flex-wrap:wrap}
.ci .meta span{color:var(--acc2)}
/* アルバム詳細 */
.pp{max-width:860px;margin:0 auto;padding:22px 18px}
.ph{display:flex;gap:20px;margin-bottom:22px;align-items:flex-start}
.ph img{width:145px;height:145px;object-fit:cover;border-radius:9px;
border:1px solid var(--border);flex-shrink:0}
.ph .ni2{width:145px;height:145px;border-radius:9px;border:1px solid var(--border);
flex-shrink:0;display:flex;align-items:center;justify-content:center;
font-size:3rem;background:var(--card);color:var(--border)}
.pm h2{font-size:1.2rem;font-weight:700;margin-bottom:4px}
.pm .par{color:var(--muted);font-size:.88rem;margin-bottom:8px}
.pm .ptag{display:inline-flex;align-items:center;gap:5px;padding:6px 15px;
background:var(--acc);color:#fff;border:none;border-radius:7px;
font-size:.82rem;font-weight:700;cursor:pointer;margin-right:8px;
text-decoration:none;transition:opacity .15s}
.pm .ptag:hover{opacity:.85}
.pm .pback{display:inline-flex;align-items:center;gap:4px;padding:5px 12px;
background:var(--hover);border:1px solid var(--border);border-radius:7px;
color:var(--muted);text-decoration:none;font-size:.78rem}
.pm .pback:hover{border-color:var(--acc);color:var(--acc2)}
.pm .pstat{font-size:.75rem;color:var(--muted);margin-bottom:10px}
.tr{display:flex;align-items:center;gap:11px;padding:9px 13px;
border-radius:7px;cursor:pointer;border:1px solid transparent;
text-decoration:none;color:inherit;margin-bottom:3px;transition:all .12s;position:relative}
.tr:hover{background:var(--hover);border-color:var(--acc)}
.tr.selected{background:var(--hover)!important;border-color:var(--acc)!important;
box-shadow:0 0 0 2px rgba(255,68,88,.2)!important}
.tno{width:28px;text-align:right;font-size:.76rem;color:var(--muted);
font-family:monospace;flex-shrink:0}
.pic{font-size:.72rem;flex-shrink:0;opacity:0;color:var(--acc2)}
.tr:hover .pic{opacity:1}
.ttl2{flex:1;font-size:.85rem;font-weight:500}
.tar{font-size:.72rem;color:var(--muted);white-space:nowrap;overflow:hidden;
text-overflow:ellipsis;max-width:200px}
.tdur{font-size:.72rem;color:var(--muted);font-family:monospace;flex-shrink:0}
/* お気に入り */
.fav-item{display:flex;align-items:center;gap:12px;padding:10px 14px;
border-radius:8px;border:1px solid var(--border);margin-bottom:6px;
cursor:pointer;transition:all .12s}
.fav-item:hover{background:var(--hover);border-color:var(--acc)}
.fav-art{width:50px;height:50px;border-radius:5px;object-fit:cover;
border:1px solid var(--border);flex-shrink:0}
.fav-ni{width:50px;height:50px;border-radius:5px;border:1px solid var(--border);
flex-shrink:0;display:flex;align-items:center;justify-content:center;
font-size:1.5rem;background:var(--card);color:var(--border)}
.fav-info{flex:1;min-width:0}
.fav-title{font-size:.88rem;font-weight:600;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.fav-artist{font-size:.76rem;color:var(--muted)}
.fav-album{font-size:.68rem;color:var(--muted);font-style:italic}
.fav-preset{font-size:.68rem;color:var(--acc2);margin-top:2px}
.fav-no{font-size:.7rem;color:var(--muted);font-family:monospace;flex-shrink:0}
.fav-del{background:none;border:none;cursor:pointer;color:var(--red);
font-size:1.1rem;flex-shrink:0;padding:4px 6px;border-radius:4px;
opacity:.65;transition:opacity .15s}
.fav-del:hover{opacity:1;background:rgba(224,85,85,.12)}
/* empty */
.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.empty .icon{font-size:3rem;margin-bottom:12px}
.empty p{font-size:.9rem}
/* toast */
#toast{position:fixed;bottom:80px;right:24px;background:var(--acc);color:#fff;
padding:10px 18px;border-radius:8px;font-size:.85rem;font-weight:600;
opacity:0;transition:opacity .3s;z-index:999;pointer-events:none}
#toast.show{opacity:1}
/* 選択チェックボックス */
.sel-cb{position:absolute;top:6px;left:6px;width:17px;height:17px;
cursor:pointer;accent-color:var(--acc);z-index:10;
opacity:0;transition:opacity .15s}
.card:hover .sel-cb,.sel-cb:checked{opacity:1}
.card.selected{border-color:var(--acc)!important;
box-shadow:0 0 0 2px rgba(255,68,88,.4)!important}
.card.selected .sel-cb{opacity:1}
.fav-item .sel-cb{position:static;opacity:1;flex-shrink:0;margin-right:2px}
.fav-item.selected{border-color:var(--acc)!important;
box-shadow:0 0 0 2px rgba(255,68,88,.4)!important}
.tr .sel-cb{position:static;opacity:0;flex-shrink:0}
.tr:hover .sel-cb,.tr .sel-cb:checked{opacity:1}
/* キューバー */
#queue-bar{position:fixed;bottom:0;left:0;right:0;
background:var(--surf);border-top:2px solid var(--acc);
padding:11px 20px;display:none;align-items:center;gap:12px;flex-wrap:wrap;
z-index:500;box-shadow:0 -4px 24px rgba(255,68,88,.2)}
#queue-bar.show{display:flex}
#queue-count{font-size:.88rem;font-weight:700;color:var(--acc2);flex:1;min-width:120px}
.qbtn{padding:7px 18px;border:none;border-radius:7px;
cursor:pointer;font-size:.8rem;font-weight:700;white-space:nowrap}
.qbtn-play{background:var(--acc);color:#fff}
.qbtn-play:hover{background:var(--acc2)}
.qbtn-clear{background:transparent;border:1px solid var(--border);color:var(--muted)}
.qbtn-clear:hover{border-color:var(--red);color:var(--red)}
"""

TOAST_JS = """
function toast(msg){
  var t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(function(){t.classList.remove('show')},2200);
}
function playTrack(url,title,artist,album,art,dur){
  fetch('/play',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:url,title:title,artist:artist,album:album,
                         cover_url:art,duration:dur})})
  .then(r=>r.json()).then(d=>{
    if(d.ok) toast('▶ '+title.substring(0,30));
    else toast('⚠ '+d.error);
  }).catch(()=>toast('⚠ Connection error'));
}
function playFavorite(url,title,artist,album,art,dur,audioJson){
  var audio={};
  try{ audio=JSON.parse(audioJson); }catch(e){}
  fetch('/play',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:url,title:title,artist:artist,album:album,
                         cover_url:art,duration:dur,audio_settings:audio})})
  .then(r=>r.json()).then(d=>{
    if(d.ok) toast('▶ Favourite: '+title.substring(0,30));
    else toast('⚠ '+d.error);
  }).catch(()=>toast('⚠ Connection error'));
}
function playPlaylist(tracksJson){
  var tracks=JSON.parse(tracksJson);
  fetch('/play',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({type:'playlist',tracks:tracks})})
  .then(r=>r.json()).then(d=>{
    if(d.ok) toast('▶ Playlist: '+tracks.length+' tracks');
    else toast('⚠ '+d.error);
  }).catch(()=>toast('⚠ Connection error'));
}
// キュー管理
var _queue=(function(){
  try{return JSON.parse(localStorage.getItem('ytm_queue')||'[]');}
  catch(e){return [];}
})();
function _updateQueueBar(){
  try{localStorage.setItem('ytm_queue',JSON.stringify(_queue));}catch(e){}
  var bar=document.getElementById('queue-bar');
  var cnt=document.getElementById('queue-count');
  if(!bar) return;
  if(_queue.length>0){
    bar.classList.add('show');
    cnt.textContent='🎵 '+_queue.length+' selected';
  } else {
    bar.classList.remove('show');
  }
}
function _cardOf(el){
  return el.closest('.card')||el.closest('.fav-item')||el.closest('.tr');
}
function toggleSelect(event){
  event.stopPropagation();
  var cb=event.target;
  if(cb.type!=='checkbox') return;
  var card=_cardOf(cb);
  var d=cb.dataset;
  if(cb.checked){
    if(!_queue.find(function(t){return t.url===d.url;})){
      _queue.push({url:d.url,title:d.title,artist:d.artist,album:d.album||'',
                   cover_url:d.art,duration:parseInt(d.dur)||0});
    }
    if(card) card.classList.add('selected');
  } else {
    _queue=_queue.filter(function(t){return t.url!==d.url;});
    if(card) card.classList.remove('selected');
  }
  _updateQueueBar();
}
function selectAllToggle(){
  var cbs=document.querySelectorAll('.sel-cb');
  if(!cbs.length) return;
  var allChecked=Array.from(cbs).every(function(cb){return cb.checked;});
  _queue=[];
  cbs.forEach(function(cb){
    var card=_cardOf(cb);
    if(allChecked){
      cb.checked=false;
      if(card) card.classList.remove('selected');
    } else {
      cb.checked=true;
      if(card) card.classList.add('selected');
      var d=cb.dataset;
      if(!_queue.find(function(t){return t.url===d.url;})){
        _queue.push({url:d.url,title:d.title,artist:d.artist,album:d.album||'',
                     cover_url:d.art,duration:parseInt(d.dur)||0});
      }
    }
  });
  _updateQueueBar();
}
function playQueue(){
  if(_queue.length===0){toast('⚠ No tracks selected');return;}
  playPlaylist(JSON.stringify(_queue));
  clearQueue();
}
function clearQueue(){
  _queue=[];
  try{localStorage.removeItem('ytm_queue');}catch(e){}
  document.querySelectorAll('.sel-cb').forEach(function(cb){cb.checked=false;});
  document.querySelectorAll('.card.selected,.fav-item.selected,.tr.selected')
    .forEach(function(c){c.classList.remove('selected');});
  _updateQueueBar();
}
function deleteAlbumFav(event, browseId){
  event.stopPropagation();
  fetch('/api/delete_album_favorite',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({browse_id:browseId})})
  .then(function(r){return r.json();})
  .then(function(d){
    if(d.ok){
      var item=event.target.closest('.fav-item');
      if(item) item.remove();
      toast('🗑 Album removed');
    } else { toast('⚠ Remove failed'); }
  }).catch(function(){toast('⚠ Connection error');});
}
function deleteFav(event,url){
  event.stopPropagation();
  fetch('/api/delete_favorite',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:url})})
  .then(function(r){return r.json();})
  .then(function(d){
    if(d.ok){
      var item=event.target.closest('.fav-item');
      if(item) item.remove();
      _queue=_queue.filter(function(t){return t.url!==url;});
      _updateQueueBar();
      toast('🗑 Removed');
    } else { toast('⚠ Remove failed'); }
  }).catch(function(){toast('⚠ Connection error');});
}
function playAlbumFav(el){
  try{
    var tracksJson=el.getAttribute('data-tracks');
    var tracks=JSON.parse(tracksJson);
    if(!tracks.length){toast('⚠ No tracks');return;}
    playPlaylist(JSON.stringify(tracks));
  }catch(e){toast('⚠ Album play error: '+e.message);}
}
function playAllFavorites(){
  var el=document.getElementById('favs-data');
  if(!el){toast('⚠ No data');return;}
  try{
    var tracks=JSON.parse(el.textContent);
    if(!tracks.length){toast('⚠ No favourites');return;}
    playPlaylist(JSON.stringify(tracks));
  }catch(e){toast('⚠ Data load error');}
}
// ── data-* 属性から再生（onclick引数のアポストロフィ問題を完全回避）
function onCardClick(el){
  // チェックボックスのクリックは無視
  if(event && event.target && event.target.type==='checkbox') return;
  var d=el.dataset;
  playTrack(d.url,d.title,d.artist,d.album||'',d.art||'',parseInt(d.dur)||0);
}
function onTrackClick(el){
  if(event && event.target && event.target.type==='checkbox') return;
  var d=el.dataset;
  playTrack(d.url,d.title,d.artist,d.album||'',d.art||'',parseInt(d.dur)||0);
}
// アルバムページ専用（<script id="album-data"> から読む）
function playAlbumAll(){
  var el=document.getElementById('album-data');
  if(!el){toast('⚠ No data');return;}
  try{
    var data=JSON.parse(el.textContent||el.innerText);
    var tracks=(data.tracks||[]).map(function(t){
      return {url:t.url,title:t.title,artist:t.artist,
              album:t.album||data.title||'',
              cover_url:t.cover_url||t.art||data.cover_url||'',
              duration:t.duration||0};
    });
    if(!tracks.length){toast('⚠ No tracks');return;}
    playPlaylist(JSON.stringify(tracks));
  }catch(e){toast('⚠ Error: '+e.message);}
}
function saveAlbum(){
  var el=document.getElementById('album-data');
  if(!el){toast('⚠ No album data');return;}
  var albumData;
  try{albumData=JSON.parse(el.textContent||el.innerText);}
  catch(e){toast('⚠ Parse error: '+e.message);return;}
  fetch('/api/save_album',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(albumData)})
  .then(function(r){if(!r.ok) throw new Error('HTTP '+r.status);return r.json();})
  .then(function(d){
    if(d.ok) toast('💾 Saved: '+d.title+' ('+d.track_count+' tracks)');
    else toast('⚠ Save failed: '+(d.error||''));
  })
  .catch(function(e){toast('\u26a0 '+e.message);});
}
setInterval(function(){
  fetch('/api/status').then(function(r){return r.json();}).then(function(d){
    if(d.close){window.close();}
  }).catch(function(){});
},1500);
(function(){
  if(_queue.length===0) return;
  var qUrls=new Set(_queue.map(function(t){return t.url;}));
  document.querySelectorAll('.sel-cb').forEach(function(cb){
    if(qUrls.has(cb.dataset.url)){
      cb.checked=true;
      var card=_cardOf(cb);
      if(card) card.classList.add('selected');
    }
  });
  _updateQueueBar();
})();
"""


def _page(title: str, body: str, search_val: str = '',
          tab: str = 'songs', nav_active: str = 'search') -> str:
    nav_items = [
        ('search',    '🔍 Search',    '/search'),
        ('favorites', '⭐ Favourites', '/favorites'),
    ]
    nav_html = ''.join(
        f'<a href="{href}" class="{"active" if k == nav_active else ""}">{label}</a>'
        for k, label, href in nav_items
    )
    # 検索タブ（songs / videos / albums）
    if nav_active == 'search':
        tab_html = (
            '<div class="stabs">'
            f'<button class="stab {"active" if tab=="songs" else ""}" '
            f'onclick="switchTab(\'songs\')">🎵 Songs</button>'
            f'<button class="stab {"active" if tab=="videos" else ""}" '
            f'onclick="switchTab(\'videos\')">🎬 Videos</button>'
            f'<button class="stab {"active" if tab=="albums" else ""}" '
            f'onclick="switchTab(\'albums\')">💿 Albums</button>'
            '</div>'
        )
    else:
        tab_html = ''

    return f"""<!DOCTYPE html><html lang="ja"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>{CSS}</style></head><body>
<header>
  <div class="logo">▶ YouTube Music <span>× Qji</span></div>
  <nav class="nav">{nav_html}</nav>
  {tab_html}
  <form class="sf" action="/search" method="get">
    <input name="q" value="{_esc(search_val)}" placeholder="Artist / title / album..."
           id="sq" autocomplete="off">
    <button type="submit">Search</button>
  </form>
</header>
{body}
<div id="toast"></div>
<div id="queue-bar">
  <span id="queue-count">🎵 0 selected</span>
  <button class="qbtn qbtn-clear" onclick="clearQueue()">✕ Clear</button>
  <button class="qbtn qbtn-play" onclick="playQueue()">▶ Play selected</button>
</div>
<script>
{TOAST_JS}
function switchTab(t){{
  sessionStorage.setItem('ytm_tab',t);
  var q=document.getElementById('sq').value;
  if(q) window.location='/search?q='+encodeURIComponent(q)+'&tab='+t;
}}
(function(){{
  var saved=sessionStorage.getItem('ytm_tab');
  if(saved && window.location.search.indexOf('tab=')===-1 && saved!=='songs'){{
    var q=document.getElementById('sq').value;
    if(q && window.location.pathname==='/search'){{
      window.location='/search?q='+encodeURIComponent(q)+'&tab='+saved;
    }}
  }}
}})();
</script>
</body></html>"""


# ─── ページ: 曲検索 ───────────────────────────────────────────────────────

def _track_cards(items: list, engine: str) -> str:
    if not items:
        return ''
    cards = []
    for t in items:
        art_html = (f'<img src="/cover?url={quote(t["art"])}" loading="lazy" alt="">'
                    if t.get('art') else '<div class="ni">🎵</div>')
        dur_badge = (f'<div class="pbadge">{_dur_str(t["duration"])}</div>'
                     if t.get('duration') else '')
        plays = f'<span>▶ {_num_fmt(t.get("plays",0))}</span>' if t.get('plays') else ''
        dur   = f'<span>{_dur_str(t["duration"])}</span>'      if t.get('duration') else ''
        album = f'<div class="alb">《{_esc(t["album"])}》</div>' if t.get('album') else ''
        cb = (
            f'<input type="checkbox" class="sel-cb"'
            f' data-url="{_esc(t["url"])}"'
            f' data-title="{_esc(t["title"])}"'
            f' data-artist="{_esc(t["artist"])}"'
            f' data-album="{_esc(t.get("album",""))}"'
            f' data-art="{_esc(t.get("art",""))}"'
            f' data-dur="{_di(t.get("duration"))}"'
            f' onclick="toggleSelect(event)">'
        )
        cards.append(
            f'<div class="card"'
            f' data-url="{_esc(t["url"])}"'
            f' data-title="{_esc(t["title"])}"'
            f' data-artist="{_esc(t["artist"])}"'
            f' data-album="{_esc(t.get("album",""))}"'
            f' data-art="{_esc(t.get("art",""))}"'
            f' data-dur="{_di(t.get("duration"))}"'
            f' onclick="onCardClick(this)">'
            f'{cb}'
            f'<div class="cov">{art_html}{dur_badge}</div>'
            f'<div class="ci">'
            f'<div class="ttl">{_esc(t["title"])}</div>'
            f'<div class="art">{_esc(t["artist"])}</div>'
            f'{album}'
            f'<div class="meta">{plays}{dur}</div>'
            f'</div></div>'
        )
    return ''.join(cards)


def page_search_songs(query: str, limit: int = 80) -> str:
    if not query:
        body = '<div class="empty"><div class="icon">🎵</div><p>Enter a keyword to search</p></div>'
        return _page('YouTube Music × Qji', body, tab='songs')
    result = search_songs(query, limit=limit)
    items  = result['items']
    engine = result.get('engine', '')
    if not items:
        body = f'<div class="empty"><div class="icon">🔍</div><p>No results for "{_esc(query)}"</p></div>'
        return _page(f'{query} — YouTube Music', body, search_val=query, tab='songs')

    all_json = json.dumps([
        {'url': t['url'], 'title': t['title'], 'artist': t['artist'],
         'album': t.get('album',''), 'cover_url': t.get('art',''), 'duration': t.get('duration',0)}
        for t in items
    ], ensure_ascii=False)
    all_json = _jsarg(all_json)

    engine_badge = f'<span class="engine-badge">{engine}</span>'
    # 「もっと読み込む」ボタン（さらに多く取得する場合）
    more_btn = ''
    if len(items) >= limit:
        next_limit = limit + 80
        more_btn = (
            f'<a href="/search?q={quote(query)}&tab=songs&limit={next_limit}" '
            f'style="display:inline-block;padding:5px 16px;background:var(--hover);'
            f'border:1px solid var(--border);border-radius:7px;color:var(--muted);'
            f'text-decoration:none;font-size:.8rem;font-weight:600;white-space:nowrap;margin-left:8px">'
            f'▼ Load more ({next_limit})</a>'
        )
    toolbar = (
        f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px;flex-wrap:wrap">'
        f'<div class="gi" style="margin:0">🎵 {len(items)} results{engine_badge}</div>'
        f'<button onclick="selectAllToggle()" '
        f'style="padding:4px 12px;background:var(--hover);border:1px solid var(--border);'
        f'border-radius:7px;color:var(--muted);cursor:pointer;font-size:.76rem;white-space:nowrap">'
        f'☑ Select all / Deselect</button>'
        f'<button onclick="playPlaylist({all_json})" '
        f'style="margin-left:auto;padding:5px 16px;background:var(--hover);'
        f'border:1px solid var(--border);border-radius:7px;color:var(--muted);'
        f'cursor:pointer;font-size:.8rem;font-weight:600;white-space:nowrap">'
        f'▶ Play all {len(items)}</button>'
        f'{more_btn}'
        f'</div>'
    )
    body = f'<div class="gw">{toolbar}<div class="grid">{_track_cards(items, engine)}</div></div>'
    return _page(f'{query} — YouTube Music', body, search_val=query, tab='songs')


def page_search_videos(query: str, limit: int = 80) -> str:
    if not query:
        body = '<div class="empty"><div class="icon">🎬</div><p>Enter a keyword to search</p></div>'
        return _page('YouTube Music × Qji', body, tab='videos')
    result = search_videos(query, limit=limit)
    items  = result['items']
    engine = result.get('engine', '')
    if not items:
        body = f'<div class="empty"><div class="icon">🔍</div><p>No results for "{_esc(query)}"</p></div>'
        return _page(f'{query} — YouTube Music', body, search_val=query, tab='videos')
    engine_badge = f'<span class="engine-badge">{engine}</span>'
    more_btn = ''
    if len(items) >= limit:
        next_limit = limit + 80
        more_btn = (
            f'<a href="/search?q={quote(query)}&tab=videos&limit={next_limit}" '
            f'style="display:inline-block;padding:5px 16px;background:var(--hover);'
            f'border:1px solid var(--border);border-radius:7px;color:var(--muted);'
            f'text-decoration:none;font-size:.8rem;font-weight:600;white-space:nowrap;margin-left:8px">'
            f'▼ Load more ({next_limit})</a>'
        )
    toolbar = (
        f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px;flex-wrap:wrap">'
        f'<div class="gi" style="margin:0">🎬 {len(items)} results{engine_badge}</div>'
        f'<button onclick="selectAllToggle()" '
        f'style="padding:4px 12px;background:var(--hover);border:1px solid var(--border);'
        f'border-radius:7px;color:var(--muted);cursor:pointer;font-size:.76rem;white-space:nowrap">'
        f'☑ Select all / Deselect</button>'
        f'{more_btn}'
        f'</div>'
    )
    body = f'<div class="gw">{toolbar}<div class="grid">{_track_cards(items, engine)}</div></div>'
    return _page(f'{query} — YouTube Music', body, search_val=query, tab='videos')


# ─── ページ: アルバム検索 ─────────────────────────────────────────────────

def page_search_albums(query: str) -> str:
    if not query:
        body = '<div class="empty"><div class="icon">💿</div><p>Enter a keyword to search</p></div>'
        return _page('YouTube Music × Qji', body, tab='albums')
    result = search_albums(query, limit=16)
    items  = result['items']
    if not items:
        ytm = _get_ytmusic()
        if not ytm:
            body = ('<div class="empty"><div class="icon">⚠</div>'
                    '<p>Album search requires ytmusicapi.<br>'
                    '<code>pip install ytmusicapi --break-system-packages</code></p></div>')
        else:
            body = f'<div class="empty"><div class="icon">🔍</div><p>No albums found for "{_esc(query)}"</p></div>'
        return _page(f'{query} — YouTube Music', body, search_val=query, tab='albums')

    cards = []
    for a in items:
        art_html = (f'<img src="/cover?url={quote(a["art"])}" loading="lazy" alt="">'
                    if a.get('art') else '<div class="ni">💿</div>')
        kind  = a.get('type', 'Album')
        tc    = f'<span>{_di(a["track_count"])} tracks</span>' if a.get('track_count') else ''
        year  = f'<span>{_esc(a["year"])}</span>'           if a.get('year')        else ''
        cards.append(
            f'<a class="card" href="/album?id={quote(a["browse_id"])}">'
            f'<div class="cov">{art_html}'
            f'<div class="type-badge">{_esc(kind)}</div>'
            f'</div>'
            f'<div class="ci">'
            f'<div class="ttl">{_esc(a["title"])}</div>'
            f'<div class="art">{_esc(a["artist"])}</div>'
            f'<div class="meta">{tc}{year}</div>'
            f'</div></a>'
        )
    body = f'<div class="gw"><div class="gi">💿 {len(items)} albums</div><div class="grid">{"".join(cards)}</div></div>'
    return _page(f'{query} — YouTube Music', body, search_val=query, tab='albums')


# ─── ページ: アルバム詳細 ─────────────────────────────────────────────────

def page_album(browse_id: str) -> str:
    if not browse_id:
        return _page('Error', '<div class="empty"><p>No ID specified</p></div>')
    detail = get_album_detail(browse_id)
    if not detail or not detail.get('tracks'):
        return _page('Album', '<div class="empty"><div class="icon">⚠</div>'
                     '<p>Could not fetch tracks.</p></div>')

    tracks      = detail['tracks']
    total_dur   = sum(t.get('duration', 0) for t in tracks)
    art_html    = (f'<img src="/cover?url={quote(detail["art"])}" alt="">'
                   if detail.get('art') else '<div class="ni2">💿</div>')


    rows = []
    for i, t in enumerate(tracks, 1):
        dur_html  = f'<span class="tdur">{_dur_str(t["duration"])}</span>' if t.get('duration') else ''
        art_thumb = (f'<img src="/cover?url={quote(t["art"])}" '
                     f'style="width:36px;height:36px;border-radius:4px;object-fit:cover;'
                     f'border:1px solid var(--border);flex-shrink:0" loading="lazy" alt="">'
                     if t.get('art') else '')
        cb = (
            f'<input type="checkbox" class="sel-cb"'
            f' data-url="{_esc(t["url"])}"'
            f' data-title="{_esc(t["title"])}"'
            f' data-artist="{_esc(t["artist"])}"'
            f' data-album="{_esc(t.get("album",""))}"'
            f' data-art="{_esc(t.get("art",""))}"'
            f' data-dur="{_di(t.get("duration"))}"'
            f' onclick="toggleSelect(event)">'
        )
        rows.append(
            f'<div class="tr"'
            f' data-url="{_esc(t["url"])}"'
            f' data-title="{_esc(t["title"])}"'
            f' data-artist="{_esc(t["artist"])}"'
            f' data-album="{_esc(t.get("album",""))}"'
            f' data-art="{_esc(t.get("art",""))}"'
            f' data-dur="{_di(t.get("duration"))}"'
            f' onclick="onTrackClick(this)">'
            f'{cb}'
            f'<span class="tno">{i}</span>'
            f'<span class="pic">▶</span>'
            f'{art_thumb}'
            f'<span class="ttl2">{_esc(t["title"])}</span>'
            f'<span class="tar">{_esc(t["artist"])}</span>'
            f'{dur_html}</div>'
        )

    pl_toolbar = (
        f'<div style="display:flex;align-items:center;gap:8px;'
        f'margin-bottom:10px;flex-wrap:wrap;padding:6px 0">'
        f'<span style="font-size:.76rem;color:var(--muted)">🎵 {len(tracks)} tracks</span>'
        f'<button onclick="selectAllToggle()" '
        f'style="padding:3px 11px;background:var(--hover);border:1px solid var(--border);'
        f'border-radius:7px;color:var(--muted);cursor:pointer;font-size:.74rem;white-space:nowrap">'
        f'☑ Select all / Deselect</button>'
        f'</div>'
    )

    # <script id='album-data'> にアルバム全データを埋め込む
    # onclick 属性へのJSON渡しはアポストロフィで壊れるため script タグ方式を採用
    album_script_data = _json_in_script({
        'browse_id':  browse_id,
        'title':      detail['title'],
        'artist':     detail['artist'],
        'year':       detail.get('year', ''),
        'cover_url':  detail.get('art', ''),
        'tracks':     [
            {'url': t['url'], 'title': t['title'], 'artist': t['artist'],
             'album': t.get('album', detail['title']),
             'cover_url': t.get('art', detail.get('art', '')),
             'art':       t.get('art', detail.get('art', '')),
             'duration':  t.get('duration', 0)}
            for t in tracks
        ],
    })
    body = f"""<div class="pp">
<a href="javascript:history.back()" style="display:inline-flex;align-items:center;
gap:4px;padding:5px 12px;background:var(--hover);border:1px solid var(--border);
border-radius:7px;color:var(--muted);text-decoration:none;font-size:.78rem;margin-bottom:14px">
← Back</a>
<div class="ph">
  {art_html}
  <div class="pm">
    <h2>{_esc(detail["title"])}</h2>
    <div class="par">{_esc(detail["artist"])}  {_esc(detail.get("year",""))}</div>
    <div class="pstat">{len(tracks)} tracks  ·  {_dur_str(total_dur)}</div>
    <button class="ptag" onclick="playAlbumAll()">▶ Play all</button>
    <button class="ptag" style="background:var(--card);border:1px solid var(--acc);color:var(--acc2)" onclick="saveAlbum()">💾 Save to favourites</button>
  </div>
</div>
{pl_toolbar}
{"".join(rows)}
</div>
<script id="album-data" type="application/json">{album_script_data}</script>
"""
    return _page(f'{detail["title"]} — YouTube Music', body)


# ─── ページ: お気に入り ────────────────────────────────────────────────────

def page_favorites() -> str:
    # ─── アルバムお気に入り ───────────────────────────────────────
    album_favs = load_album_favorites()
    album_rows = []
    for a in album_favs:
        tracks_json_a = (_esc(json.dumps([
            {'url': t['url'], 'title': t['title'], 'artist': t['artist'],
             'album': t.get('album', a['title']),
             'cover_url': t.get('cover_url', t.get('art', a.get('cover_url', ''))),
             'duration': t.get('duration', 0)}
            for t in a.get('tracks', [])
        ], ensure_ascii=False)))
        art_html = (
            f'<img class="fav-art" src="/cover?url={quote(a.get("cover_url",""))}"'
            f' loading="lazy" alt="">'
            if a.get('cover_url') else '<div class="fav-ni">\U0001f4bf</div>'
        )
        tc   = _di(a.get('track_count', len(a.get('tracks', []))))
        year = ('  ' + a['year']) if a.get('year') else ''
        bid_js = _jsarg(a['browse_id'])
        del_btn = (
            '<button class="fav-del" title="\u524a\u9664"'
            f' onclick="deleteAlbumFav(event,{bid_js})">\U0001f5d1</button>'
        )
        album_rows.append(
            f'<div class="fav-item album-fav-item" data-tracks="{tracks_json_a}"'
            f' onclick="playAlbumFav(this)">'
            + art_html
            + '<div class="fav-info">'
            + f'<div class="fav-title">\U0001f4bf {_esc(a["title"])}</div>'
            + f'<div class="fav-artist">{_esc(a["artist"])}{_esc(year)}</div>'
            + f'<div class="fav-album">{tc}\u66f2</div>'
            + '</div>'
            + f'<span class="fav-no">#{_no3(a.get("no"))}</span>'
            + del_btn
            + '</div>'
        )

    favs = load_favorites()
    if not favs and not album_favs:
        body = ('<div class="empty"><div class="icon">⭐</div>'
                '<p>No favourites yet.<br>Press [s] during playback to add tracks.</p></div>')
        return _page('Favourites — YouTube Music', body, nav_active='favorites')

    rows = []
    for f in favs:
        art_html = (f'<img class="fav-art" src="/cover?url={quote(f.get("cover_url",""))}" '
                    f'loading="lazy" alt="">'
                    if f.get('cover_url') else '<div class="fav-ni">🎵</div>')
        dur   = _dur_str(f.get('duration', 0))
        audio = f.get('audio_settings') or {}
        fp_lb = FILTER_PRESET_LABELS.get(audio.get('filter_preset', ''), '')
        gp_db = GAIN_PRESETS_DB.get(audio.get('gain_preset', ''))
        vol   = audio.get('volume')
        gp_lb = (f'🔊{gp_db:+.1f}dB' if gp_db is not None else '')
        vl_lb = (f'Vol:{_esc(vol)}dB'       if vol is not None   else '')
        preset_display = '  '.join(x for x in [fp_lb, gp_lb, vl_lb] if x)
        audio_json = _jsarg(json.dumps(audio, ensure_ascii=False))
        cb = (
            f'<input type="checkbox" class="sel-cb"'
            f' data-url="{_esc(f["url"])}"'
            f' data-title="{_esc(f["title"])}"'
            f' data-artist="{_esc(f["artist"])}"'
            f' data-album="{_esc(f.get("album",""))}"'
            f' data-art="{_esc(f.get("cover_url",""))}"'
            f' data-dur="{_di(f.get("duration"))}"'
            f' onclick="toggleSelect(event)">'
        )
        del_btn = (
            f'<button class="fav-del" title="Remove"'
            f' onclick="deleteFav(event,{_jsarg(f["url"])})">🗑</button>'
        )
        album_line = (f'<div class="fav-album">{_esc(f["album"])}</div>'
                      if f.get('album') else '')
        rows.append(
            f'<div class="fav-item" onclick="playFavorite({_jsarg(f["url"])},'
            f'{_jsarg(f["title"])},{_jsarg(f["artist"])},'
            f'{_jsarg(f.get("album",""))},{_jsarg(f.get("cover_url",""))},'
            f'{_di(f.get("duration"))},{audio_json})">'
            f'{cb}'
            f'{art_html}'
            f'<div class="fav-info">'
            f'<div class="fav-title">{_esc(f["title"])}</div>'
            f'<div class="fav-artist">{_esc(f["artist"])}  {dur}</div>'
            f'{album_line}'
            f'<div class="fav-preset">{preset_display}</div>'
            f'</div>'
            f'<span class="fav-no">#{_no3(f.get("no"))}</span>'
            f'{del_btn}'
            f'</div>'
        )

    favs_json = _json_in_script([
        {'url': f['url'], 'title': f['title'], 'artist': f['artist'],
         'album': f.get('album',''), 'cover_url': f.get('cover_url',''),
         'duration': _di(f.get('duration'))}
        for f in favs
    ])

    if favs:
        toolbar = (
            f'<div style="display:flex;align-items:center;gap:8px;'
            f'margin-bottom:14px;flex-wrap:wrap">'
            f'<div class="gi" style="margin:0">⭐ Favourites — {len(favs)}</div>'
            f'<button onclick="selectAllToggle()" '
            f'style="padding:4px 12px;background:var(--hover);border:1px solid var(--border);'
            f'border-radius:7px;color:var(--muted);cursor:pointer;font-size:.76rem;white-space:nowrap">'
            f'☑ Select all / Deselect</button>'
            f'<button onclick="playAllFavorites()" '
            f'style="margin-left:auto;padding:5px 16px;background:var(--hover);'
            f'border:1px solid var(--border);border-radius:7px;color:var(--muted);'
            f'cursor:pointer;font-size:.8rem;font-weight:600;white-space:nowrap">'
            f'▶ Play all {len(favs)}</button>'
            f'</div>'
        )
    else:
        toolbar = ''
    data_tag = f'<script id="favs-data" type="application/json">{favs_json}</script>'
    album_section = ''
    if album_rows:
        album_section = (
            '<div style="margin-bottom:8px;font-size:.78rem;color:var(--muted);'
            'font-weight:600;padding:4px 0">'
            f'💿 Saved albums ({len(album_rows)})</div>'
            + ''.join(album_rows)
            + '<hr style="border:none;border-top:1px solid var(--border);margin:14px 0">'
        )
    body = f'<div class="gw">{toolbar}{album_section}{"".join(rows)}</div>{data_tag}'
    return _page('Favourites — YouTube Music', body, nav_active='favorites')


# ═══════════════════════════════════════════════════════════════════════════
# HTTP ハンドラ
# ═══════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _sec_headers(self, html: bool = False):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        if html:
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header(
                'Content-Security-Policy',
                "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src 'self'; "
                "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")

    def _guard(self, post: bool = False) -> bool:
        """Host / Origin / Content-Type を検証する（DNS リバインディング・CSRF 対策）。"""
        port = self.server.server_address[1]
        host = (self.headers.get('Host') or '').lower()
        if host not in {f'localhost:{port}', f'127.0.0.1:{port}', f'[::1]:{port}'}:
            self._json({'ok': False, 'error': 'forbidden'}, 403)
            return False
        if post:
            origin = self.headers.get('Origin')
            if origin is not None and origin != f'http://{host}':
                self._json({'ok': False, 'error': 'forbidden origin'}, 403)
                return False
            site = self.headers.get('Sec-Fetch-Site')
            if site is not None and site not in ('same-origin', 'none'):
                self._json({'ok': False, 'error': 'forbidden'}, 403)
                return False
            ctype = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            if ctype != 'application/json':
                self._json({'ok': False, 'error': 'unsupported media type'}, 415)
                return False
        return True

    def _send(self, html: str, code: int = 200):
        data = html.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(data))
        self._sec_headers(html=True)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: dict, code: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(data))
        self.send_header('Cache-Control', 'no-store')
        self._sec_headers()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _img(self, data: bytes):
        if not data:
            self.send_response(204); self.end_headers(); return
        ctype = _sniff_img(data)
        if not ctype:
            self.send_response(204); self.end_headers(); return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', len(data))
        self.send_header('Cache-Control', 'max-age=3600')
        self._sec_headers()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self._guard():
            return
        pars = urlparse(self.path)
        qs   = parse_qs(pars.query)
        def q(k, d=''): return qs.get(k, [''])[0] or d   # parse_qs でデコード済み
        path = pars.path

        if path in ('/', '/search'):
            query = _clip(q('q'), 200).strip()
            tab   = q('tab', 'songs')
            try:
                limit = int(qs.get('limit', ['80'])[0])
                limit = max(20, min(limit, 400))  # 20〜400件の範囲に制限
            except (ValueError, KeyError):
                limit = 80
            if tab == 'albums':
                self._send(page_search_albums(query))
            elif tab == 'videos':
                self._send(page_search_videos(query, limit=limit))
            else:
                self._send(page_search_songs(query, limit=limit))

        elif path == '/album':
            bid = q('id')
            if bid and not _BID_RE.match(bid):
                self._send('<p>Bad Request</p>', code=400)
            else:
                self._send(page_album(bid))

        elif path == '/favorites':
            self._send(page_favorites())

        elif path == '/cover':
            self._img(fetch_cover(q('url')))

        elif path == '/api/status':
            closed = _CLOSE_SIGNAL.is_set()
            self._json({'active': not closed, 'close': closed})

        else:
            self._send('<p>Not Found</p>', code=404)

    def do_POST(self):
        if not self._guard(post=True):
            return
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            length = -1
        if length < 0 or length > _MAX_BODY:
            self._json({'ok': False, 'error': 'invalid or oversized request'}, 413)
            return
        try:
            body = (json.loads(self.rfile.read(length), parse_constant=_bad_const)
                    if length else {})
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}

        if path == '/play':
            if body.get('type', 'track') == 'playlist':
                tracks = _clean_tracks(body.get('tracks'))
                if not tracks:
                    self._json({'ok': False, 'error': 'no playable tracks'}, 400)
                    return
                req = {
                    'type':         'playlist',
                    'tracks':       tracks,
                    'start_from':   _to_int(body.get('start_from', 1), 1, len(tracks), 1),
                    'requested_at': time.time(),
                }
                try:
                    _write_private(REQUEST_PATH, json.dumps(req, ensure_ascii=False))
                    print(f'\n  📋 Playlist play request: {len(tracks)} tracks')
                    self._json({'ok': True})
                except Exception as e:
                    print(f'  ⚠ Request write error: {e}')
                    self._json({'ok': False, 'error': 'write failed'}, 500)

            else:
                t = _clean_track({
                    'url':       body.get('url'),
                    'title':     body.get('title'),
                    'artist':    body.get('artist'),
                    'album':     body.get('album'),
                    'cover_url': body.get('cover_url'),
                    'duration':  body.get('duration'),
                })
                if not t:
                    self._json({'ok': False, 'error': 'a YouTube URL is required'}, 400)
                    return
                req = {
                    'type':           'track',
                    'url':            t['url'],
                    'title':          t['title'],
                    'artist':         t['artist'],
                    'album':          t['album'],
                    'cover_url':      t['cover_url'],
                    'duration':       t['duration'],
                    'audio_settings': _clean_audio(body.get('audio_settings')),
                    'requested_at':   time.time(),
                }
                try:
                    _write_private(REQUEST_PATH, json.dumps(req, ensure_ascii=False))
                    print(f'\n  📱 Browser play: {req["artist"]} — {req["title"] or req["url"]}')
                    self._json({'ok': True})
                except Exception as e:
                    print(f'  ⚠ Request write error: {e}')
                    self._json({'ok': False, 'error': 'write failed'}, 500)

        elif path == '/api/delete_favorite':
            url = body.get('url', '')
            if not isinstance(url, str) or not url or len(url) > 500:
                self._json({'ok': False, 'error': 'URL required'}, 400)
                return
            try:
                with _fav_lock:
                    favs     = load_favorites()
                    new_favs = [f for f in favs if f.get('url') != url]
                    found    = len(new_favs) != len(favs)
                    if found:
                        _write_private(FAVS_PATH, json.dumps(
                            new_favs, ensure_ascii=False, indent=2))
                if not found:
                    self._json({'ok': False, 'error': 'not found'}, 404)
                    return
                print(f'\n  🗑 Favourite removed: {_clip(url, 60)}')
                self._json({'ok': True})
            except Exception as e:
                print(f'  ⚠ Favourite removal error: {e}')
                self._json({'ok': False, 'error': 'removal failed'}, 500)

        elif path == '/api/save_album':
            browse_id = body.get('browse_id', '')
            title     = _clip(body.get('title'), 300)
            if not isinstance(browse_id, str) or not _BID_RE.match(browse_id) or not title:
                self._json({'ok': False, 'error': 'browse_id / title required'}, 400)
                return
            tracks = _clean_tracks(body.get('tracks'))
            cover  = body.get('cover_url')
            cover  = cover if _is_image_url(cover) else ''
            try:
                entry = save_album_favorite(
                    browse_id, title,
                    _clip(body.get('artist'), 300),
                    _clip(body.get('year'), 16),
                    cover, tracks)
                print(f'\n  💾 Album saved: {title} ({len(tracks)} tracks)')
                self._json({'ok': True, 'title': title,
                            'no': entry['no'], 'track_count': len(tracks)})
            except Exception as e:
                print(f'  ⚠ Album save error: {e}')
                self._json({'ok': False, 'error': 'save failed'}, 500)

        elif path == '/api/delete_album_favorite':
            browse_id = body.get('browse_id', '')
            if not isinstance(browse_id, str) or not _BID_RE.match(browse_id):
                self._json({'ok': False, 'error': 'browse_id required'}, 400)
                return
            try:
                with _fav_lock:
                    favs     = load_album_favorites()
                    new_favs = [f for f in favs if f.get('browse_id') != browse_id]
                    found    = len(new_favs) != len(favs)
                    if found:
                        _write_private(ALBUM_FAVS_PATH, json.dumps(
                            new_favs, ensure_ascii=False, indent=2))
                if not found:
                    self._json({'ok': False, 'error': 'not found'}, 404)
                    return
                self._json({'ok': True})
            except Exception as e:
                print(f'  ⚠ Album removal error: {e}')
                self._json({'ok': False, 'error': 'removal failed'}, 500)
        else:
            self._send('<p>Not Found</p>', code=404)


# ═══════════════════════════════════════════════════════════════════════════
# サーバー管理
# ═══════════════════════════════════════════════════════════════════════════

def start_browser_server(port: int = PORT, open_browser: bool = True) -> bool:
    global _server, _server_thread
    if _server:
        return True
    try:
        # このPC専用: 127.0.0.1 のみで待ち受ける（LAN には公開しない）
        _server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        _server.daemon_threads = True
        _server_thread = threading.Thread(
            target=_server.serve_forever, daemon=True)
        _server_thread.start()
        print(f'  🌐 YouTube Music browser UI: http://localhost:{port}')
        if open_browser:
            time.sleep(0.3)
            try:
                subprocess.Popen(['xdg-open', f'http://localhost:{port}'],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception:
                pass
        return True
    except Exception as e:
        _server = None
        print(f'  ⚠ Browser UI launch failed: {e}')
        return False


def stop_browser_server():
    global _server
    srv, _server = _server, None
    if srv:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()      # ポートを確実に解放する
        except Exception:
            pass


def send_close_signal():
    """qji_ytmusic.py の b キー押下時に呼ぶ。ブラウザタブを閉じる。"""
    _CLOSE_SIGNAL.set()
    def _reset():
        time.sleep(3); _CLOSE_SIGNAL.clear()
    threading.Thread(target=_reset, daemon=True).start()


def _drop_request():
    try:
        REQUEST_PATH.unlink()
    except OSError:
        pass


def check_browser_request() -> Optional[dict]:
    try:
        st = os.lstat(REQUEST_PATH)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        _drop_request()
        return None
    try:
        req = json.loads(REQUEST_PATH.read_text(encoding='utf-8'),
                         parse_constant=_bad_const)
    except Exception:
        _drop_request()          # 壊れたファイルを残さない
        return None
    _drop_request()
    if not isinstance(req, dict):
        return None
    try:
        age = time.time() - float(req.get('requested_at', 0))
    except (TypeError, ValueError, OverflowError):
        return None
    if age > 10:
        return None
    return req


# ═══════════════════════════════════════════════════════════════════════════
# スタンドアロン起動
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import signal
    print(f'🎵 Qji × YouTube Music Browser UI  port:{PORT}')
    start_browser_server(PORT, open_browser=True)
    def _sig(s, f):
        print('\nExiting')
        stop_browser_server()
        sys.exit(0)
    signal.signal(signal.SIGINT, _sig)
    print('  Ctrl+C to exit')
    while True:
        req = check_browser_request()
        if req:
            print(f'  📱 Request: {req.get("type")} — {req.get("title", req.get("url",""))}')
        time.sleep(0.5)
