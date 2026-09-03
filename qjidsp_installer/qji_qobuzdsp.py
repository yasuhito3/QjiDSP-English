#!/usr/bin/env python3
"""
qji_qobuz.py  —  Qji 統合 Qobuz ストリーミングモジュール
qji.py から Q キーで呼び出される。

再生中キー:
  [n]次  [b]前  [q]メニューへ
  [g]ゲイン切替  [G]ギャップレス ON/OFF  [w]APL ON/OFF  [c]プリセット選択
  [+]/[=]音量+1dB  [-]音量-1dB
  [s]音響設定を保存
"""

import os, re, sys, json, select, hashlib, time, threading
import glob as _glob
import termios, tty, subprocess, tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Tuple

# ═══════════════════════════════════════════════════════════════════════════
# 定数
# ═══════════════════════════════════════════════════════════════════════════

CONFIG_PATH    = Path.home() / '.config' / 'qji_qobuz_direct.json'
FAVORITES_PATH = Path.home() / '.config' / 'qji_qobuz_favorites.json'
QOBUZ_API_BASE = 'https://www.qobuz.com/api.json/0.2/'
QUALITY_PREFS  = [27, 7, 6, 5]

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
    'classical': 0.0,
    'general':  -1.5,
    'jazz_pop': -3.5,
    'loud':     -5.0,
}
GAIN_ORDER = ['classical', 'general', 'jazz_pop', 'loud']

# ジャケット一時ファイル
_JACKET_TMP = Path(tempfile.gettempdir()) / 'qji_qobuz_jacket.jpg'

# ═══════════════════════════════════════════════════════════════════════════
# シークレットウィンドウ管理（j キー・ジャケットモード方式）
# ═══════════════════════════════════════════════════════════════════════════
#
# 【設計メモ】
# Chrome/Chromium は通常シングルトン動作をするため、すでに起動中の場合に
# --incognito --new-window を渡すと既存プロセスがウィンドウを開いて
# 子プロセスはすぐ終了する。この場合 Popen でプロセスを追跡できず
# terminate() でウィンドウを閉じることができない（ウィンドウが残り続ける）。
#
# 解決策: --user-data-dir に一時ディレクトリを指定する。
# Chrome はプロファイルディレクトリが異なると必ず独立した新プロセスを立てるため、
# Popen が本物のウィンドウプロセスを保持できる。
# feh がジャケットモードで毎回独立プロセスとして起動・終了できるのと同じ原理。
# ═══════════════════════════════════════════════════════════════════════════

import shutil as _shutil


def _is_dsp_mode_active() -> bool:
    """
    呼び出し元(qji.py)のグローバル変数 dsp_mode_active を参照する。
    qji.py は `import qji_qobuzdsp as qji_qobuz` として本モジュールを読み込むため、
    sys.modules 経由で呼び出し元の qji.py 本体（__main__）を探し、
    dsp_mode_active フラグを取得する。取得できない場合は False（通常モード扱い）。
    """
    try:
        main_mod = sys.modules.get('__main__')
        return bool(getattr(main_mod, 'dsp_mode_active', False))
    except Exception:
        return False

_incognito_proc: Optional[subprocess.Popen] = None
_incognito_tmp_dir: Optional[str] = None


def _open_incognito_browser(url: str) -> bool:
    """シークレットウィンドウでブラウザを開く。
    Chrome/Chromium には --user-data-dir で一時プロファイルを渡し、
    シングルトン問題を回避して独立プロセスとして起動する。
    再生終了後に _close_incognito_browser() でプロセスごと閉じられる。"""
    global _incognito_proc, _incognito_tmp_dir
    _close_incognito_browser()  # 既存があれば先に閉じて一時ディレクトリも削除

    import tempfile
    tmp = tempfile.mkdtemp(prefix='qji_qobuz_')
    _incognito_tmp_dir = tmp

    # --user-data-dir で独立プロセス強制 + --incognito でシークレット表示
    chrome_flags = [
        f'--user-data-dir={tmp}',
        '--incognito',
        '--new-window',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-extensions',
    ]
    candidates = [
        (['chromium-browser'] + chrome_flags + [url], 'Chromium'),
        (['chromium']         + chrome_flags + [url], 'Chromium'),
        (['google-chrome']    + chrome_flags + [url], 'Chrome'),
        (['google-chrome-stable'] + chrome_flags + [url], 'Chrome'),
        # Firefox は --new-instance で独立プロセス強制
        (['firefox', '--new-instance', '--private-window', url], 'Firefox'),
    ]
    for cmd, name in candidates:
        try:
            _incognito_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,   # ★ 親の stdin を継承させない（terminal 汚染防止）
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            # 起動直後にすぐ終了していないか確認（0.3秒待って poll）
            time.sleep(0.3)
            if _incognito_proc.poll() is None:
                print(f'  🔒 {name} opened in private window (independent process)')
                return True
            else:
                # 即終了した → シングルトンに吸収された可能性。次候補へ
                _incognito_proc = None
                continue
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f'  ⚠ {name} launch error: {e}')
            continue

    # 全候補失敗 → フォールバック: xdg-open（プロセス追跡なし）
    _shutil.rmtree(tmp, ignore_errors=True)
    _incognito_tmp_dir = None
    try:
        subprocess.Popen(['xdg-open', url],
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f'  🌐 Browser opened (no private-mode fallback): {url}')
        return True
    except Exception as e:
        print(f'  ⚠ Could not open browser: {e}')
        return False


def _close_incognito_browser():
    """シークレットウィンドウのプロセスを終了して閉じ、一時プロファイルを削除する。"""
    global _incognito_proc, _incognito_tmp_dir
    if _incognito_proc is not None:
        try:
            if _incognito_proc.poll() is None:
                _incognito_proc.terminate()
                try:
                    _incognito_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    _incognito_proc.kill()
                    # kill() は非同期: SIGKILL を送った直後はまだファイルハンドルが
                    # 解放されていない。rmtree が一時ディレクトリを削除しようとすると
                    # ブロックすることがある。0.4 秒待ってカーネルに解放を促す。
                    time.sleep(0.4)
                print('  🔒 Private window closed')
        except Exception:
            pass
        finally:
            _incognito_proc = None
    # 一時プロファイルディレクトリを削除
    if _incognito_tmp_dir is not None:
        _shutil.rmtree(_incognito_tmp_dir, ignore_errors=True)
        _incognito_tmp_dir = None


# ═══════════════════════════════════════════════════════════════════════════
# requests 確認
# ═══════════════════════════════════════════════════════════════════════════

def _ensure_requests() -> bool:
    try:
        import requests; return True
    except ImportError:
        print('📦 Installing requests...')
        r = subprocess.run([sys.executable, '-m', 'pip', 'install',
                            '--user', '--quiet', 'requests'], capture_output=True)
        return r.returncode == 0


# ═══════════════════════════════════════════════════════════════════════════
# ターミナル管理（freeze 対策）
# ═══════════════════════════════════════════════════════════════════════════

_saved_term    = None
_baseline_term = None   # 起動時に1度だけ保存する「汚染されない」基準ターミナル設定


def _capture_baseline_term():
    """
    プログラム起動時に1度だけ呼ぶ。
    raw モード操作前の正常なターミナル設定を _baseline_term へ保存する。
    _saved_term は再生中に raw モードで呼ばれると汚染されうるが、
    _baseline_term は絶対に上書きしない。
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
    ターミナルを確実に正常状態へ戻す。

    修正ポイント:
      旧実装は stty sane 後に再度 _saved_term を適用していたが、
      _saved_term が raw モード中に保存されて汚染されている場合に
      stty sane の効果を打ち消していた。
      新実装は起動時ベースライン (_baseline_term) を優先し、
      _saved_term には依存しない。
    """
    global _saved_term
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
    #    _saved_term は raw モード中に汚染されうるため使用しない
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


def _qobuz_readline(prompt: str = '') -> str:
    """
    キーリスナーの raw モード中に安全にテキスト入力を受け取る。
    /dev/tty を直接オープンして読み込む（sys.stdin の raw モードに干渉しない）。
    """
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
# 設定読み書き
# ═══════════════════════════════════════════════════════════════════════════

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    except Exception:
        return {}

def _save_config(config: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    CONFIG_PATH.chmod(0o600)


# ═══════════════════════════════════════════════════════════════════════════
# Qobuz API クライアント
# ═══════════════════════════════════════════════════════════════════════════

class QobuzClient:
    def __init__(self, app_id: str, app_secret: str, auth_token: str):
        import requests
        self.app_id = app_id; self.app_secret = app_secret
        self.auth_token = auth_token
        self.session = requests.Session()
        self.session.headers.update({
            'X-App-Id': app_id, 'X-User-Auth-Token': auth_token,
            'User-Agent': 'Mozilla/5.0',
        })

    def _get(self, endpoint: str, params: dict = None) -> Optional[dict]:
        p = dict(params or {})
        p['app_id'] = self.app_id; p['user_auth_token'] = self.auth_token
        try:
            r = self.session.get(QOBUZ_API_BASE + endpoint, params=p, timeout=8)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    def ping(self) -> bool:
        d = self._get('user/get'); return bool(d and 'id' in d)

    def get_album(self, album_id: str) -> Optional[dict]:
        return self._get('album/get', {'album_id': album_id})

    def get_track(self, track_id: str) -> Optional[dict]:
        return self._get('track/get', {'track_id': track_id})

    def get_file_url(self, track_id: str) -> Optional[Tuple[str, int, int]]:
        for fmt_id in QUALITY_PREFS:
            ts  = time.time()
            sig = hashlib.md5(
                f'trackgetFileUrlformat_id{fmt_id}intentstream'
                f'track_id{track_id}{ts}{self.app_secret}'.encode()
            ).hexdigest()
            d = self._get('track/getFileUrl', {
                'track_id': track_id, 'format_id': fmt_id,
                'intent': 'stream', 'request_ts': ts, 'request_sig': sig,
            })
            if d and d.get('url'):
                sr = int((d.get('sampling_rate') or 44.1) * 1000)
                bd = int(d.get('bit_depth') or 16)
                return d['url'], sr, bd
        return None

    def get_playlist_tracks(self, playlist_id: str, limit: int = 500) -> List[dict]:
        """Qobuz プレイリストのトラック一覧を取得して返す。"""
        d = self._get('playlist/get', {
            'playlist_id': playlist_id,
            'extra':       'tracks',
            'limit':       limit,
            'offset':      0,
        })
        if not d:
            return []
        # パターン1: d['tracks']['items']  （標準）
        tracks_obj = d.get('tracks')
        if isinstance(tracks_obj, dict):
            items = tracks_obj.get('items', [])
            if items:
                return items
        # パターン2: d['playlist']['tracks']['items']  （ラッパーあり）
        pl_obj = d.get('playlist') or {}
        tracks_obj = pl_obj.get('tracks')
        if isinstance(tracks_obj, dict):
            items = tracks_obj.get('items', [])
            if items:
                return items
        # パターン3: d['items']
        return d.get('items', [])


# ═══════════════════════════════════════════════════════════════════════════
# URL パース / トラックリスト変換
# ═══════════════════════════════════════════════════════════════════════════

def _parse_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    for pattern in [
        r'https?://open\.qobuz\.com/(album|track)/([^/?#]+)',
        r'https?://(?:www\.)?qobuz\.com(?:/[a-z]{2}-[a-z]{2})?/(album|track)(?:/[^/]+)?/([^/?#]+)/?$',
    ]:
        m = re.match(pattern, url)
        if m: return m.group(1), m.group(2)
    return None, None

def _album_to_tracks(album: dict) -> List[dict]:
    artist = (album.get('artist') or {}).get('name', '')
    title  = album.get('title', '')
    cover  = ((album.get('image') or {}).get('large') or
               (album.get('image') or {}).get('small') or '')
    tracks = []
    for item in (album.get('tracks') or {}).get('items', []):
        if not item.get('id'): continue
        tracks.append({
            'track_id':     str(item['id']),
            'title':        item.get('title', ''),
            'artist':       (item.get('performer') or {}).get('name', '') or artist,
            'album':        title,
            'album_artist': artist,
            'cover_url':    cover,
            'duration':     int(item.get('duration') or 0),
            'track_number': int(item.get('track_number') or 0),
        })
    return tracks


# ═══════════════════════════════════════════════════════════════════════════
# ジャケット画像 & Now Playing 連携（qji.py のグローバルを直接更新）
# ═══════════════════════════════════════════════════════════════════════════

def _download_jacket(cover_url: str) -> Optional[str]:
    """Qobuz のカバー画像を一時ファイルにダウンロードして返す"""
    if not cover_url:
        return None
    try:
        import requests
        r = requests.get(cover_url, timeout=10)
        if r.status_code == 200:
            _JACKET_TMP.write_bytes(r.content)
            return str(_JACKET_TMP)
    except Exception:
        pass
    return None

# feh プロセス（デスクトップ表示用）
_feh_proc      = None
_feh_hidden    = False   # ESC で非表示にした状態

# プレイリスト再生状態（ブラウザUIへのポーリング用）
_PLAYLIST_STATUS: dict = {
    'active':  False,   # プレイリスト再生中か
    'total':   0,
    'current': 0,       # 1-indexed の現在アルバム番号
    'title':   '',
    'artist':  '',
    'img':     '',
    'queue':   [],      # [{title, artist, img, done:bool}]
}


def _feh_hide():
    """feh を終了して画像を非表示にする（ESC キー）"""
    global _feh_proc, _feh_hidden
    if _feh_proc and _feh_proc.poll() is None:
        try:
            _feh_proc.terminate()
            _feh_proc.wait(timeout=1)
        except Exception:
            pass
    _feh_proc   = None
    _feh_hidden = True


def _feh_show(track_info: dict = None, jacket_path: str = None):
    """feh を起動して画像を再表示する（i キー）"""
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
                try:
                    _feh_proc.terminate(); _feh_proc.wait(timeout=1)
                except Exception:
                    pass
            _feh_proc = show_fn(path, track_info)
        else:
            if _feh_proc and _feh_proc.poll() is None:
                try:
                    _feh_proc.terminate(); _feh_proc.wait(timeout=1)
                except Exception:
                    pass
            _feh_proc = subprocess.Popen(
                ['feh', '--fullscreen', '--auto-zoom', '--hide-pointer',
                 '--borderless', '--no-menus', path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        sys.stdout.write('\r  🖼 Cover art displayed              \n')
        sys.stdout.flush()
    except Exception as e:
        sys.stdout.write(f'\r  ⚠ Cover art error: {e}        \n')
        sys.stdout.flush()

def _update_now_playing(track: dict, state: dict, elapsed: int = 0):
    """
    qji.py の current_track_info / current_image_path を更新し、
    デスクトップ表示（feh + ANSI オーバーレイ）も起動する。
    """
    global _feh_proc
    try:
        import __main__ as _qji
        lock = getattr(_qji, 'info_display_lock', None)
        ctx  = lock if lock else __import__('contextlib').nullcontext()

        preset  = FILTER_PRESET_LABELS.get(state.get('filter_preset', 'musikverein'), '')
        gain_db = GAIN_PRESETS_DB.get(state.get('gain_preset', 'classical'), 0.0)
        vol     = state.get('volume', 12)
        apl     = '🌿 ON' if state.get('air_layer', True) else 'OFF'
        fmt     = state.get('fmt_name', '')

        track_info = {
            'title':        track.get('title', ''),
            'artist':       track.get('artist', ''),
            'album':        track.get('album', ''),
            'composer':     '',
            'conductor':    '',
            'performer':    track.get('album_artist', ''),
            'genre':        f'Qobuz  {fmt}',
            'tempo':        f'{preset}  Gain:{gain_db:+.1f}dB  APL:{apl}  Vol:{vol}dB',
            'mode':         'qobuz',
            'track_num':    track.get('track_number', 0),
            'total_tracks': 0,
            'file_path':    '',
            'duration':     str(track.get('duration', 0)),
            'elapsed':      elapsed,
        }

        with ctx:
            info = getattr(_qji, 'current_track_info', {})
            info.update(track_info)
            if hasattr(_qji, 'current_track_info'):
                _qji.current_track_info.update(track_info)

        # ジャケット画像をダウンロード
        jacket_path = _download_jacket(track.get('cover_url', ''))

        # ★★★ 修正: ジャケットバッジ表示用に qji.py 側の current_filter_preset /
        # current_gain_preset を「今この曲で実際に使っているプリセット」に確定させておく。
        # _get_filter_args() は音声フィルター構築のためだけに一時的にこれらを書き換えて
        # すぐ元の値へ戻す(finallyで復元)ため、その後に呼ばれるバッジ表示がそのままだと
        # 巻き戻った古い値（前の曲やローカル再生時の値）を読んでしまい、実際のプリセット
        # と表示がズレる不具合があった。
        try:
            _qji.current_filter_preset = state.get('filter_preset', 'musikverein')
            _qji.current_gain_preset   = state.get('gain_preset', 'classical')
        except Exception:
            pass

        # デスクトップ: 曲情報付きジャケット（新曲に変わったら自動表示）
        # ESC で非表示中（_feh_hidden=True）の場合は表示しない
        if jacket_path and not _feh_hidden:
            _feh_show(track_info=track_info, jacket_path=jacket_path)

        # now-playing サーバー用に image_path も更新
        if hasattr(_qji, 'current_image_path'):
            _qji.current_image_path = jacket_path

        # ターミナル ANSI オーバーレイ（Qji の start_info_display）
        start_fn = getattr(_qji, 'start_info_display', None)
        if start_fn and not getattr(_qji, 'info_display_active', False):
            start_fn()

        # i キー再表示用に state へ保存
        state['current_track_info']  = track_info
        state['current_jacket_path'] = jacket_path

    except Exception:
        pass

def _clear_now_playing():
    """再生終了時: 情報クリア + feh 終了 + ANSI オーバーレイ停止"""
    global _feh_proc
    # feh を終了
    if _feh_proc and _feh_proc.poll() is None:
        try:
            _feh_proc.terminate(); _feh_proc.wait(timeout=2)
        except Exception:
            pass
        _feh_proc = None
    try:
        import __main__ as _qji
        # ANSI オーバーレイ停止
        stop_fn = getattr(_qji, 'stop_info_display', None)
        if stop_fn and getattr(_qji, 'info_display_active', False):
            stop_fn()
        # 情報クリア
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
# 音響設定保存（qji.py の save_current_preset を利用）
# ═══════════════════════════════════════════════════════════════════════════

def _save_to_favorites(state: dict):
    """
    再生中に s キーで呼ぶ。
    現在再生中の曲情報（state['current_album'] / state['current_track'] /
    state['current_playlist_id']）と その時点の音響設定を番号付きで保存する。

    重複キー方式:
      アルバム  → album_id   = Qobuz アルバム ID（数字文字列）
      プレイリスト → album_id = 'pl:' + Qobuz プレイリスト ID
      単曲      → album_id   = ''（重複チェックなし、毎回追加）
    """
    album   = state.get('current_album')
    track   = state.get('current_track')
    url     = state.get('current_url', '')
    pl_id   = state.get('current_playlist_id', '')
    pl_name = state.get('current_playlist_name', '')

    if not album and not track and not pl_id:
        print('  ⚠ No track info available to save')
        return

    favs = _load_favorites()

    # ── ユニークキー決定 ──────────────────────────────────────────────────
    if not album and pl_id:
        key = f'pl:{pl_id}'             # プレイリスト合成キー
    elif album:
        key = str(album.get('id', ''))  # アルバム ID
    else:
        key = ''                         # 単曲は重複チェックなし

    # ── 既存エントリ検索（キーが空でなければ） ───────────────────────────
    existing_idx = None
    if key:
        for i, f in enumerate(favs):
            if f.get('album_id') == key:
                existing_idx = i
                break

    # ── 連番決定（上書き時は既存 no を継承、新規は最大値+1） ──────────────
    if existing_idx is not None:
        next_no = favs[existing_idx].get('no', 1)
    else:
        existing_nos = [f.get('no', 0) for f in favs if isinstance(f.get('no'), int)]
        next_no      = (max(existing_nos) + 1) if existing_nos else 1

    audio_settings = {
        'gain_preset':   state.get('gain_preset', 'classical'),
        'filter_preset': state.get('filter_preset', 'musikverein'),
        'air_layer':     state.get('air_layer', True),
        'musikverein':   state.get('musikverein', True),
        'volume':        state.get('volume', 12),
        'echo_mode':     state.get('echo_mode', 'classical'),
        'tinnitus':      state.get('tinnitus', False),
        'gapless':       state.get('gapless', False),
    }

    if not album and pl_id:
        # ── プレイリスト保存 ─────────────────────────────────────────────
        cover = track.get('cover_url', '') if track else ''
        entry = {
            'no':           next_no,
            'album_id':     key,            # 'pl:XXXX'
            'playlist_id':  pl_id,
            'entry_type':   'playlist',
            'title':        pl_name or (track.get('album', '') if track else ''),
            'artist':       '',
            'cover_url':    cover,
            'source_url':   url,
            'saved_at':     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'audio_settings': audio_settings,
        }
    elif album:
        # ── アルバム保存 ─────────────────────────────────────────────────
        entry = {
            'no':           next_no,
            'album_id':     key,
            'title':        album.get('title', ''),
            'artist':       (album.get('artist') or {}).get('name', ''),
            'cover_url':    ((album.get('image') or {}).get('large') or
                             (album.get('image') or {}).get('small') or ''),
            'source_url':   url,
            'saved_at':     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'audio_settings': audio_settings,
        }
    else:
        # ── 単曲保存 ─────────────────────────────────────────────────────
        entry = {
            'no':           next_no,
            'album_id':     '',
            'title':        track.get('title', ''),
            'artist':       track.get('artist', ''),
            'cover_url':    track.get('cover_url', ''),
            'source_url':   url,
            'saved_at':     datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'audio_settings': audio_settings,
        }

    if existing_idx is not None:
        favs[existing_idx] = entry
        action = '🔄 上書き'
    else:
        favs.append(entry)
        action = '✅ 新規保存'

    _save_favorites(favs)
    preset_lbl = FILTER_PRESET_LABELS.get(audio_settings['filter_preset'], '')
    gain_db    = GAIN_PRESETS_DB.get(audio_settings['gain_preset'], 0.0)
    apl        = '🌿 ON' if audio_settings['air_layer'] else 'OFF'
    type_lbl   = '🎵 PL' if entry.get('entry_type') == 'playlist' else '📀'
    print(f'  {action} [{next_no:03d}] {type_lbl}"{entry["title"]}" saved')
    print(f'       Audio: {preset_lbl}  Gain:{gain_db:+.1f}dB  APL:{apl}  Vol:{audio_settings["volume"]}dB')


# ═══════════════════════════════════════════════════════════════════════════
# ジャンル自動検出 → 音響プリセット自動適用
# ═══════════════════════════════════════════════════════════════════════════

def _auto_detect_preset(album: dict) -> Optional[str]:
    """
    Qobuz のアルバム情報（ジャンル・タイトル・アーティスト名）を
    キーワードスコアリングで解析し、最適な音響プリセット名を返す。
    判定できない場合は None を返す。
    """
    genre_obj  = album.get('genre') or {}
    genre_name = ''
    if isinstance(genre_obj, dict):
        genre_name = (genre_obj.get('name') or genre_obj.get('slug') or '').lower()
    elif isinstance(genre_obj, str):
        genre_name = genre_obj.lower()

    title  = (album.get('title')  or '').lower()
    artist = ((album.get('artist') or {}).get('name') or '').lower()
    text   = f'{genre_name} {title} {artist}'

    KEYWORDS: Dict[str, List[str]] = {
        'jazz': [
            'jazz', 'blues', 'swing', 'bebop', 'bop', 'fusion',
            'improvis', 'ジャズ', 'ブルース', 'ragtime',
        ],
        'piano': [
            'piano recital', 'recital', 'solo recital',
            'piano solo', 'solo piano', 'am klavier', 'au piano',
            'piano sonata', 'klaviersonate', 'sonata for piano',
            'nocturne', 'nocturnes', 'étude', 'etude', 'etudes',
            'ballade', 'ballades', 'prélude', 'prelude', 'preludes',
            'impromptu', 'mazurka', 'waltz', 'waltzes',
            'ピアノ独奏', 'ピアノソナタ', 'ピアノリサイタル',
        ],
        'chamber': [
            'quartet', 'quintet', 'trio', 'duo', 'sextet', 'octet',
            'chamber music', 'kammermusik',
            'string quartet', 'piano trio', 'violin sonata', 'cello sonata',
            '弦楽四重奏', '室内楽', 'divertimento',
        ],
        'vocal': [
            'opera', 'lieder', 'lied', 'song cycle', 'mélodies',
            'cantata', 'cantate', 'oratorio', 'requiem',
            'mass', 'messe', 'choral', 'choir', 'chorus',
            'soprano', 'mezzo', 'tenor', 'baritone',
            'vocal', 'オペラ', '声楽', '歌曲', '合唱', 'カンタータ',
        ],
        'musikverein': [
            'symphony', 'symphonie', 'sinfonie', 'sinfonia',
            'concerto', 'orchestra', 'orchestre', 'philharmonic', 'philharmoniker',
            'overture', 'ouverture', 'symphonic', 'tone poem',
            '交響曲', '管弦楽', 'オーケストラ', '協奏曲',
        ],
    }

    scores: Dict[str, int] = {k: 0 for k in KEYWORDS}
    for preset, kws in KEYWORDS.items():
        for kw in kws:
            if kw in text:
                scores[preset] += 1

    # ジャンル名直接マッチ時は優先度ブースト
    GENRE_MAP = {
        'jazz': 'jazz',       'blues': 'jazz',
        'classical': 'musikverein', 'classique': 'musikverein',
        'symphon': 'musikverein',
        'chamber': 'chamber', 'contemporary': 'chamber',
        'opera': 'vocal',     'vocal': 'vocal',  'choral': 'vocal',
        'piano': 'piano',     'keyboard': 'piano', 'klavier': 'piano',
    }
    for slug, preset in GENRE_MAP.items():
        if slug in genre_name:
            scores[preset] += 2

    # タイトルに「concerto」があればピアノスコアを相殺（ピアノ協奏曲 → musikverein 優先）
    if 'concerto' in text or '協奏曲' in text:
        scores['piano'] = max(0, scores['piano'] - 2)

    # 「recital」はほぼ確実にソロ演奏 → piano に追加ブースト
    if 'recital' in text:
        scores['piano'] += 1

    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else None


def _apply_auto_preset(album: Optional[dict], state: dict) -> bool:
    """
    アルバム情報から音響プリセットを自動検出して state に適用する。
    manual_preset_locked が True の場合は何もしない。
    戻り値: プリセットを変更した場合 True
    """
    if state.get('manual_preset_locked', False):
        return False
    if not album:
        return False
    detected = _auto_detect_preset(album)
    if not detected:
        return False
    prev    = state.get('filter_preset', 'musikverein')
    state['filter_preset'] = detected
    label   = FILTER_PRESET_LABELS.get(detected, detected)
    if detected != prev:
        print(f'  🤖 Auto-detected genre → applying {label}')
    else:
        print(f'  🤖 Auto-detected genre → {label} (no change)')
    return detected != prev


# ═══════════════════════════════════════════════════════════════════════════
# お気に入り
# ═══════════════════════════════════════════════════════════════════════════

def _load_favorites() -> List[dict]:
    try:
        return json.loads(FAVORITES_PATH.read_text()) if FAVORITES_PATH.exists() else []
    except Exception:
        return []

def _save_favorites(favs: List[dict]):
    FAVORITES_PATH.parent.mkdir(parents=True, exist_ok=True)
    FAVORITES_PATH.write_text(json.dumps(favs, ensure_ascii=False, indent=2))

def _add_favorite(url: str, album: dict, state: dict = None):
    """アルバムをお気に入りに追加。state を渡すと音響設定も一緒に保存。"""
    favs     = _load_favorites()
    album_id = str(album.get('id', ''))
    existing_idx = next((i for i, f in enumerate(favs)
                         if f.get('album_id') == album_id), None)

    entry = {
        'album_id':   album_id,
        'title':      album.get('title', ''),
        'artist':     (album.get('artist') or {}).get('name', ''),
        'cover_url':  ((album.get('image') or {}).get('large') or
                       (album.get('image') or {}).get('small') or ''),
        'source_url': url,
        'added_at':   datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }

    # 音響設定を保存
    if state:
        entry['audio_settings'] = {
            'gain_preset':   state.get('gain_preset', 'classical'),
            'filter_preset': state.get('filter_preset', 'musikverein'),
            'air_layer':     state.get('air_layer', True),
            'musikverein':   state.get('musikverein', True),
            'volume':        state.get('volume', 12),
            'echo_mode':     state.get('echo_mode', 'classical'),
            'tinnitus':      state.get('tinnitus', False),
        }
        print(f'  💾 Audio settings also saved: {FILTER_PRESET_LABELS.get(state.get("filter_preset",""), "")}')

    if existing_idx is not None:
        # 既存エントリを更新（音響設定のみ上書き）
        favs[existing_idx].update(entry)
        print(f'  ⭐ Favourites updated ({len(favs)})')
    else:
        favs.append(entry)
        print(f'  ⭐ Added to favourites (total: {len(favs)})')
    _save_favorites(favs)


# ═══════════════════════════════════════════════════════════════════════════
# 再生エンジン
# ═══════════════════════════════════════════════════════════════════════════

_stop_flag   = False
_next_flag   = False
_prev_flag   = False
_replay_flag = False
# キーリスナースレッドへの「今すぐ終了」シグナル。
# _cleanup() が flags を False にリセットしてもリスナーが動き続けないようにする。
_kt_stop     = threading.Event()
_procs: dict = {}

# ── ギャップレス再生用グローバル ──────────────────────────────────────────
# 設計:
#   ffmpeg は通常と同じ -f wav 出力を使う。
#   フォワーダー Thread が ffmpeg stdout を読み、aplay パイプへ書き込む。
#   1曲目 → WAV ヘッダごと転送（aplay がフォーマットを自動検出）
#   2曲目以降 → WAV ヘッダをスキップし PCM データのみ転送（aplay は読み続ける）
#   → raw PCM モード不要 / チャンネル数・フォーマット指定ミスが起こらない
_gapless_aplay_proc:  Optional[subprocess.Popen] = None
_gapless_pipe_w:      Optional[int]               = None   # aplay stdin パイプの書き込み端
_gapless_current_sr:  Optional[int]               = None   # 現在の aplay のサンプルレート
_gapless_track_count: int                         = 0      # 現 aplay インスタンスで再生した曲数

# ── 先読みバッファ ────────────────────────────────────────────────────────
# 次曲の PCM を現曲の再生中にバックグラウンドで先読みし、
# 曲間の ffmpeg 起動待ち時間をゼロにしてギャップを排除する。
#
#   _pb_next : 次曲を蓄積中の先読みスロット（_play_one_gapless が go シグナル前に使う）
#   _pb_cur  : 現在 aplay パイプへ書き込み中の先読みスロット（go シグナル後）
#
# 各スロットは以下のキーを持つ dict:
#   ffmpeg / thread / go / done / buf / wav_hdr / tid / sr / fkey / pipe_w / inc_hdr / abort
PRE_BUFFER_BYTES: int       = 3 * 1024 * 1024   # 3 MB ≈ 4〜8 秒（SR により異なる）
_pb_next: Optional[dict]    = None
_pb_cur:  Optional[dict]    = None


def _cleanup():
    for proc in _procs.values():
        if proc and proc.poll() is None:
            try:
                proc.terminate(); proc.wait(timeout=2)
            except Exception:
                pass


def _gapless_teardown():
    """ギャップレス用 aplay を停止してパイプを閉じる。"""
    global _gapless_aplay_proc, _gapless_pipe_w, _gapless_current_sr, _gapless_track_count
    if _gapless_pipe_w is not None:
        try:
            os.close(_gapless_pipe_w)
        except OSError:
            pass
        _gapless_pipe_w = None
    if _gapless_aplay_proc and _gapless_aplay_proc.poll() is None:
        try:
            _gapless_aplay_proc.terminate()
            _gapless_aplay_proc.wait(timeout=3)
        except Exception:
            pass
    _gapless_aplay_proc  = None
    _gapless_current_sr  = None
    _gapless_track_count = 0


def _gapless_ensure_aplay(sample_rate: int, output_device: str) -> bool:
    """
    ギャップレスモード用: 正しいサンプルレートの aplay が動いていなければ起動する。
    aplay は WAV モードで起動し、1曲目の WAV ヘッダからフォーマットを自動検出する。
    これにより raw PCM モードでのチャンネル数・フォーマット指定ミスを回避する。

    戻り値: True = (再)起動した / False = 既存プロセスを流用（ギャップなし）
    """
    global _gapless_aplay_proc, _gapless_pipe_w, _gapless_current_sr, _gapless_track_count

    if (_gapless_aplay_proc is not None
            and _gapless_aplay_proc.poll() is None
            and _gapless_current_sr == sample_rate
            and _gapless_pipe_w is not None):
        return False   # 既存プロセスを流用

    _gapless_teardown()   # 既存を停止

    pipe_r, pipe_w = os.pipe()

    # WAV モードで起動: フォーマットは 1曲目の WAV ヘッダから自動取得
    # (-t raw / -f S32_LE / -c 2 は不要 → フォーマット不一致を防ぐ)
    if output_device == 'bluealsa':
        cmd_ap = ['aplay', '-D', 'bluealsa',
                  '--buffer-size=262144', '--period-size=32768']
    else:
        cmd_ap = ['aplay', '-D', output_device,
                  '--buffer-size=262144', '--period-size=32768']

    _gapless_aplay_proc  = subprocess.Popen(cmd_ap, stdin=pipe_r, stderr=subprocess.DEVNULL)
    os.close(pipe_r)          # 読み取り端は aplay のみが使う
    _gapless_pipe_w      = pipe_w
    _gapless_current_sr  = sample_rate
    _gapless_track_count = 0
    return True


def _read_wav_header(src) -> bytes:
    """
    ファイルオブジェクト src から WAV ヘッダを読み込み、
    src を PCM データ先頭まで進める。ヘッダバイト列を返す。

    WAV 構造: RIFF(12) → チャンク列(fmt/fact/LIST…data) → PCM data
    'data' チャンクヘッダ(8バイト)の直後が PCM 先頭。
    ヘッダを保存しておくことで、サンプルレート変更時に新しい aplay へ
    WAV ヘッダを再送できる。
    """
    hdr  = b''
    riff = src.read(12)
    if len(riff) < 12 or riff[:4] != b'RIFF' or riff[8:12] != b'WAVE':
        return riff   # WAV でない場合はそのまま返す（PCM 先頭と見なす）
    hdr += riff
    while True:
        ch = src.read(8)
        if len(ch) < 8:
            hdr += ch
            break
        hdr += ch
        cid  = ch[:4]
        clen = int.from_bytes(ch[4:8], 'little')
        if cid == b'data':
            break                              # PCM データ先頭に到達
        hdr += src.read(clen + (clen % 2))    # fmt / fact / LIST etc. をスキップ
    return hdr


def _wav_to_pipe(p_ff: subprocess.Popen, pipe_w_fd: int,
                 is_first: bool, done_event: threading.Event):
    """
    ffmpeg stdout (WAV ストリーム) を aplay パイプへ転送するフォワーダー Thread。

    is_first=True  → WAV ヘッダを aplay へ送り ALSA を設定した後、
                     先頭 PCM を PRE_FILL_BYTES 分だけバッファしてから
                     一括書き込みする（初頭 xrun 防止）。
    is_first=False → WAV ヘッダをスキップし PCM データのみ転送。
                     （先読みバッファがすでに十分なデータを持っているため不要）
    """
    CHUNK         = 65536
    PRE_FILL_BYTES = 65536   # 64 KB ≈ 83 ms @96kHz / 181 ms @44.1kHz
    src = p_ff.stdout
    try:
        if not is_first:
            _read_wav_header(src)   # WAV ヘッダをスキップ（戻り値は不要）

        else:
            # ── WAV ヘッダを読んで aplay へ転送（ALSA デバイスを設定させる） ──
            hdr = _read_wav_header(src)
            if hdr:
                os.write(pipe_w_fd, hdr)

            # ── PCM 先頭を PRE_FILL_BYTES 分バッファしてから一括書き込み ──────
            # aplay が ALSA リングバッファへ書き始める前に十分なデータを注入し、
            # ffmpeg の初期化遅延による xrun（冒頭スタッタ）を防ぐ。
            pre_buf = bytearray()
            while len(pre_buf) < PRE_FILL_BYTES:
                n    = min(CHUNK, PRE_FILL_BYTES - len(pre_buf))
                data = src.read(n)
                if not data:
                    break
                pre_buf.extend(data)
            if pre_buf:
                os.write(pipe_w_fd, bytes(pre_buf))

        # ── 残りをリアルタイムストリーミング ──────────────────────────────
        while True:
            data = src.read(CHUNK)
            if not data:
                break
            os.write(pipe_w_fd, data)

    except (OSError, BrokenPipeError):
        pass   # aplay パイプが閉じられた (teardown 等) → 正常終了
    finally:
        done_event.set()


# ═══════════════════════════════════════════════════════════════════════════
# 先読みバッファ (Pre-buffer) システム
# ═══════════════════════════════════════════════════════════════════════════

def _fa_key(fa: list) -> str:
    """filter_args リストをハッシュ化してキー文字列にする（変更検出用）。"""
    return hashlib.md5(str(fa).encode()).hexdigest()


def _pb_run(sd: dict, url: str, sr: int, fa: list):
    """
    先読みバッファスレッドの本体。

    1. ffmpeg を起動して WAV ヘッダを読み込む（保存しておく）
    2. PCM データを PRE_BUFFER_BYTES まで sd['buf'] に蓄積
    3. go イベントを待つ
    4. go 後: ヘッダ（必要なら）＋バッファ＋残りストリームを aplay パイプへ書く
    """
    CHUNK = 65536
    cmd_ff = (
        ['ffmpeg', '-loglevel', 'error',
         '-fflags', 'nobuffer',
         '-reconnect',                  '1',
         '-reconnect_streamed',         '1',
         '-reconnect_on_network_error', '1',
         '-reconnect_delay_max',        '5',
         '-i', url,
         '-vn', '-ar', str(48000 if _is_dsp_mode_active() else sr), '-acodec', 'pcm_s32le']
        + fa + ['-f', 'wav', '-']
    )
    p = None
    try:
        p = subprocess.Popen(cmd_ff, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        sd['ffmpeg'] = p

        # WAV ヘッダを読み込んで保存（SR 変更時に新 aplay へ再送するため）
        sd['wav_hdr'] = _read_wav_header(p.stdout)

        # PCM を PRE_BUFFER_BYTES まで蓄積（go シグナルが来たら蓄積を中断して進む）
        while len(sd['buf']) < PRE_BUFFER_BYTES:
            if sd.get('abort') or sd['go'].is_set():
                break
            data = p.stdout.read(CHUNK)
            if not data:
                break
            sd['buf'].extend(data)

        # go シグナル待ち
        sd['go'].wait()

        if sd.get('abort'):
            return

        pw = sd.get('pipe_w')
        if pw is None:
            return

        # SR 変更で aplay が再起動した場合は WAV ヘッダを先送り
        if sd.get('inc_hdr') and sd['wav_hdr']:
            os.write(pw, sd['wav_hdr'])

        # 蓄積済みバッファを送出
        buf_bytes = bytes(sd['buf'])
        sd['buf'] = bytearray()          # メモリ解放
        if buf_bytes:
            os.write(pw, buf_bytes)

        # ffmpeg の残りをストリーミング
        while True:
            if sd.get('abort'):
                break
            try:
                data = p.stdout.read(CHUNK)
            except Exception:
                break
            if not data:
                break
            os.write(pw, data)

    except (OSError, BrokenPipeError):
        pass
    except Exception:
        pass
    finally:
        if p and p.poll() is None:
            try:
                p.kill(); p.wait()
            except Exception:
                pass
        sd['done'].set()


def _pb_start_next(url: str, sr: int, fa: list, tid: str):
    """次曲の先読みを開始する。既存の _pb_next は中断してから起動する。"""
    global _pb_next
    if _pb_next:
        if _pb_next.get('tid') == tid:
            return   # 同じ曲を既に先読み中 → スキップ
        _pb_abort_next()

    sd: dict = {
        'ffmpeg':  None,
        'thread':  None,
        'go':      threading.Event(),
        'done':    threading.Event(),
        'buf':     bytearray(),
        'wav_hdr': b'',
        'tid':     tid,
        'sr':      sr,
        'fkey':    _fa_key(fa),
        'pipe_w':  None,
        'inc_hdr': False,
        'abort':   False,
    }
    _pb_next = sd
    t = threading.Thread(target=_pb_run, args=(sd, url, sr, fa), daemon=True)
    sd['thread'] = t
    t.start()


def _pb_signal_go(pipe_w: int, include_header: bool = False):
    """
    _pb_next へ go シグナルを送り _pb_cur へ昇格させる。
    以降 _pb_next スロットは空になり、次の曲の先読みを受け付ける。
    """
    global _pb_next, _pb_cur
    if _pb_next is None:
        return
    sd       = _pb_next
    _pb_next = None          # スロット解放 → 次の _pb_start_next を受け付け可能に
    _pb_cur  = sd
    sd['pipe_w']  = pipe_w
    sd['inc_hdr'] = include_header
    sd['go'].set()           # スレッドを起こす


def _pb_abort_next():
    """_pb_next の先読みを中断・クリアする。"""
    global _pb_next
    if _pb_next is None:
        return
    sd       = _pb_next
    _pb_next = None
    sd['abort'] = True
    sd['go'].set()           # wait() をアンブロック
    ff = sd.get('ffmpeg')
    if ff and ff.poll() is None:
        try: ff.kill()
        except Exception: pass
    sd['done'].wait(timeout=3)


def _pb_abort_all():
    """_pb_next と _pb_cur を両方中断・クリアする（teardown 時に呼ぶ）。"""
    global _pb_cur
    _pb_abort_next()
    if _pb_cur:
        sd, _pb_cur = _pb_cur, None
        sd['abort'] = True
        ff = sd.get('ffmpeg')
        if ff and ff.poll() is None:
            try: ff.kill()
            except Exception: pass
        sd['done'].wait(timeout=3)


def _key_listener_thread(state: dict, fd_orig_settings):
    """
    再生中キー入力スレッド。
    fd_orig_settings: tty.setraw() 前の termios 設定（restore 用）
    """
    global _stop_flag, _next_flag, _prev_flag, _replay_flag

    fd = sys.stdin.fileno()
    try:
        tty.setraw(fd)
        # ★ _kt_stop.is_set() を条件に追加。
        # _cleanup() が flags を False にリセットしても、_kt_stop が set されていれば即終了。
        # これがなかったため、cleanup 後もリスナーが sys.stdin を読み続け
        # qji.py の input() がフリーズする原因になっていた。
        while not _kt_stop.is_set() and not (_stop_flag or _next_flag or _prev_flag or _replay_flag):
            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch = sys.stdin.read(1)
                cl = ch.lower()

                # ESC キー検出（\x1b）
                if ch == '\x1b':
                    _feh_hide()
                    sys.stdout.write('\r  🖼 Image hidden ([i] to show again)        \n')
                    sys.stdout.flush()
                    continue

                if cl == 'q':
                    _stop_flag = True

                elif cl == 'n':
                    _next_flag = True

                elif cl == 'b':
                    _prev_flag = True

                elif cl == 'i':
                    # ジャケット画像を再表示
                    _feh_show(
                        track_info=state.get('current_track_info'),
                        jacket_path=state.get('current_jacket_path'),
                    )

                elif cl in ('+', '='):
                    state['volume'] = min(state.get('volume', 12) + 1, 30)
                    _replay_flag = True
                    termios.tcsetattr(fd, termios.TCSANOW, fd_orig_settings)
                    print(f'\r  🔊 Volume: {state["volume"]}dB  (from next track)        ', flush=True)
                    tty.setraw(fd)

                elif cl == '-':
                    state['volume'] = max(state.get('volume', 12) - 1, -40)
                    _replay_flag = True
                    termios.tcsetattr(fd, termios.TCSANOW, fd_orig_settings)
                    print(f'\r  🔉 Volume: {state["volume"]}dB  (from next track)        ', flush=True)
                    tty.setraw(fd)

                elif ch == 'G':   # 大文字 G = ギャップレス ON/OFF
                    state['gapless'] = not state.get('gapless', False)
                    termios.tcsetattr(fd, termios.TCSANOW, fd_orig_settings)
                    s = 'ON ✅' if state['gapless'] else 'OFF'
                    print(f'\r  🎵 Gapless: {s}  (from next gap)        ', flush=True)
                    tty.setraw(fd)

                elif cl == 'g':
                    cur = state.get('gain_preset', 'classical')
                    idx = GAIN_ORDER.index(cur) if cur in GAIN_ORDER else 0
                    nxt = GAIN_ORDER[(idx + 1) % len(GAIN_ORDER)]
                    state['gain_preset'] = nxt
                    _replay_flag = True
                    termios.tcsetattr(fd, termios.TCSANOW, fd_orig_settings)
                    db  = GAIN_PRESETS_DB.get(nxt, 0.0)
                    print(f'\r  🎚️  Gain: {nxt} ({db:+.1f}dB)  (from next track)        ', flush=True)
                    tty.setraw(fd)

                elif cl == 'w':
                    state['air_layer'] = not state.get('air_layer', True)
                    _replay_flag = True
                    termios.tcsetattr(fd, termios.TCSANOW, fd_orig_settings)
                    s = '🌿 ON' if state['air_layer'] else 'OFF'
                    print(f'\r  🌿 Air Particle Layer: {s}  (from next track)        ', flush=True)
                    tty.setraw(fd)

                elif cl == 'c':
                    # プリセット選択メニュー
                    # /dev/tty 経由で入力を受け取る（sys.stdin の raw モードに干渉しない）
                    # _qobuz_readline() は内部で stty sane + /dev/tty readline を行う
                    print('\n')
                    print('  ┌─────────────────────────────────────┐')
                    print('  │  🎼 Audio preset selection           │')
                    print('  ├─────────────────────────────────────┤')
                    cur = state.get('filter_preset', 'musikverein')
                    for ki, pk in enumerate(FILTER_PRESETS, 1):
                        mark  = ' ◀' if pk == cur else '  '
                        label = FILTER_PRESET_LABELS.get(pk, pk)
                        print(f'  │  {ki}: {label:<28}{mark}│')
                    print('  │  Enter: Cancel                      │')
                    print('  └─────────────────────────────────────┘')
                    # ★ Fix: sys.stdin.readline() → _qobuz_readline() で /dev/tty を直接使う
                    sel = _qobuz_readline(f'  選択 (1-{len(FILTER_PRESETS)}): ')
                    # readline 後に raw モードを確実に復元
                    try:
                        termios.tcflush(fd, termios.TCIFLUSH)
                        tty.setraw(fd)
                    except Exception:
                        pass
                    if sel.isdigit() and 1 <= int(sel) <= len(FILTER_PRESETS):
                        nxt = FILTER_PRESETS[int(sel) - 1]
                        state['filter_preset'] = nxt
                        state['manual_preset_locked'] = True   # 手動選択 → 自動検出をロック
                        _replay_flag = True
                        label = FILTER_PRESET_LABELS.get(nxt, nxt)
                        print(f'  ✓ Changed to {label} (from next track)')
                    else:
                        print('  (Cancelled)')
                    if _replay_flag:
                        return          # スレッド終了 → _play_one が replay を返す
                    # キャンセル時は _qobuz_readline() の後ですでに raw 復元済み → 何もしない

                elif cl == 's':
                    # 曲情報＋音響設定を番号付きでお気に入りに保存
                    termios.tcsetattr(fd, termios.TCSANOW, fd_orig_settings)
                    try:
                        termios.tcflush(fd, termios.TCIFLUSH)
                    except Exception:
                        pass
                    _save_to_favorites(state)
                    tty.setraw(fd)

                elif cl == 'a':
                    # 自動ジャンル検出モード（手動ロックを解除して再適用）
                    try:
                        termios.tcsetattr(fd, termios.TCSAFLUSH, fd_orig_settings)
                    except Exception:
                        pass
                    state['manual_preset_locked'] = False
                    changed = _apply_auto_preset(state.get('current_album'), state)
                    if changed:
                        _replay_flag = True
                        return   # スレッド終了 → _play_one が replay を返す
                    else:
                        try:
                            termios.tcflush(fd, termios.TCIFLUSH)
                            tty.setraw(fd)
                        except Exception:
                            pass

    except Exception:
        pass
    finally:
        # 必ず元の設定に戻す（raw モード解除）
        # TCSAFLUSH: 送信バッファの出力完了を待ってから設定を変更し、受信バッファを破棄
        # → raw モード中の残留バイト（矢印キー等のエスケープシーケンス）を確実に除去する
        try:
            termios.tcsetattr(fd, termios.TCSAFLUSH, fd_orig_settings)
        except Exception:
            pass
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except Exception:
            pass


def _play_one(stream_url: str, sample_rate: int,
              filter_args: list, output_device: str,
              state: dict) -> str:
    """
    1ストリームを再生。
    戻り値: 'finished' | 'next' | 'prev' | 'quit' | 'replay'
    """
    global _stop_flag, _next_flag, _prev_flag, _replay_flag, _procs
    _stop_flag = _next_flag = _prev_flag = _replay_flag = False
    _kt_stop.clear()   # ★ 新しいキーリスナー起動前にリセット ──────────────────────
    # -reconnect / -reconnect_streamed: HTTP切断時に自動再接続
    # -reconnect_on_network_error: TCP/TLSエラーでも再試行
    # -reconnect_delay_max 5: 最大5秒待って再接続（デフォルト120秒から短縮）
    # ※ これら4つは ffmpeg 6.x で確実にサポートされているオプション
    cmd_ff = (
        ['ffmpeg', '-loglevel', 'error',
         '-fflags', 'nobuffer',
         '-reconnect',                  '1',
         '-reconnect_streamed',         '1',
         '-reconnect_on_network_error', '1',
         '-reconnect_delay_max',        '5',
         '-i', stream_url,
         '-vn', '-ar', str(48000 if _is_dsp_mode_active() else sample_rate), '-acodec', 'pcm_s32le']
        + filter_args
        + ['-f', 'wav', '-']
    )

    if output_device == 'bluealsa':
        cmd_ap = ['aplay', '-D', 'bluealsa',
                  '--buffer-size=262144', '--period-size=32768']
    else:
        cmd_ap = ['aplay', '-D', output_device, '-r', str(48000 if _is_dsp_mode_active() else sample_rate),
                  '--buffer-size=262144', '--period-size=32768']

    # ターミナル設定を保存してから raw モードへ
    fd = sys.stdin.fileno()
    try:
        orig_settings = termios.tcgetattr(fd)
    except Exception:
        orig_settings = None
    restore_settings = _baseline_term if _baseline_term is not None else orig_settings
    _save_terminal()

    try:
        p_ff = subprocess.Popen(cmd_ff, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        p_ap = subprocess.Popen(cmd_ap, stdin=p_ff.stdout,      stderr=subprocess.DEVNULL)
        p_ff.stdout.close()
        _procs = {'ffmpeg': p_ff, 'aplay': p_ap}

        kt = threading.Thread(
            target=_key_listener_thread,
            args=(state, restore_settings),
            daemon=True
        )
        kt.start()

        while p_ap.poll() is None:
            time.sleep(0.1)
            if _stop_flag or _next_flag or _prev_flag or _replay_flag:
                break

        _kt_stop.set()   # ★ キーリスナーへ「今すぐ終了」を通知してから cleanup
        _cleanup()
        # キーリスナー終了を待つ（raw モードを抜けるまで待機）
        kt.join(timeout=2.0)
        # タイムアウト後もスレッドが生きている場合に備えて termios を強制復元
        if kt.is_alive():
            try:
                if orig_settings is not None:
                    termios.tcsetattr(fd, termios.TCSAFLUSH, orig_settings)
                termios.tcflush(fd, termios.TCIFLUSH)
            except Exception:
                pass
        time.sleep(0.4 if output_device == 'bluealsa' else 0.2)

        if _stop_flag:   return 'quit'
        if _replay_flag: return 'replay'
        if _next_flag:   return 'next'
        if _prev_flag:   return 'prev'
        return 'finished'

    except Exception as e:
        print(f'\n  ⚠ Playback error: {e}')
        _cleanup()
        return 'finished'
    finally:
        # キーリスナーが終わってから確実に復元
        _restore_terminal()
        # 少し待って入力を受け付けられる状態にする
        time.sleep(0.1)


def _play_one_gapless(stream_url: str, sample_rate: int,
                      filter_args: list, output_device: str,
                      state: dict,
                      track_id: Optional[str] = None,
                      prebuf_info: Optional[tuple] = None) -> str:
    """
    ギャップレスモード用 _play_one。2 つの動作モードを持つ。

    【通常モード】(adopt_prebuf=False)
      ffmpeg → WAV → _wav_to_pipe Thread → aplay パイプ
      再生中に次曲の先読み (_pb_start_next) を開始。
      自然終了時に _pb_next へ go シグナル → _pb_cur へ昇格。

    【引き継ぎモード】(adopt_prebuf=True)
      _pb_cur（前曲の _pb_next が昇格したもの）がすでに書き込み中。
      ffmpeg / forwarder は _pb_cur のものを流用し、
      本関数はキーリスナーの管理のみ担う。→ 曲間ギャップなし ✅

    戻り値: 'finished' | 'next' | 'prev' | 'quit' | 'replay'
    """
    global _stop_flag, _next_flag, _prev_flag, _replay_flag, _procs
    global _gapless_track_count, _pb_cur, _pb_next

    _stop_flag = _next_flag = _prev_flag = _replay_flag = False
    _kt_stop.clear()   # ★ 新しいキーリスナー起動前にリセット

    # ── 引き継ぎモード判定 ────────────────────────────────────────────────
    adopt_prebuf = (
        _pb_cur is not None
        and track_id is not None
        and _pb_cur.get('tid') == track_id
        and not _pb_cur['done'].is_set()
    )

    # ── aplay / ffmpeg の準備 ─────────────────────────────────────────────
    if adopt_prebuf:
        # 前曲の _pb_next が昇格した先読みスレッドがすでに aplay へ書き込み中。
        # aplay のサンプルレート設定は go シグナル時に済んでいる。
        _gapless_track_count += 1
        p_ff     = _pb_cur.get('ffmpeg')    # 先読みスレッドが使っている ffmpeg
        fwd_done = _pb_cur['done']          # 先読みスレッドの完了イベント
        _procs   = {'ffmpeg': p_ff} if p_ff else {}
    else:
        # 通常フロー: aplay を確認・起動してから ffmpeg + forwarder を起動
        _gapless_ensure_aplay(sample_rate, output_device)
        is_first      = (_gapless_track_count == 0)
        pipe_w_fd     = _gapless_pipe_w
        _gapless_track_count += 1

        cmd_ff = (
            ['ffmpeg', '-loglevel', 'error',
             '-fflags', 'nobuffer',
             '-reconnect',                  '1',
             '-reconnect_streamed',         '1',
             '-reconnect_on_network_error', '1',
             '-reconnect_delay_max',        '5',
             '-i', stream_url,
             '-vn', '-ar', str(48000 if _is_dsp_mode_active() else sample_rate), '-acodec', 'pcm_s32le']
            + filter_args
            + ['-f', 'wav', '-']
        )
        p_ff = subprocess.Popen(cmd_ff, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        _procs = {'ffmpeg': p_ff}

        fwd_done = threading.Event()
        fwd = threading.Thread(
            target=_wav_to_pipe,
            args=(p_ff, pipe_w_fd, is_first, fwd_done),
            daemon=True
        )
        fwd.start()

    # ── 次曲の先読みを開始（現曲の再生中に蓄積）───────────────────────────
    if prebuf_info:
        _pb_start_next(*prebuf_info)

    # ── キーリスナー ─────────────────────────────────────────────────────
    fd = sys.stdin.fileno()
    try:
        orig_settings = termios.tcgetattr(fd)
    except Exception:
        orig_settings = None
    # ベースライン優先: _saved_term は raw モード中に汚染されうるため
    # 起動時に保存した _baseline_term を復元設定として使う
    restore_settings = _baseline_term if _baseline_term is not None else orig_settings
    _save_terminal()

    def _do_signal_go():
        """自然終了時: _pb_next へ go シグナルを送り次曲の先読みを aplay へ流す。"""
        if _pb_next is None:
            return
        need_restart = _gapless_ensure_aplay(_pb_next['sr'], output_device)
        _pb_signal_go(_gapless_pipe_w, include_header=need_restart)

    try:
        kt = threading.Thread(
            target=_key_listener_thread,
            args=(state, restore_settings),
            daemon=True
        )
        kt.start()

        # ffmpeg / 先読みスレッドの完了を待ちながらキーイベントを監視
        while not fwd_done.wait(timeout=0.05):
            if _stop_flag or _next_flag or _prev_flag or _replay_flag:
                if p_ff and p_ff.poll() is None:
                    p_ff.kill()
                    p_ff.wait()
                fwd_done.wait(timeout=3.0)
                break

        _kt_stop.set()   # ★ キーリスナーへ「今すぐ終了」を通知
        kt.join(timeout=2.0)
        if kt.is_alive():
            try:
                if orig_settings is not None:
                    termios.tcsetattr(fd, termios.TCSAFLUSH, orig_settings)
                termios.tcflush(fd, termios.TCIFLUSH)
            except Exception:
                pass

        # ── 結果処理 ─────────────────────────────────────────────────────
        # ① パイプを先に閉じる → _pb_run の os.write() ブロックが EPIPE で即解除
        # ② その後スレッド終了を待つ（パイプ閉じ後なので速やかに完了）
        if _stop_flag:
            _gapless_teardown()
            _pb_abort_all()
            return 'quit'

        if _prev_flag:
            _gapless_teardown()
            _pb_abort_all()
            return 'prev'

        if _replay_flag:
            _gapless_teardown()
            _pb_abort_all()
            return 'replay'

        if _next_flag:
            _pb_abort_next()
            _pb_cur = None   # この曲の先読みは中断済み
            return 'next'    # aplay は継続（わずかなクリックのみ）

        # 自然終了: 次曲の先読みへ go シグナルを送ってギャップをゼロにする
        _pb_cur = None
        _do_signal_go()      # _pb_next → _pb_cur へ昇格し aplay への書き込み開始
        return 'finished'

    except Exception as e:
        print(f'\n  ⚠ Gapless playback error: {e}')
        _gapless_teardown()
        _pb_abort_all()
        return 'finished'
    finally:
        _restore_terminal()
        time.sleep(0.05)


def _play_list(client: QobuzClient, tracks: List[dict],
               start: int, build_filter_func, state: dict):
    """トラックリストを順番に再生。次トラックの URL はバックグラウンドで先読みする。"""

    def _get_filter_args(sample_rate: int = 44100):
        import __main__ as _qji
        import re as _re
        gain_db = GAIN_PRESETS_DB.get(state.get('gain_preset', 'classical'), 0.0)
        _prev   = getattr(_qji, 'current_filter_preset', 'musikverein')
        _qji.current_filter_preset = state.get('filter_preset', 'musikverein')
        try:
            fa = build_filter_func(
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
            _qji.current_filter_preset = _prev

        # ── Qobuz ストリーミング向け高域平滑化補正 ─────────────────────────────
        # Qobuz ストリームは高域成分が豊富で、プリセット内の treble / highshelf
        # ブーストと相乗してローカル FLAC より「鋭く硬い」音になりやすい。
        # サンプリングレートに応じた穏やかな高域シェルフカットで、
        # ローカル再生に近いゆとりある質感に調整する。
        #
        # 方針:
        #   Hi-Res (96kHz / 192kHz): 8kHz 以上をより強めに平滑化
        #   CD 品質 (44.1 / 48kHz) : 10kHz 以上を控えめに平滑化
        #
        # 挿入位置: 最終 alimiter (limit=0.95) の直前
        # → ピークリミッターが最後に働くため平滑化フィルターが過負荷になる心配なし
        if sample_rate >= 96000:
            # Hi-Res: 8kHz 高域シェルフ -1.6dB + 12kHz 付近を追加整形
            softening = (
                'highshelf=f=8000:g=-2.0:w=0.5,'
                'equalizer=f=11000:t=q:w=2.0:g=-1.2'
            )
        else:
            # CD 品質: 10kHz 高域シェルフ -0.7dB（控えめ）
            softening = 'highshelf=f=10000:g=-0.7:w=0.55'

        if fa[0] == '-filter_complex':
            # APL ON (filter_complex): 末尾 [out] ラベルの直前に挿入
            # 末尾パターン: "release=数字[out]"  ← 最終 alimiter のみが [out] を持つ
            fa[1] = _re.sub(
                r'(release=\d+)\[out\]$',
                r'\1[_qsft];[_qsft]' + softening + '[out]',
                fa[1]
            )
        else:
            # -af モード: 最終 alimiter (limit=0.95) の直前に挿入
            fa[1] = _re.sub(
                r',alimiter=level_in=1\.0:level_out=1\.0:limit=0\.95',
                ',' + softening + r',alimiter=level_in=1.0:level_out=1.0:limit=0.95',
                fa[1]
            )

        # ★★★ ピークモニターGUI用ログ出力: Qobuz再生でも有効化 ★★★
        # （qji.py本体と同じ astats+ametadata パススルー方式）
        # ★★★ 修正: ギャップレス再生時は書き出さない ★★★
        # ギャップレスモードは複数曲分のffmpegプロセスが1本の常駐aplayへ
        # パイプで橋渡しする構造のため、曲ごとに個別のffmpegプロセスが
        # 短命に終了・交代する形になる。ログファイルもそれに合わせて
        # 頻繁に切り替わるため、モニターGUI側の「最新更新ファイルへの
        # 追従」がうまく噛み合わず、曲の途中で表示が止まる／消えるなど
        # 挙動が不安定になることが分かった。無理に追従させるより、
        # ギャップレス再生時は素直に非表示のままにする方が実用的。
        if not state.get('gapless', False):
            try:
                _qobuz_declip_log_path = f'/tmp/qji_declip_{os.getpid()}_{int(time.time()*1000)}.log'
                fa = _qji._wrap_filter_args_with_declip_monitor(fa, _qobuz_declip_log_path)
                # 溜まった古いログ（このプロセス分のみ）を軽く後始末。直近3件は再生中/直後の
                # 可能性があるため残し、それより古いものだけ削除する。
                _old_logs = sorted(
                    _glob.glob(f'/tmp/qji_declip_{os.getpid()}_*.log'),
                    key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                    reverse=True
                )
                for _old in _old_logs[3:]:
                    try:
                        os.remove(_old)
                    except Exception:
                        pass
            except Exception:
                pass

        return fa

    output_device = state.get('output_device', 'hw:0,0')
    cached_url: dict    = {}   # tid → (url, sr, bd)
    prefetch_busy: set  = set()  # 先読み中 tid（二重起動防止）

    def _prefetch(tid: str):
        if tid in cached_url or tid in prefetch_busy:
            return
        prefetch_busy.add(tid)
        try:
            result = client.get_file_url(tid)
            if result:
                cached_url[tid] = result
        except Exception:
            pass
        finally:
            prefetch_busy.discard(tid)

    def _start_prefetch(idx: int):
        """idx が有効なら先読みスレッドを起動する。"""
        if 0 <= idx < len(tracks):
            tid = tracks[idx]['track_id']
            if tid not in cached_url and tid not in prefetch_busy:
                threading.Thread(target=_prefetch, args=(tid,), daemon=True).start()

    i = start

    while 0 <= i < len(tracks):
        t = tracks[i]
        dur = ''
        if t.get('duration'):
            m, s = divmod(t['duration'], 60)
            dur = f' [{m}:{s:02d}]'
        print(f'\n  ♫  {t.get("artist", "")} — {t.get("title", "")}{dur}')
        gapless_ind = ' 🔗GL' if state.get('gapless') else ''
        q_hint = '[q]Next PL' if state.get('playlist_queue_mode') else '[q]Stop'
        print(f'     [n]Next [b]Prev {q_hint} [g]Gain [G]Gapless{gapless_ind} [w]APL [c]Preset [a]Auto [+/-]Vol [s]Save [i]Image [ESC]Stop')

        tid = t['track_id']
        if tid not in cached_url:
            print('  📡 Fetching stream URL...')
            result = client.get_file_url(tid)
            if not result:
                print('  ⚠ URL fetch failed — next.')
                i += 1; continue
            cached_url[tid] = result

        # ★ 現トラック再生開始直前に次トラックの URL を先読み（2段先まで）
        _start_prefetch(i + 1)
        _start_prefetch(i + 2)   # 2曲先も先に取得し、引き継ぎモード用 prebuf_info を確実に構築

        stream_url, sample_rate, bit_depth = cached_url[tid]
        fmt_name = {44100: 'CD 44.1kHz', 96000: 'Hi-Res 96kHz',
                    192000: 'Hi-Res 192kHz'}.get(sample_rate, f'{sample_rate//1000}kHz')
        state['fmt_name'] = fmt_name
        gain_db    = GAIN_PRESETS_DB.get(state.get('gain_preset', 'classical'), 0.0)
        apl        = '🌿' if state.get('air_layer', True) else '  '
        preset_lbl = FILTER_PRESET_LABELS.get(state.get('filter_preset', 'musikverein'), '')
        print(f'  📻 {fmt_name}/{bit_depth}bit  Vol:{state.get("volume",12)}dB'
              f'  Gain:{gain_db:+.1f}dB  APL:{apl}  {preset_lbl}')

        state['current_track'] = t
        _update_now_playing(t, state)

        filter_args = _get_filter_args(sample_rate)

        # ギャップレスモード: 次曲の先読み情報を構築（URL が先読み済みの場合のみ）
        prebuf_info = None
        if state.get('gapless') and i + 1 < len(tracks):
            next_tid = tracks[i + 1]['track_id']
            if next_tid in cached_url:
                nu, nsr, _ = cached_url[next_tid]
                # 次曲のサンプリングレートで高域平滑化を再計算する
                # （同一アルバムではほぼ同値だが、クロスアルバム時の整合性のため）
                next_filter_args = _get_filter_args(nsr)
                prebuf_info = (nu, nsr, next_filter_args, next_tid)

        if state.get('gapless'):
            outcome = _play_one_gapless(stream_url, sample_rate, filter_args, output_device, state,
                                        track_id=tid, prebuf_info=prebuf_info)
        else:
            outcome = _play_one(stream_url, sample_rate, filter_args, output_device, state)

        if outcome == 'quit':
            break
        elif outcome == 'replay':
            pass   # 同曲を新設定で再生（先読みキャッシュはそのまま）
        elif outcome in ('finished', 'next'):
            i += 1
            _start_prefetch(i + 1)  # さらに1つ先も先読み
        elif outcome == 'prev':
            i = max(0, i - 1)

    _clear_now_playing()
    # ギャップレスモードの後始末
    if state.get('gapless'):
        # ① まずパイプを閉じる → _pb_run の os.write() ブロックが EPIPE で解除される
        _gapless_teardown()
        # ② その後スレッド終了を待つ（パイプ閉じ後なので速やかに完了する）
        _pb_abort_all()
    # ターミナルを確実に正規化してからメニューへ戻る
    _restore_terminal()


# ═══════════════════════════════════════════════════════════════════════════
# 初回セットアップ
# ═══════════════════════════════════════════════════════════════════════════

def _setup_token(config: dict) -> bool:
    print("""
  ── Qobuz トークン設定 ──────────────────────────────
  1. Firefox/Chrome で https://play.qobuz.com にログイン
  2. F12 → ネットワーク → F5 リロード
  3. フィルター欄に「api.json」と入力
  4. リクエストヘッダーから:
       X-User-Auth-Token  （長い文字列）
       X-App-Id           （数字）
  ─────────────────────────────────────────────────────""")
    username   = input('  Email address (display only): ').strip()
    auth_token = input('  X-User-Auth-Token       : ').strip()
    app_id     = input('  X-App-Id                : ').strip()
    if not auth_token or not app_id:
        print('  ❌ Required fields are missing'); return False

    app_secret = ''
    if config.get('app_secret') and config.get('app_id') == app_id:
        app_secret = config['app_secret']
        print('  ✓ Reusing existing app_secret')
    else:
        print('  🔍 Fetching app_secret...')
        try:
            from qobuz_dl.bundle import Bundle
            secrets = list(Bundle().get_secrets().values())
            app_secret = secrets[-1] if secrets else ''
            print(f'  ✓ app_secret: {app_secret[:8]}...')
        except Exception:
            app_secret = input('  app_secret (manual entry): ').strip()

    config.update({'username': username, 'app_id': app_id,
                   'app_secret': app_secret, 'auth_token': auth_token})
    _save_config(config)

    client = QobuzClient(app_id, app_secret, auth_token)
    d = client._get('user/get')
    if d and 'id' in d:
        config['user_id'] = str(d['id']); _save_config(config)
        print(f'  ✓ Authenticated: {d.get("display_name", username)}')
    else:
        print('  ⚠ Could not verify (settings saved anyway)')
    return True


# ═══════════════════════════════════════════════════════════════════════════
# URL 再生 / お気に入りメニュー
# ═══════════════════════════════════════════════════════════════════════════

def _url_flow(client: QobuzClient, build_filter_func, state: dict):
    print('\n  🔗 Play from Qobuz URL  (open.qobuz.com / www.qobuz.com)')
    url = input('  URL (q=back): ').strip()
    if url.lower() in ('q', '') or 'qobuz.com' not in url:
        if 'qobuz.com' not in url and url.lower() not in ('q', ''):
            print('  ⚠ Please enter a Qobuz URL')
        return

    kind, qid = _parse_url(url)
    if not kind or not qid:
        print(f'  ❌ Could not parse URL'); return

    if kind == 'album':
        print('  📀 Fetching album info...')
        album = client.get_album(qid)
        if not album: print('  ❌ Fetch failed'); return
        tracks = _album_to_tracks(album)
        artist = (album.get('artist') or {}).get('name', '')
        print(f'\n  📀 {artist} — {album.get("title","")}  ({len(tracks)} tracks)')
        for i, t in enumerate(tracks, 1):
            dur = f' [{t["duration"]//60}:{t["duration"]%60:02d}]' if t.get('duration') else ''
            print(f'     {i:2d}. {t["title"]}{dur}')
        start_s = input(f'\n  Start from track? (1-{len(tracks)} / Enter=1): ').strip()
        try:    start = max(0, int(start_s) - 1)
        except: start = 0
        # 再生中の s キー保存のために情報を state に記憶
        state['current_album'] = album
        state['current_url']   = url
        # ジャンル自動検出（手動ロックがなければ適用）
        _apply_auto_preset(album, state)
        _play_list(client, tracks, start, build_filter_func, state)

    elif kind == 'track':
        print('  🎵 Fetching track info...')
        t_data = client.get_track(qid)
        if not t_data: print('  ❌ Fetch failed'); return
        track = {
            'track_id': str(t_data['id']), 'title': t_data.get('title', ''),
            'artist': (t_data.get('performer') or {}).get('name', ''),
            'album':  (t_data.get('album') or {}).get('title', ''),
            'album_artist': '', 'cover_url': '',
            'duration': int(t_data.get('duration') or 0), 'track_number': 0,
        }
        _play_list(client, [track], 0, build_filter_func, state)


def _favorites_flow(client: QobuzClient, build_filter_func, state: dict):
    """お気に入りリストからアルバムを選んで再生する。
    ジャケットモードと同じく再生終了後は自動でリストに戻り、
    連続して次のアルバムを選べる。b で Qobuz メインメニューへ戻る。"""
    favs = _load_favorites()
    if not favs:
        print('\n  No favourites yet.')
        input('  Press Enter to go back...'); return

    # ─── 再生後のターミナル復元ヘルパー ────────────────────────────────
    def _after_play():
        """再生後に必ず呼ぶ: ターミナル復元 + 残留バイト破棄"""
        _restore_terminal()
        try:
            import termios as _t
            _t.tcflush(sys.stdin.fileno(), _t.TCIFLUSH)
        except Exception:
            pass

    while True:
        # ── 最新のお気に入りリストを毎回読み直す（削除後に反映）──────────
        favs = _load_favorites()

        print('\n' + '═' * 56)
        print('  ⭐ Favourites  —  sequential cover-art playback')
        print('═' * 56)
        if not favs:
            print('  No favourites yet.')
            input('  Press Enter to go back...'); return

        for i, f in enumerate(favs, 1):
            no    = f.get('no') if f.get('no') is not None else i
            audio = f.get('audio_settings', {})
            pst   = FILTER_PRESET_LABELS.get(audio.get('filter_preset',''), '')
            dur   = f.get('duration_str', '')   # 保存されていれば所要時間
            line  = f'  [{no:03d}] {f.get("artist","")} — {f.get("title","")}'
            if pst: line += f'  {pst}'
            if dur: line += f'  ⏱{dur}'
            print(line)

        print()
        print('  Enter number → play immediately (auto-returns to this list after playback)')
        print('  d : ⭐ Delete    n : ⏭ Next album    q : ← Back')
        print('─' * 56)

        # ── 入力（残留バイト破棄してから受け付ける）──────────────────────
        try:
            import termios as _t
            _t.tcflush(sys.stdin.fileno(), _t.TCIFLUSH)
        except Exception:
            pass
        ch = input('\n  Select (number / d / n / q): ').strip().lower()

        if ch == 'q':
            break

        if ch == 'd':
            try:
                del_ch = input('  Number to delete: ').strip()
                del_num = int(del_ch)
                del_idx = next((i for i, f in enumerate(favs)
                                if (f.get('no') or i + 1) == del_num), None)
                if del_idx is None:
                    if 1 <= del_num <= len(favs):
                        del_idx = del_num - 1
                if del_idx is not None:
                    removed = favs[del_idx]
                    favs.pop(del_idx)
                    _save_favorites(favs)
                    print(f'  ✓ [{del_num:03d}] {removed.get("artist","")} — {removed.get("title","")} removed')
                else:
                    print('  ⚠ Number not found')
            except (ValueError, IndexError):
                print('  ⚠ Cancelled')
            continue

        # ── 番号選択 → 再生 ──────────────────────────────────────────────
        try:
            num = int(ch)
            idx = next((i for i, f in enumerate(favs) if f.get('no') == num), None)
            if idx is None:
                if 1 <= num <= len(favs):
                    idx = num - 1
                else:
                    raise ValueError
            if not 0 <= idx < len(favs):
                raise ValueError
        except ValueError:
            print('  ⚠ Please enter a number')
            continue

        fav = favs[idx]
        artist      = fav.get('artist', '')
        title       = fav.get('title', '')
        album_id_key = fav.get('album_id', '')

        # ── プレイリストエントリ ─────────────────────────────────────────
        if album_id_key.startswith('pl:'):
            playlist_id = album_id_key[3:]
            print(f'\n  🎵 {title}  fetching tracks...')
            raw_tracks = client.get_playlist_tracks(playlist_id)
            if not raw_tracks:
                print('  ❌ Could not fetch playlist tracks')
                _after_play(); continue
            tracks = []
            for i, t in enumerate(raw_tracks, 1):
                tid = str(t.get('id') or '')
                if not tid: continue
                alb  = t.get('album') or {}
                art  = ((t.get('performer') or {}).get('name', '')
                        or (alb.get('artist') or {}).get('name', ''))
                cover = ((alb.get('image') or {}).get('large')
                         or (alb.get('image') or {}).get('small') or '')
                tracks.append({
                    'track_id': tid, 'title': t.get('title', ''),
                    'artist': art, 'album': alb.get('title', ''),
                    'album_artist': art, 'cover_url': cover,
                    'duration': int(t.get('duration') or 0), 'track_number': i,
                })
            if not tracks:
                print('  ❌ No valid track IDs')
                _after_play(); continue
            saved_audio = fav.get('audio_settings')
            if saved_audio:
                for k, v in saved_audio.items():
                    state[k] = v
                preset_lbl = FILTER_PRESET_LABELS.get(saved_audio.get('filter_preset', ''), '')
                print(f'  🎼 Applying saved audio settings: {preset_lbl}')
                state['manual_preset_locked'] = True
            state['current_playlist_id']   = playlist_id
            state['current_playlist_name'] = title
            state['current_album']         = None
            print(f'  🎵 {len(tracks)} tracks  — starting playback (returns to list when done)')
            _play_list(client, tracks, 0, build_filter_func, state)
            _after_play(); continue

        # ── アルバムエントリ ─────────────────────────────────────────────
        print(f'\n  📀 {artist} — {title}  fetching...')
        album = client.get_album(album_id_key)
        if not album:
            print('  ❌ Failed to fetch album info')
            _after_play()
            continue

        # 保存されている音響設定を state に適用
        saved_audio = fav.get('audio_settings')
        if saved_audio:
            for k, v in saved_audio.items():
                state[k] = v
            preset_lbl = FILTER_PRESET_LABELS.get(saved_audio.get('filter_preset',''), '')
            print(f'  🎼 Applying saved audio settings: {preset_lbl}')
            state['manual_preset_locked'] = True   # 保存設定を優先（自動検出をスキップ）
        else:
            # 保存設定なし → ジャンル自動検出
            _apply_auto_preset(album, state)

        tracks = _album_to_tracks(album)
        print(f'  🎵 {len(tracks)} tracks  — starting playback (returns to list when done)')

        state['current_album'] = album
        state['current_url']   = album_id_key
        _play_list(client, tracks, 0, build_filter_func, state)

        # 再生終了 → ブラウザはそのまま（run() がセッション全体で管理）、リストへ戻る
        _after_play()


# ═══════════════════════════════════════════════════════════════════════════
# ブラウザリクエスト処理
# ═══════════════════════════════════════════════════════════════════════════

class _PlaylistStatusBar:
    """
    ターミナル最下部にプレイリストキューを固定表示するクラス。
    バックグラウンドスレッドで定期的に再描画し、スクロールに追従する。
    """
    def __init__(self, queue_items: list):
        self._queue   = list(queue_items)
        self._current = 0          # 1-indexed
        self._stop    = False
        self._lock    = __import__('threading').Lock()
        self._thread  = None

    def update(self, current: int, queue_items: list):
        with self._lock:
            self._current = current
            self._queue   = list(queue_items)

    def _bar_lines(self) -> list:
        """表示する行リストを生成"""
        with self._lock:
            q = self._queue
            cur = self._current
        cols, _ = __import__('shutil').get_terminal_size((80, 24))
        W = min(cols - 4, 72)
        lines = []
        # ヘッダー
        done_n = sum(1 for i in q if i.get('done'))
        hdr = f' 🎵 Playlist  {cur}/{len(q)}  (✅{done_n}完了)'
        lines.append('  ┌' + '─' * W + '┐')
        lines.append('  │' + hdr.ljust(W) + '│')
        lines.append('  ├' + '─' * W + '┤')
        for i, item in enumerate(q, 1):
            if item.get('done'):
                mark = '✅'; col = '\033[2m'   # 暗く
            elif i == cur:
                mark = '▶ '; col = '\033[96m'  # シアン
            else:
                mark = f'{i:2d}'; col = ''
            reset = '\033[0m' if col else ''
            ttl = item['title'][:W - 18] if item['title'] else '?'
            art = item.get('artist', '')[:16]
            suffix = f'  {art}' if art else ''
            body = f' {mark} {ttl}{suffix}'
            # 可視幅でパディング（ANSIコードは幅0）
            visible_len = len(f' {mark} {ttl}{suffix}')
            pad = max(0, W - visible_len)
            lines.append(f'  │{col}{body}{" " * pad}{reset}│')
        lines.append('  └' + '─' * W + '┘')
        return lines

    def _redraw(self):
        """カーソルを保存→最下部へ移動→描画→カーソル復元"""
        try:
            _, rows = __import__('shutil').get_terminal_size((80, 24))
            bar = self._bar_lines()
            n = len(bar)
            out = []
            out.append('\033[s')              # カーソル位置保存
            for i, line in enumerate(bar):
                row = rows - n + i + 1
                out.append(f'\033[{row};1H')  # 行へ移動
                out.append('\033[2K')         # 行クリア
                out.append(line)
            out.append('\033[u')              # カーソル復元
            sys.stdout.write(''.join(out))
            sys.stdout.flush()
        except Exception:
            pass

    def _loop(self):
        while not self._stop:
            self._redraw()
            __import__('time').sleep(1.5)

    def start(self):
        # スクロール領域を狭めて最下部N行をバー用に確保
        try:
            _, rows = __import__('shutil').get_terminal_size((80, 24))
            n = len(self._bar_lines())
            sys.stdout.write(f'\033[1;{rows - n}r')  # スクロール領域を上部に制限
            sys.stdout.flush()
        except Exception:
            pass
        self._thread = __import__('threading').Thread(
            target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        # スクロール領域をリセット
        try:
            _, rows = __import__('shutil').get_terminal_size((80, 24))
            sys.stdout.write(f'\033[1;{rows}r')   # スクロール領域を全画面に戻す
            # バー部分をクリア
            n = len(self._bar_lines())
            for i in range(n):
                sys.stdout.write(f'\033[{rows - n + i + 1};1H\033[2K')
            sys.stdout.write('\033[u')
            sys.stdout.flush()
        except Exception:
            pass


def _handle_playlist_request(client: QobuzClient, build_filter_func,
                             state: dict, req: dict):
    """
    ブラウザUIからのプレイリスト（複数アルバム）再生リクエストを処理する。
    album_ids を順番に再生し、[q] で中断するか全曲終了したら戻る。
    """
    global _PLAYLIST_STATUS

    _PLAYLIST_STATUS_PATH = Path('/tmp/qji_playlist_status.json')

    def _update_status(**kw):
        global _PLAYLIST_STATUS
        _PLAYLIST_STATUS.update(kw)
        # ファイル経由でブラウザに状態を共有（モジュール参照より確実）
        try:
            _PLAYLIST_STATUS_PATH.write_text(
                __import__('json').dumps(_PLAYLIST_STATUS, ensure_ascii=False))
        except Exception:
            pass

    _restore_terminal()
    album_ids = req.get('album_ids', [])
    titles    = req.get('titles', [])
    imgs      = req.get('imgs', [])
    artists   = req.get('artists', [])
    if not album_ids:
        print('  ⚠ album_ids is empty')
        return

    total = len(album_ids)

    # キュー全体を初期化
    queue_items = []
    for i, aid in enumerate(album_ids):
        queue_items.append({
            'title':  titles[i]  if i < len(titles)  else aid,
            'artist': artists[i] if i < len(artists) else '',
            'img':    imgs[i]    if i < len(imgs)    else '',
            'done':   False,
        })
    _update_status(
        active=True, total=total, current=0,
        title='', artist='', img='', queue=queue_items,
    )

    # 画面下部固定バーを起動
    bar = _PlaylistStatusBar(queue_items)
    bar.start()

    state['manual_preset_locked'] = False

    try:
        for idx, album_id in enumerate(album_ids, 1):
            print(f'\n  [{idx}/{total}] Fetching album info...')

            album = client.get_album(album_id)
            if not album:
                print(f'  ❌ Album fetch failed → skipping')
                queue_items[idx-1]['done'] = True
                bar.update(idx, queue_items)
                continue

            tracks = _album_to_tracks(album)
            if not tracks:
                print(f'  ⚠ No tracks → skipping')
                queue_items[idx-1]['done'] = True
                bar.update(idx, queue_items)
                continue

            artist  = (album.get('artist') or {}).get('name', '')
            alb_ttl = album.get('title', '')
            alb_img = ((album.get('image') or {}).get('large')
                       or (album.get('image') or {}).get('small') or '')

            queue_items[idx-1].update(
                {'title': alb_ttl, 'artist': artist, 'img': alb_img})
            _update_status(
                current=idx, title=alb_ttl, artist=artist,
                img=alb_img, queue=queue_items,
            )
            bar.update(idx, queue_items)

            state['current_album'] = album
            state['current_url']   = ''
            state['manual_preset_locked'] = False
            _apply_auto_preset(album, state)

            result = _play_list(client, tracks, 0, build_filter_func, state)
            _restore_terminal()

            queue_items[idx-1]['done'] = True
            _update_status(queue=queue_items)
            bar.update(idx, queue_items)

            if result == 'quit':
                print(f'\n  ⏹ Playlist interrupted')
                _update_status(active=False)
                return

            if idx < total:
                print(f'\n  ✅ {alb_ttl} — done  ⏭ next...')

        _update_status(active=False)
        print(f'\n  🎵 Playlist playback complete')

    finally:
        # 必ずバーを停止してスクロール領域を元に戻す
        bar.stop()
        _restore_terminal()
        # ステータスファイルを削除
        try:
            _PLAYLIST_STATUS_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        # ターミナルを確実に通常モードへ
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
            try:
                os.system('stty sane 2>/dev/null')
            except Exception:
                pass

def _handle_playlist_tracks_request(client: QobuzClient, build_filter_func,
                                    state: dict, req: dict):
    """
    ブラウザUIからのQobuzプレイリスト個別曲再生リクエストを処理する。
    異なるアルバムに属する曲を track_id 単位で順番に再生する。
    track_list: [{track_id, album_id, title, artist, album, cover_url}, ...]
    """
    _restore_terminal()

    track_list = req.get('track_list', [])
    if not track_list:
        print('  ⚠ track_list is empty')
        return

    # _play_list が期待する形式に変換
    tracks = []
    for i, t in enumerate(track_list, 1):
        tid = str(t.get('track_id') or t.get('id') or '')
        if not tid:
            continue
        tracks.append({
            'track_id':     tid,
            'title':        t.get('title', ''),
            'artist':       t.get('artist', ''),
            'album':        t.get('album', ''),
            'album_artist': t.get('artist', ''),
            'cover_url':    t.get('cover_url', ''),
            'duration':     int(t.get('duration') or 0),
            'track_number': i,
        })

    if not tracks:
        print('  ⚠ No valid track IDs')
        return

    total = len(tracks)
    print(f'\n  🎵 Playlist playback ({total} tracks)')
    for i, t in enumerate(tracks[:5], 1):
        print(f'    [{i}] {t["title"]} / {t["artist"]}')
    if total > 5:
        print(f'    ... and {total - 5} more')

    state['manual_preset_locked']  = False
    state['current_playlist_id']   = req.get('playlist_id', '')
    state['current_playlist_name'] = req.get('playlist_name', '')
    state['current_album']         = None   # アルバムではなくPLとして保存させる

    result = _play_list(client, tracks, 0, build_filter_func, state)
    _restore_terminal()

    if result == 'quit':
        print(f'\n  ⏹ Playlist interrupted')
    else:
        print(f'\n  🎵 Playlist playback complete')

def _handle_playlist_queue_request(client: QobuzClient, build_filter_func,
                                   state: dict, req: dict):
    """
    複数の Qobuz プレイリストをキュー再生する（ジャケットモード方式）。
    [q] キーで現在のプレイリストを中断して次へ進む。
    最終プレイリストで [q] を押した時、または全完了時に終了する。
    """
    _restore_terminal()
    playlists = req.get('playlists', [])   # [{playlist_id, name}, ...]
    if not playlists:
        print('  ⚠ playlists is empty')
        return

    total_pl = len(playlists)
    print(f'\n  🎵 Playlist queue ({total_pl})')
    for i, pl in enumerate(playlists, 1):
        print(f'    [{i}] {pl.get("name", pl.get("playlist_id", "?"))}')
    print(f'  ℹ [q] skip to next playlist (exits after last)')

    # _play_list のヒントテキストをキューモード表示に切替
    state['playlist_queue_mode'] = True

    try:
        for pl_idx, pl_info in enumerate(playlists, 1):
            pid  = str(pl_info.get('playlist_id', ''))
            name = pl_info.get('name', pid)

            print(f'\n  ─── [{pl_idx}/{total_pl}] 🎵 {name} ───')
            print(f'  Fetching tracks...')
            raw_tracks = client.get_playlist_tracks(pid)

            if not raw_tracks:
                print(f'  ⚠ Could not fetch tracks → skipping')
                continue

            # _play_list 形式に変換
            tracks = []
            for i, t in enumerate(raw_tracks, 1):
                tid = str(t.get('id') or '')
                if not tid:
                    continue
                alb  = t.get('album') or {}
                art  = (
                    (t.get('performer') or {}).get('name', '')
                    or (alb.get('artist') or {}).get('name', '')
                )
                cover = (
                    (alb.get('image') or {}).get('large')
                    or (alb.get('image') or {}).get('small') or ''
                )
                tracks.append({
                    'track_id':     tid,
                    'title':        t.get('title', ''),
                    'artist':       art,
                    'album':        alb.get('title', ''),
                    'album_artist': art,
                    'cover_url':    cover,
                    'duration':     int(t.get('duration') or 0),
                    'track_number': i,
                })

            if not tracks:
                print(f'  ⚠ No valid track IDs → skipping')
                continue

            print(f'  {len(tracks)} tracks — starting')
            state['manual_preset_locked']  = False
            state['current_playlist_id']   = pid
            state['current_playlist_name'] = name
            state['current_album']         = None
            result = _play_list(client, tracks, 0, build_filter_func, state)
            _restore_terminal()

            if result == 'quit':
                if pl_idx < total_pl:
                    print(f'\n  ⏭ {name} — skipped → next playlist')
                    # continue ループで次のPLへ自動進行
                else:
                    print(f'\n  ⏹ Playlist queue stopped')
                    break
            else:
                if pl_idx < total_pl:
                    print(f'\n  ✅ {name} — done  ⏭ next playlist...')
                else:
                    print(f'\n  🎵 Playlist queue complete')

    finally:
        state['playlist_queue_mode'] = False
        _restore_terminal()

def _handle_browser_request(client: QobuzClient, build_filter_func,
                            state: dict, req: dict):
    """
    ブラウザUIからの再生リクエストを処理する。
    保存済み音響設定を state に適用してからアルバムを再生する。
    """
    _restore_terminal()   # 再生後 raw モードが残留している場合に備えて
    album_id   = req.get('album_id', '')
    start_from = int(req.get('start_from', 1))
    audio      = req.get('audio_settings', {})

    if not album_id:
        print('  ⚠ album_id missing from request')
        return

    # 保存された音響設定を適用
    if audio:
        for k, v in audio.items():
            state[k] = v
        preset_lbl = FILTER_PRESET_LABELS.get(audio.get('filter_preset', ''), '')
        print(f'\n  📱 Playback request from browser')
        if preset_lbl:
            print(f'  🎼 Applying audio settings: {preset_lbl}')
        state['manual_preset_locked'] = True   # ブラウザ設定を優先
    else:
        print(f'\n  📱 Playback request from browser')

    # アルバム情報を取得して再生
    print(f'  📀 Fetching album info...')
    album = client.get_album(album_id)
    if not album:
        print('  ❌ Failed to fetch album info')
        return

    tracks = _album_to_tracks(album)
    artist = (album.get('artist') or {}).get('name', '')
    print(f'  📀 {artist} — {album.get("title", "")}  ({len(tracks)} tracks)')
    for i, t in enumerate(tracks, 1):
        dur = f' [{t["duration"]//60}:{t["duration"]%60:02d}]' if t.get('duration') else ''
        marker = ' ◀ ここから' if i == start_from else ''
        print(f'     {i:2d}. {t["title"]}{dur}{marker}')

    start_idx = max(0, start_from - 1)
    state['current_album'] = album
    state['current_url']   = req.get('source_url', '')
    # ブラウザ設定がない場合はジャンル自動検出
    if not audio:
        _apply_auto_preset(album, state)
    _play_list(client, tracks, start_idx, build_filter_func, state)

    # 再生終了後のターミナル復元（ブラウザは run() が管理するため閉じない）
    _restore_terminal()
    try:
        import termios
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ═══════════════════════════════════════════════════════════════════════════

def run(build_filter_func, gain_preset: str, tinnitus: bool,
        musikverein: bool, loudness_filter: str, eq_part: str,
        volume: int, air_layer: bool, echo_mode: str,
        output_device: str):
    """qji.py の Q キーハンドラから呼び出す唯一の関数"""
    # ターミナルが正常状態のうちにベースラインを保存（フリーズ防止の要）
    _capture_baseline_term()

    if not _ensure_requests():
        print('  ❌ requests is required: pip install requests')
        input('  Press Enter to go back...'); return

    import __main__ as _qji
    state = {
        'gain_preset':   gain_preset,
        'filter_preset': getattr(_qji, 'current_filter_preset', 'musikverein'),
        'tinnitus':      tinnitus,
        'musikverein':   musikverein,
        'loudness':      loudness_filter,
        'eq_part':       eq_part,
        'volume':        volume,
        'air_layer':     air_layer,
        'echo_mode':     echo_mode,
        'output_device': output_device,
        'fmt_name':      '',
        'current_album':      None,   # s キー保存用
        'current_playlist_id':   '',  # プレイリスト保存用
        'current_playlist_name': '',  # プレイリスト保存用
        'current_track':      None,
        'current_url':        '',
        'current_track_info': None,   # i キー再表示用
        'current_jacket_path': None,
        'manual_preset_locked': False,  # True = [c]手動選択済 → 自動検出をスキップ
        'gapless':              False,  # True = ギャップレス再生モード ([G] キーで切替)
    }

    config = _load_config()
    if not config.get('auth_token') or not config.get('app_id'):
        print('\n  🔑 Qobuz initial setup required.')
        if not _setup_token(config):
            input('  Press Enter to go back...'); return
        config = _load_config()

    client = QobuzClient(config['app_id'], config['app_secret'], config['auth_token'])
    if not client.ping():
        print('\n  ⚠ Token has expired. Please re-authenticate.')
        if not _setup_token(config):
            input('  Press Enter to go back...'); return
        config = _load_config()
        client = QobuzClient(config['app_id'], config['app_secret'], config['auth_token'])

    # ブラウザUIサーバー起動（バックグラウンド）
    _browser_available = False
    try:
        import qji_qobuz_browser as _browser
        _browser.start_browser_server(open_browser=False)
        _browser_available = True
        # ★ j キー・ジャケットモード方式: 入場時に自動でシークレットウィンドウを開く
        _open_incognito_browser('http://localhost:8080')
    except ImportError:
        pass

    _last_ping = time.time()   # トークン有効性の最終確認時刻
    _PING_INTERVAL = 30 * 60  # 30分ごとに自動確認

    while True:
        # ── 定期的なトークン有効性確認 ─────────────────────────────────────
        now = time.time()
        _token_ok = True
        if now - _last_ping > _PING_INTERVAL:
            _token_ok = client.ping()
            _last_ping = now
        print('\n' + '═' * 56)
        print('🎵  Qji × Qobuz  —  Streaming')
        print('═' * 56)
        preset_lbl = FILTER_PRESET_LABELS.get(state['filter_preset'], '')
        gain_db    = GAIN_PRESETS_DB.get(state['gain_preset'], 0.0)
        apl        = '🌿 ON' if state['air_layer'] else 'OFF'
        print(f'  Account     : {config.get("username","(not set)")}')
        print(f'  Output dev. : {output_device}')
        print(f'  Audio preset: {preset_lbl}  Gain:{gain_db:+.1f}dB  APL:{apl}  Vol:{state["volume"]}dB')
        print(f'  Favourites  : {len(_load_favorites())}')
        if _browser_available:
            print(f'  Browser UI  : http://localhost:8080')
        if not _token_ok:
            print(f'  ⚠ Token may have expired. Press [c] to re-authenticate.')
        print()
        print('  u : 🔗 Paste URL to play')
        print('  f : ⭐ Play from favourites')
        print('  v : 🌐 Open browser UI')
        print('  c : 🔑 Re-authenticate')
        print('  q : ← Return to Qji main menu')
        print('─' * 56)

        # ── メニュー入力 ──────────────────────────────────────────────────
        # ブラウザリクエストのポーリングは別スレッドで行う。
        # メインスレッドは /dev/tty の readline だけを担当することで
        # select.select ループ内での状態混乱を防ぐ。
        ch = ''
        _browser_req_result: dict = {}
        _browser_req_event = threading.Event()
        def _poll_browser_request():
            """ブラウザリクエストを 0.3 秒間隔で確認し、見つかったら Event をセットする。"""
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
        poll_th = threading.Thread(target=_poll_browser_request, daemon=True)
        poll_th.start()
        try:
            with open('/dev/tty', 'r') as _tty:
                try:
                    subprocess.run(['stty', 'sane'], stdin=_tty,
                                   check=False, timeout=2, capture_output=True)
                except Exception:
                    pass
                print('\n  Select (u / f / v / c / q): ', end='', flush=True)
                while True:
                    # ブラウザリクエストが届いたか確認
                    if _browser_req_event.is_set():
                        print()
                        req = _browser_req_result.get('req')
                        if req:
                            if req.get('type') == 'playlist':
                                _handle_playlist_request(client, build_filter_func, state, req)
                            elif req.get('type') == 'playlist_tracks':
                                _handle_playlist_tracks_request(client, build_filter_func, state, req)
                            elif req.get('type') == 'playlist_queue':
                                _handle_playlist_queue_request(client, build_filter_func, state, req)
                            else:
                                _handle_browser_request(client, build_filter_func, state, req)
                        _restore_terminal()
                        # ★ ブラウザからの再生終了後もQobuzメニューへ留まる
                        ch = '__browser__'
                        break   # 内側ループを抜けて外側whileループの先頭へ
                    rlist, _, _ = select.select([_tty], [], [], 0.5)
                    if rlist:
                        line = _tty.readline()
                        ch = line.strip().lower() if line else ''
                        _browser_req_event.set()   # poll スレッドを終了させる
                        break
        except (KeyboardInterrupt, EOFError):
            _browser_req_event.set()
            break
        except Exception:
            _browser_req_event.set()
            try:
                print('\n  Select (u / f / v / c / q): ', end='', flush=True)
                ch = sys.stdin.readline().strip().lower()
            except Exception:
                break
        if ch == '__browser__':
            continue

        if ch == 'q':
            # ブラウザタブに閉じるシグナルを送る
            if _browser_available:
                try:
                    _browser.send_close_signal()
                except Exception:
                    pass
            # ★ シークレットウィンドウも閉じる
            _close_incognito_browser()
            break
        elif ch == 'u':
            _url_flow(client, build_filter_func, state)
            _restore_terminal()
            # ★ 再生終了後もQobuzメニューへ留まる（ブラウザは閉じない）
        elif ch == 'f':
            _favorites_flow(client, build_filter_func, state)
            _restore_terminal()
            # ★ 再生終了後もQobuzメニューへ留まる（ブラウザは閉じない）
        elif ch == 'c':
            _restore_terminal()
            _setup_token(config)
            config = _load_config()
            client = QobuzClient(config['app_id'], config['app_secret'], config['auth_token'])
        elif ch == 'v':
            # ★ シークレットウィンドウで再度開く（閉じてしまった場合の再オープン）
            if _browser_available:
                _open_incognito_browser('http://localhost:8080')
            else:
                print('  ⚠ qji_qobuz_browser.py not found')
        else:
            _restore_terminal()
            print('  ⚠ Please enter u / f / c / b / v / q')

    _clear_now_playing()
    _restore_terminal()
    print('\n  🎵 Returning to Qji main menu...\n')
