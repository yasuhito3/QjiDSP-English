#!/usr/bin/env python3
"""
qji_ytmusic.py  —  Qji 統合 YouTube Music ストリーミングモジュール
qji.py から Y キーで呼び出される。

依存関係:
  必須: yt-dlp
  推奨: ytmusicapi  (pip install ytmusicapi --break-system-packages)
        → 未インストールの場合は yt-dlp 検索に自動フォールバック

再生中キー:
  [n]次  [b]前  [q]メニューへ
  [g]ゲイン切替  [w]APL ON/OFF  [c]プリセット選択
  [+]/[=]Vol +1dB  [-]Vol -1dB
  [s]Save to favourites  [i]Show cover  [ESC]Hide cover

認証（任意）:
  ytmusicapi のブラウザ認証を設定すると「いいね」した曲やライブラリに
  アクセス可能になる。
    python3 -c "from ytmusicapi import YTMusic; YTMusic.setup(filepath='~/.config/qji_ytmusic_auth.json')"
"""

import os, re, sys, json, select, time, threading, subprocess, tempfile
import termios, tty
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict

# ═══════════════════════════════════════════════════════════════════════════
# 定数
# ═══════════════════════════════════════════════════════════════════════════

CONFIG_PATH  = Path.home() / '.config' / 'qji_ytmusic.json'
FAVS_PATH    = Path.home() / '.config' / 'qji_ytmusic_favorites.json'
AUTH_PATH    = Path.home() / '.config' / 'qji_ytmusic_auth.json'
REQUEST_PATH = Path('/tmp/qji_ytmusic_request.json')
_JACKET_TMP  = Path(tempfile.gettempdir()) / 'qji_ytmusic_jacket.jpg'

PORT           = 8082
YT_SAMPLE_RATE = 48000   # YouTube 音声のネイティブレート

FILTER_PRESETS = ['musikverein', 'piano', 'chamber', 'vocal', 'jazz', 'calm', 'deep', 'spatial', 'radio', 'bypass']
FILTER_PRESET_LABELS = {
    'musikverein': '🎻 Musikverein',
    'piano':       '🎹 Piano',
    'chamber':     '🏠 Chamber',
    'vocal':       '🎙 Vocal',
    'jazz':        '🎷 Jazz',
    'calm':        '🌿 Calm (soothing)',
    'deep':        '🌊 Deep (abyssal)',
    'spatial':     '🌐 Spatial (3D spatial audio)',
    'radio':       '📻 Radio',
    'bypass':      '⚪ Bypass (unprocessed)',
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
    tmp = _tf.mkdtemp(prefix='qji_yt_')
    _incognito_tmp_dir = tmp
    chrome_flags = [f'--user-data-dir={tmp}', '--incognito', '--new-window',
                    '--no-first-run', '--no-default-browser-check',
                    '--disable-extensions']
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
_baseline_term = None


def _capture_baseline_term():
    """run() 先頭で一度だけ呼ぶ。rawモード操作前の正常なターミナル設定を保存。"""
    global _baseline_term
    if _baseline_term is not None:
        return
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
    fd = sys.stdin.fileno()
    try:
        with open('/dev/tty', 'r') as _tty:
            subprocess.run(['stty', 'sane'], stdin=_tty,
                           check=False, timeout=2)
    except Exception:
        try:
            os.system('stty sane 2>/dev/null')
        except Exception:
            pass
    ref = _baseline_term if _baseline_term is not None else _saved_term
    try:
        if ref is not None:
            termios.tcsetattr(fd, termios.TCSANOW, ref)
    except Exception:
        pass
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except Exception:
        pass
    sys.stdout.flush()


def _yt_readline(prompt: str = '') -> str:
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
# 設定管理
# ═══════════════════════════════════════════════════════════════════════════

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    except Exception:
        return {}

def _save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    CONFIG_PATH.chmod(0o600)


# ═══════════════════════════════════════════════════════════════════════════
# ytmusicapi / yt-dlp ラッパー
# ═══════════════════════════════════════════════════════════════════════════

def _ytmusicapi_available() -> bool:
    try:
        import ytmusicapi
        return True
    except ImportError:
        return False


def _get_ytmusic():
    """YTMusic インスタンスを返す。認証ファイルがあれば使用。"""
    try:
        from ytmusicapi import YTMusic
        if AUTH_PATH.exists():
            return YTMusic(str(AUTH_PATH))
        return YTMusic()
    except Exception:
        return None


def _best_thumbnail(thumbs: list) -> str:
    """サムネイルリストから最大サイズを選択し、可能なら高解像度化する。"""
    if not thumbs:
        return ''
    best = max(thumbs, key=lambda t: t.get('width', 0) * t.get('height', 0))
    url = best.get('url', '')
    if not url:
        return ''
    # Google usercontent サムネイルを 500px に拡大
    url = re.sub(r'=w\d+-h\d+[^&\s]*', '=w500-h500-l90-rj', url)
    url = re.sub(r'=s\d+', '=s500', url)
    return url


def _parse_duration(d) -> int:
    """'3:45' または秒数 (int/float) を秒数 int に変換する。"""
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


def _artists_str(artists) -> str:
    if not artists:
        return ''
    if isinstance(artists, list):
        return ', '.join(a.get('name', '') for a in artists if a.get('name'))
    return str(artists)


def _ytm_track_to_dict(r: dict) -> Optional[dict]:
    """ytmusicapi の検索結果 1 件を内部 track dict に変換する。"""
    vid = r.get('videoId', '')
    if not vid:
        return None
    artists = _artists_str(r.get('artists', []))
    album   = (r.get('album') or {}).get('name', '')
    thumbs  = r.get('thumbnails', [])
    cover   = _best_thumbnail(thumbs)
    dur_sec = r.get('duration_seconds') or _parse_duration(r.get('duration', 0))
    return {
        'url':      f'https://music.youtube.com/watch?v={vid}',
        'id':       vid,
        'title':    r.get('title', ''),
        'artist':   artists,
        'album':    album,
        'cover_url': cover,
        'duration': dur_sec,
        'genre':    '',
        'plays':    r.get('views', 0),
    }


def search_songs(query: str, limit: int = 100) -> List[dict]:
    """
    曲を検索する。ytmusicapi + yt-dlp 補完方式。
    ytmusicapi は内部で continuations を処理するが、YouTube Music 側の
    上限（通常 40〜50 件前後）を超えることはできない。
    不足分は yt-dlp ytsearch で補完し、重複を除いて limit 件まで返す。
    """
    seen: set = set()
    tracks: List[dict] = []

    # ① ytmusicapi（内部で continuation 自動処理）
    yt = _get_ytmusic()
    if yt:
        try:
            results = yt.search(query, filter='songs', limit=limit)
            for r in results:
                t = _ytm_track_to_dict(r)
                if t and t['id'] not in seen:
                    seen.add(t['id'])
                    tracks.append(t)
        except Exception:
            pass

    # ② 不足分を yt-dlp で補完
    remaining = limit - len(tracks)
    if remaining > 0:
        ytdlp_tracks = _search_ytdlp(query, remaining * 2)  # 重複考慮で多めに取得
        for t in ytdlp_tracks:
            if t['id'] not in seen:
                seen.add(t['id'])
                tracks.append(t)
                if len(tracks) >= limit:
                    break

    return tracks[:limit]


def search_videos(query: str, limit: int = 100) -> List[dict]:
    """ビデオ（MV等）を検索する。ytmusicapi + yt-dlp 補完方式。"""
    seen: set = set()
    tracks: List[dict] = []

    yt = _get_ytmusic()
    if yt:
        try:
            results = yt.search(query, filter='videos', limit=limit)
            for r in results:
                t = _ytm_track_to_dict(r)
                if t and t['id'] not in seen:
                    seen.add(t['id'])
                    tracks.append(t)
        except Exception:
            pass

    remaining = limit - len(tracks)
    if remaining > 0:
        ytdlp_tracks = _search_ytdlp(query, remaining * 2)
        for t in ytdlp_tracks:
            if t['id'] not in seen:
                seen.add(t['id'])
                tracks.append(t)
                if len(tracks) >= limit:
                    break

    return tracks[:limit]


def search_albums(query: str, limit: int = 16) -> List[dict]:
    """アルバムを検索する（ytmusicapi が必要）。"""
    yt = _get_ytmusic()
    if not yt:
        return []
    try:
        results = yt.search(query, filter='albums', limit=limit)
        albums = []
        for r in results:
            bid = r.get('browseId', '')
            if not bid:
                continue
            artists = _artists_str(r.get('artists', []))
            thumbs  = r.get('thumbnails', [])
            cover   = _best_thumbnail(thumbs)
            albums.append({
                'browse_id':   bid,
                'title':       r.get('title', ''),
                'artist':      artists,
                'year':        str(r.get('year', '')),
                'cover_url':   cover,
                'track_count': r.get('trackCount', 0),
                'is_album':    r.get('type', '') == 'Album',
            })
        return albums
    except Exception:
        return []


def get_album_tracks(browse_id: str) -> Optional[dict]:
    """アルバムのトラックリストを取得する（ytmusicapi）。"""
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
            if not vid:
                continue
            artists = _artists_str(t.get('artists') or album.get('artists', []))
            thumbs  = t.get('thumbnails') or album.get('thumbnails', [])
            cover   = _best_thumbnail(thumbs)
            dur_sec = t.get('duration_seconds') or _parse_duration(t.get('duration', 0))
            tracks.append({
                'url':       f'https://music.youtube.com/watch?v={vid}',
                'id':        vid,
                'title':     t.get('title', ''),
                'artist':    artists,
                'album':     album.get('title', ''),
                'cover_url': cover,
                'duration':  dur_sec,
                'genre':     '',
            })
        thumbs_main = album.get('thumbnails', [])
        return {
            'title':       album.get('title', ''),
            'artist':      _artists_str(album.get('artists', [])),
            'year':        str(album.get('year', '')),
            'cover_url':   _best_thumbnail(thumbs_main),
            'track_count': len(tracks),
            'tracks':      tracks,
        }
    except Exception as e:
        print(f'  ⚠ Album fetch error: {e}')
        return None


def get_playlist_tracks(playlist_url: str) -> List[dict]:
    """
    YouTube Music プレイリスト URL からトラックリストを取得する。
    yt-dlp --flat-playlist を使用。
    """
    cmd = ['yt-dlp', '--flat-playlist', '--dump-json', '--no-warnings',
           '--quiet', playlist_url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        tracks = []
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                vid = item.get('id', '')
                if not vid:
                    continue
                thumb = item.get('thumbnail', '') or item.get('thumbnails', [{}])
                if isinstance(thumb, list):
                    thumb = _best_thumbnail(thumb)
                tracks.append({
                    'url':       f'https://music.youtube.com/watch?v={vid}',
                    'id':        vid,
                    'title':     item.get('title', ''),
                    'artist':    item.get('uploader', item.get('channel', '')),
                    'album':     item.get('album', ''),
                    'cover_url': str(thumb),
                    'duration':  int(item.get('duration') or 0),
                    'genre':     '',
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return tracks
    except Exception as e:
        print(f'  ⚠ Playlist fetch error: {e}')
        return []


def _search_ytdlp(query: str, limit: int = 20) -> List[dict]:
    """yt-dlp ytsearch を使った検索（ytmusicapi なし時のフォールバック）。"""
    cmd = ['yt-dlp', f'ytsearch{limit}:{query}',
           '--dump-json', '--flat-playlist', '--no-warnings', '--quiet']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        tracks = []
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                vid = item.get('id', '')
                if not vid:
                    continue
                thumb = item.get('thumbnail', '')
                tracks.append({
                    'url':       f'https://music.youtube.com/watch?v={vid}',
                    'id':        vid,
                    'title':     item.get('title', ''),
                    'artist':    item.get('uploader', item.get('channel', '')),
                    'album':     '',
                    'cover_url': thumb,
                    'duration':  int(item.get('duration') or 0),
                    'genre':     '',
                    'plays':     item.get('view_count', 0),
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return tracks
    except subprocess.TimeoutExpired:
        print('  ⚠ yt-dlp search timed out')
        return []
    except Exception as e:
        print(f'  ⚠ yt-dlp search error: {e}')
        return []


def _resolve_stream_url(track_url: str) -> Optional[str]:
    """
    yt-dlp でストリームURLを取得する。
    bestaudio を優先、音声専用フォーマットを選択する。
    """
    # YouTube Music URL → yt-dlp が直接対応
    # 通常の YouTube URL でも動作する
    cmd = ['yt-dlp', '-g',
           '-f', 'bestaudio[ext=webm]/bestaudio[acodec=opus]/bestaudio[ext!=m4a]/bestaudio/best',
           '--no-playlist', '--no-warnings', track_url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if r.returncode == 0:
            url = r.stdout.strip().split('\n')[0].strip()
            if url:
                return url
        if r.stderr.strip():
            # 詳細エラーの最初の1行だけ表示
            err = r.stderr.strip().split('\n')[0]
            print(f'  ⚠ yt-dlp: {err[:120]}')
    except subprocess.TimeoutExpired:
        print('  ⚠ yt-dlp timed out (25 s)')
    except Exception as e:
        print(f'  ⚠ yt-dlp error: {e}')
    return None


def _get_track_info_ytdlp(track_url: str) -> Optional[dict]:
    """yt-dlp でトラックのメタデータを取得する。"""
    cmd = ['yt-dlp', '--dump-json', '--no-playlist', '--no-warnings',
           '--quiet', track_url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            lines = [l for l in r.stdout.strip().split('\n') if l.strip()]
            if lines:
                item = json.loads(lines[0])
                thumb = item.get('thumbnail', '')
                vid   = item.get('id', '')
                return {
                    'url':       f'https://music.youtube.com/watch?v={vid}' if vid else track_url,
                    'id':        vid,
                    'title':     item.get('title', ''),
                    'artist':    item.get('uploader', item.get('channel', '')),
                    'album':     item.get('album', ''),
                    'cover_url': thumb,
                    'duration':  int(item.get('duration') or 0),
                    'genre':     item.get('genre', ''),
                    'plays':     item.get('view_count', 0),
                }
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
        print('📦 Installing requests...')
        r = subprocess.run([sys.executable, '-m', 'pip', 'install',
                            '--user', '--quiet', 'requests'],
                           capture_output=True)
        return r.returncode == 0


def _try_install_ytmusicapi():
    """ytmusicapi をインストール試行（失敗してもエラー扱いにしない）。"""
    if _ytmusicapi_available():
        return True
    print('  📦 Installing ytmusicapi...')
    r = subprocess.run(
        [sys.executable, '-m', 'pip', 'install', 'ytmusicapi',
         '--break-system-packages', '--quiet'],
        capture_output=True)
    if r.returncode == 0:
        print('  ✅ ytmusicapi installed successfully')
        return True
    else:
        print('  ⚠ ytmusicapi install failed (yt-dlp search still works)')
        return False


# ═══════════════════════════════════════════════════════════════════════════
# ジャケット & Now Playing
# ═══════════════════════════════════════════════════════════════════════════

_feh_proc   = None
_feh_hidden = False


def _download_jacket(cover_url: str) -> Optional[str]:
    if not cover_url:
        return None
    try:
        import requests
        headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
        r = requests.get(cover_url, timeout=10, headers=headers)
        if r.status_code == 200:
            _JACKET_TMP.write_bytes(r.content)
            return str(_JACKET_TMP)
    except Exception:
        pass
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

        album_disp = track.get('album', '') or 'YouTube Music'

        track_info = {
            'title':        track.get('title', ''),
            'artist':       track.get('artist', ''),
            'album':        album_disp,
            'composer':     '',
            'conductor':    '',
            'performer':    track.get('artist', ''),
            'genre':        f'YouTube Music  {track.get("genre", "")}',
            'tempo':        f'{preset}  Gain:{gain_db:+.1f}dB  APL:{apl}  Vol:{vol}dB',
            'mode':         'ytmusic',
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

def _load_favorites() -> List[dict]:
    try:
        return json.loads(FAVS_PATH.read_text()) if FAVS_PATH.exists() else []
    except Exception:
        return []

def _save_favorites(favs: List[dict]):
    FAVS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FAVS_PATH.write_text(json.dumps(favs, ensure_ascii=False, indent=2))


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
        'album':        track.get('album', ''),
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
    album  = (track.get('album') or '').lower()
    text   = f'{genre} {title} {artist} {album}'

    KEYWORDS: Dict[str, List[str]] = {
        'jazz':   ['jazz', 'blues', 'swing', 'bebop', 'fusion', 'improvis',
                   'ジャズ', 'ブルース'],
        'piano':  ['piano solo', 'solo piano', 'piano recital',
                   'nocturne', 'etude', 'prelude', 'ピアノ'],
        'chamber':['quartet', 'trio', 'chamber', 'string quartet', '室内楽'],
        'vocal':  ['opera', 'lieder', 'vocal', 'choir', 'soprano', 'tenor',
                   'オペラ', '声楽', 'song cycle'],
        'musikverein': ['symphony', 'orchestra', 'concerto', 'philharmonic',
                        '交響曲', 'オーケストラ', 'overture'],
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


def _kill_playback(p_ff=None, p_ap=None, p_ytdlp=None):
    # ytdlp → ffmpeg → aplay の順で終了させる（逆順だとパイプが詰まる）
    for proc in [p_ytdlp or _procs.get('ytdlp'),
                 p_ff    or _procs.get('ffmpeg'),
                 p_ap    or _procs.get('aplay')]:
        if proc and proc.poll() is None:
            try:
                proc.terminate(); proc.wait(timeout=2)
            except Exception:
                try: proc.kill()
                except Exception: pass


def _play_one(track: dict, build_filter_func, state: dict,
              start_at: float = 0.0) -> str:
    """
    1トラックを再生する。
    戻り値: 'next' | 'prev' | 'quit' | 'restart'
    """
    url = track.get('url', '')
    if not url:
        print('  ⚠ URL is empty — skipping.')
        return 'next'

    # フィルターチェーン構築
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

    # ★★★ ピークモニターGUI用ログ出力: YouTube Music再生でも有効化 ★★★
    # （qji.py本体と同じ astats+ametadata パススルー方式。音声そのものには影響しない）
    # ★ Qobuzと異なりギャップレス先読みが無い逐次再生のため、Radio/AirPlayと
    #   同じ「固定パス＋曲ごとにos.remove()して作り直す」方式で安全。
    try:
        _yt_declip_log_path = f'/tmp/qji_declip_{os.getpid()}.log'
        if os.path.exists(_yt_declip_log_path):
            os.remove(_yt_declip_log_path)
        filter_args = _qji._wrap_filter_args_with_declip_monitor(filter_args, _yt_declip_log_path)
    except Exception:
        pass

    state['fmt_name'] = 'YouTube Music'

    # ──────────────────────────────────────────────────────────────────
    # yt-dlp | ffmpeg | aplay  3段パイプライン
    #
    # 【修正理由】
    # YouTube 音声は DASH（分割セグメント）配信。
    # 旧方式 (yt-dlp -g → URL → ffmpeg -i URL) では ffmpeg が
    # 最初のセグメントを読み切った時点で EOF と判定して再生停止。
    # yt-dlp を直接パイプ入力にすることで全セグメントを内部継ぎ接ぎし、
    # ffmpeg へ単一連続ストリームとして渡す。
    # ──────────────────────────────────────────────────────────────────
    # ──────────────────────────────────────────────────────────────────
    # フォーマット選択ポリシー:
    #   1. webm/opus  → pipe 完全対応、最優先
    #   2. opus(any)  → 同上
    #   3. m4a/aac    → moov atom 問題あり。yt-dlp に --hls-use-mpegts
    #                   を付けると ffmpeg が ts 形式で受け取れるため対処可能。
    #                   それでも失敗する場合は後述の tmpfile フォールバックへ。
    #   4. bestaudio  → 上記すべてが存在しない場合（UGC 動画等）
    #
    # しなプシュ等の UGC 動画は webm(opus) がなく m4a しかない場合がある。
    # その場合 --hls-use-mpegts で mpegts コンテナに変換してパイプ転送する。
    # ──────────────────────────────────────────────────────────────────
    FMT_PIPE    = 'bestaudio[ext=webm]/bestaudio[acodec=opus]/bestaudio[acodec=vorbis]'
    FMT_TMPFILE = 'bestaudio'

    _tmpfile = Path(tempfile.gettempdir()) / 'qji_yt_tmp_audio'

    # ── 事前フォーマット確認（高速、~2秒）────────────────────────────────
    def _can_pipe() -> bool:
        """webm/opus がある → True（パイプ再生可能）"""
        try:
            r = subprocess.run(
                ['yt-dlp', '-j', '--no-playlist', '--no-warnings', '--quiet', url],
                capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return False
            info = json.loads(r.stdout.strip().split('\n')[0])
            for fmt in info.get('formats', []):
                ext   = fmt.get('ext', '')
                acodec = fmt.get('acodec', '')
                if ext == 'webm' or 'opus' in acodec or 'vorbis' in acodec:
                    return True
            return False
        except Exception:
            return False   # 判定失敗はパイプを試みる

    use_pipe = _can_pipe()

    # ── tmpfile 再生 ────────────────────────────────────────────────────
    def _play_via_tmpfile() -> str:
        print('  📥 Downloading (m4a)...')
        for old in _tmpfile.parent.glob(f'{_tmpfile.name}.*'):
            try: old.unlink()
            except Exception: pass

        cmd_dl = [
            'yt-dlp', '-o', str(_tmpfile) + '.%(ext)s',
            '-q', '--no-playlist', '--no-warnings',
            '-f', FMT_TMPFILE,
            url,
        ]
        try:
            r = subprocess.run(cmd_dl, timeout=180)
            if r.returncode != 0:
                print('  ⚠ Download failed — skipping.')
                return 'next'
        except subprocess.TimeoutExpired:
            print('  ⚠ Download timed out — skipping.')
            return 'next'

        saved = sorted(_tmpfile.parent.glob(f'{_tmpfile.name}.*'),
                       key=lambda p: p.stat().st_mtime)
        if not saved:
            print('  ⚠ File not found — skipping.')
            return 'next'
        audio_file = saved[-1]
        print(f'  📁 {audio_file.suffix.lstrip(".")} format — playing')

        cmd_ff2 = (
            ['ffmpeg', '-loglevel', 'warning',
             '-i', str(audio_file),
             '-vn', '-ar', str(YT_SAMPLE_RATE), '-acodec', 'pcm_s32le']
            + filter_args
            + ['-f', 'wav', '-']
        )
        dev = state.get('output_device', 'default')
        if dev == 'bluealsa':
            cmd_ap2 = ['aplay', '-D', 'bluealsa',
                       '--buffer-size=262144', '--period-size=32768']
        else:
            cmd_ap2 = ['aplay', '-D', dev, '-r', str(YT_SAMPLE_RATE),
                       '--buffer-size=262144', '--period-size=32768']
        try:
            p_f2 = subprocess.Popen(cmd_ff2, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            p_a2 = subprocess.Popen(cmd_ap2, stdin=p_f2.stdout,
                                    stderr=subprocess.DEVNULL)
            p_f2.stdout.close()
            p_yt2 = subprocess.Popen(['true'])   # ダミー
            _procs['ytdlp']  = p_yt2
            _procs['ffmpeg'] = p_f2
            _procs['aplay']  = p_a2
        except Exception as e:
            print(f'  ⚠ Playback launch error: {e}')
            try: audio_file.unlink()
            except Exception: pass
            return 'next'

        def _cleanup():
            p_f2.wait()
            try: audio_file.unlink()
            except Exception: pass
        threading.Thread(target=_cleanup, daemon=True).start()

        return _key_loop(p_f2, p_a2, p_yt2, track, state,
                         play_start=time.time(), start_at=start_at,
                         build_filter_func=build_filter_func)

    # ── tmpfile モードへ直行 ─────────────────────────────────────────────
    if not use_pipe:
        state['current_track'] = track
        _update_now_playing(track, state, elapsed=int(start_at))
        return _play_via_tmpfile()

    # ── パイプ再生（webm/opus 確認済み）─────────────────────────────────
    cmd_ytdlp = [
        'yt-dlp', '-o', '-',
        '-q', '--no-part', '--no-playlist', '--no-warnings',
        '-f', FMT_PIPE,
        url,
    ]
    cmd_ff = (
        ['ffmpeg', '-loglevel', 'warning',
         '-fflags', '+genpts',
         '-i', 'pipe:0',
         '-vn', '-ar', str(YT_SAMPLE_RATE), '-acodec', 'pcm_s32le']
        + filter_args
        + ['-f', 'wav', '-']
    )
    dev = state.get('output_device', 'default')
    if dev == 'bluealsa':
        cmd_ap = ['aplay', '-D', 'bluealsa',
                  '--buffer-size=262144', '--period-size=32768']
    else:
        cmd_ap = ['aplay', '-D', dev, '-r', str(YT_SAMPLE_RATE),
                  '--buffer-size=262144', '--period-size=32768']

    state['current_track'] = track
    _update_now_playing(track, state, elapsed=int(start_at))

    try:
        p_ytdlp = subprocess.Popen(cmd_ytdlp,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        p_ff    = subprocess.Popen(cmd_ff,
                                   stdin=p_ytdlp.stdout,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        p_ap    = subprocess.Popen(cmd_ap,
                                   stdin=p_ff.stdout,
                                   stderr=subprocess.DEVNULL)
        p_ytdlp.stdout.close()
        p_ff.stdout.close()
        _procs['ytdlp']  = p_ytdlp
        _procs['ffmpeg'] = p_ff
        _procs['aplay']  = p_ap
    except Exception as e:
        print(f'  ⚠ Playback launch error: {e}')
        return 'next'

    return _key_loop(p_ff, p_ap, p_ytdlp, track, state,
                     play_start=time.time(), start_at=start_at,
                     build_filter_func=build_filter_func)


def _dur_str(sec: int) -> str:
    sec = int(sec)
    if sec >= 3600:
        return f'{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}'
    return f'{sec // 60}:{sec % 60:02d}'


def _key_loop(p_ff, p_ap, p_ytdlp, track: dict, state: dict,
              play_start: float, start_at: float,
              build_filter_func) -> str:
    """再生中のキー入力ループ。"""
    duration = track.get('duration', 0)
    title    = track.get('title', '')
    artist   = track.get('artist', '')
    album    = track.get('album', '')

    preset_lbl = FILTER_PRESET_LABELS.get(state.get('filter_preset', ''), '')
    gain_db    = GAIN_PRESETS_DB.get(state.get('gain_preset', 'classical'), 0.0)
    vol        = state.get('volume', 12)
    apl        = '🌿' if state.get('air_layer', True) else '  '

    print(f'\n  ▶ {artist} — {title}')
    if album:
        print(f'     Album: {album}')
    print(f'     {preset_lbl}  Gain:{gain_db:+.1f}dB  APL:{apl}  Vol:{vol}dB')
    print(f'     [n]Next  [b]Prev  [q]Back  [g]Gain  [w]APL  [c]Preset'
          f'  [+/-]Vol  [s]Save  [i]Image  [ESC]Hide')

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
                global _remote_next_flag, _remote_stop_flag
                if _remote_next_flag:
                    _remote_next_flag = False
                    _kill_playback(p_ff, p_ap, p_ytdlp)
                    result = 'next'; break
                if _remote_stop_flag:
                    _remote_stop_flag = False
                    _kill_playback(p_ff, p_ap, p_ytdlp)
                    result = 'quit'; break
                r, _, _ = select.select([tty_fd], [], [], 0.1)
                if not r:
                    continue
                ch = tty_fd.read(1)
                if not ch:
                    break

                if ch == b'n':
                    _kill_playback(p_ff, p_ap, p_ytdlp)
                    result = 'next'; break
                elif ch == b'b':
                    _kill_playback(p_ff, p_ap, p_ytdlp)
                    result = 'prev'; break
                elif ch in (b'q', b'Q'):
                    _kill_playback(p_ff, p_ap, p_ytdlp)
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
                    cur_idx = GAIN_ORDER.index(state['gain_preset']) \
                              if state['gain_preset'] in GAIN_ORDER else 0
                    state['gain_preset'] = GAIN_ORDER[(cur_idx + 1) % len(GAIN_ORDER)]
                    new_db = GAIN_PRESETS_DB[state['gain_preset']]
                    sys.stdout.write(
                        f'\r  🎚 ゲイン: {state["gain_preset"]}  ({new_db:+.1f}dB)  ※次曲から適用  \n')
                    sys.stdout.flush()
                elif ch == b'w':
                    state['air_layer'] = not state.get('air_layer', True)
                    apl_str = '🌿 ON' if state['air_layer'] else 'OFF'
                    sys.stdout.write(
                        f'\r  🌿 Air Particle Layer: {apl_str}  ※次曲から適用  \n')
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
                    _kill_playback(p_ff, p_ap, p_ytdlp)
                    _stop_timer.set()
                    _restore_terminal()
                    _save_to_favorites(state)
                    result = 'restart'; break
                elif ch == b'c':
                    _kill_playback(p_ff, p_ap, p_ytdlp)
                    _stop_timer.set()
                    _restore_terminal()
                    print('\n  Select preset:')
                    for i, k in enumerate(FILTER_PRESETS, 1):
                        marker = ' ◀' if k == state.get('filter_preset') else ''
                        print(f'    {i}: {FILTER_PRESET_LABELS[k]}{marker}')
                    choice = _yt_readline('  番号を入力 (Enter でキャンセル): ')
                    if choice.isdigit():
                        idx = int(choice) - 1
                        if 0 <= idx < len(FILTER_PRESETS):
                            state['filter_preset']       = FILTER_PRESETS[idx]
                            state['manual_preset_locked'] = True
                            print(f'  ✅ {FILTER_PRESET_LABELS[FILTER_PRESETS[idx]]} selected')
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

    try:
        p_ff.wait(timeout=5)
    except Exception:
        _kill_playback(p_ff, p_ap, p_ytdlp)
    try:
        p_ap.wait(timeout=3)
    except Exception:
        _kill_playback(p_ff, p_ap, p_ytdlp)
    try:
        if p_ytdlp.poll() is None:
            p_ytdlp.terminate(); p_ytdlp.wait(timeout=2)
    except Exception:
        pass

    sys.stdout.write('\r                                              \r')
    sys.stdout.flush()
    return result


def _play_list(tracks: List[dict], start_idx: int,
               build_filter_func, state: dict):
    """トラックリストを再生する。"""
    i = start_idx
    while 0 <= i < len(tracks):
        track = tracks[i]
        _apply_auto_preset(track, state)
        result = _play_one(track, build_filter_func, state)
        if result == 'next':
            i += 1
        elif result == 'prev':
            i = max(0, i - 1)
        elif result == 'restart':
            pass
        elif result == 'quit':
            return


# ═══════════════════════════════════════════════════════════════════════════
# メニューフロー
# ═══════════════════════════════════════════════════════════════════════════

def _url_flow(build_filter_func, state: dict):
    """URL 入力 → 再生フロー（単曲 / プレイリスト URL 対応）"""
    url = _yt_readline('\n  🔗 YouTube Music URL を入力: ')
    if not url:
        return
    if 'youtube' not in url and 'youtu.be' not in url:
        print('  ⚠ Please enter a YouTube / YouTube Music URL')
        return

    is_playlist = ('list=' in url or '/playlist' in url)
    if is_playlist:
        print(f'  📋 Fetching playlist...')
        tracks = get_playlist_tracks(url)
        if not tracks:
            print('  ⚠ Could not retrieve tracks')
            return
        print(f'  📋 Retrieved {len(tracks)} tracks')
        _play_list(tracks, 0, build_filter_func, state)
    else:
        print(f'  🔍 Fetching metadata...')
        info = _get_track_info_ytdlp(url)
        if info:
            track = info
        else:
            track = {'url': url, 'title': url.split('v=')[-1][:20],
                     'artist': '', 'album': '', 'cover_url': '', 'duration': 0}
        _apply_auto_preset(track, state)
        while True:
            result = _play_one(track, build_filter_func, state)
            if result != 'restart':
                break


def _search_flow(build_filter_func, state: dict):
    """検索フロー（ターミナル）"""
    ytm_ok = _ytmusicapi_available()
    hint   = 'ytmusicapi' if ytm_ok else 'yt-dlp (ytmusicapi 未インストール)'
    query  = _yt_readline(f'\n  🔍 検索ワードを入力 ({hint}): ')
    if not query:
        return

    print(f'  Searching...')
    tracks = search_songs(query, limit=40)
    if not tracks:
        print('  ⚠ No search results')
        return

    print(f'\n  🎵 Search results for "{query}"')
    for i, t in enumerate(tracks, 1):
        dur   = f' [{_dur_str(t["duration"])}]' if t.get('duration') else ''
        album = f' 《{t["album"]}》'            if t.get('album')    else ''
        print(f'    {i:2d}. {t["artist"]} — {t["title"]}{album}{dur}')

    choice = _yt_readline('  番号を入力 (Enter でキャンセル): ')
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
    """お気に入り再生フロー（トラック＋アルバム）"""
    ALBUM_FAVS_PATH = Path.home() / '.config' / 'qji_ytmusic_album_favs.json'
    album_favs = []
    try:
        if ALBUM_FAVS_PATH.exists():
            album_favs = json.loads(ALBUM_FAVS_PATH.read_text())
    except Exception:
        pass

    favs = _load_favorites()
    if not favs:
        print('  ⚠ No favourites yet')
        return
    total = len(favs) + len(album_favs)
    print(f'\n  ⭐ Favourites (tracks:{len(favs)}  albums:{len(album_favs)})')
    if album_favs:
        print('  ─ 💿 Albums ─')
        for a in album_favs:
            tc = a.get('track_count', len(a.get('tracks', [])))
            print(f'    A{a.get("no",0):03d}. {a["artist"]} — {a["title"]}  ({tc} tracks)')
    if favs:
        print('  ─ 🎵 Tracks ─')
    for f in favs:
        dur       = f' [{_dur_str(f["duration"])}]' if f.get('duration') else ''
        audio     = f.get('audio_settings') or {}
        preset_lb = FILTER_PRESET_LABELS.get(audio.get('filter_preset', ''), '')
        gain_db   = GAIN_PRESETS_DB.get(audio.get('gain_preset', ''), None)
        vol       = audio.get('volume')
        gain_str  = f'  🔊{gain_db:+.1f}dB' if gain_db is not None else ''
        vol_str   = f'  Vol:{vol}dB'         if vol is not None    else ''
        album     = f' 《{f["album"]}》'     if f.get('album')     else ''
        print(f'    {f["no"]:03d}. {f["artist"]} — {f["title"]}{album}{dur}'
              f'  {preset_lb}{gain_str}{vol_str}')

    choice = _yt_readline('  番号を入力 (トラック: 数字 / アルバム: A+数字、Enter でキャンセル): ')
    if not choice:
        return
    # アルバム選択: A001 など
    if choice.lower().startswith('a') and choice[1:].isdigit():
        ano = int(choice[1:])
        af  = next((a for a in album_favs if a.get('no') == ano), None)
        if not af:
            print('  ⚠ Album favourite not found'); return
        tracks = [
            {'url': t['url'], 'title': t['title'], 'artist': t['artist'],
             'album': t.get('album', af['title']),
             'cover_url': t.get('cover_url', t.get('art', af.get('cover_url', ''))),
             'duration': t.get('duration', 0), 'genre': ''}
            for t in af.get('tracks', [])
        ]
        if not tracks:
            print('  ⚠ No tracks available'); return
        print(f'  💿 Playing album: {af["artist"]} — {af["title"]}  ({len(tracks)} tracks)')
        _play_list(tracks, 0, build_filter_func, state); return
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
        'album':     fav.get('album', ''),
        'cover_url': fav.get('cover_url', ''),
        'duration':  fav.get('duration', 0),
        'genre':     fav.get('genre', ''),
    }
    audio = fav.get('audio_settings', {})
    if audio:
        state.update({
            'gain_preset':   audio.get('gain_preset',   state.get('gain_preset')),
            'filter_preset': audio.get('filter_preset', state.get('filter_preset')),
            'air_layer':     audio.get('air_layer',     state.get('air_layer')),
            'volume':        audio.get('volume',        state.get('volume')),
            'echo_mode':     audio.get('echo_mode',     state.get('echo_mode')),
            'tinnitus':      audio.get('tinnitus',      state.get('tinnitus')),
            'musikverein':   audio.get('musikverein',   state.get('musikverein')),
        })
        state['manual_preset_locked'] = True
    while True:
        result = _play_one(track, build_filter_func, state)
        if result != 'restart':
            break


def _handle_browser_request(build_filter_func, state: dict, req: dict):
    """ブラウザUIからのリクエストを処理する。"""
    _restore_terminal()
    req_type = req.get('type', 'track')

    if req_type == 'playlist':
        tracks = req.get('tracks', [])
        if not tracks:
            print('  ⚠ Playlist is empty')
            return
        start = max(0, req.get('start_from', 1) - 1)
        print(f'\n  📋 Playlist playback: {len(tracks)} tracks, starting from #{start + 1}')
        _play_list(tracks, start, build_filter_func, state)
    else:
        track = {
            'url':       req.get('url', ''),
            'title':     req.get('title', ''),
            'artist':    req.get('artist', ''),
            'album':     req.get('album', ''),
            'cover_url': req.get('cover_url', ''),
            'duration':  req.get('duration', 0),
            'genre':     req.get('genre', ''),
        }
        if not track['url']:
            print('  ⚠ No URL available')
            return
        audio = req.get('audio_settings', {})
        if audio:
            state.update({
                'gain_preset':   audio.get('gain_preset',   state['gain_preset']),
                'filter_preset': audio.get('filter_preset', state['filter_preset']),
                'air_layer':     audio.get('air_layer',     state['air_layer']),
                'volume':        audio.get('volume',        state['volume']),
            })
        _apply_auto_preset(track, state)
        print(f'\n  📱 Browser playback: {track["artist"]} — {track["title"]}')
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
    """qji.py の Y キーハンドラから呼び出す唯一の関数"""
    _capture_baseline_term()

    if not _ensure_ytdlp():
        input('  Press Enter to go back...'); return
    if not _ensure_requests():
        print('  ❌ requests is required: pip install requests')
        input('  Press Enter to go back...'); return

    # ytmusicapi インストール試行（失敗しても続行）
    _try_install_ytmusicapi()

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

    # ブラウザUI サーバー起動
    _browser_available = False
    try:
        import qji_ytmusic_browser as _browser
        _browser.start_browser_server(open_browser=False)
        _browser_available = True
        _open_incognito_browser(f'http://localhost:{PORT}')
    except ImportError:
        pass

    def _show_menu():
        print('\n' + '═' * 56)
        print('🎵  Qji × YouTube Music  —  Streaming')
        print('═' * 56)
        preset_lbl  = FILTER_PRESET_LABELS.get(state['filter_preset'], '')
        gain_db     = GAIN_PRESETS_DB.get(state['gain_preset'], 0.0)
        apl         = '🌿 ON' if state['air_layer'] else 'OFF'
        favs_count  = len(_load_favorites())
        ytm_status  = '✅ ytmusicapi' if _ytmusicapi_available() else '⚠ yt-dlp のみ'
        auth_status = '🔑 認証済み' if AUTH_PATH.exists() else '未認証'
        last_title  = (state.get('current_track') or {}).get('title', '')
        print(f'  Output device : {output_device}')
        print(f'  Audio preset  : {preset_lbl}  Gain:{gain_db:+.1f}dB'
              f'  APL:{apl}  Vol:{state["volume"]}dB')
        print(f'  Favourites    : {favs_count}')
        print(f'  Search engine : {ytm_status}  {auth_status}')
        if last_title:
            print(f'  Last played   : {last_title[:40]}')
        if _browser_available:
            print(f'  Browser UI    : http://localhost:{PORT}')
        print()
        print('  u : 🔗 Paste URL to play')
        print('  k : 🔍 Keyword search (terminal)')
        print('  f : ⭐ Play from favourites')
        print('  v : 🌐 Reopen browser UI')
        print('  q : ← Return to Qji main menu')
        print('─' * 56)

    try:
        while True:
            _show_menu()

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
            ch = ''
            try:
                with open('/dev/tty', 'r') as _tty:
                    try:
                        subprocess.run(['stty', 'sane'], stdin=_tty,
                                       check=False, timeout=2, capture_output=True)
                    except Exception:
                        pass
                    print('\n  Select (u / k / f / v / q): ', end='', flush=True)
                    while True:
                        if _browser_req_event.is_set():
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
                    print('\n  Select (u / k / f / v / q): ', end='', flush=True)
                    ch = sys.stdin.readline().strip().lower()
                except Exception:
                    break

            # ── ブラウザからの再生リクエスト ─────────────────────────────
            if _pending_browser_req is not None:
                _handle_browser_request(build_filter_func, state,
                                        _pending_browser_req)
                _restore_terminal()
                # ブラウザを閉じずにメニューへ戻る
                continue

            # ── キーボード入力 ────────────────────────────────────────────
            if ch in ('q', 'b'):
                # Qji メインメニューへ戻る
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
                # ブラウザは維持したままメニューへ戻る
                continue

            elif ch == 'k':
                _search_flow(build_filter_func, state)
                _restore_terminal()
                continue

            elif ch == 'f':
                _favorites_flow(build_filter_func, state)
                _restore_terminal()
                continue

            elif ch == 'v':
                if _browser_available:
                    _open_incognito_browser(f'http://localhost:{PORT}')
                else:
                    print('  ⚠ qji_ytmusic_browser.py not found')

            else:
                _restore_terminal()
                if ch:
                    print('  ⚠ Please enter u / k / f / v / q')

    finally:
        _clear_now_playing()
        _restore_terminal()
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
