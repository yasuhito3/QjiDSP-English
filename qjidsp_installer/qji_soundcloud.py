#!/usr/bin/env python3
"""
qji_soundcloud.py  —  Qji 統合 SoundCloud ストリーミングモジュール
qji.py から S キーで呼び出される。

再生中キー:
  [n]次  [b]前  [q]メニューへ
  [g]ゲイン切替  [w]APL ON/OFF  [c]プリセット選択
  [+]/[=]Vol +1dB  [-]Vol -1dB
  [s]Save to favourites  [i]Show cover  [ESC]Hide cover
"""

import os, re, sys, json, select, time, threading, subprocess, tempfile
import termios, tty
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Tuple
import stat as _stat
from urllib.parse import urlparse as _urlparse, urljoin as _urljoin

# ═══════════════════════════════════════════════════════════════════════════
# Security helpers (functions for not blindly trusting URLs, strings and files that come from outside)
# ═══════════════════════════════════════════════════════════════════════════

# Only SoundCloud URLs (including subdomains) may be played or fetched
_ALLOWED_MEDIA_HOSTS  = ('soundcloud.com',)
# Hosts for SoundCloud API v2 and the transcoding URLs it returns
_ALLOWED_API_HOSTS    = ('soundcloud.com', 'sndcdn.com')
# Hosts that serve the actual audio stream (the only hosts whose URLs may be passed to ffmpeg)
_ALLOWED_STREAM_HOSTS = ('sndcdn.com', 'soundcloud.com', 'soundcloud.cloud')
# Hosts cover art may be fetched from
_ALLOWED_IMAGE_HOSTS  = ('sndcdn.com', 'soundcloud.com')
# Hosts the JavaScript used for client_id extraction may be fetched from
_ALLOWED_SCRIPT_HOSTS = ('sndcdn.com',)
_MAX_IMAGE_BYTES  = 8 * 1024 * 1024
_MAX_JSON_BYTES   = 8 * 1024 * 1024
_MAX_SCRIPT_BYTES = 8 * 1024 * 1024
_ANSI_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]')
_CTRL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')


def _host_allowed(host, allowed) -> bool:
    host = (host or '').lower().rstrip('.')
    return any(host == h or host.endswith('.' + h) for h in allowed)


def _safe_url(url, allowed_hosts, schemes) -> Optional[str]:
    """Return the URL only if it points at an allowed host. Anything else (strings starting with '-', control characters,
    other hosts, embedded credentials, unusual ports, etc.) gives None."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > 2000 or url.startswith('-') or _CTRL_RE.search(url) or ' ' in url:
        return None
    try:
        u = _urlparse(url)
        port = u.port
    except ValueError:
        return None
    if u.scheme not in schemes or u.username or u.password or port not in (None, 80, 443):
        return None
    if not _host_allowed(u.hostname, allowed_hosts):
        return None
    return url


def _safe_media_url(url) -> Optional[str]:
    return _safe_url(url, _ALLOWED_MEDIA_HOSTS, ('http', 'https'))


def _safe_image_url(url) -> Optional[str]:
    return _safe_url(url, _ALLOWED_IMAGE_HOSTS, ('https',))


def _safe_stream_url(url) -> Optional[str]:
    """Stream URL that may be passed to ffmpeg (https on SoundCloud delivery hosts only)."""
    return _safe_url(url, _ALLOWED_STREAM_HOSTS, ('https',))


def _clean(value, maxlen: int = 300) -> str:
    """Strip terminal control sequences (colours, cursor movement, title changes, etc.) from external strings such as track titles."""
    text = '' if value is None else str(value)
    text = _ANSI_RE.sub('', text)
    text = _CTRL_RE.sub(' ', text)
    return text.strip()[:maxlen]


def _as_int(v, default, lo=None, hi=None) -> int:
    try:
        if isinstance(v, bool):
            raise ValueError
        n = int(v)
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _fetch_limited(url, allowed_hosts, *, params=None, headers=None, timeout=12,
                   max_bytes=_MAX_JSON_BYTES, content_type=None):
    """Fetch only https URLs on allowed hosts. Each redirect is validated, and
    the download is cut off once the size limit is exceeded. Returns: (HTTP status, body bytes or None).
    Status 0 means "refused before connecting / network error"."""
    try:
        import requests
        cur = _safe_url(url, allowed_hosts, ('https',))
        if not cur:
            return 0, None
        hdrs = {'User-Agent': SC_AGENT}
        hdrs.update(headers or {})
        cur_params = params
        for _hop in range(4):
            r = requests.get(cur, params=cur_params, headers=hdrs, timeout=timeout,
                             stream=True, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                nxt = _safe_url(_urljoin(cur, r.headers.get('Location', '')),
                                allowed_hosts, ('https',))
                r.close()
                if not nxt:
                    return 0, None
                cur, cur_params = nxt, None
                continue
            break
        else:
            return 0, None
        try:
            status = r.status_code
            if status != 200:
                return status, None
            if content_type and not str(r.headers.get('Content-Type', '')).lower().startswith(content_type):
                return status, None
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > max_bytes:
                    return status, None
            return status, bytes(buf)
        finally:
            r.close()
    except Exception:
        return 0, None


_PRIV_DIR = None


def _private_dir() -> Path:
    """A work folder only you can read and write. Used so fixed-name files are not left in the shared /tmp.
    If a candidate is owned by someone else or is a symlink it is not used; a new private folder is made instead."""
    global _PRIV_DIR
    if _PRIV_DIR is not None:
        return _PRIV_DIR
    uid = os.getuid()
    cands = []
    xdg = os.environ.get('XDG_RUNTIME_DIR')
    if xdg:
        cands.append(Path(xdg) / 'qji')
    cands.append(Path(tempfile.gettempdir()) / f'qji-{uid}')
    for p in cands:
        try:
            p.mkdir(mode=0o700, exist_ok=True)
            st = os.lstat(p)
            if not _stat.S_ISDIR(st.st_mode) or st.st_uid != uid:
                continue
            if st.st_mode & 0o077:
                os.chmod(p, 0o700)
            _PRIV_DIR = p
            return p
        except OSError:
            continue
    _PRIV_DIR = Path(tempfile.mkdtemp(prefix='qji-'))
    return _PRIV_DIR


def _write_private(path, data) -> None:
    """Create with mode 600, then replace (no window in which other users could read it while it is being written)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode('utf-8')
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(str(tmp), flags, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    os.replace(str(tmp), str(path))


def _ensure_private_file(path) -> bool:
    """Check that a file holding secrets (e.g. cookies) is safe to read.
    Symlinks and files owned by other users are rejected; loose permissions are tightened to 600."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return False
    if not _stat.S_ISREG(st.st_mode):
        print(f'  ⚠ {path} is not a regular file — not using it')
        return False
    if st.st_uid != os.getuid():
        print(f'  ⚠ {path} is owned by another user — not using it')
        return False
    if st.st_mode & 0o077:
        try:
            os.chmod(str(path), 0o600)
            print(f'  🔒 {path} permissions tightened to 600 (it holds cookies, so only you should be able to read it)')
        except OSError:
            print(f'  ⚠ {path} permissions could not be fixed; please run chmod 600 manually')
            return False
    return True


_install_declined = set()


def _confirm_install(pkg: str, cmd: list) -> bool:
    """Show what will be installed and ask before installing automatically at runtime (default: No)."""
    if pkg in _install_declined:
        return False
    print(f'  📦 {pkg} was not found. Install it with the following command?')
    print('     ' + ' '.join(cmd))
    try:
        ans = input('     [y/N]: ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ''
    if ans in ('y', 'yes'):
        return True
    _install_declined.add(pkg)
    return False


def _apply_audio_settings(state: dict, audio) -> None:
    """Accept audio settings from favourites, saved playlists and the browser UI,
    but only for allowed items and ranges (so arbitrary values cannot end up in the filter string)."""
    if not isinstance(audio, dict):
        return
    if audio.get('gain_preset') in GAIN_PRESETS_DB:
        state['gain_preset'] = audio['gain_preset']
    if audio.get('filter_preset') in FILTER_PRESETS:
        state['filter_preset'] = audio['filter_preset']
    for k in ('air_layer', 'musikverein', 'musikverein_room_effects', 'tinnitus'):
        if isinstance(audio.get(k), bool):
            state[k] = audio[k]
    v = audio.get('volume')
    if not isinstance(v, bool) and isinstance(v, (int, float)):
        state['volume'] = _as_int(v, state.get('volume', 12), -20, 30)
    em = audio.get('echo_mode')
    if isinstance(em, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,24}', em):
        state['echo_mode'] = em


def _sanitize_track(t) -> Optional[dict]:
    """Validate track info that comes from outside (browser UI, saved files, etc.).
    Returns None if the URL is not allowed. Strings lose control characters; numbers are clamped to a sane range."""
    if not isinstance(t, dict):
        return None
    url = _safe_media_url(t.get('url', ''))
    if not url:
        return None
    return {
        'url': url,
        'id': _clean(t.get('id', ''), 64),
        'title': _clean(t.get('title', '')), 'artist': _clean(t.get('artist', '')),
        'genre': _clean(t.get('genre', '')),
        'cover_url': str(t.get('cover_url') or t.get('art') or '')[:500],
        'duration': _as_int(t.get('duration', 0), 0, 0, 24 * 3600),
        'likes': _as_int(t.get('likes', 0), 0, 0, 10**12),
        'plays': _as_int(t.get('plays', 0), 0, 0, 10**12),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 定数
# ═══════════════════════════════════════════════════════════════════════════

CONFIG_PATH      = Path.home() / '.config' / 'qji_soundcloud.json'
FAVS_PATH        = Path.home() / '.config' / 'qji_soundcloud_favorites.json'
PLAYLISTS_PATH   = Path.home() / '.config' / 'qji_soundcloud_playlists.json'
REQUEST_PATH = _private_dir() / 'qji_soundcloud_request.json'   # a folder of your own, not the shared /tmp
_JACKET_TMP  = _private_dir() / 'qji_soundcloud_jacket.jpg'
BROWSER_PORT = 8081
BROWSER_URL  = f'http://127.0.0.1:{BROWSER_PORT}'   # the browser UI is reachable only from this PC (127.0.0.1)
SC_API_BASE  = 'https://api-v2.soundcloud.com'
SC_AGENT     = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

FILTER_PRESETS = ['musikverein', 'piano', 'chamber', 'vocal', 'jazz', 'calm', 'deep', 'spatial', 'radio', 'bypass']
FILTER_PRESET_LABELS = {
    'musikverein': '🎻 Musikverein',
    'piano':       '🎹 Piano',
    'chamber':     '🏠 Chamber',
    'vocal':       '🎙 Vocal',
    'jazz':        '🎷 Jazz',
    'calm':        '🌿 Calm (安らぎ)',
    'deep':        '🌊 Deep (深淵)',
    'spatial':     '🌐 Spatial (3D空間音響)',
    'radio':       '📻 Radio',
    'bypass':      '⚪ Bypass (素通し)',
}
GAIN_PRESETS_DB = {
    'classical': 0.0, 'general': -1.5, 'jazz_pop': -3.5, 'loud': -5.0,
}
GAIN_ORDER = ['classical', 'general', 'jazz_pop', 'loud']

# ─── ブラウザウィンドウ管理 ──────────────────────────────────────────────
import shutil as _shutil
_incognito_proc: Optional[subprocess.Popen] = None
_incognito_tmp_dir: Optional[str] = None


def _open_incognito_browser(url: str) -> bool:
    global _incognito_proc, _incognito_tmp_dir
    _close_incognito_browser()
    import tempfile as _tf
    tmp = _tf.mkdtemp(prefix='qji_sc_')
    _incognito_tmp_dir = tmp
    chrome_flags = [f'--user-data-dir={tmp}', '--incognito', '--new-window',
                    '--no-first-run', '--no-default-browser-check', '--disable-extensions']
    candidates = [
        (['chromium-browser'] + chrome_flags + [url], 'Chromium'),
        (['chromium']         + chrome_flags + [url], 'Chromium'),
        (['google-chrome']    + chrome_flags + [url], 'Chrome'),
        (['google-chrome-stable'] + chrome_flags + [url], 'Chrome'),
        (['firefox', '--new-instance', '--private-window', url], 'Firefox'),
    ]
    for cmd, name in candidates:
        try:
            _incognito_proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.3)
            if _incognito_proc.poll() is None:
                print(f'  🔒 {name} opened in private window')
                return True
            _incognito_proc = None
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f'  ⚠ {name} launch error: {e}'); continue
    _shutil.rmtree(tmp, ignore_errors=True); _incognito_tmp_dir = None
    try:
        subprocess.Popen(['xdg-open', url], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f'  🌐 Browser opened (fallback): {url}')
        return True
    except Exception as e:
        print(f'  ⚠ Could not open browser: {e}'); return False


def _close_incognito_browser():
    global _incognito_proc, _incognito_tmp_dir
    if _incognito_proc is not None:
        try:
            if _incognito_proc.poll() is None:
                _incognito_proc.terminate()
                try:
                    _incognito_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    _incognito_proc.kill(); time.sleep(0.4)
                print('  🔒 Private window closed')
        except Exception:
            pass
        finally:
            _incognito_proc = None
    if _incognito_tmp_dir is not None:
        _shutil.rmtree(_incognito_tmp_dir, ignore_errors=True)
        _incognito_tmp_dir = None


# ═══════════════════════════════════════════════════════════════════════════
# ターミナル管理
# ═══════════════════════════════════════════════════════════════════════════

_saved_term    = None
_baseline_term = None   # 起動時に1度だけ保存する「汚染されない」基準設定


def _capture_baseline_term():
    """
    run() の先頭で1度だけ呼ぶ。rawモード操作前の正常なターミナル設定を
    _baseline_term へ保存する。_baseline_term は絶対に上書きしない。
    """
    global _baseline_term
    if _baseline_term is not None:
        return   # ★ 2回目以降は上書きしない（汚染防止）
    try:
        _baseline_term = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass


def _save_terminal():
    global _saved_term
    try:
        _saved_term = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass


def _restore_terminal():
    """
    ターミナルを確実に正常状態へ戻す（Qobuzと同等の3ステップ実装）。
    _saved_term は rawモード中に汚染されうるため使用しない。
    _baseline_term（起動時ベースライン）を優先して適用する。
    """
    fd = sys.stdin.fileno()

    # 1. stty sane: ターミナルを POSIX 標準の正常状態へリセット
    try:
        with open('/dev/tty', 'r') as _tty:
            subprocess.run(['stty', 'sane'], stdin=_tty,
                           check=False, timeout=2)
    except Exception:
        try:
            os.system('stty sane 2>/dev/null')
        except Exception:
            pass

    # 2. ベースライン設定を適用（stty sane の後に上書き → 確実な復元）
    ref = _baseline_term if _baseline_term is not None else _saved_term
    try:
        if ref is not None:
            termios.tcsetattr(fd, termios.TCSANOW, ref)
    except Exception:
        pass

    # 3. 残留入力をフラッシュ
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except Exception:
        pass

    sys.stdout.flush()


def _sc_readline(prompt: str = '') -> str:
    result = ''
    tty_fd = None
    try:
        time.sleep(0.1)
        tty_fd = open('/dev/tty', 'r', encoding='utf-8', errors='replace')
        subprocess.run(['stty', 'sane'], stdin=tty_fd, check=False, timeout=1)
        if prompt:
            print(prompt, end='', flush=True)
        result = tty_fd.readline().rstrip('\n').strip()
    except Exception as e:
        print(f'\n  ⚠ Input error: {e}')
    finally:
        if tty_fd:
            try: tty_fd.close()
            except Exception: pass
        try:
            time.sleep(0.05)
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except Exception:
            pass
    return result


# ═══════════════════════════════════════════════════════════════════════════
# 設定・client_id 管理
# ═══════════════════════════════════════════════════════════════════════════

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    except Exception:
        return {}

def _save_config(cfg: dict):
    _write_private(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))


def _extract_client_id() -> Optional[str]:
    """Extract client_id from the JavaScript on the SoundCloud website.
    Fetches only from https on soundcloud.com and sndcdn.com (redirects validated, size-capped)."""
    status, body = _fetch_limited('https://soundcloud.com', ('soundcloud.com',),
                                  timeout=15, max_bytes=_MAX_SCRIPT_BYTES)
    if not body:
        return None
    html_text = body.decode('utf-8', errors='replace')
    scripts = re.findall(
        r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"',
        html_text)
    for script_url in reversed(scripts[-8:]):
        st, js = _fetch_limited(script_url, _ALLOWED_SCRIPT_HOSTS,
                                timeout=10, max_bytes=_MAX_SCRIPT_BYTES)
        if not js:
            continue
        text = js.decode('utf-8', errors='replace')
        for pattern in [
            r'client_id:"([a-zA-Z0-9]{32})"',
            r'client_id,"([a-zA-Z0-9]{32})"',
            r'"client_id":"([a-zA-Z0-9]{32})"',
            r'clientId:"([a-zA-Z0-9]{32})"',
        ]:
            m = re.search(pattern, text)
            if m:
                return m.group(1)
    return None


def _get_client_id(force_refresh: bool = False) -> Optional[str]:
    """
    キャッシュ済み client_id を返す。
    存在しないか 24 時間以上経過していれば再取得してキャッシュに保存する。
    """
    cfg = _load_config()
    cid       = cfg.get('client_id', '')
    fetched   = cfg.get('client_id_fetched_at', 0)
    age_hours = (time.time() - fetched) / 3600

    if cid and age_hours < 24 and not force_refresh:
        return cid

    print('  🔍 Fetching SoundCloud client_id...')
    new_cid = _extract_client_id()
    if new_cid:
        cfg['client_id']             = new_cid
        cfg['client_id_fetched_at']  = time.time()
        _save_config(cfg)
        print(f'  ✅ client_id obtained')
        return new_cid
    if cid:
        print('  ⚠ client_id update failed (using cached value)')
        return cid
    print('  ❌ Could not obtain client_id')
    return None


# ═══════════════════════════════════════════════════════════════════════════
# SoundCloud API v2 / yt-dlp ラッパー
# ═══════════════════════════════════════════════════════════════════════════

def _sc_api(endpoint: str, params: dict, client_id: str) -> Optional[dict]:
    """SoundCloud API v2 を呼び出す。"""
    p = {'client_id': client_id, 'limit': 20, **params}
    status, body = _fetch_limited(f'{SC_API_BASE}{endpoint}', _ALLOWED_API_HOSTS,
                                  params=p, timeout=12, max_bytes=_MAX_JSON_BYTES)
    if status != 200 or not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _sc_api_raw(endpoint: str, params: dict, client_id: str):
    """Call SoundCloud API v2 and return the JSON (dict or list) as is. None on failure."""
    p = {'client_id': client_id, **params}
    status, body = _fetch_limited(f'{SC_API_BASE}{endpoint}', _ALLOWED_API_HOSTS,
                                  params=p, timeout=15, max_bytes=_MAX_JSON_BYTES)
    if status != 200 or not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, (dict, list)) else None


def _playlist_tracks_via_api(playlist_url: str) -> Optional[List[dict]]:
    """
    Fetch the tracks of a playlist (/sets/...) through the SoundCloud API (no yt-dlp needed).
      1. /resolve?url=...  → the playlist; only the first few tracks carry full details, the rest are bare id stubs
      2. /tracks?ids=...   → fetch the details of the stub tracks in batches (30 at a time)
    Returns None for non-playlists (user pages, etc.) or on failure (the caller falls back to yt-dlp).
    """
    for attempt in (0, 1):
        cid = _get_client_id(force_refresh=(attempt == 1))
        if not cid:
            return None
        data = _sc_api_raw('/resolve', {'url': playlist_url}, cid)
        if isinstance(data, dict):
            break
    else:
        return None
    raw = data.get('tracks')
    if not isinstance(raw, list) or not raw:
        return None
    raw = [t for t in raw[:500] if isinstance(t, dict)]
    full, need = {}, []
    for t in raw:
        if t.get('permalink_url') and t.get('title'):
            full[t.get('id')] = t
        elif isinstance(t.get('id'), int):
            need.append(t['id'])
    for k in range(0, len(need), 30):
        chunk = need[k:k + 30]
        res = _sc_api_raw('/tracks', {'ids': ','.join(str(i) for i in chunk)}, cid)
        for it in (res if isinstance(res, list) else []):
            if isinstance(it, dict):
                full[it.get('id')] = it
    out = []
    for t in raw:                       # keep the playlist's original order
        it = full.get(t.get('id'))
        x = _track_from_api(it) if it else None
        if x:
            out.append(x)
    return out or None


def _normalize_artwork(url: Optional[str]) -> str:
    """artwork_url を 500×500 サイズに変換する。"""
    if not url:
        return ''
    return re.sub(r'-(large|t300x300|t67x67|small|badge|tiny|crop|t20x20)\.jpg',
                  '-t500x500.jpg', url)


def _track_from_api(item: dict) -> Optional[dict]:
    """Convert one API response item into an internal track dict. None if the URL is not allowed."""
    if not isinstance(item, dict):
        return None
    url = _safe_media_url(item.get('permalink_url', ''))
    if not url:
        return None
    user = item.get('user') if isinstance(item.get('user'), dict) else {}
    return {
        'url':       url,
        'id':        _clean(item.get('id', ''), 64),
        'title':     _clean(item.get('title', '')),
        'artist':    _clean(user.get('username', '')),
        'cover_url': _normalize_artwork(
                         item.get('artwork_url') or user.get('avatar_url'))[:500],
        'duration':  _as_int(item.get('duration') or 0, 0, 0, 10**9) // 1000,   # ms → sec
        'genre':     _clean(item.get('genre', '')),
        'likes':     _as_int(item.get('likes_count', 0), 0, 0, 10**12),
        'plays':     _as_int(item.get('playback_count', 0), 0, 0, 10**12),
    }


def search_tracks(query: str, limit: int = 20,
                  client_id: Optional[str] = None) -> List[dict]:
    """キーワードでトラックを検索する。"""
    cid = client_id or _get_client_id()
    if not cid:
        return []
    d = _sc_api('/search/tracks', {'q': query, 'limit': limit}, cid)
    if not d:
        return []
    coll = d.get('collection', [])
    if not isinstance(coll, list):
        return []
    return [x for x in (_track_from_api(t) for t in coll) if x]


def search_playlists(query: str, limit: int = 10,
                     client_id: Optional[str] = None) -> List[dict]:
    """キーワードでプレイリストを検索する。"""
    cid = client_id or _get_client_id()
    if not cid:
        return []
    d = _sc_api('/search/playlists', {'q': query, 'limit': limit}, cid)
    if not d:
        return []
    results = []
    coll = d.get('collection', [])
    for item in (coll if isinstance(coll, list) else []):
        if not isinstance(item, dict):
            continue
        url = _safe_media_url(item.get('permalink_url', ''))
        if not url:
            continue
        user = item.get('user') if isinstance(item.get('user'), dict) else {}
        results.append({
            'url':         url,
            'id':          _clean(item.get('id', ''), 64),
            'title':       _clean(item.get('title', '')),
            'artist':      _clean(user.get('username', '')),
            'cover_url':   _normalize_artwork(item.get('artwork_url'))[:500],
            'track_count': _as_int(item.get('track_count', 0), 0, 0, 100000),
            'duration':    _as_int(item.get('duration') or 0, 0, 0, 10**9) // 1000,
        })
    return results


def get_playlist_tracks(playlist_url: str) -> List[dict]:
    """
    SoundCloud プレイリスト/セット URL からトラックリストを取得する。
    yt-dlp の --flat-playlist を使って高速に取得する。
    """
    playlist_url = _safe_media_url(playlist_url)
    if not playlist_url:
        print('  ⚠ Unsupported URL (soundcloud.com only)')
        return []
    # ① SoundCloud API (primary — works even when yt-dlp's SoundCloud support is broken)
    try:
        via_api = _playlist_tracks_via_api(playlist_url)
    except Exception:
        via_api = None
    if via_api:
        return via_api

    # ② yt-dlp (fallback)
    cmd = ['yt-dlp', '--flat-playlist', '--dump-json', '--no-warnings',
           '--quiet', '--', playlist_url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        tracks = []
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, KeyError):
                continue
            if not isinstance(item, dict):
                continue
            t = _sanitize_track({
                'url':       item.get('url', '') or item.get('webpage_url', ''),
                'id':        item.get('id', ''),
                'title':     item.get('title', ''),
                'artist':    item.get('uploader', item.get('channel', '')),
                'cover_url': _normalize_artwork(item.get('thumbnail', '')),
                'duration':  item.get('duration') or 0,
                'genre':     item.get('genre', ''),
                'likes':     item.get('like_count', 0),
                'plays':     item.get('view_count', 0),
            })
            if t:
                tracks.append(t)
            if len(tracks) >= 500:
                break
        return tracks
    except Exception as e:
        print(f'  ⚠ Playlist fetch error: {_clean(e, 120)}')
        return []


class _DrmProtected(Exception):
    """This track is DRM-protected only and cannot be played with ffmpeg (retrying or falling back is pointless)."""


def _resolve_via_sc_api(track_url: str, client_id: str) -> Optional[str]:
    """
    SoundCloud API v2 を使って直接ストリームURLを取得する。
    yt-dlp 不要・高速・タイムアウトなし。

    手順:
      1. /resolve?url=...  → track info (media.transcodings and track_authorization)
      2. transcoding URL   → 実際のストリームURL
    優先順位: progressive MP3 > HLS MP3 > HLS Opus
    - Encrypted HLS (DRM: cbc/ctr-encrypted-hls, skd:// keys) cannot be played by ffmpeg, so it is excluded.
    - If one format fails (404, etc.), the next candidate is tried in turn.
    - Requests carry track_authorization (as yt-dlp does; some tracks return 404/401 without it).

    URLs returned by the API are not trusted as they are:
      - transcoding URLs are fetched only over https on SoundCloud (client_id is never sent elsewhere)
      - the final stream URL is allowed only over https on SoundCloud delivery hosts
    """
    track_url = _safe_media_url(track_url)
    if not track_url:
        return None
    try:
        # Step 1: URLからトラック情報を取得
        status, body = _fetch_limited(f'{SC_API_BASE}/resolve', _ALLOWED_API_HOSTS,
                                      params={'url': track_url, 'client_id': client_id},
                                      timeout=12)
        if status != 200 or not body:
            print(f'  ⚠ /resolve → HTTP {status}')
            return None

        data = json.loads(body)
        if not isinstance(data, dict):
            return None
        media = data.get('media') if isinstance(data.get('media'), dict) else {}
        transcodings = [t for t in (media.get('transcodings') or []) if isinstance(t, dict)]
        if not transcodings:
            print('  ⚠ No transcodings (private track?)')
            return None

        auth = data.get('track_authorization')
        auth = auth if isinstance(auth, str) and re.fullmatch(r'[A-Za-z0-9._~-]{1,4000}', auth) else None

        # Step 2: sort the candidates by score (encrypted ones excluded)
        def _info(tc):
            fmt = tc.get('format') if isinstance(tc.get('format'), dict) else {}
            return str(fmt.get('protocol', '')), str(fmt.get('mime_type', ''))

        def _score(tc):
            proto, mime = _info(tc)
            if proto == 'progressive' and 'mpeg' in mime:
                return 3
            if proto == 'hls' and 'mpeg' in mime:
                return 2
            if proto == 'hls':
                return 1
            if proto == 'progressive':
                return 1
            return 0

        usable, skipped = [], []
        for tc in transcodings:
            proto, mime = _info(tc)
            (skipped if 'encrypted' in proto else usable).append(tc)
        usable.sort(key=_score, reverse=True)
        print('  📡 Available: ' + (', '.join(f'{_clean(_info(t)[0], 30)}' for t in usable) or 'none')
              + (f'  / excluded as encrypted (DRM): {len(skipped)}' if skipped else ''))
        if not usable:
            raise _DrmProtected()

        # Step 3: resolve the transcoding URL to the actual stream URL (on failure, move to the next candidate)
        for tc in usable:
            tc_url = tc.get('url', '')
            proto, mime = _info(tc)
            if not tc_url:
                continue
            params = {'client_id': client_id}
            if auth:
                params['track_authorization'] = auth
            status2, body2 = _fetch_limited(tc_url, _ALLOWED_API_HOSTS, params=params, timeout=12)
            if status2 != 200 or not body2:
                print(f'  ⚠ {_clean(proto, 30)} / {_clean(mime, 40)} → HTTP {status2} (trying the next format)')
                continue
            try:
                data2 = json.loads(body2)
            except ValueError:
                continue
            raw_stream = data2.get('url', '') if isinstance(data2, dict) else ''
            stream_url = _safe_stream_url(raw_stream)
            if not stream_url:
                if raw_stream:
                    host = (_urlparse(str(raw_stream)).hostname or '?') if isinstance(raw_stream, str) else '?'
                    print(f'  ⚠ Not using this format — the delivery host is not allowed: {_clean(host, 80)}')
                continue
            # Just in case: if the delivery path is DRM (FairPlay, etc.), do not use it even when the protocol name is 'hls\'
            if re.search(r'/(cbcs|cenc|ctr|cbc)/', _urlparse(stream_url).path):
                print(f'  ⚠ Not using this format — DRM-protected delivery ({_clean(proto, 30)})')
                continue
            print(f'  📡 Format: {_clean(proto, 30)} / {_clean(mime, 40)}')
            print(f'  ✅ Stream URL obtained ({_clean(proto, 30)})')
            return stream_url

        print('  ⚠ No playable format found')
        return None

    except _DrmProtected:
        raise
    except Exception as e:
        print(f'  ⚠ API stream URL error: {_clean(e, 120)}')
        return None


def _resolve_stream_url(track_url: str,
                        cookies_file: Optional[str] = None) -> Optional[str]:
    """
    SoundCloud API v2 でストリームURLを取得する。
    client_id が失効していた場合は自動更新して1回再試行する。
    両方失敗した場合のみ yt-dlp にフォールバック。
    """
    track_url = _safe_media_url(track_url)
    if not track_url:
        return None

    # ① SoundCloud API（メイン・高速）
    try:
        cid = _get_client_id()
        if cid:
            url = _resolve_via_sc_api(track_url, cid)
            if url:
                return url

            # API failed → client_id may have expired. Force a refresh and retry
            print('  🔄 Updating client_id and retrying...')
            cid_new = _get_client_id(force_refresh=True)
            if cid_new and cid_new != cid:
                url = _resolve_via_sc_api(track_url, cid_new)
                if url:
                    print('  ✅ Stream URL obtained after refreshing client_id')
                    return url
            print('  ⚠ SoundCloud API failed — falling back to yt-dlp...')
    except _DrmProtected:
        # DRM-protected-only track: refreshing client_id or calling yt-dlp is pointless, so skip right away
        print('  🔒 This track is DRM-protected and cannot be played — skipping')
        return None

    # ② yt-dlp（フォールバック・タイムアウト20秒）
    try:
        cmd = ['yt-dlp', '-g', '--no-playlist', '-f', 'bestaudio/best',
               '--no-warnings']
        if cookies_file:
            cpath = Path(str(cookies_file)).expanduser()
            # A cookie file is your login itself. Never use one that is owned by someone else, a symlink, or loosely permissioned
            if cpath.exists() and _ensure_private_file(cpath):
                cmd += ['--cookies', str(cpath)]
        cmd += ['--', track_url]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            first = r.stdout.strip().split('\n')[0].strip()
            url = _safe_stream_url(first)
            if url:
                print(f'  ✅ Stream URL obtained via yt-dlp')
                return url
            if first:
                print('  ⚠ Not playing — the host yt-dlp returned is not allowed')
        if r.stderr.strip():
            print(f'  ⚠ yt-dlp: {_clean(r.stderr.strip().split(chr(10))[0], 150)}')
    except subprocess.TimeoutExpired:
        print('  ⚠ yt-dlp timed out')
    except Exception as e:
        print(f'  ⚠ yt-dlp error: {_clean(e, 120)}')

    return None


def _get_track_info_ytdlp(track_url: str) -> Optional[dict]:
    """yt-dlp でトラックのメタデータを取得する（フル情報）。"""
    track_url = _safe_media_url(track_url)
    if not track_url:
        return None
    cmd = ['yt-dlp', '--dump-json', '--no-playlist', '--no-warnings',
           '--quiet', '--', track_url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            lines = [l for l in r.stdout.strip().split('\n') if l.strip()]
            if lines:
                item = json.loads(lines[0])
                if not isinstance(item, dict):
                    return None
                return _sanitize_track({
                    'url':       item.get('webpage_url') or track_url,
                    'id':        item.get('id', ''),
                    'title':     item.get('title', ''),
                    'artist':    item.get('uploader', item.get('artist', '')),
                    'cover_url': _normalize_artwork(item.get('thumbnail', '')),
                    'duration':  item.get('duration') or 0,
                    'genre':     item.get('genre', ''),
                    'likes':     item.get('like_count', 0),
                    'plays':     item.get('view_count', 0),
                })
    except Exception:
        pass
    return None


def _ensure_ytdlp() -> bool:
    """yt-dlp がインストールされているか確認する。"""
    try:
        r = subprocess.run(['yt-dlp', '--version'], capture_output=True, timeout=5)
        return r.returncode == 0
    except FileNotFoundError:
        print('  ⚠ yt-dlp not found. Install with:')
        print('    pip install yt-dlp --break-system-packages')
        return False


def _ensure_requests() -> bool:
    try:
        import requests; return True
    except ImportError:
        cmd = [sys.executable, '-m', 'pip', 'install', '--user', '--quiet', 'requests']
        if not _confirm_install('requests', cmd):
            print('    To install it manually: pip install requests')
            return False
        r = subprocess.run(cmd, capture_output=True)
        return r.returncode == 0


# ═══════════════════════════════════════════════════════════════════════════
# ジャケット & Now Playing
# ═══════════════════════════════════════════════════════════════════════════

_feh_proc   = None
_feh_hidden = False


def _download_jacket(cover_url: str) -> Optional[str]:
    """Fetch cover art. Limited to SoundCloud image hosts; every redirect is validated,
    the size is capped (8MB) and the content type checked, then it is saved in a folder of your own."""
    url = _safe_image_url(cover_url)
    if not url:
        return None
    status, body = _fetch_limited(url, _ALLOWED_IMAGE_HOSTS, timeout=10,
                                  max_bytes=_MAX_IMAGE_BYTES, content_type='image/')
    if status != 200 or not body:
        return None
    try:
        _write_private(_JACKET_TMP, body)
        return str(_JACKET_TMP)
    except Exception:
        return None


def _feh_hide():
    global _feh_proc, _feh_hidden
    if _feh_proc and _feh_proc.poll() is None:
        try:
            _feh_proc.terminate(); _feh_proc.wait(timeout=1)
        except Exception:
            pass
    _feh_proc   = None
    _feh_hidden = True


def _feh_show(track_info: dict = None, jacket_path: str = None):
    global _feh_proc, _feh_hidden
    _feh_hidden = False
    path = jacket_path or (str(_JACKET_TMP) if _JACKET_TMP.exists() else None)
    if not path:
        sys.stdout.write('\r  ⚠ No cover art available            \n')
        sys.stdout.flush()
        return
    try:
        import __main__ as _qji
        show_fn = getattr(_qji, 'show_cover_image_with_info', None)
        if show_fn and track_info:
            if _feh_proc and _feh_proc.poll() is None:
                try: _feh_proc.terminate(); _feh_proc.wait(timeout=1)
                except Exception: pass
            _feh_proc = show_fn(path, track_info)
        else:
            if _feh_proc and _feh_proc.poll() is None:
                try: _feh_proc.terminate(); _feh_proc.wait(timeout=1)
                except Exception: pass
            _feh_proc = subprocess.Popen(
                ['feh', '--fullscreen', '--auto-zoom', '--hide-pointer',
                 '--borderless', '--no-menus', path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sys.stdout.write('\r  🖼 Cover art displayed              \n')
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(f'\r  ⚠ Cover art error: {e}        \n')
        sys.stdout.flush()


def _update_now_playing(track: dict, state: dict, elapsed: int = 0):
    global _feh_proc
    try:
        import __main__ as _qji
        lock = getattr(_qji, 'info_display_lock', None)
        ctx  = lock if lock else __import__('contextlib').nullcontext()

        preset  = FILTER_PRESET_LABELS.get(state.get('filter_preset', 'musikverein'), '')
        gain_db = GAIN_PRESETS_DB.get(state.get('gain_preset', 'classical'), 0.0)
        vol     = state.get('volume', 12)
        apl     = '🌿 ON' if state.get('air_layer', True) else 'OFF'

        track_info = {
            'title':        track.get('title', ''),
            'artist':       track.get('artist', ''),
            'album':        'SoundCloud',
            'composer':     '',
            'conductor':    '',
            'performer':    track.get('artist', ''),
            'genre':        f'SoundCloud  {track.get("genre", "")}',
            'tempo':        f'{preset}  Gain:{gain_db:+.1f}dB  APL:{apl}  Vol:{vol}dB',
            'mode':         'soundcloud',
            'track_num':    0,
            'total_tracks': 0,
            'file_path':    track.get('url', ''),
            'duration':     str(track.get('duration', 0)),
            'elapsed':      elapsed,
        }

        with ctx:
            info = getattr(_qji, 'current_track_info', {})
            info.update(track_info)
            if hasattr(_qji, 'current_track_info'):
                _qji.current_track_info.update(track_info)

        jacket_path = _download_jacket(track.get('cover_url', ''))
        if jacket_path and not _feh_hidden:
            _feh_show(track_info=track_info, jacket_path=jacket_path)

        if hasattr(_qji, 'current_image_path'):
            _qji.current_image_path = jacket_path

        start_fn = getattr(_qji, 'start_info_display', None)
        if start_fn and not getattr(_qji, 'info_display_active', False):
            start_fn()

        state['current_track_info']  = track_info
        state['current_jacket_path'] = jacket_path

    except Exception:
        pass


def _clear_now_playing():
    global _feh_proc
    if _feh_proc and _feh_proc.poll() is None:
        try: _feh_proc.terminate(); _feh_proc.wait(timeout=2)
        except Exception: pass
        _feh_proc = None
    try:
        import __main__ as _qji
        stop_fn = getattr(_qji, 'stop_info_display', None)
        if stop_fn and getattr(_qji, 'info_display_active', False):
            stop_fn()
        lock = getattr(_qji, 'info_display_lock', None)
        ctx  = lock if lock else __import__('contextlib').nullcontext()
        with ctx:
            info = getattr(_qji, 'current_track_info', {})
            for k in ('title', 'artist', 'album', 'genre', 'tempo', 'mode',
                      'composer', 'conductor', 'performer'):
                info[k] = ''
            if hasattr(_qji, 'current_track_info'):
                _qji.current_track_info.update(info)
            if hasattr(_qji, 'current_image_path'):
                _qji.current_image_path = None
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# お気に入り
# ═══════════════════════════════════════════════════════════════════════════

def _sanitize_favorite(f) -> Optional[dict]:
    """Validate one favourite read from the saved file (dropped if its URL is not allowed)."""
    t = _sanitize_track(f)
    if not t:
        return None
    t['no']             = _as_int(f.get('no', 0), 0, 0, 10**9)
    t['saved_at']       = _clean(f.get('saved_at', ''), 32)
    t['audio_settings'] = f.get('audio_settings') if isinstance(f.get('audio_settings'), dict) else {}
    return t


def _load_favorites() -> List[dict]:
    try:
        raw = json.loads(FAVS_PATH.read_text()) if FAVS_PATH.exists() else []
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [x for x in (_sanitize_favorite(f) for f in raw) if x]

def _save_favorites(favs: List[dict]):
    _write_private(FAVS_PATH, json.dumps(favs, ensure_ascii=False, indent=2))


# ─── 保存済みプレイリスト ──────────────────────────────────────────────────

def _sanitize_playlist(p) -> Optional[dict]:
    """Validate one playlist read from the saved file (tracks with disallowed URLs are dropped)."""
    if not isinstance(p, dict):
        return None
    raw_tracks = p.get('tracks') if isinstance(p.get('tracks'), list) else []
    tracks = [x for x in (_sanitize_track(t) for t in raw_tracks[:2000]) if x]
    name = _clean(p.get('name', ''), 80)
    if not name:
        return None
    return {
        'no':             _as_int(p.get('no', 0), 0, 0, 10**9),
        'name':           name,
        'tracks':         tracks,
        'count':          len(tracks),
        'saved_at':       _clean(p.get('saved_at', ''), 32),
        'audio_settings': p.get('audio_settings') if isinstance(p.get('audio_settings'), dict) else {},
    }


def _load_playlists() -> List[dict]:
    try:
        raw = json.loads(PLAYLISTS_PATH.read_text()) if PLAYLISTS_PATH.exists() else []
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [x for x in (_sanitize_playlist(p) for p in raw) if x]

def _save_playlists(pls: List[dict]):
    _write_private(PLAYLISTS_PATH, json.dumps(pls, ensure_ascii=False, indent=2))

def _save_current_queue_as_playlist(tracks: List[dict], name: str,
                                     state: Optional[dict] = None) -> bool:
    """現在のキューをプレイリストとして保存する。name は空文字不可。
    state が渡された場合は音響設定（filter_preset / gain_preset / volume /
    air_layer / musikverein_room_effects）も一緒に保存する。
    """
    name = _clean(name, 80)
    tracks = [x for x in (_sanitize_track(t) for t in (tracks or [])[:2000]) if x]
    if not tracks or not name:
        return False
    pls = _load_playlists()
    nos = [p.get('no', 0) for p in pls if isinstance(p.get('no'), int)]
    next_no = (max(nos) + 1) if nos else 1
    # 同名が既存なら上書き
    existing_idx = next((i for i, p in enumerate(pls) if p.get('name') == name.strip()), None)
    if existing_idx is not None:
        next_no = pls[existing_idx].get('no', next_no)

    # 音響設定スナップショット
    audio_settings: dict = {}
    if state:
        audio_settings = {
            'filter_preset':            state.get('filter_preset', 'musikverein'),
            'gain_preset':              state.get('gain_preset', 'classical'),
            'volume':                   state.get('volume', 12),
            'air_layer':                state.get('air_layer', True),
            'musikverein_room_effects': state.get('musikverein_room_effects', True),
        }

    entry = {
        'no':             next_no,
        'name':           name.strip(),
        'tracks':         tracks,
        'count':          len(tracks),
        'saved_at':       datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'audio_settings': audio_settings,
    }
    if existing_idx is not None:
        pls[existing_idx] = entry
        print(f'  🔄 Playlist "{name.strip()}" overwritten ({len(tracks)} tracks)')
    else:
        pls.append(entry)
        print(f'  ✅ Playlist "{name.strip()}" saved ({len(tracks)} tracks)')

    # 音響設定の確認表示
    if audio_settings:
        fp_lbl  = FILTER_PRESET_LABELS.get(audio_settings['filter_preset'], audio_settings['filter_preset'])
        gp_db   = GAIN_PRESETS_DB.get(audio_settings['gain_preset'], 0.0)
        apl_str = '🌿 ON' if audio_settings['air_layer'] else 'OFF'
        print(f'       Audio: {fp_lbl}  Gain:{gp_db:+.1f}dB  APL:{apl_str}  Vol:{audio_settings["volume"]}dB')

    _save_playlists(pls)
    return True


def _playlists_flow(build_filter_func, state: dict):
    """保存済みプレイリスト再生フロー"""
    pls = _load_playlists()
    if not pls:
        print('  ⚠ No saved playlists')
        return
    print(f'\n  📁 Saved playlists ({len(pls)})')
    for p in pls:
        # 音響設定サマリーを表示
        audio = p.get('audio_settings', {})
        if audio:
            fp_lbl  = FILTER_PRESET_LABELS.get(audio.get('filter_preset', ''), '')
            gp_db   = GAIN_PRESETS_DB.get(audio.get('gain_preset', ''), 0.0)
            apl_str = '🌿' if audio.get('air_layer', True) else '  '
            vol     = audio.get('volume', 12)
            audio_str = f'  [{fp_lbl}  {gp_db:+.1f}dB  APL:{apl_str}  Vol:{vol}dB]'
        else:
            audio_str = ''
        print(f'    {p["no"]:03d}. {p["name"]}  ({p["count"]} tracks)  {p.get("saved_at","")[:10]}{audio_str}')
    print('  d  : Delete')
    choice = _sc_readline('  番号を入力 (d+番号で削除 / Enter でキャンセル): ')
    if not choice:
        return
    # 削除: "d3" / "d 3"
    del_match = re.match(r'^d\s*(\d+)$', choice.strip().lower())
    if del_match:
        no = int(del_match.group(1))
        target = next((p for p in pls if p.get('no') == no), None)
        if not target:
            print('  ⚠ Playlist not found')
            return
        pls = [p for p in pls if p.get('no') != no]
        _save_playlists(pls)
        print(f'  🗑 "{target["name"]}" deleted')
        return
    if not choice.isdigit():
        return
    no = int(choice)
    pl = next((p for p in pls if p.get('no') == no), None)
    if not pl:
        print('  ⚠ Playlist not found')
        return
    tracks = pl.get('tracks', [])
    if not tracks:
        print('  ⚠ No tracks in playlist')
        return

    # ── 保存済み音響設定を state へ適用 ──
    audio = pl.get('audio_settings', {})
    if audio:
        _apply_audio_settings(state, audio)
        fp_lbl  = FILTER_PRESET_LABELS.get(state['filter_preset'], state['filter_preset'])
        gp_db   = GAIN_PRESETS_DB.get(state['gain_preset'], 0.0)
        apl_str = '🌿 ON' if state['air_layer'] else 'OFF'
        print(f'  🎛 Audio settings restored: {fp_lbl}  Gain:{gp_db:+.1f}dB  APL:{apl_str}  Vol:{state["volume"]}dB')

    print(f'\n  📁 "{pl["name"]}" — playing {len(tracks)} tracks')
    _play_list(tracks, 0, build_filter_func, state)


def _save_to_favorites(state: dict):
    track = state.get('current_track')
    if not track:
        print('  ⚠ No track info available to save')
        return

    favs    = _load_favorites()
    url_key = track.get('url', '')
    existing_idx = next((i for i, f in enumerate(favs)
                         if f.get('url') == url_key), None)

    nos = [f.get('no', 0) for f in favs if isinstance(f.get('no'), int)]
    next_no = (max(nos) + 1) if nos else 1
    if existing_idx is not None:
        next_no = favs[existing_idx].get('no', next_no)

    audio = {
        'gain_preset':   state.get('gain_preset', 'classical'),
        'filter_preset': state.get('filter_preset', 'musikverein'),
        'air_layer':     state.get('air_layer', True),
        'musikverein':   state.get('musikverein', True),
        'volume':        state.get('volume', 12),
        'echo_mode':     state.get('echo_mode', 'classical'),
        'tinnitus':      state.get('tinnitus', False),
    }
    entry = {
        'no':           next_no,
        'url':          url_key,
        'title':        track.get('title', ''),
        'artist':       track.get('artist', ''),
        'cover_url':    track.get('cover_url', ''),
        'duration':     track.get('duration', 0),
        'genre':        track.get('genre', ''),
        'saved_at':     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'audio_settings': audio,
    }

    if existing_idx is not None:
        favs[existing_idx] = entry
        action = '🔄 上書き'
    else:
        favs.append(entry)
        action = '✅ New save'

    _save_favorites(favs)
    preset_lbl = FILTER_PRESET_LABELS.get(audio['filter_preset'], '')
    gain_db    = GAIN_PRESETS_DB.get(audio['gain_preset'], 0.0)
    apl        = '🌿 ON' if audio['air_layer'] else 'OFF'
    print(f'  {action} [{next_no:03d}] "{entry["title"]}" saved')
    print(f'       Audio: {preset_lbl}  Gain:{gain_db:+.1f}dB  APL:{apl}  Vol:{audio["volume"]}dB')


# ═══════════════════════════════════════════════════════════════════════════
# ジャンル自動検出
# ═══════════════════════════════════════════════════════════════════════════

def _auto_detect_preset(track: dict) -> Optional[str]:
    genre  = (track.get('genre') or '').lower()
    title  = (track.get('title') or '').lower()
    artist = (track.get('artist') or '').lower()
    text   = f'{genre} {title} {artist}'

    KEYWORDS: Dict[str, List[str]] = {
        'jazz': ['jazz', 'blues', 'swing', 'bebop', 'fusion', 'improvis',
                 'ジャズ', 'ブルース'],
        'piano': ['piano solo', 'solo piano', 'piano recital',
                  'nocturne', 'etude', 'prelude', 'ピアノ'],
        'chamber': ['quartet', 'trio', 'chamber', 'string', '室内楽'],
        'vocal': ['opera', 'lieder', 'vocal', 'choir', 'soprano',
                  'オペラ', '声楽'],
        'musikverein': ['symphony', 'orchestra', 'concerto', 'philharmonic',
                        '交響曲', 'オーケストラ'],
    }
    scores = {k: 0 for k in KEYWORDS}
    for preset, kws in KEYWORDS.items():
        for kw in kws:
            if kw in text:
                scores[preset] += 1

    GENRE_MAP = {
        'jazz': 'jazz', 'blues': 'jazz',
        'classical': 'musikverein', 'orchestral': 'musikverein',
        'chamber': 'chamber', 'piano': 'piano',
        'opera': 'vocal', 'vocal': 'vocal',
    }
    for slug, preset in GENRE_MAP.items():
        if slug in genre:
            scores[preset] += 2

    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else None


def _apply_auto_preset(track: Optional[dict], state: dict) -> bool:
    if state.get('manual_preset_locked', False) or not track:
        return False
    detected = _auto_detect_preset(track)
    if not detected:
        return False
    prev = state.get('filter_preset', 'musikverein')
    state['filter_preset'] = detected
    label = FILTER_PRESET_LABELS.get(detected, detected)
    if detected != prev:
        print(f'  🤖 Auto-detected genre → applying {label}')
    return detected != prev


# ═══════════════════════════════════════════════════════════════════════════
# 再生エンジン
# ═══════════════════════════════════════════════════════════════════════════

_procs: dict = {}

# ★ Webリモートコントロール用フラグ（qji.py の NowPlayingHandler から操作）
_remote_next_flag: bool = False
_remote_stop_flag: bool = False


def _kill_playback(p_ff=None, p_ap=None):
    for proc in [p_ff or _procs.get('ffmpeg'),
                 p_ap or _procs.get('aplay')]:
        if proc and proc.poll() is None:
            try:
                proc.terminate(); proc.wait(timeout=2)
            except Exception:
                try: proc.kill()
                except Exception: pass


def _play_one(track: dict, build_filter_func, state: dict,
              start_at: float = 0.0,
              current_queue: Optional[List[dict]] = None) -> str:
    """
    1トラックを再生する。
    current_queue が渡された場合は [P] キーでプレイリスト保存が可能。
    戻り値: 'next' | 'prev' | 'quit' | 'restart'
    """
    url = _safe_media_url(track.get('url', ''))
    if not url:
        print('  ⚠ URL is empty or unsupported — skipping.')
        return 'next'

    print(f'\n  ⏳ Fetching stream URL...')
    cfg = _load_config()
    stream_url = _resolve_stream_url(url, cookies_file=cfg.get('cookies_file'))
    if not stream_url:
        print(f'  ⚠ Could not get stream URL for "{_clean(track.get("title") or url[:60], 80)}" — skipping.')
        return 'next'

    # フィルターチェーン構築（Qobuzと同じ引数順序・戻り値形式）
    import __main__ as _qji
    gain_db  = GAIN_PRESETS_DB.get(state.get('gain_preset', 'classical'), 0.0)
    _prev_fp = getattr(_qji, 'current_filter_preset', 'musikverein')
    _qji.current_filter_preset = state.get('filter_preset', 'musikverein')
    try:
        filter_args = build_filter_func(
            gain_db,
            state.get('tinnitus', False),
            state.get('musikverein', True),
            state.get('loudness', ''),
            state.get('eq_part', ''),
            state.get('volume', 12),
            state.get('air_layer', True),
            echo_mode=state.get('echo_mode', 'classical'),
        )
    finally:
        _qji.current_filter_preset = _prev_fp

    # ★★★ Log output for the Peak Monitor GUI: also enabled for SoundCloud playback ★★★
    # (Same astats+ametadata pass-through as qji_ytmusic.py / qji.py itself; it does not affect the audio.)
    # The log location is /tmp/qji_declip_<PID>.log, which the Peak Monitor reads from a fixed path (do not change).
    # Playback is sequential, so removing and recreating it per track with os.remove() is safe.
    try:
        _sc_declip_log_path = f'/tmp/qji_declip_{os.getpid()}.log'
        if os.path.exists(_sc_declip_log_path):
            os.remove(_sc_declip_log_path)
        filter_args = _qji._wrap_filter_args_with_declip_monitor(filter_args, _sc_declip_log_path)
    except Exception:
        pass

    state['fmt_name'] = 'SoundCloud MP3'

    # ffmpeg コマンド（Qobuzと同じ構成）
    # Direct output: keep SoundCloud MP3's native 44100Hz.
    # DSP mode: CamillaDSP / aplay always assume 48000Hz, so ffmpeg converts to 48000Hz before handing the audio over
    #   (44100Hz data played as 48000Hz runs about 1.088x too fast — the pitch rises).
    #   Same "48000 when DSP is active" approach as qji.py's other playback paths.
    _dsp_on = bool(getattr(_qji, 'dsp_mode_active', False))
    SC_SAMPLE_RATE = 48000 if _dsp_on else 44100
    if _dsp_on:
        print('  🔊 DSP mode: output 48000Hz')
    cmd_ff = (
        ['ffmpeg', '-loglevel', 'warning',
         '-fflags', 'nobuffer',
         '-reconnect',                  '1',
         '-reconnect_streamed',         '1',
         '-reconnect_on_network_error', '1',
         '-reconnect_delay_max',        '5',
         # Whatever the URL contains, keep ffmpeg from reading local things such as file:
         '-protocol_whitelist',         'https,tls,tcp,crypto',
         '-i', stream_url,
         '-vn', '-ar', str(SC_SAMPLE_RATE), '-acodec', 'pcm_s32le']
        + filter_args
        + ['-f', 'wav', '-']
    )

    dev = state.get('output_device', 'default')
    if dev == 'bluealsa':
        cmd_ap = ['aplay', '-D', 'bluealsa',
                  '--buffer-size=262144', '--period-size=32768']
    else:
        cmd_ap = ['aplay', '-D', dev, '-r', str(SC_SAMPLE_RATE),
                  '--buffer-size=262144', '--period-size=32768']

    state['current_track'] = track
    _update_now_playing(track, state, elapsed=int(start_at))

    # ★ ffmpegのエラーをファイルに保存（rawモード中でも消えないように）
    #   Created in a folder of your own (not the shared /tmp), mode 600, without following symlinks
    _errlog = _private_dir() / 'qji_sc_ffmpeg.log'
    try:
        _ferr = os.fdopen(os.open(str(_errlog),
                                  os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0),
                                  0o600), 'w')
        # Qobuzと同じ接続順序: p_ff.stdout=PIPE → p_ap.stdin=p_ff.stdout
        p_ff = subprocess.Popen(cmd_ff, stdout=subprocess.PIPE, stderr=_ferr)
        p_ap = subprocess.Popen(cmd_ap, stdin=p_ff.stdout,      stderr=subprocess.DEVNULL)
        _ferr.close()
        p_ff.stdout.close()   # p_apに渡したのでPython側を閉じる
        _procs['ffmpeg'] = p_ff
        _procs['aplay']  = p_ap
    except Exception as e:
        print(f'  ⚠ Playback launch error: {e}')
        return 'next'

    result = _key_loop(p_ff, p_ap, track, state,
                       play_start=time.time(), start_at=start_at,
                       build_filter_func=build_filter_func,
                       current_queue=current_queue)

    # ★ key_loop終了後にffmpegエラーを必ず表示（\rで消えない）
    try:
        err_text = _errlog.read_text().strip()
        if err_text:
            print(f'\n  🔴 ffmpeg log:')
            for line in err_text.split('\n')[:20]:
                print(f'     {_clean(line, 200)}')
            print()
    except Exception:
        pass

    return result


def _dur_str(sec: int) -> str:
    return f'{sec // 60}:{sec % 60:02d}'


def _key_loop(p_ff, p_ap, track: dict, state: dict,
              play_start: float, start_at: float,
              build_filter_func,
              current_queue: Optional[List[dict]] = None) -> str:
    """
    再生中のキー入力ループ。ffmpeg / aplay が終了するか
    ナビゲーションキーが押されると終了する。
    """
    duration = track.get('duration', 0)
    title    = track.get('title', '')
    artist   = track.get('artist', '')
    genre    = track.get('genre', '')

    preset_lbl = FILTER_PRESET_LABELS.get(state.get('filter_preset', ''), '')
    gain_db    = GAIN_PRESETS_DB.get(state.get('gain_preset', 'classical'), 0.0)
    vol        = state.get('volume', 12)
    apl        = '🌿' if state.get('air_layer', True) else '  '

    print(f'\n  ▶ {artist} — {title}')
    if genre:
        print(f'     Genre: {genre}')
    print(f'     {preset_lbl}  Gain:{gain_db:+.1f}dB  APL:{apl}  Vol:{vol}dB')
    print(f'     [n]Next  [b]Prev  [q]Back  [g]Gain  [w]APL  [c]Preset'
          f'  [+/-]Vol  [s]Save  [P]Save queue  [i]Image  [ESC]Hide')

    # 時間表示スレッド
    _stop_timer = threading.Event()
    def _timer():
        while not _stop_timer.is_set() and p_ff.poll() is None:
            elapsed = int(start_at + (time.time() - play_start))
            dur_str  = f'/{_dur_str(duration)}' if duration else ''
            sys.stdout.write(f'\r  ⏱ {_dur_str(elapsed)}{dur_str}    ')
            sys.stdout.flush()
            time.sleep(1)
    timer_th = threading.Thread(target=_timer, daemon=True)
    timer_th.start()

    result = 'next'
    try:
        tty_fd = open('/dev/tty', 'rb')
        old_settings = termios.tcgetattr(tty_fd)
        tty.setraw(tty_fd.fileno())
        try:
            while p_ff.poll() is None:
                # ★ Webリモートコントロールフラグ確認
                global _remote_next_flag, _remote_stop_flag
                if _remote_next_flag:
                    _remote_next_flag = False
                    _kill_playback(p_ff, p_ap)
                    result = 'next'; break
                if _remote_stop_flag:
                    _remote_stop_flag = False
                    _kill_playback(p_ff, p_ap)
                    result = 'quit'; break
                r, _, _ = select.select([tty_fd], [], [], 0.1)
                if not r:
                    continue
                ch = tty_fd.read(1)
                if not ch:
                    break

                if ch == b'n':
                    _kill_playback(p_ff, p_ap)
                    result = 'next'; break
                elif ch == b'b':
                    _kill_playback(p_ff, p_ap)
                    result = 'prev'; break
                elif ch in (b'q', b'Q'):
                    _kill_playback(p_ff, p_ap)
                    result = 'quit'; break
                elif ch == b'\x1b':  # ESC
                    _feh_hide()
                    sys.stdout.write('\r  🖼 Cover art hidden                 \n')
                    sys.stdout.flush()
                elif ch == b'i':
                    _feh_show(
                        track_info=state.get('current_track_info'),
                        jacket_path=state.get('current_jacket_path'))
                elif ch == b'g':
                    # ゲインプリセット切替
                    cur_idx = GAIN_ORDER.index(state['gain_preset']) \
                              if state['gain_preset'] in GAIN_ORDER else 0
                    state['gain_preset'] = GAIN_ORDER[(cur_idx + 1) % len(GAIN_ORDER)]
                    new_db = GAIN_PRESETS_DB[state['gain_preset']]
                    sys.stdout.write(f'\r  🎚 ゲイン: {state["gain_preset"]}  ({new_db:+.1f}dB)  ※次曲から適用  \n')
                    sys.stdout.flush()
                elif ch == b'w':
                    state['air_layer'] = not state.get('air_layer', True)
                    apl_str = '🌿 ON' if state['air_layer'] else 'OFF'
                    sys.stdout.write(f'\r  🌿 Air Particle Layer: {apl_str}  ※次曲から適用  \n')
                    sys.stdout.flush()
                elif ch in (b'+', b'='):
                    state['volume'] = min(state.get('volume', 12) + 1, 30)
                    sys.stdout.write(f'\r  🔊 Vol: {state["volume"]}dB  (from next track)  \n')
                    sys.stdout.flush()
                elif ch == b'-':
                    state['volume'] = max(state.get('volume', 12) - 1, -20)
                    sys.stdout.write(f'\r  🔉 Vol: {state["volume"]}dB  (from next track)  \n')
                    sys.stdout.flush()
                elif ch == b's':
                    _kill_playback(p_ff, p_ap)
                    _stop_timer.set()
                    _restore_terminal()
                    _save_to_favorites(state)
                    result = 'restart'; break
                elif ch == b'c':
                    # プリセット選択メニュー
                    _kill_playback(p_ff, p_ap)
                    _stop_timer.set()
                    _restore_terminal()
                    print('\n  Select preset:')
                    for i, k in enumerate(FILTER_PRESETS, 1):
                        marker = ' ◀' if k == state.get('filter_preset') else ''
                        print(f'    {i}: {FILTER_PRESET_LABELS[k]}{marker}')
                    choice = _sc_readline('  番号を入力 (Enter でキャンセル): ')
                    if choice.isdigit():
                        idx = int(choice) - 1
                        if 0 <= idx < len(FILTER_PRESETS):
                            state['filter_preset']       = FILTER_PRESETS[idx]
                            state['manual_preset_locked'] = True
                            print(f'  ✅ {FILTER_PRESET_LABELS[FILTER_PRESETS[idx]]} selected')
                    result = 'restart'; break
                elif ch == b'P':
                    # [P] キュー全体をプレイリストとして保存
                    _kill_playback(p_ff, p_ap)
                    _stop_timer.set()
                    _restore_terminal()
                    if current_queue:
                        pl_name = _sc_readline('  📁 プレイリスト名を入力 (Enter でキャンセル): ')
                        if pl_name.strip():
                            _save_current_queue_as_playlist(current_queue, pl_name, state)
                        else:
                            print('  Cancelled')
                    else:
                        print('  ⚠ No queue to save (single-track playback)')
                    result = 'restart'; break
        finally:
            try:
                termios.tcsetattr(tty_fd, termios.TCSANOW, old_settings)
                tty_fd.close()
            except Exception:
                pass
    except Exception as e:
        print(f'\n  ⚠ Key input error: {e}')

    _stop_timer.set()

    # ffmpeg / aplay が終了していなければ待つ
    try:
        p_ff.wait(timeout=5)
    except Exception:
        _kill_playback(p_ff, p_ap)
    try:
        p_ap.wait(timeout=3)
    except Exception:
        _kill_playback(p_ff, p_ap)

    sys.stdout.write('\r                                              \r')
    sys.stdout.flush()
    return result


def _play_list(tracks: List[dict], start_idx: int,
               build_filter_func, state: dict):
    """トラックリストを再生する。n/b キーでナビゲーション。"""
    i = start_idx
    while 0 <= i < len(tracks):
        track = tracks[i]
        _apply_auto_preset(track, state)
        result = _play_one(track, build_filter_func, state,
                           current_queue=tracks)  # [P] キュー保存用
        if result == 'next':
            i += 1
        elif result == 'prev':
            i = max(0, i - 1)
        elif result == 'restart':
            pass  # 同じ曲を再再生
        elif result == 'quit':
            return


# ═══════════════════════════════════════════════════════════════════════════
# メニューフロー
# ═══════════════════════════════════════════════════════════════════════════

def _url_flow(build_filter_func, state: dict):
    """URL 入力 → 再生フロー（単曲 / プレイリスト URL 対応）"""
    url = _sc_readline('\n  🔗 SoundCloud URL を入力: ')
    if not url:
        return
    url = _safe_media_url(url)
    if not url:
        print('  ⚠ Please enter a SoundCloud (soundcloud.com) URL')
        return

    _path = _urlparse(url).path
    is_playlist = ('/sets/' in _path
                   or '/likes' in _path
                   or '/reposts' in _path
                   or '/tracks' in _path)
    if is_playlist:
        print(f'  📋 Fetching playlist...')
        tracks = get_playlist_tracks(url)
        if not tracks:
            print('  ⚠ Could not retrieve tracks')
            return
        print(f'  📋 Retrieved {len(tracks)} tracks')
        _play_list(tracks, 0, build_filter_func, state)
    else:
        # 単曲: まずメタデータを取得
        info = _get_track_info_ytdlp(url)
        if info:
            track = info
        else:
            # 最低限の情報で再生を試みる
            track = {'url': url, 'title': _clean(_urlparse(url).path.rstrip('/').split('/')[-1], 80),
                     'artist': '', 'cover_url': '', 'duration': 0}
        _apply_auto_preset(track, state)
        while True:
            result = _play_one(track, build_filter_func, state)
            if result != 'restart':
                break


def _search_flow(build_filter_func, state: dict, client_id: str):
    """検索フロー（ターミナル上でシンプルに選択）"""
    query = _sc_readline('\n  🔍 検索ワードを入力: ')
    if not query:
        return

    print(f'  Searching...')
    tracks = search_tracks(query, limit=15, client_id=client_id)
    if not tracks:
        print('  ⚠ No search results')
        return

    print(f'\n  🎵 Search results for "{_clean(query, 80)}」')
    for i, t in enumerate(tracks, 1):
        dur = f' [{_dur_str(t["duration"])}]' if t.get('duration') else ''
        genre = f' [{t["genre"]}]' if t.get('genre') else ''
        print(f'    {i:2d}. {t["artist"]} — {t["title"]}{dur}{genre}')

    choice = _sc_readline('  番号を入力 (Enter でキャンセル): ')
    if not choice.isdigit():
        return
    idx = int(choice) - 1
    if not (0 <= idx < len(tracks)):
        return

    track = tracks[idx]
    _apply_auto_preset(track, state)
    while True:
        result = _play_one(track, build_filter_func, state)
        if result != 'restart':
            break


def _favorites_flow(build_filter_func, state: dict):
    """お気に入り再生フロー"""
    favs = _load_favorites()
    if not favs:
        print('  ⚠ No favourites yet')
        return
    print(f'\n  ⭐ Favourites ({len(favs)})')
    for f in favs:
        dur = f' [{_dur_str(f["duration"])}]' if f.get('duration') else ''
        audio      = f.get('audio_settings') or {}
        preset_lbl = FILTER_PRESET_LABELS.get(audio.get('filter_preset', ''), '')
        gain_db    = GAIN_PRESETS_DB.get(audio.get('gain_preset', ''), None)
        vol        = audio.get('volume')
        gain_str   = f'  🔊{gain_db:+.1f}dB' if gain_db is not None else ''
        vol_str    = f'  Vol:{vol}dB'         if vol is not None    else ''
        print(f'    {f["no"]:03d}. {f["artist"]} — {f["title"]}{dur}  {preset_lbl}{gain_str}{vol_str}')

    choice = _sc_readline('  番号を入力 (Enter でキャンセル): ')
    if not choice.isdigit():
        return
    no = int(choice)
    fav = next((f for f in favs if f.get('no') == no), None)
    if not fav:
        print('  ⚠ Matching favourite not found')
        return

    track = {
        'url':       fav.get('url', ''),
        'title':     fav.get('title', ''),
        'artist':    fav.get('artist', ''),
        'cover_url': fav.get('cover_url', ''),
        'duration':  fav.get('duration', 0),
        'genre':     fav.get('genre', ''),
    }
    audio = fav.get('audio_settings', {})
    if audio:
        _apply_audio_settings(state, audio)    # apply only allowed items and ranges
        state['manual_preset_locked'] = True   # 保存時の設定を優先
    while True:
        result = _play_one(track, build_filter_func, state)
        if result != 'restart':
            break


def _handle_browser_request(build_filter_func, state: dict, req: dict):
    """Handle a request from the browser UI. Its contents are not trusted; they are validated before use."""
    _restore_terminal()   # 再生後 rawモードが残留している場合に備えて
    if not isinstance(req, dict):
        print('  ⚠ Invalid request')
        return
    req_type = req.get('type', 'track')

    if req_type == 'playlist':
        # プレイリスト再生
        raw = req.get('tracks', [])
        tracks = [x for x in (_sanitize_track(t) for t in (raw if isinstance(raw, list) else [])[:500]) if x]
        if not tracks:
            print('  ⚠ Playlist is empty (or the URLs are unsupported)')
            return
        start = _as_int(req.get('start_from', 1), 1, 1, len(tracks)) - 1
        print(f'\n  📋 Playlist playback: {len(tracks)} tracks, starting from #{start + 1}')
        _play_list(tracks, start, build_filter_func, state)
    else:
        # 単曲再生
        track = _sanitize_track(req)
        if not track:
            print('  ⚠ No URL (or the URL is unsupported)')
            return
        _apply_audio_settings(state, req.get('audio_settings', {}))
        _apply_auto_preset(track, state)
        print(f'\n  📱 Browser playback: {track["artist"]} — {track["title"]}')
        # c/s キーで restart が返った場合は同じ曲を新設定で再再生
        while True:
            result = _play_one(track, build_filter_func, state)
            if result != 'restart':
                break


# ═══════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ═══════════════════════════════════════════════════════════════════════════

def run(build_filter_func, gain_preset: str, tinnitus: bool,
        musikverein: bool, loudness_filter: str, eq_part: str,
        volume: int, air_layer: bool, echo_mode: str,
        output_device: str):
    """qji.py の S キーハンドラから呼び出す唯一の関数"""
    # ターミナルが正常状態のうちにベースラインを保存（フリーズ防止の要）
    _capture_baseline_term()

    if not _ensure_ytdlp():
        input('  Press Enter to go back...'); return
    if not _ensure_requests():
        print('  ❌ requests is required: pip install requests')
        input('  Press Enter to go back...'); return

    import __main__ as _qji
    state = {
        'gain_preset':         gain_preset,
        'filter_preset':       getattr(_qji, 'current_filter_preset', 'musikverein'),
        'tinnitus':            tinnitus,
        'musikverein':         musikverein,
        'loudness':            loudness_filter,
        'eq_part':             eq_part,
        'volume':              volume,
        'air_layer':           air_layer,
        'echo_mode':           echo_mode,
        'output_device':       output_device,
        'fmt_name':            '',
        'current_track':       None,
        'current_track_info':  None,
        'current_jacket_path': None,
        'manual_preset_locked': False,
    }

    _save_terminal()

    # client_id を事前取得（バックグラウンドで）
    client_id_box = [None]
    def _fetch_cid():
        client_id_box[0] = _get_client_id()
    threading.Thread(target=_fetch_cid, daemon=True).start()

    # ブラウザUI サーバー起動
    _browser_available = False
    try:
        import qji_soundcloud_browser as _browser
        _browser.start_browser_server(open_browser=False)
        _browser_available = True
        _open_incognito_browser(BROWSER_URL)
    except ImportError:
        pass

    try:
        while True:
            print('\n' + '═' * 56)
            print('🎵  Qji × SoundCloud  —  Streaming')
            print('═' * 56)
            preset_lbl = FILTER_PRESET_LABELS.get(state['filter_preset'], '')
            gain_db    = GAIN_PRESETS_DB.get(state['gain_preset'], 0.0)
            apl        = '🌿 ON' if state['air_layer'] else 'OFF'
            favs_count = len(_load_favorites())
            pl_count   = len(_load_playlists())
            print(f'  Output device : {output_device}')
            print(f'  Audio preset  : {preset_lbl}  Gain:{gain_db:+.1f}dB'
                  f'  APL:{apl}  Vol:{state["volume"]}dB')
            print(f'  Favourites    : {favs_count}  / Saved playlists: {pl_count}')
            if _browser_available:
                print(f'  Browser UI    : {BROWSER_URL}')
            print()
            print('  u : 🔗 Paste URL to play')
            print('  k : 🔍 Keyword search (terminal)')
            print('  f : ⭐ Play from favourites')
            print('  p : 📁 Play from saved playlists')
            print('  v : 🌐 Open browser UI')
            print('  r : 🔄 Refresh client_id')
            print('  q : ← Return to Qji main menu')
            print('─' * 56)

            # ブラウザリクエストのポーリング
            ch = ''
            _browser_req_result: dict = {}
            _browser_req_event = threading.Event()

            def _poll_browser():
                while not _browser_req_event.is_set():
                    if _browser_available:
                        try:
                            req = _browser.check_browser_request()
                            if req:
                                _browser_req_result['req'] = req
                                _browser_req_event.set()
                                return
                        except Exception:
                            pass
                    time.sleep(0.3)

            poll_th = threading.Thread(target=_poll_browser, daemon=True)
            poll_th.start()

            _pending_browser_req = None
            try:
                with open('/dev/tty', 'r') as _tty:
                    try:
                        subprocess.run(['stty', 'sane'], stdin=_tty,
                                       check=False, timeout=2, capture_output=True)
                    except Exception:
                        pass
                    print('\n  Select (u / k / f / v / r / q): ', end='', flush=True)
                    while True:
                        if _browser_req_event.is_set():
                            # ★ withブロック内では何もせずreqだけ受け取って抜ける
                            _pending_browser_req = _browser_req_result.get('req')
                            print()
                            break
                        rlist, _, _ = select.select([_tty], [], [], 0.5)
                        if rlist:
                            line = _tty.readline()
                            ch = line.strip().lower() if line else ''
                            _browser_req_event.set()
                            break
            except (KeyboardInterrupt, EOFError):
                _browser_req_event.set()
                break
            except Exception:
                _browser_req_event.set()
                try:
                    print('\n  Select (u / k / f / p / v / r / q): ', end='', flush=True)
                    ch = sys.stdin.readline().strip().lower()
                except Exception:
                    break

            # ★ withブロックを完全に抜けてから再生処理（rawモード競合を回避）
            if _pending_browser_req is not None:
                _handle_browser_request(build_filter_func, state, _pending_browser_req)
                _restore_terminal()
                # ブラウザを維持したままSoundCloudメニューへ戻る
                continue

            if ch == 'q':
                if _browser_available:
                    try: _browser.send_close_signal()
                    except Exception: pass
                _close_incognito_browser()
                if _browser_available:
                    try: _browser.stop_browser_server()
                    except Exception: pass
                break
            elif ch == 'u':
                _url_flow(build_filter_func, state)
                _restore_terminal()
                # ブラウザを維持したままSoundCloudメニューへ戻る
            elif ch == 'k':
                cid = client_id_box[0] or _get_client_id()
                _search_flow(build_filter_func, state, cid or '')
                _restore_terminal()
            elif ch == 'f':
                _favorites_flow(build_filter_func, state)
                _restore_terminal()
                # ブラウザを維持したままSoundCloudメニューへ戻る
            elif ch == 'p':
                _playlists_flow(build_filter_func, state)
                _restore_terminal()
            elif ch == 'v':
                if _browser_available:
                    _open_incognito_browser(BROWSER_URL)
                else:
                    print('  ⚠ qji_soundcloud_browser.py not found')
            elif ch == 'r':
                _restore_terminal()
                client_id_box[0] = _get_client_id(force_refresh=True)
            else:
                _restore_terminal()
                if ch:
                    print('  ⚠ Please enter u / k / f / p / v / r / q')

    finally:
        # ─── 確実な終了処理（Qobuzと同等）─────────────────────────────
        _clear_now_playing()
        _restore_terminal()
        # ECHO + ICANON を明示的に有効化（rawモード残留を確実に解除）
        try:
            fd = sys.stdin.fileno()
            attrs = termios.tcgetattr(fd)
            attrs[3] |= (termios.ECHO | termios.ICANON)
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
        except Exception:
            pass
        try:
            subprocess.run(['stty', 'sane'], timeout=2,
                           stdin=open('/dev/tty'), capture_output=True)
        except Exception:
            try: os.system('stty sane 2>/dev/null')
            except Exception: pass
        print('\n  🎵 Returning to Qji main menu...\n')
