# -*- coding: utf-8 -*-
"""
Qji.py - Hi-fi music player for Linux (encoding-robust build) + preset system
Cover-art browser / music playback system

[New features] Preset management
- Save current settings (volume, audio preset, gain preset, etc.) by name
- Load saved presets at any time
- List and delete presets
- Config file: ~/.music_player_presets.json
"""

import os
import json
import random
import subprocess
import signal
import threading
import sys
import tty
import termios
import select
import atexit
from mutagen import File
import time
import re
import curses
import tempfile
import shutil
import locale
import unicodedata

# 音声認識のインポート
try:
    from vosk import Model, KaldiRecognizer, SetLogLevel
    import sounddevice as sd
    import queue
    SetLogLevel(-1)  # ★ Voskの内部起動ログ(C++/Kaldi層)を抑制。
                      #   これを呼ばないと、モデル読み込み時に
                      #   "LOG (VoskAPI:ReadDataFiles()...)" のような行が
                      #   直接stderrへ出力され、再生中のステータス表示
                      #   （カーソル位置制御による上書き更新）と競合して
                      #   画面が乱れる・文字化けする原因になっていた。
    VOICE_RECOGNITION_AVAILABLE = True
except ImportError:
    VOICE_RECOGNITION_AVAILABLE = False

# 音声認識の有効/無効フラグ（--no-voice オプションで False に設定可能）
VOICE_RECOGNITION_ENABLED = True


# ===== ★★★ Sonia Intelligence System ★★★ =====
import sys as _sys_si
_SI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sonia_intelligence")
if _SI_DIR not in _sys_si.path:
    _sys_si.path.insert(0, _SI_DIR)

try:
    from profile_db import SoniaIntelligence as _SoniaIntelligence
    from filter_builder import build_filter_chain as _si_build_chain
    from acoustic_spaces import get_space as _si_get_space
    _si_instance = _SoniaIntelligence()
    SI_AVAILABLE = True
    print("✅ Sonia Intelligence System: loaded successfully")
except Exception as _si_e:
    SI_AVAILABLE = False
    _si_instance = None
    print(f"⚠️  Sonia Intelligence System: disabled ({_si_e})")
# ===== ★★★ Sonia Intelligence System ここまで ★★★ =====

# Webサーバー用インポート
try:
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    import socketserver
    WEB_SERVER_AVAILABLE = True
except ImportError:
    WEB_SERVER_AVAILABLE = False

# ===== 日本語対応ユーティリティ =====
def get_display_width(text):
    """文字列の表示幅を正確に計算（全角=2、半角=1）"""
    width = 0
    for char in text:
        ea_width = unicodedata.east_asian_width(char)
        if ea_width in ('F', 'W'):
            width += 2
        elif ea_width in ('H', 'Na', 'N'):
            width += 1
        else:
            width += 2
    return width

def truncate_string_by_width(text, max_width):
    """表示幅を考慮して文字列を切り詰める"""
    if get_display_width(text) <= max_width:
        return text
    result = ""
    current_width = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in ('F', 'W') else 1
        if current_width + char_width > max_width - 2:
            result += ".."
            break
        result += char
        current_width += char_width
    return result

def pad_string_by_width(text, target_width):
    """表示幅を考慮して文字列をパディング"""
    current_width = get_display_width(text)
    if current_width >= target_width:
        return text
    padding = target_width - current_width
    return text + (' ' * padding)

def check_locale_support():
    """Check locale settings and warn if not UTF-8"""
    try:
        encoding = locale.getpreferredencoding()
        if encoding.upper() != 'UTF-8':
            print("\n" + "="*60)
            print("⚠️  Warning: locale configuration issue")
            print("="*60)
            print(f"Current encoding: {encoding}")
            print("Non-UTF-8 encoding detected; some characters may not display correctly.")
            print("\nFix with:")
            print("  export LANG=ja_JP.UTF-8")
            print("  export LC_ALL=ja_JP.UTF-8")
            print("="*60 + "\n")
            time.sleep(2)
            return False
        return True
    except Exception as e:
        print(f"⚠️ Error checking locale: {e}")
        return True

def safe_print(text):
    """Print text safely, falling back to ASCII on encode error"""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode('utf-8', errors='replace').decode('utf-8'))

# ===== エンコーディング対応強化 =====
def safe_decode_tag(tag_value):
    """タグの値を安全にデコードする"""
    if tag_value is None:
        return ''
    
    if isinstance(tag_value, (list, tuple)):
        if len(tag_value) == 0:
            return ''
        tag_value = tag_value[0]
    
    if isinstance(tag_value, str):
        try:
            if all(ord(c) < 256 for c in tag_value):
                try:
                    bytes_data = tag_value.encode('latin-1')
                    decoded = bytes_data.decode('utf-8')
                    if len(decoded) > 0:
                        return decoded
                except (UnicodeDecodeError, UnicodeEncodeError):
                    pass
        except:
            pass
        return tag_value
    
    if isinstance(tag_value, bytes):
        for encoding in ['utf-8', 'utf-16', 'shift_jis', 'euc-jp', 'iso-2022-jp', 'latin-1']:
            try:
                decoded = tag_value.decode(encoding)
                control_chars = sum(1 for c in decoded if ord(c) < 32 and c not in '\n\r\t')
                if control_chars < len(decoded) * 0.1:
                    return decoded
            except:
                continue
        return tag_value.decode('utf-8', errors='replace')
    
    return str(tag_value)

def get_tag_safe(audio, *keys):
    """複数のキー候補から安全にタグを取得"""
    if not audio or not audio.tags:
        return ''
    
    for key in keys:
        if key in audio.tags:
            value = audio.tags[key]
            decoded = safe_decode_tag(value)
            if decoded:
                return decoded
    
    return ''

# ===== 設定 =====
MUSIC_DIRS = [
    '/var/lib/mpd/music',
    os.path.expanduser('~/Music'),
    os.path.expanduser('~/AudioFiles'),                      # ★ 追加: 新着音源用フォルダ
    '/mnt/b6311abc-2b4c-4560-91d0-609272f0af0c',  # メイン音源ドライブ
    '/media/yasuhito/DATA',                         # DATAドライブ（/media経由）
    '/mnt/sonia',                                   # soniaドライブ
    '/media',                                       # USBマウント共通ルート
    '/mnt',                                         # マウントポイント共通ルート
    os.path.expanduser('~/Desktop'),                # デスクトップにマウントされたUSB等
]
SUPPORTED_EXTENSIONS = ('.wav', '.flac', '.wma', '.aiff', '.aif', '.mp3', '.m4a', '.aac', '.ogg')  # ★ .aac を追加
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.bmp', '.gif')
DATABASE_FILE = os.path.expanduser('~/music_mood_db.json')
PRESETS_FILE = os.path.expanduser('~/.music_player_presets.json')  # ★★★ 追加: プリセットファイル ★★★
FAVORITES_FILE = os.path.expanduser('~/.music_player_favorites.json')  # ★★★ Favorites file ★★★
CURRENT_VOLUME = 12  # dB
VOSK_MODEL_PATH = os.path.expanduser("~/vosk-model-ja-0.22")

# グローバル状態
current_playback_mode = 'tempo'
stop_playback = False
current_processes = {'ffmpeg': None, 'aplay': None, 'feh': None}
output_device = 'hw:2,0'
dsp_mode_active = False  # DSPモード時はTrue（48000Hz固定）
current_playing_track = None
current_image_path = None
next_track_requested = False
prev_track_requested = False
mode_change_requested = False
replay_requested = False  # ★★★ 追加: 曲を頭から再生し直すリクエスト ★★★
current_folder_tracks = []
current_playlist = []
search_keyword = ""
current_audio_preset = 'none'
current_gain_preset = 'classical'  # ★★★ 追加: ゲインプリセット（classical/jazz_pop） ★★★
_gain_preset_auto_linked_jazz = False  # ★★★ 追加: jazz自動連動でjazz_popに昇格させたかどうか（曲が変わった際の引き継ぎ防止用） ★★★

# ★★★ 自動歪み軽減装置（Auto De-Clip）★★★ 通常/ランダム再生専用（フォルダー再生では作動しない）
auto_declip_enabled = True       # 有効/無効フラグ
_DECLIP_GAIN_ORDER = ['classical', 'general', 'jazz_pop', 'loud']
_declip_baseline_preset = None   # 自動調整前（曲頭時点）のゲインプリセット。Noneなら未調整
_declip_auto_step = 0            # 現在の曲で自動的に何段階ゲインを下げたか
_declip_last_track_path = None   # 直前に play_one_track に渡された曲のパス（リプレイと新曲の判別用）
loudness_normalization = False  # ★★★ 追加: 音量一定化オプション ★★★
tinnitus_reduction_mode = False  # ★★★ 追加: 耳鳴り低減モード（高音域抑制） ★★★
air_particle_layer = True        # ★★★ 追加: 音場調整（Air Particle Layer / ピンクノイズ空気層）★★★
musikverein_room_effects = True  # ★★★ 追加: 楽友協会ルームエフェクト（黄金反射以下）ON/OFF ★★★
musikverein_echo_mode = 'classical'  # ★★★ 追加: エコーモード（classical/jazz_vocal） ★★★
current_filter_preset = 'musikverein'  # ★★★ 追加: フィルタープリセット（musikverein/piano/chamber/vocal/jazz/calm/deep） ★★★

# ★★★ 追加: 「ユーザーが選んだ音場を維持する」プリセット群 ★★★
# bypass・calm・deep・spatial(3D)・radio(ラジオ用ffmpeg音場) は、SI/ジャンル/タイトル
# 自動判定やトラック別保存プロファイルによって自動的に上書きされず、
# 明示的に[F]（起動時）/[C]（再生中）で変更するまで維持される。
_STICKY_FILTER_PRESETS = ('bypass', 'calm', 'deep', 'spatial', 'vinyl_emotion', 'radio')

# ★★★ AirPlay レシーバー設定 ★★★
_SHAIRPORT_CONF_PATH = os.path.expanduser('~/.config/qji_shairport.conf')
_AIRPLAY_DEVICE_NAME = 'Qji'
_GMRENDER_DEVICE_NAME = 'Qji'          # ★★★ UPnP/DLNA デバイス名 ★★★
# グローバル状態
# ... 既存のグローバル変数 ...
web_selection_result = None
web_server_running = False
web_server_instance = None
next_album_selection = []  # ★★★ 追加: 次に再生するアルバムのキュー（複数予約対応） ★★★
# ★★★ ギャップレス再生用の変数 ★★★
gapless_mode_enabled = False  # ギャップレス再生モードのON/OFF
gapless_current_index = 0  # ギャップレス再生中の現在の曲インデックス

# ★★★ Sonia Intelligence グローバル変数 ★★★
si_feedback_requested = False   # [z]キーでフィードバック入力を要求
si_hall_requested = False       # [h]キーでホール選択を要求
si_input_active = False         # SI入力中はkeyboard_listenerを一時停止
# threading.Eventで情報表示スレッドを確実に一時停止
import threading as _threading_si
_si_display_event = _threading_si.Event()
_si_display_event.set()  # 初期値: 描画許可（set=許可, clear=停止）


# ★★★ アップサンプリング設定 ★★★
upsampling_target_rate = 0  # 0=OFF, 192000=192kHz, 384000=384kHz

# ★★★ ラジオステーション一覧 ★★★
RADIO_STATIONS = [
    {
        'name': 'Classic FM',
        'url': 'https://media-ssl.musicradio.com/ClassicFM',
        'description': "UK's classical music station",
        'country': '🇬🇧'
    },
    {
        'name': 'Classic FM Calm',
        'url': 'https://media-ssl.musicradio.com/ClassicFMCalm',
        'description': 'Classic FM - relaxing classical',
        'country': '🇬🇧'
    },
    {
        'name': 'Classic FM Movies',
        'url': 'https://media-ssl.musicradio.com/ClassicFM-M-Movies',
        'description': 'Classic FM - film music channel',
        'country': '🇬🇧'
    },
    {
        'name': 'Radio X Classic Rock',
        'url': 'https://media-ssl.musicradio.com/RadioXClassicRock',
        'description': 'Classic rock station',
        'country': '🇬🇧'
    },
    {
        'name': 'Capital FM',
        'url': 'https://media-ssl.musicradio.com/CapitalUK',
        'description': 'Pop / Top 40 chart station',
        'country': '🇬🇧'
    },
    {
        'name': 'Heart',
        'url': 'https://media-ssl.musicradio.com/HeartUK',
        'description': 'Adult contemporary pop',
        'country': '🇬🇧'
    },
    {
        'name': 'Capital Xtra',
        'url': 'https://media-ssl.musicradio.com/CapitalXTRANational',
        'description': 'Hip-hop / R&B station',
        'country': '🇬🇧'
    },
    {
        'name': 'Smooth Radio',
        'url': 'https://media-ssl.musicradio.com/SmoothUK',
        'description': 'Smooth R&B, soul, and adult contemporary',
        'country': '🇬🇧'
    },
    {
        'name': 'Jazz24',
        'url': 'https://knkx-live-a.edge.audiocdn.com/6285_256k',
        'description': 'NPR-affiliated jazz station, AAC 256kbps (Seattle, USA)',
        'country': '🇺🇸'
    },
    {
        'name': 'KJazz 88.1 FM',
        'url': 'https://streaming.live365.com/a49833',
        'description': "From California State University, Long Beach — one of the USA's leading jazz & blues stations (since 1981)",
        'country': '🇺🇸'
    },
    {
        'name': 'France Musique',
        'url': 'https://icecast.radiofrance.fr/francemusique-hifi.aac',
        'description': "France's national classical music broadcaster, AAC HiFi",
        'country': '🇫🇷'
    },
    {
        'name': 'France Musique: Classical Easy',
        'url': 'https://icecast.radiofrance.fr/francemusiqueeasyclassique-hifi.aac',
        'description': 'France Musique - easy-listening classical, AAC HiFi',
        'country': '🇫🇷'
    },
    {
        'name': 'France Musique: Concert',
        'url': 'https://icecast.radiofrance.fr/francemusiqueconcertsradiofrance-hifi.aac',
        'description': 'France Musique - live concert channel, AAC HiFi',
        'country': '🇫🇷'
    },
    {
        'name': 'France Musique: Films',
        'url': 'https://icecast.radiofrance.fr/francemusiquelabo-hifi.aac',
        'description': 'France Musique - film scores & experimental music, AAC HiFi',
        'country': '🇫🇷'
    },
    {
        'name': 'France Musique: Baroque',
        'url': 'https://icecast.radiofrance.fr/francemusiquebaroque-hifi.aac',
        'description': 'France Musique - baroque music channel, AAC HiFi',
        'country': '🇫🇷'
    },
    {
        'name': 'France Musique: Jazz',
        'url': 'https://icecast.radiofrance.fr/francemusiquelajazz-hifi.aac',
        'description': 'France Musique - jazz channel, AAC HiFi',
        'country': '🇫🇷'
    },
    {
        'name': 'France Musique: La Contemporaine',
        'url': 'https://icecast.radiofrance.fr/francemusiquelacontemporaine-hifi.aac',
        'description': 'France Musique - contemporary music channel, AAC HiFi',
        'country': '🇫🇷'
    },
    # Add other stations here
    # {
    #     'name': 'NHK-FM',
    #     'url': 'https://nhkradiostreaming.nhk.or.jp/nhkworld/nhkfm_320k.mp3',
    #     'description': 'NHK FM broadcast',
    #     'country': '🇯🇵'
    # },
]

# ★★★ 曲情報表示用のグローバル変数 ★★★
current_track_info = {
    'title': '',
    'artist': '',
    'album': '',
    'composer': '',
    'conductor': '',
    'performer': '',
    'genre': '',
    'tempo': '',
    'mode': '',
    'track_num': 0,
    'total_tracks': 0,
    'file_path': '',
    'duration': '',
    'elapsed': 0
}
info_display_active = False
info_display_lock = threading.Lock()
info_display_thread = None
# 曲情報オーバーレイ（ANSI カーソル保存/復元）と他スレッドの print が競合すると表示が斜めに崩れるため stdout を直列化する
terminal_io_lock = threading.Lock()


def terminal_print(*args, **kwargs):
    """再生中オーバーレイ表示中は stdout をロックして ANSI と通常出力の混線を防ぐ。"""
    flush = kwargs.pop('flush', True)
    if info_display_active:
        with terminal_io_lock:
            print(*args, **kwargs, flush=flush)
    else:
        print(*args, **kwargs, flush=flush)

# ===== ★★★ Now Playing ミラーサーバー ★★★ =====
NOW_PLAYING_PORT = 8766
now_playing_server_running = False
now_playing_server_instance = None
now_playing_server_thread = None

# ★★★ 楽曲情報キャッシュ（Wikipedia APIで取得した情報を再利用） ★★★
_music_info_cache = {}
_music_info_lock = threading.Lock()

def get_local_ip():
    """LAN内のIPアドレスを取得"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return '127.0.0.1'

def get_now_playing_html():
    """Now Playing HTMLを生成（楽曲情報パネル付き・Wikipedia無料版）"""
    return """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Now Playing</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0a; color: #f0f0f0;
    font-family: 'Helvetica Neue', Arial, sans-serif;
    display: flex; flex-direction: column;
    align-items: center; justify-content: flex-start;
    min-height: 100vh; padding: 20px 16px 40px;
  }
  #jacket {
    width: 100%; max-width: 360px; aspect-ratio: 1/1;
    border-radius: 12px; overflow: hidden;
    box-shadow: 0 8px 32px rgba(0,0,0,0.7);
    background: #1a1a1a; margin-bottom: 24px; transition: opacity 0.4s;
  }
  #jacket img { width: 100%; height: 100%; object-fit: contain; display: block; }
  .no-image {
    width: 100%; height: 100%; display: flex;
    align-items: center; justify-content: center;
    font-size: 80px; color: #333;
  }
  #info { width: 100%; max-width: 360px; text-align: center; }
  #title {
    font-size: 1.25rem; font-weight: 700; color: #fff;
    margin-bottom: 10px; line-height: 1.4; word-break: break-word;
  }
  .meta-row { font-size: 0.88rem; color: #aaa; margin-bottom: 6px;
    line-height: 1.5; word-break: break-word; }
  .meta-label { color: #666; font-size: 0.75rem; margin-right: 4px; }
  #track-num { font-size: 0.78rem; color: #555; margin-top: 14px; }
  #status-dot {
    display: inline-block; width: 8px; height: 8px;
    background: #1db954; border-radius: 50%; margin-right: 6px;
    animation: pulse 1.8s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.7); }
  }
  #waiting { margin-top: 60px; color: #444; font-size: 1rem;
    text-align: center; display: none; }
  #waiting .icon { font-size: 3rem; margin-bottom: 12px; }

  /* ★ 楽曲情報ボタン */
  #info-btn {
    display: none; margin-top: 22px;
    background: transparent; color: #7b8cde;
    border: 1px solid #2a2a5a; border-radius: 20px;
    padding: 9px 26px; font-size: 0.88rem; cursor: pointer;
    letter-spacing: 0.04em; transition: background 0.2s, border-color 0.2s;
  }
  #info-btn:active { background: #1a1a3a; border-color: #7b8cde; }

  /* ★ モーダル */
  #modal-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,0.88); z-index: 200;
    overflow-y: auto; padding: 24px 16px 48px;
    -webkit-overflow-scrolling: touch;
  }
  .modal-inner { max-width: 420px; margin: 0 auto; }
  .modal-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 16px;
  }
  .modal-title-text { font-size: 0.8rem; color: #666; letter-spacing: 0.06em; }
  .modal-close-btn {
    background: none; border: 1px solid #333; border-radius: 16px;
    color: #888; font-size: 0.8rem; padding: 5px 14px; cursor: pointer;
  }
  .modal-close-btn:active { color: #fff; border-color: #888; }
  .modal-track-label {
    font-size: 0.78rem; color: #444; text-align: center;
    margin-bottom: 18px; word-break: break-word; line-height: 1.5;
  }

  /* ★ 情報カード */
  .info-card {
    background: #111; border-radius: 12px;
    padding: 18px 16px; margin-bottom: 14px;
    border-left: 3px solid #7b8cde;
  }
  .info-card-header {
    font-size: 0.72rem; color: #7b8cde;
    letter-spacing: 0.08em; margin-bottom: 10px;
  }
  .info-card-body {
    font-size: 0.93rem; color: #ccc; line-height: 1.8;
    word-break: break-word;
  }
  .info-source {
    font-size: 0.7rem; color: #333; text-align: right;
    margin-top: 10px;
  }
  .info-loading {
    text-align: center; color: #555; padding: 50px 0;
    font-size: 0.9rem; line-height: 2.2;
  }
  .spinner {
    display: inline-block; width: 24px; height: 24px;
    border: 2px solid #333; border-top-color: #7b8cde;
    border-radius: 50%; animation: spin 0.9s linear infinite;
    margin-bottom: 14px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .info-error {
    color: #e07070; font-size: 0.88rem;
    text-align: center; padding: 30px 10px; line-height: 1.8;
  }
</style>
</head>
<body>
<div id="waiting"><div class="icon">🎵</div><div>再生待機中...</div></div>
<div id="jacket"><div class="no-image">🎵</div></div>
<div id="info">
  <div id="title">—</div>
  <div class="meta-row"><span class="meta-label">作曲家</span><span id="composer">—</span></div>
  <div class="meta-row"><span class="meta-label">演奏者</span><span id="performer">—</span></div>
  <div class="meta-row"><span class="meta-label">指揮者</span><span id="conductor">—</span></div>
  <div id="track-num"><span id="status-dot"></span><span id="track-pos"></span></div>
  <button id="info-btn" onclick="showMusicInfo()">📖 楽曲情報</button>
</div>

<!-- ★ 楽曲情報モーダル -->
<div id="modal-overlay">
  <div class="modal-inner">
    <div class="modal-header">
      <span class="modal-title-text">📖 楽曲情報</span>
      <button class="modal-close-btn" onclick="closeModal()">✕ 閉じる</button>
    </div>
    <div id="modal-track-label" class="modal-track-label"></div>
    <div id="modal-content">
      <div class="info-loading"><div class="spinner"></div><br>情報を検索中...</div>
    </div>
  </div>
</div>

<script>
  let lastImageUrl = '';
  let currentData = {};
  let infoCache = {};

  function update(data) {
    const hasTrack = data.title && data.title !== '';
    document.getElementById('waiting').style.display = hasTrack ? 'none' : 'block';
    document.getElementById('jacket').style.display = hasTrack ? 'block' : 'none';
    document.getElementById('info').style.display = hasTrack ? 'block' : 'none';
    if (!hasTrack) return;
    currentData = data;
    document.getElementById('title').textContent = data.title || '—';
    document.getElementById('composer').textContent = data.composer || '—';
    document.getElementById('performer').textContent = data.performer || '—';
    document.getElementById('conductor').textContent = data.conductor || '—';
    const pos = (data.track_num && data.total_tracks)
      ? data.track_num + ' / ' + data.total_tracks : '';
    document.getElementById('track-pos').textContent = pos;
    // ラジオ中はボタン非表示
    document.getElementById('info-btn').style.display =
      data.is_radio ? 'none' : 'inline-block';
    const newUrl = '/jacket?' + data.image_ts;
    if (newUrl !== lastImageUrl && data.has_image) {
      lastImageUrl = newUrl;
      const img = new Image();
      img.onload = function() {
        document.getElementById('jacket').innerHTML =
          '<img src="' + newUrl + '" alt="jacket">';
      };
      img.onerror = function() {
        document.getElementById('jacket').innerHTML =
          '<div class="no-image">🎵</div>';
      };
      img.src = newUrl;
    } else if (!data.has_image) {
      document.getElementById('jacket').innerHTML = '<div class="no-image">🎵</div>';
      lastImageUrl = '';
    }
  }

  function showMusicInfo() {
    const key = (currentData.title||'')+'|'+(currentData.composer||'')+'|'+(currentData.performer||'');
    const overlay = document.getElementById('modal-overlay');
    overlay.style.display = 'block';
    overlay.scrollTop = 0;
    const lbl = [];
    if (currentData.title)     lbl.push(currentData.title);
    if (currentData.composer)  lbl.push('Composer: ' + currentData.composer);
    if (currentData.performer) lbl.push('Performer: ' + currentData.performer);
    document.getElementById('modal-track-label').textContent = lbl.join('  /  ');
    if (infoCache[key]) { renderInfo(infoCache[key]); return; }
    document.getElementById('modal-content').innerHTML =
      '<div class="info-loading"><div class="spinner"></div><br>' +
      'Wikipedia で情報を検索中...<br><small style="color:#333">数秒かかります</small></div>';
    const params = new URLSearchParams({
      title:     currentData.title     || '',
      composer:  currentData.composer  || '',
      performer: currentData.performer || '',
      album:     currentData.album     || ''
    });
    fetch('/info?' + params.toString())
      .then(function(r){ return r.json(); })
      .then(function(d){ infoCache[key]=d; renderInfo(d); })
      .catch(function(err){
        document.getElementById('modal-content').innerHTML =
          '<div class="info-error">⚠️ 通信エラー: ' + err + '</div>';
      });
  }

  function renderInfo(d) {
    if (d.error) {
      document.getElementById('modal-content').innerHTML =
        '<div class="info-error">⚠️ ' + esc(d.error) + '</div>';
      return;
    }
    const src = d.source ? '<div class="info-source">出典: ' + esc(d.source) + '</div>' : '';
    document.getElementById('modal-content').innerHTML =
      card('🎼 作曲家について', d.composer_info) +
      card('📖 楽曲の背景', d.piece_background) +
      card('🎹 演奏者について', d.performer_info) + src;
  }

  function card(heading, text) {
    return '<div class="info-card">' +
      '<div class="info-card-header">' + heading + '</div>' +
      '<div class="info-card-body">' + esc(text || '—') + '</div>' +
      '</div>';
  }

  function esc(s) {
    return String(s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function closeModal() {
    document.getElementById('modal-overlay').style.display = 'none';
  }
  document.getElementById('modal-overlay').addEventListener('click', function(e){
    if (e.target === this) closeModal();
  });

  function connectSSE() {
    const es = new EventSource('/events');
    es.onmessage = function(e){ try{ update(JSON.parse(e.data)); }catch(err){} };
    es.onerror = function(){ es.close(); setTimeout(connectSSE, 3000); };
  }
  connectSSE();
  document.addEventListener('touchmove', function(e){
    if (!document.getElementById('modal-overlay').contains(e.target))
      e.preventDefault();
  }, {passive: false});
</script>
</body>
</html>"""

# ── 音楽関連キーワード（関連性チェック用） ──
_MUSIC_KEYWORDS_JA = [
    'ミュージシャン','音楽家','歌手','作曲家','演奏家','ジャズ','クラシック',
    'ピアニスト','ギタリスト','指揮者','楽曲','アルバム','バンド','歌曲',
    'ヴォーカル','サックス','トランペット','ベーシスト','ドラマー','ソングライター',
    '音楽','シンガー','奏者','楽器','作詞','作曲','演奏','録音','レコード',
    '管弦楽団','フィルハーモニー','交響楽団','室内楽','弦楽四重奏','オーケストラ',
    'アンサンブル','オペラ','合唱','ソリスト','コンサート','音楽監督',
]
_MUSIC_KEYWORDS_EN = [
    'musician','singer','composer','jazz','classical','pianist','guitarist',
    'conductor','song','album','band','vocalist','saxophonist','trumpeter',
    'bassist','drummer','music','artist','performer','songwriter','recording',
    'discography','jazz musician','blues','rhythm','harmony','melody',
    'orchestra','philharmonic','symphony','ensemble','chamber','quartet',
    'opera','choir','soloist','concerto','recital','music director',
    'string quartet','wind','brass','woodwind',
]

def _is_music_related(text, description='', lang='ja'):
    """テキスト・説明が音楽関連かどうか判定する"""
    combined = (text + ' ' + description).lower()
    kws = _MUSIC_KEYWORDS_JA if lang == 'ja' else _MUSIC_KEYWORDS_EN
    return any(kw.lower() in combined for kw in kws)


def _normalize_piece_title(title):
    """楽曲タイトルを検索向けに正規化する（#1 → No. 1 等）"""
    title = re.sub(r'#\s*(\d+)', r'No. \1', title)
    title = re.sub(r'\bop\.?\s*(\d+)', r'Op.\1', title, flags=re.IGNORECASE)
    # 動作・楽章番号の前置き除去（"I. Allegro" → "Allegro"）
    title = re.sub(r'^[IVXivx]+\.\s+', '', title)
    return title.strip()


def _words_overlap(query, target, min_len=3):
    """queryの単語がtargetに含まれる割合（0.0〜1.0）を返す"""
    words = [w.lower() for w in re.split(r'[\s\-\.,\(\)\[\]#/・]', query)
             if len(w) >= min_len]
    if not words:
        return 0.0
    target_l = target.lower()
    matched = sum(1 for w in words if w in target_l)
    return matched / len(words)


def _wiki_page_summary(page_title, lang='ja'):
    """Wikipedia REST APIで指定ページの要約テキストを取得。"""
    import urllib.request, urllib.parse
    encoded = urllib.parse.quote(page_title.replace(' ', '_'))
    url = f'https://{lang}.wikipedia.org/api/rest_v1/page/summary/{encoded}'
    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': 'Qji-MusicPlayer/1.0 (music info display)'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            extract     = data.get('extract', '').strip()
            description = data.get('description', '').strip()
            if extract and len(extract) > 30:
                return extract[:500], description
    except Exception:
        pass
    return None, ''


def _wiki_search_best(query, lang='ja', limit=6, relevance_fn=None):
    """Wikipedia検索で全候補をスコアリングし、最も関連性の高い記事を返す。
    戻り値: (extract, description, article_title)"""
    import urllib.request, urllib.parse
    encoded = urllib.parse.quote(query)
    url = (f'https://{lang}.wikipedia.org/w/api.php'
           f'?action=query&list=search&srsearch={encoded}'
           f'&format=json&srlimit={limit}&srprop=snippet')
    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': 'Qji-MusicPlayer/1.0'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            results = data.get('query', {}).get('search', [])

        candidates = []
        for r in results:
            art_title = r['title']
            extract, description = _wiki_page_summary(art_title, lang)
            if not extract:
                continue
            music_ok  = _is_music_related(extract, description, lang)
            rel_score = relevance_fn(art_title) if relevance_fn else 0.5
            # 音楽関連でなければスコアを大幅ペナルティ
            final = rel_score * (1.0 if music_ok else 0.15)
            candidates.append((final, extract, description, art_title))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            best = candidates[0]
            if best[0] > 0:
                return best[1], best[2], best[3]
    except Exception:
        pass
    return None, '', ''


# 後方互換ラッパー（他の箇所から呼ばれている場合のため）
def _wiki_search_summary(query, lang='ja', limit=3):
    extract, description, _ = _wiki_search_best(query, lang, limit)
    return extract, description


def _get_person_info(name):
    """人名・楽団名で音楽関連のWikipedia情報を取得する。
    記事タイトルと名前の単語一致度でスコアリングし、
    最も確からしい記事を返す。"""
    if not name:
        return None

    def name_score(article_title):
        return _words_overlap(name, article_title, min_len=2)

    # 名前の有効単語（2文字以上）と有効語数を求める
    name_words = [w.lower() for w in name.split() if len(w) >= 2]
    # 3語以上の複合名（オーケストラ等）は高い閾値を要求する
    is_compound = len(name_words) >= 3

    def _name_in_extract(extract, title=''):
        """名前の主要語がextract冒頭またはarticle_titleに含まれるか確認。
        複合名（NY Phil等）はより多くの語の一致を要求する。"""
        extract_head = extract[:160].lower()
        target = (title + ' ' + extract_head).lower()
        matched = sum(1 for w in name_words if w in target)
        if is_compound:
            # 複合名は全単語の60%以上が一致している必要あり
            return matched / len(name_words) >= 0.6 if name_words else False
        else:
            return matched >= 1

    # ① ページ直接参照（タイトル完全一致 → 最も信頼性が高い）
    for lang in ['en', 'ja']:
        extract, description = _wiki_page_summary(name, lang)
        if extract and _is_music_related(extract, description, lang):
            if _name_in_extract(extract, name):
                return extract

    # ② 「名前 + 種別コンテキスト」でスコアリング検索
    for lang in ['en', 'ja']:
        context = 'conductor composer pianist performer orchestra philharmonic' \
                  if lang == 'en' else '指揮者 作曲家 演奏家 管弦楽団 フィルハーモニー'
        extract, desc, art_title = _wiki_search_best(
            f'{name} {context}', lang, relevance_fn=name_score
        )
        threshold = 0.55 if is_compound else 0.4
        if extract and _is_music_related(extract, desc, lang) \
                and name_score(art_title) >= threshold:
            if _name_in_extract(extract, art_title):
                return extract

    # ③ 名前単体でスコアリング検索（閾値を下げて最終手段）
    for lang in ['en', 'ja']:
        extract, desc, art_title = _wiki_search_best(
            name, lang, relevance_fn=name_score
        )
        threshold = 0.5 if is_compound else 0.3
        if extract and _is_music_related(extract, desc, lang) \
                and name_score(art_title) >= threshold:
            if _name_in_extract(extract, art_title):
                return extract

    return None


def _get_piece_info(title, composer=''):
    """楽曲情報をWikipediaで取得する。
    タイトル正規化 + 記事タイトルとの単語一致スコアリングで
    正しい楽曲記事を優先的に選ぶ。"""
    if not title:
        return None

    title_norm    = _normalize_piece_title(title)
    # 作曲家の姓だけ抽出（"Johannes Brahms" → "Brahms"）
    composer_last = composer.split()[-1] if composer else ''

    def piece_score(article_title):
        t = _words_overlap(title_norm, article_title, min_len=2)
        c = _words_overlap(composer_last, article_title, min_len=2) \
            if composer_last else 0.0
        return t * 0.65 + c * 0.35

    # ① 英語Wikipedia の正規形タイトル直接参照
    #    例: "Symphony No. 1 (Brahms)"
    if composer_last:
        canonical = f'{title_norm} ({composer_last})'
        extract, description = _wiki_page_summary(canonical, 'en')
        if extract and _is_music_related(extract, description, 'en'):
            return extract

    # ② 作曲家+曲名でスコアリング検索（英語優先）
    if composer:
        for lang in ['en', 'ja']:
            extract, desc, art_title = _wiki_search_best(
                f'{composer} {title_norm}', lang,
                limit=6, relevance_fn=piece_score
            )
            if extract and _is_music_related(extract, desc, lang) \
                    and piece_score(art_title) >= 0.2:
                return extract

    # ③ 曲名のみで検索（閾値なし・最終手段）
    for lang in ['en', 'ja']:
        # まず直接参照
        extract, description = _wiki_page_summary(title_norm, lang)
        if extract and _is_music_related(extract, description, lang):
            return extract
        # 次に検索
        extract, desc, art_title = _wiki_search_best(
            title_norm, lang, relevance_fn=piece_score
        )
        if extract and _is_music_related(extract, desc, lang):
            return extract

    return None


def _fetch_music_info(title, composer, performer, album=''):
    """Wikipedia APIを使って楽曲情報（作曲家・楽曲背景・演奏者）を取得。"""
    cache_key = f"{title}|{composer}|{performer}"
    with _music_info_lock:
        if cache_key in _music_info_cache:
            return _music_info_cache[cache_key]

    # ① 作曲家情報
    composer_info = _get_person_info(composer) or '作曲家情報が見つかりませんでした。'

    # ② 楽曲背景
    piece_background = _get_piece_info(title, composer) or '楽曲情報が見つかりませんでした。'

    # ③ 演奏者情報
    performer_info = _get_person_info(performer) or '演奏者情報が見つかりませんでした。'

    result = {
        'composer_info':    composer_info,
        'piece_background': piece_background,
        'performer_info':   performer_info,
        'source': 'Wikipedia',
    }
    with _music_info_lock:
        _music_info_cache[cache_key] = result
    return result


def get_now_playing_control_html():
    """コントロール付き Now Playing HTML（次の曲・停止ボタン付き）"""
    return """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Qji Control</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0a0a0a; color: #f0f0f0;
    font-family: 'Helvetica Neue', Arial, sans-serif;
    display: flex; flex-direction: column;
    align-items: center; justify-content: flex-start;
    min-height: 100vh; padding: 20px 16px 40px;
  }
  #jacket {
    width: 100%; max-width: 360px; aspect-ratio: 1/1;
    border-radius: 12px; overflow: hidden;
    box-shadow: 0 8px 32px rgba(0,0,0,0.7);
    background: #1a1a1a; margin-bottom: 24px; transition: opacity 0.4s;
  }
  #jacket img { width: 100%; height: 100%; object-fit: contain; display: block; }
  .no-image {
    width: 100%; height: 100%; display: flex;
    align-items: center; justify-content: center;
    font-size: 80px; color: #333;
  }
  #info { width: 100%; max-width: 360px; text-align: center; }
  #title {
    font-size: 1.25rem; font-weight: 700; color: #fff;
    margin-bottom: 10px; line-height: 1.4; word-break: break-word;
  }
  .meta-row { font-size: 0.88rem; color: #aaa; margin-bottom: 6px;
    line-height: 1.5; word-break: break-word; }
  .meta-label { color: #666; font-size: 0.75rem; margin-right: 4px; }
  #track-num { font-size: 0.78rem; color: #555; margin-top: 14px; }
  #status-dot {
    display: inline-block; width: 8px; height: 8px;
    background: #1db954; border-radius: 50%; margin-right: 6px;
    animation: pulse 1.8s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.7); }
  }
  #waiting { margin-top: 60px; color: #444; font-size: 1rem;
    text-align: center; display: none; }
  #waiting .icon { font-size: 3rem; margin-bottom: 12px; }

  /* ── コントロールバー ── */
  #controls {
    display: none; width: 100%; max-width: 360px;
    margin-top: 32px;
    display: flex; flex-direction: column; gap: 14px;
  }
  .ctrl-btn {
    width: 100%; padding: 18px 0; border: none; border-radius: 14px;
    font-size: 1.1rem; font-weight: 600; letter-spacing: 0.04em;
    cursor: pointer; transition: opacity 0.15s, transform 0.1s;
    display: flex; align-items: center; justify-content: center; gap: 10px;
  }
  .ctrl-btn:active { opacity: 0.7; transform: scale(0.97); }
  #btn-next {
    background: #1a2a4a; color: #7eb8f7;
    border: 1px solid #2a4a7a;
  }
  #btn-stop {
    background: #2a1010; color: #f07070;
    border: 1px solid #6a2020;
  }
  /* フィードバックトースト */
  #toast {
    position: fixed; bottom: 36px; left: 50%; transform: translateX(-50%);
    background: #222; color: #ccc; border-radius: 20px;
    padding: 10px 24px; font-size: 0.88rem;
    opacity: 0; transition: opacity 0.3s; pointer-events: none;
    white-space: nowrap;
  }
  #toast.show { opacity: 1; }
</style>
</head>
<body>
<div id="waiting"><div class="icon">🎵</div><div>再生待機中...</div></div>
<div id="jacket" style="display:none"><div class="no-image">🎵</div></div>
<div id="info" style="display:none">
  <div id="title">—</div>
  <div class="meta-row"><span class="meta-label">作曲家</span><span id="composer">—</span></div>
  <div class="meta-row"><span class="meta-label">演奏者</span><span id="performer">—</span></div>
  <div class="meta-row"><span class="meta-label">指揮者</span><span id="conductor">—</span></div>
  <div id="track-num"><span id="status-dot"></span><span id="track-pos"></span></div>
</div>

<div id="controls">
  <button class="ctrl-btn" id="btn-next" onclick="sendCmd('next')">
    <span style="font-size:1.4rem">⏭</span> 次の曲へ
  </button>
  <button class="ctrl-btn" id="btn-stop" onclick="sendCmd('stop')">
    <span style="font-size:1.4rem">⏹</span> 再生を終了
  </button>
</div>

<div id="toast"></div>

<script>
  let lastImageUrl = '';
  let hasTrack = false;

  function update(data) {
    hasTrack = !!(data.title && data.title !== '');
    document.getElementById('waiting').style.display = hasTrack ? 'none' : 'block';
    document.getElementById('jacket').style.display = hasTrack ? 'block' : 'none';
    document.getElementById('info').style.display = hasTrack ? 'block' : 'none';
    document.getElementById('controls').style.display = 'flex';  /* 常時表示 */
    if (!hasTrack) return;

    document.getElementById('title').textContent = data.title || '—';
    document.getElementById('composer').textContent = data.composer || '—';
    document.getElementById('performer').textContent = data.performer || '—';
    document.getElementById('conductor').textContent = data.conductor || '—';
    const pos = (data.track_num && data.total_tracks)
      ? data.track_num + ' / ' + data.total_tracks : '';
    document.getElementById('track-pos').textContent = pos;

    const newUrl = '/jacket?' + data.image_ts;
    if (newUrl !== lastImageUrl && data.has_image) {
      lastImageUrl = newUrl;
      const img = new Image();
      img.onload = function() {
        document.getElementById('jacket').innerHTML =
          '<img src="' + newUrl + '" alt="jacket">';
      };
      img.onerror = function() {
        document.getElementById('jacket').innerHTML =
          '<div class="no-image">🎵</div>';
      };
      img.src = newUrl;
    } else if (!data.has_image) {
      document.getElementById('jacket').innerHTML = '<div class="no-image">🎵</div>';
      lastImageUrl = '';
    }
  }

  function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2000);
  }

  function sendCmd(action) {
    const btn = document.getElementById(action === 'next' ? 'btn-next' : 'btn-stop');
    btn.disabled = true;
    fetch('/cmd', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: action})
    })
    .then(r => r.json())
    .then(d => {
      if (action === 'next') showToast('⏭  次の曲へスキップしました');
      else                   showToast('⏹  再生を終了しました');
    })
    .catch(() => showToast('⚠️  送信できませんでした'))
    .finally(() => { setTimeout(() => { btn.disabled = false; }, 1500); });
  }

  const evtSrc = new EventSource('/events');
  evtSrc.onmessage = function(e) {
    try { update(JSON.parse(e.data)); } catch(ex) {}
  };
  evtSrc.onerror = function() {
    document.getElementById('waiting').style.display = 'block';
    document.getElementById('jacket').style.display  = 'none';
    document.getElementById('info').style.display    = 'none';
  };
</script>
</body>
</html>"""


def start_now_playing_server():
    """Now Playingミラーサーバーをバックグラウンドで起動"""
    global now_playing_server_running, now_playing_server_instance, now_playing_server_thread
    if now_playing_server_running:
        return
    import socketserver as _ss

    class NowPlayingHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/' or self.path == '/index.html':
                html = get_now_playing_html().encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif self.path == '/control' or self.path == '/control.html':
                html = get_now_playing_control_html().encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif self.path.startswith('/jacket'):
                with info_display_lock:
                    img_path = current_image_path
                if img_path and os.path.exists(img_path):
                    try:
                        with open(img_path, 'rb') as f:
                            data = f.read()
                        ext = os.path.splitext(img_path)[1].lower().lstrip('.')
                        mime = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                                'png': 'image/png', 'gif': 'image/gif',
                                'bmp': 'image/bmp'}.get(ext, 'image/jpeg')
                        self.send_response(200)
                        self.send_header('Content-Type', mime)
                        self.send_header('Content-Length', str(len(data)))
                        self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    except:
                        pass
                self.send_response(404)
                self.end_headers()
            elif self.path.startswith('/info'):
                # ★ 楽曲情報エンドポイント（Wikipedia無料版）
                from urllib.parse import urlparse, parse_qs, unquote as _unq
                _parsed = urlparse(self.path)
                _params = parse_qs(_parsed.query)
                _title     = _unq(_params.get('title',     [''])[0])
                _composer  = _unq(_params.get('composer',  [''])[0])
                _performer = _unq(_params.get('performer', [''])[0])
                _album     = _unq(_params.get('album',     [''])[0])
                _result    = _fetch_music_info(_title, _composer, _performer, _album)
                _body = json.dumps(_result, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(_body)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(_body)
            elif self.path == '/events':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'keep-alive')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                last_sent = {}
                try:
                    while now_playing_server_running:
                        with info_display_lock:
                            info = current_track_info.copy()
                            img_path = current_image_path
                        has_image = bool(img_path and os.path.exists(img_path))
                        payload = {
                            'title':        info.get('title', ''),
                            'composer':     info.get('composer', ''),
                            'performer':    info.get('performer', ''),
                            'conductor':    info.get('conductor', ''),
                            'album':        info.get('album', ''),
                            'is_radio':     info.get('mode', '') == 'radio',
                            'track_num':    info.get('track_num', 0),
                            'total_tracks': info.get('total_tracks', 0),
                            'has_image':    has_image,
                            'image_ts':     str(int(os.path.getmtime(img_path) * 1000))
                                             if has_image else '0',
                        }
                        if payload != last_sent:
                            line = 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'
                            self.wfile.write(line.encode('utf-8'))
                            self.wfile.flush()
                            last_sent = payload
                        time.sleep(1.0)
                except:
                    pass
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == '/cmd':
                import json as _json
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                try:
                    data = _json.loads(body)
                    action = data.get('action', '')
                except Exception:
                    action = ''

                global next_track_requested, stop_playback
                msg = 'unknown'
                if action == 'next':
                    next_track_requested = True
                    # ローカル再生プロセスをスキップ
                    for proc in list(current_processes.values()):
                        if proc and proc.poll() is None:
                            try:
                                proc.terminate()
                            except Exception:
                                pass
                    # Qobuz が実行中なら _next_flag + プリバッファ中断
                    import sys as _sys
                    _qobuz = _sys.modules.get('qji_qobuzdsp')
                    if _qobuz:
                        _qobuz._next_flag = True
                        try: _qobuz._pb_abort_next()
                        except Exception: pass
                    # SoundCloud が実行中なら remote フラグ
                    _sc = _sys.modules.get('qji_soundcloud')
                    if _sc:
                        _sc._remote_next_flag = True
                        for proc in list(_sc._procs.values()):
                            if proc and proc.poll() is None:
                                try: proc.terminate()
                                except Exception: pass
                    # YouTube Music が実行中なら remote フラグ
                    _yt = _sys.modules.get('qji_ytmusic')
                    if _yt:
                        _yt._remote_next_flag = True
                        for proc in list(_yt._procs.values()):
                            if proc and proc.poll() is None:
                                try: proc.terminate()
                                except Exception: pass
                    msg = 'ok'
                elif action == 'stop':
                    stop_playback = True
                    for proc in list(current_processes.values()):
                        if proc and proc.poll() is None:
                            try:
                                proc.terminate()
                            except Exception:
                                pass
                    # Qobuz: _stop_flag + 全プリバッファ中断
                    import sys as _sys
                    _qobuz = _sys.modules.get('qji_qobuzdsp')
                    if _qobuz:
                        _qobuz._stop_flag = True
                        try: _qobuz._pb_abort_all()
                        except Exception: pass
                    # SoundCloud: remote フラグ + terminate
                    _sc = _sys.modules.get('qji_soundcloud')
                    if _sc:
                        _sc._remote_stop_flag = True
                        for proc in list(_sc._procs.values()):
                            if proc and proc.poll() is None:
                                try: proc.terminate()
                                except Exception: pass
                    # YouTube Music: remote フラグ + terminate
                    _yt = _sys.modules.get('qji_ytmusic')
                    if _yt:
                        _yt._remote_stop_flag = True
                        for proc in list(_yt._procs.values()):
                            if proc and proc.poll() is None:
                                try: proc.terminate()
                                except Exception: pass
                    msg = 'ok'

                resp = _json.dumps({'result': msg}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(resp)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(resp)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    def run_server():
        global now_playing_server_instance
        _ss.ThreadingTCPServer.allow_reuse_address = True
        try:
            with _ss.ThreadingTCPServer(('', NOW_PLAYING_PORT), NowPlayingHandler) as httpd:
                now_playing_server_instance = httpd
                httpd.timeout = 1
                while now_playing_server_running:
                    httpd.handle_request()
        except OSError as e:
            print(f"⚠️ Now Playing server failed to start: {e}")

    now_playing_server_running = True
    now_playing_server_thread = threading.Thread(target=run_server, daemon=True)
    now_playing_server_thread.start()
    ip = get_local_ip()
    print(f"\n📱 Now Playing mirror started!")
    print(f"   🖼  Cover display   → http://{ip}:{NOW_PLAYING_PORT}/")
    print(f"   🎛  Playback control → http://{ip}:{NOW_PLAYING_PORT}/control")
    print(f"   (accessible from any device on the same Wi-Fi)\n")

def stop_now_playing_server():
    """Now Playingサーバーを停止"""
    global now_playing_server_running, now_playing_server_instance
    now_playing_server_running = False
    if now_playing_server_instance:
        try:
            now_playing_server_instance.server_close()
        except:
            pass
        now_playing_server_instance = None


# ===== ★★★ プリセット管理機能 ★★★ =====

def save_current_preset(preset_name):
    """現在の設定をプリセットとして保存"""
    global CURRENT_VOLUME, output_device, current_audio_preset, current_gain_preset
    global loudness_normalization, tinnitus_reduction_mode, gapless_mode_enabled, musikverein_echo_mode
    global upsampling_target_rate, musikverein_room_effects, air_particle_layer
    
    preset_data = {
        'volume': CURRENT_VOLUME,
        'output_device': output_device,
        'audio_preset': current_audio_preset,
        'gain_preset': current_gain_preset,
        'loudness_normalization': loudness_normalization,
        'tinnitus_reduction_mode': tinnitus_reduction_mode,
        'air_particle_layer': air_particle_layer,
        'gapless_mode': gapless_mode_enabled,
        'echo_mode': musikverein_echo_mode,
        'upsampling_rate': upsampling_target_rate,
        'musikverein_room_effects': musikverein_room_effects,  # ★★★ 追加 ★★★
        'filter_preset': current_filter_preset,               # ★★★ 追加: ジャンル別フィルタープリセット ★★★
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
    }
    
    # 既存のプリセットを読み込み
    presets = load_presets()
    
    # 新しいプリセットを追加
    presets[preset_name] = preset_data
    
    # ファイルに保存
    try:
        with open(PRESETS_FILE, 'w', encoding='utf-8') as f:
            json.dump(presets, f, ensure_ascii=False, indent=2)
        print(f"✅ Preset '{preset_name}' saved")
        return True
    except Exception as e:
        print(f"⚠️ Failed to save preset: {e}")
        return False

def load_presets():
    """保存されているプリセットを全て読み込み"""
    if not os.path.exists(PRESETS_FILE):
        return {}
    
    try:
        with open(PRESETS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Failed to load preset file: {e}")
        return {}

def apply_preset(preset_name):
    """指定したプリセットを適用"""
    global CURRENT_VOLUME, output_device, current_audio_preset, current_gain_preset
    global loudness_normalization, tinnitus_reduction_mode, gapless_mode_enabled
    global upsampling_target_rate, musikverein_room_effects, current_filter_preset
    
    presets = load_presets()
    
    if preset_name not in presets:
        print(f"⚠️ Preset '{preset_name}' not found")
        return False
    
    preset = presets[preset_name]
    
    try:
        CURRENT_VOLUME = preset.get('volume', -3)
        output_device = preset.get('output_device', 'hw:2,0')
        current_audio_preset = preset.get('audio_preset', 'none')
        current_gain_preset = preset.get('gain_preset', 'classical')
        loudness_normalization = preset.get('loudness_normalization', False)
        tinnitus_reduction_mode = preset.get('tinnitus_reduction_mode', False)
        air_particle_layer = preset.get('air_particle_layer', True)
        gapless_mode_enabled = preset.get('gapless_mode', False)
        musikverein_echo_mode = preset.get('echo_mode', 'classical')
        upsampling_target_rate = preset.get('upsampling_rate', 0)
        musikverein_room_effects = preset.get('musikverein_room_effects', True)  # ★★★ 追加 ★★★
        current_filter_preset = preset.get('filter_preset', 'musikverein')       # ★★★ 追加 ★★★
        
        print(f"✅ Preset '{preset_name}' applied")
        show_current_settings()
        return True
    except Exception as e:
        print(f"⚠️ Failed to apply preset: {e}")
        return False

def delete_preset(preset_name):
    """プリセットを削除"""
    presets = load_presets()
    
    if preset_name not in presets:
        print(f"⚠️ Preset '{preset_name}' not found")
        return False
    
    del presets[preset_name]
    
    try:
        with open(PRESETS_FILE, 'w', encoding='utf-8') as f:
            json.dump(presets, f, ensure_ascii=False, indent=2)
        print(f"✅ Preset '{preset_name}' deleted")
        return True
    except Exception as e:
        print(f"⚠️ Failed to delete preset: {e}")
        return False

def list_presets():
    """保存されているプリセット一覧を表示"""
    presets = load_presets()
    
    if not presets:
        print("\n📋 No saved presets")
        return []
    
    print("\n📋 Saved presets:")
    print("=" * 80)
    
    preset_names = list(presets.keys())
    for i, name in enumerate(preset_names, 1):
        preset = presets[name]
        print(f"{i}. {name}")
        print(f"   Volume: {preset.get('volume', 'N/A')} dB")
        print(f"   Output device: {preset.get('output_device', 'N/A')}")
        print(f"   Audio preset: {preset.get('audio_preset', 'N/A')}")
        print(f"   Gain preset: {preset.get('gain_preset', 'N/A')}")
        print(f"   Loudness norm.: {'ON' if preset.get('loudness_normalization') else 'OFF'}")
        print(f"   Tinnitus red.: {'ON' if preset.get('tinnitus_reduction_mode') else 'OFF'}")
        print(f"   Musikverein: {'ON' if preset.get('musikverein_room_effects', True) else 'OFF'}")  # ★★★ 追加 ★★★
        print(f"   Gapless: {'ON' if preset.get('gapless_mode') else 'OFF'}")
        upsample = preset.get('upsampling_rate', 0)
        upsample_str = f"{upsample//1000} kHz" if upsample > 0 else "OFF"
        print(f"   Upsampling: {upsample_str}")
        print(f"   Saved at: {preset.get('timestamp', 'N/A')}")
        print()
    
    return preset_names

def show_current_settings():
    """現在の設定を表示"""
    print("\n⚙️  Current settings:")
    print("=" * 60)
    print(f"  Volume: {CURRENT_VOLUME} dB")
    print(f"  Output device: {output_device}")
    print(f"  Audio preset: {current_audio_preset}")
    print(f"  Gain preset: {current_gain_preset}")
    print(f"  Loudness norm.: {'ON' if loudness_normalization else 'OFF'}")
    print(f"  Tinnitus red.: {'ON' if tinnitus_reduction_mode else 'OFF'}")
    print(f"  Air Particle Layer: {'ON' if air_particle_layer else 'OFF'}")
    print(f"  Musikverein room effects: {'ON' if musikverein_room_effects else 'OFF'}")  # ★★★ 追加 ★★★
    print(f"  Gapless playback: {'ON' if gapless_mode_enabled else 'OFF'}")
    upsample_str = f"{upsampling_target_rate//1000} kHz" if upsampling_target_rate > 0 else "OFF"
    print(f"  Upsampling: {upsample_str}")
    print("=" * 60)

def preset_management_menu():
    """プリセット管理メニュー"""
    while True:
        print("\n" + "=" * 60)
        print("💾 Preset management")
        print("=" * 60)
        print("1. Save current settings as a preset")
        print("2. Load and apply a preset")
        print("3. List presets")
        print("4. Delete a preset")
        print("5. Show current settings")
        print("0. Back to main menu")
        print("=" * 60)
        
        choice = input("\nSelect: ").strip()
        
        if choice == '1':
            # プリセット保存
            show_current_settings()
            preset_name = input("\nEnter preset name: ").strip()
            if not preset_name:
                print("⚠️ No preset name entered")
                continue
            
            # 既存のプリセットを確認
            presets = load_presets()
            if preset_name in presets:
                confirm = input(f"⚠️ Preset '{preset_name}' already exists. Overwrite? (y/n): ").strip().lower()
                if confirm != 'y':
                    print("Cancelled")
                    continue
            
            save_current_preset(preset_name)
        
        elif choice == '2':
            # プリセット読み込み
            preset_names = list_presets()
            if not preset_names:
                input("\nPress Enter to continue...")
                continue
            
            selection = input("\nEnter preset number to load (or name): ").strip()
            
            # 番号で選択
            if selection.isdigit():
                idx = int(selection) - 1
                if 0 <= idx < len(preset_names):
                    apply_preset(preset_names[idx])
                else:
                    print("⚠️ Invalid number")
            # 名前で選択
            else:
                apply_preset(selection)
            
            input("\nPress Enter to continue...")
        
        elif choice == '3':
            # プリセット一覧表示
            list_presets()
            input("\nPress Enter to continue...")
        
        elif choice == '4':
            # プリセット削除
            preset_names = list_presets()
            if not preset_names:
                input("\nPress Enter to continue...")
                continue
            
            selection = input("\nEnter preset number to delete (or name): ").strip()
            
            # 番号で選択
            if selection.isdigit():
                idx = int(selection) - 1
                if 0 <= idx < len(preset_names):
                    confirm = input(f"⚠️ Delete preset '{preset_names[idx]}'? (y/n): ").strip().lower()
                    if confirm == 'y':
                        delete_preset(preset_names[idx])
                else:
                    print("⚠️ Invalid number")
            # 名前で選択
            else:
                confirm = input(f"⚠️ Delete preset '{selection}'? (y/n): ").strip().lower()
                if confirm == 'y':
                    delete_preset(selection)
            
            input("\nPress Enter to continue...")
        
        elif choice == '5':
            # 現在の設定表示
            show_current_settings()
            input("\nPress Enter to continue...")
        
        elif choice == '0':
            break
        
        else:
            print("⚠️ Invalid selection")

# ===== Favorites feature =====
def load_favorites():
    """Load the favorites list (returns an empty list if the file doesn't exist)"""
    if not os.path.exists(FAVORITES_FILE):
        return []
    try:
        with open(FAVORITES_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        print(f"⚠️ Failed to read the favorites file: {e}")
        return []


def _save_favorites(favorites):
    """Save the favorites list to disk"""
    try:
        with open(FAVORITES_FILE, 'w', encoding='utf-8') as f:
            json.dump(favorites, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"⚠️ Failed to save favorites: {e}")
        return False


def _derive_auto_tags(track):
    """Derive automatic organizing tags for a favorite from the track's metadata"""
    tags = []
    for key in ('genre', 'mood'):
        val = (track.get(key) or '').strip()
        if val and val not in tags:
            tags.append(val)
    return tags


def add_favorite(track, extra_tags=None, note=''):
    """Add a track to favorites (same path updates nothing — avoids duplicates)"""
    if not track or not track.get('path'):
        return False, "No track information available to register"

    favorites = load_favorites()
    path = track['path']

    tags = _derive_auto_tags(track)
    if extra_tags:
        for t in extra_tags:
            t = t.strip()
            if t and t not in tags:
                tags.append(t)

    entry = {
        'path': path,
        'title': track.get('title', os.path.basename(path)),
        'artist': track.get('artist', ''),
        'composer': track.get('composer', ''),
        'performer': track.get('performer', ''),
        'conductor': track.get('conductor', ''),
        'album': track.get('album', ''),
        'genre': track.get('genre', ''),
        'mood': track.get('mood', ''),
        'tags': tags,
        'note': note,
        'added': time.strftime('%Y-%m-%d %H:%M:%S'),
    }

    for i, existing in enumerate(favorites):
        if existing.get('path') == path:
            # Already registered — keep existing tags/notes, just report it
            return False, f"'{entry['title']}' is already in your favorites"

    favorites.append(entry)
    if _save_favorites(favorites):
        return True, f"⭐ Added '{entry['title']}' to favorites" + (f" (tags: {', '.join(tags)})" if tags else "")
    return False, "Failed to save"


def remove_favorite(path):
    """Remove a track from favorites"""
    favorites = load_favorites()
    new_favorites = [f for f in favorites if f.get('path') != path]
    if len(new_favorites) == len(favorites):
        return False, "That track wasn't found in favorites"
    if _save_favorites(new_favorites):
        return True, "🗑️ Removed from favorites"
    return False, "Failed to save the removal"


def _all_favorite_tags(favorites):
    """Tally every tag currently used across favorites, with counts"""
    counts = {}
    for f in favorites:
        for t in f.get('tags', []):
            counts[t] = counts.get(t, 0) + 1
    return counts


def print_favorites(favorites, header="⭐ Favorites"):
    """Print the favorites list with numbering"""
    print(f"\n{header} ({len(favorites)} tracks)")
    print("=" * 70)
    if not favorites:
        print("  (none registered)")
        return
    for i, f in enumerate(favorites, 1):
        exists = "" if os.path.exists(f.get('path', '')) else "  ⚠️ file not found"
        tag_str = f" [{', '.join(f.get('tags', []))}]" if f.get('tags') else ""
        artist_line = f.get('artist') or f.get('performer') or f.get('composer') or ''
        print(f"{i:>3}. {f.get('title', '?')}  — {artist_line}{tag_str}{exists}")
        if f.get('note'):
            print(f"      💬 {f['note']}")
    print("=" * 70)


def cleanup_missing_favorites():
    """Detect favorites whose file can no longer be found, and remove them after confirmation"""
    favorites = load_favorites()
    missing = [f for f in favorites if not os.path.exists(f.get('path', ''))]
    if not missing:
        print("\n✅ No favorites are missing their file")
        return
    print(f"\n⚠️ {len(missing)} favorite(s) are missing their file:")
    for f in missing:
        print(f"  - {f.get('title', '?')} ({f.get('path', '')})")
    confirm = input("\nRemove these from favorites? (y/n): ").strip().lower()
    if confirm == 'y':
        remaining = [f for f in favorites if os.path.exists(f.get('path', ''))]
        if _save_favorites(remaining):
            print(f"🗑️ Cleaned up {len(missing)} entr{'y' if len(missing) == 1 else 'ies'}")
    else:
        print("Cancelled")


def _list_favorite_tags_numbered(favorites):
    """Print tags sorted by frequency with numbers, and return the tag list"""
    tag_counts = _all_favorite_tags(favorites)
    tags_sorted = [t for t, _ in sorted(tag_counts.items(), key=lambda x: -x[1])]
    if not tags_sorted:
        return []
    print("\n📌 Tags currently in use:")
    for i, t in enumerate(tags_sorted, 1):
        print(f"  {i:>2}. {t} ({tag_counts[t]} tracks)")
    return tags_sorted


def _prompt_tag_choice(tags_sorted, prompt_text):
    """Let the user choose a tag by number, or by name (case-insensitive)"""
    sel = input(f"\n{prompt_text}").strip()
    if not sel:
        return None
    if sel.isdigit():
        idx = int(sel) - 1
        if 0 <= idx < len(tags_sorted):
            return tags_sorted[idx]
        print("⚠️ Invalid number")
        return None
    # Match against existing tags case-insensitively
    for t in tags_sorted:
        if t.lower() == sel.lower():
            return t
    # No exact match — return as-is (downstream filtering is also case-insensitive)
    return sel


def _favorites_matching_tag(favorites, tag_query):
    """Filter favorites by tag name, matched case-insensitively"""
    q = tag_query.lower()
    return [f for f in favorites if any((t or '').lower() == q for t in f.get('tags', []))]


def favorites_menu():
    """Favorites management menu (list / filter by tag / play / edit / delete / cleanup)"""
    while True:
        favorites = load_favorites()
        print("\n" + "=" * 70)
        print("⭐ Favorites")
        print("=" * 70)
        print(f"1. Show the list (all {len(favorites)} tracks)")
        print("2. Filter and show by tag")
        print("3. Shuffle-play all favorites")
        print("4. Play tracks matching a tag")
        print("5. Pick and play a single track")
        print("6. Edit tags / notes")
        print("7. Remove from favorites")
        print("8. Clean up missing files (broken-path check)")
        print("0. Back to the main menu")
        print("=" * 70)

        choice = input("\nSelect: ").strip()

        if choice == '1':
            print_favorites(favorites)
            input("\nPress Enter to continue...")

        elif choice == '2':
            tags_sorted = _list_favorite_tags_numbered(favorites)
            if not tags_sorted:
                print("\n(No favorites have tags yet)")
                input("\nPress Enter to continue...")
                continue
            tag = _prompt_tag_choice(tags_sorted, "Enter a tag to show, by number or name: ")
            if not tag:
                continue
            filtered = _favorites_matching_tag(favorites, tag)
            print_favorites(filtered, header=f"⭐ Favorites tagged \"{tag}\"")
            input("\nPress Enter to continue...")

        elif choice == '3':
            if not favorites:
                print("\n(No favorites registered)")
                input("\nPress Enter to continue...")
                continue
            playable = [f for f in favorites if os.path.exists(f.get('path', ''))]
            if not playable:
                print("\n⚠️ None of your favorites are playable (no files found)")
                input("\nPress Enter to continue...")
                continue
            print(f"\n🎧 Shuffle-playing {len(playable)} favorite track(s)")
            play_music_with_mode_switching(playable.copy())
            continue

        elif choice == '4':
            tags_sorted = _list_favorite_tags_numbered(favorites)
            if not tags_sorted:
                print("\n(No favorites have tags yet)")
                input("\nPress Enter to continue...")
                continue
            tag = _prompt_tag_choice(tags_sorted, "Enter a tag to play, by number or name: ")
            if not tag:
                continue
            filtered = [f for f in _favorites_matching_tag(favorites, tag) if os.path.exists(f.get('path', ''))]
            if not filtered:
                print(f"\n⚠️ No playable tracks tagged \"{tag}\"")
                input("\nPress Enter to continue...")
                continue
            print(f"\n🎧 Playing {len(filtered)} track(s) tagged \"{tag}\"")
            play_music_with_mode_switching(filtered.copy())
            continue

        elif choice == '5':
            if not favorites:
                print("\n(No favorites registered)")
                input("\nPress Enter to continue...")
                continue
            print_favorites(favorites)
            sel = input("\nEnter the number of the track to play: ").strip()
            if sel.isdigit() and 1 <= int(sel) <= len(favorites):
                target = favorites[int(sel) - 1]
                if not os.path.exists(target.get('path', '')):
                    print("⚠️ File not found")
                    input("\nPress Enter to continue...")
                    continue
                print(f"\n🎧 Playing '{target.get('title')}'")
                play_music_with_mode_switching([dict(target)])
                continue
            else:
                print("⚠️ Invalid number")
                input("\nPress Enter to continue...")

        elif choice == '6':
            if not favorites:
                print("\n(No favorites registered)")
                input("\nPress Enter to continue...")
                continue
            print_favorites(favorites)
            sel = input("\nEnter the number of the track to edit: ").strip()
            if sel.isdigit() and 1 <= int(sel) <= len(favorites):
                idx = int(sel) - 1
                target = favorites[idx]
                print(f"\nCurrent tags: {', '.join(target.get('tags', [])) or '(none)'}")
                new_tags = input("New tags, comma-separated (Enter to keep, '-' to clear all): ").strip()
                if new_tags == '-':
                    target['tags'] = []
                elif new_tags:
                    target['tags'] = [t.strip() for t in new_tags.split(',') if t.strip()]
                print(f"Current note: {target.get('note') or '(none)'}")
                new_note = input("New note (Enter to keep, '-' to clear): ").strip()
                if new_note == '-':
                    target['note'] = ''
                elif new_note:
                    target['note'] = new_note
                if _save_favorites(favorites):
                    print("✅ Updated")
                input("\nPress Enter to continue...")
            else:
                print("⚠️ Invalid number")
                input("\nPress Enter to continue...")

        elif choice == '7':
            if not favorites:
                print("\n(No favorites registered)")
                input("\nPress Enter to continue...")
                continue
            print_favorites(favorites)
            sel = input("\nEnter the number of the track to remove: ").strip()
            if sel.isdigit() and 1 <= int(sel) <= len(favorites):
                target = favorites[int(sel) - 1]
                confirm = input(f"⚠️ Remove '{target.get('title')}' from favorites? (y/n): ").strip().lower()
                if confirm == 'y':
                    ok, msg = remove_favorite(target.get('path'))
                    print(msg)
            else:
                print("⚠️ Invalid number")
            input("\nPress Enter to continue...")

        elif choice == '8':
            cleanup_missing_favorites()
            input("\nPress Enter to continue...")

        elif choice == '0':
            break

        else:
            print("⚠️ Invalid selection")


# ===== イコライザー統合 =====
EQUALIZER_SCRIPT = os.path.expanduser('~/audio_equalizer.py')

def get_equalizer_sox_filters():
    """イコライザーのSoXフィルターを取得"""
    try:
        result = subprocess.run(
            ['python3', EQUALIZER_SCRIPT, '--sox-filters'],
            capture_output=True, text=True, timeout=1
        )
        filters_str = result.stdout.strip()
        return filters_str.split() if filters_str else []
    except:
        return []

def get_equalizer_ffmpeg_filter():
    """イコライザーのFFmpegフィルターを取得"""
    try:
        result = subprocess.run(
            ['python3', EQUALIZER_SCRIPT, '--ffmpeg-filter'],
            capture_output=True, text=True, timeout=1
        )
        return result.stdout.strip()
    except:
        return ""
# ===== イコライザー統合ここまで =====


# ★★★ サンプリングレート取得関数 ★★★
def get_sample_rate(file_path):
    """
    音声ファイルのサンプリングレートを取得する
    取得できない場合は44100を返す（デフォルト）
    """
    try:
        # mutagenを使用してサンプリングレートを取得
        audio = File(file_path)
        if audio and hasattr(audio.info, 'sample_rate'):
            return int(audio.info.sample_rate)
    except:
        pass
    
    # mutagenで取得できなかった場合はffprobeを使用
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'a:0',
            '-show_entries', 'stream=sample_rate',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    except:
        pass
    
    # デフォルト値を返す
    return 44100


# ★★★ 音量一定化(loudnorm)用: 実測値キャッシュ ★★★
LOUDNESS_CACHE_FILE = os.path.expanduser('~/.qji_loudness_cache.json')
_loudness_cache_lock = threading.Lock()
_loudness_cache_mem = None  # メモリ上キャッシュ（プロセス内で使い回す）


def _load_loudness_cache():
    global _loudness_cache_mem
    if _loudness_cache_mem is not None:
        return _loudness_cache_mem
    try:
        with open(LOUDNESS_CACHE_FILE, 'r', encoding='utf-8') as f:
            _loudness_cache_mem = json.load(f)
    except Exception:
        _loudness_cache_mem = {}
    return _loudness_cache_mem


def _save_loudness_cache():
    if _loudness_cache_mem is None:
        return
    try:
        with open(LOUDNESS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(_loudness_cache_mem, f, ensure_ascii=False)
    except Exception:
        pass


def measure_track_loudness(file_path, target_i='-16', target_tp='-2.0', target_lra='11'):
    """
    ffmpeg loudnorm の1パス目（解析のみ・出力なし）を実行し、
    そのファイル固有の実測ラウドネス値(measured_I/TP/LRA/thresh, offset)を取得する。
    同一ファイル(パス+更新日時+サイズ)は ~/.qji_loudness_cache.json にキャッシュして再解析を省略する。

    戻り値: dict（measured_I, measured_TP, measured_LRA, measured_thresh, offset） または None（失敗時）
    """
    try:
        st = os.stat(file_path)
        cache_key = f"{file_path}|{st.st_mtime}|{st.st_size}|{target_i}|{target_tp}|{target_lra}"
    except OSError:
        return None

    with _loudness_cache_lock:
        cache = _load_loudness_cache()
        if cache_key in cache:
            return cache[cache_key]

    cmd = [
        'ffmpeg', '-hide_banner', '-nostats',
        '-i', file_path,
        '-af', f'loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json',
        '-f', 'null', '-'
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        stderr = result.stderr or ''
        start = stderr.rfind('{')
        end = stderr.rfind('}')
        if start == -1 or end == -1 or end < start:
            return None
        stats = json.loads(stderr[start:end + 1])
        measured = {
            'measured_I':      stats.get('input_i'),
            'measured_TP':     stats.get('input_tp'),
            'measured_LRA':    stats.get('input_lra'),
            'measured_thresh': stats.get('input_thresh'),
            'offset':          stats.get('target_offset'),
        }
        # 実測値が異常(nan/inf等)な場合は使わない
        for v in measured.values():
            fv = float(v)
            if fv != fv or fv in (float('inf'), float('-inf')):
                return None
    except Exception:
        return None

    with _loudness_cache_lock:
        cache = _load_loudness_cache()
        cache[cache_key] = measured
        _save_loudness_cache()

    return measured


def _format_linear_loudnorm(measured, target_i='-16', target_tp='-2.0', target_lra='11'):
    """実測値(measure_track_loudness の戻り値)から2パス(linear=true)の
    loudnormフィルター文字列を組み立てる。末尾にカンマを含む。"""
    return (
        f'loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:'
        f"measured_I={measured['measured_I']}:"
        f"measured_TP={measured['measured_TP']}:"
        f"measured_LRA={measured['measured_LRA']}:"
        f"measured_thresh={measured['measured_thresh']}:"
        f"offset={measured['offset']}:linear=true,"
    )


def _peek_cached_loudness(file_path, target_i='-16', target_tp='-2.0', target_lra='11'):
    """ffmpegを一切呼ばず、キャッシュに実測値が既にあるかどうかだけを確認する。
    os.stat()＋辞書参照のみなので実質コスト0。再生開始を絶対にブロックしない
    ためにこれを使い、無ければ従来の1パス方式にフォールバックする。"""
    if not file_path:
        return None
    try:
        st = os.stat(file_path)
        cache_key = f"{file_path}|{st.st_mtime}|{st.st_size}|{target_i}|{target_tp}|{target_lra}"
    except OSError:
        return None
    with _loudness_cache_lock:
        cache = _load_loudness_cache()
        return cache.get(cache_key)


def _prefetch_loudness_async(file_path, target_i='-16', target_tp='-2.0', target_lra='11'):
    """バックグラウンドスレッドで実測してキャッシュへ書き込む（再生は一切ブロックしない）。
    次に再生される曲の"先読み"や、未実測だった曲の"次回のための実測"に使う。"""
    if not file_path:
        return
    def _worker():
        try:
            measure_track_loudness(file_path, target_i, target_tp, target_lra)
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True, name='loudness-prefetch').start()


def build_loudnorm_filter(file_path=None, target_i='-16', target_tp='-2.0', target_lra='11'):
    """
    音量一定化フィルター文字列を構築する。
    file_path が指定され、かつ実測に成功した場合は2パス(linear=true)の
    正確なラウドネス正規化を行う（曲ごとの音量差を実際に揃える）。
    file_path が無い(ラジオ/AirPlay等のライブストリーム)、または実測失敗時は
    従来通りのリアルタイム1パス(dynamic)方式にフォールバックする。
    ★ 注意: 実測がキャッシュに無い場合ここでffmpegの解析パスを同期実行するため、
    再生開始をブロックしたくない箇所（通常/ランダム再生）では使わないこと。
    そちらは _peek_cached_loudness + _prefetch_loudness_async を使う。
    末尾にカンマを含む形式で返す（空文字なら未使用）。
    """
    if file_path:
        measured = measure_track_loudness(file_path, target_i, target_tp, target_lra)
        if measured:
            return _format_linear_loudnorm(measured, target_i, target_tp, target_lra)
    # フォールバック：ライブストリーム、または実測失敗時
    return f'loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra},'


# ★★★ ゲインプリセット設定 ★★★
GAIN_PRESETS = {
    'classical': 0.0,      # クラシック用：0dB（歪みを防ぐ）
    'general': -1.5,       # 汎用：-1.5dB（バランス型）
    'jazz_pop': -3.5,      # ジャズ・ポップス用：-3.5dB（大きい音に対応）
    'loud': -5.0           # ラウド素材用：-5dB（大音量録音・ライブ等）
}


# ★★★ 曲情報表示機能 ★★★

def display_track_info_thread():
    """曲情報を画面上部に表示するスレッド（日本語幅対応）"""
    global info_display_active, current_track_info, musikverein_room_effects
    
    try:
        with terminal_io_lock:
            sys.stdout.write("\033[?25l")
            sys.stdout.flush()
        
        while info_display_active:
            # ★★★ SI入力中は画面描画を完全停止（Eventで同期） ★★★
            if not _si_display_event.wait(timeout=0.1):
                # EventがclearされているときはSI入力中 → 待機
                continue
            with info_display_lock:
                info = current_track_info.copy()
            
            if info['title']:
                try:
                    with terminal_io_lock:
                        sys.stdout.write("\033[s")
                        sys.stdout.write("\033[H")

                        bar = "=" * 78
                        # ★★★ モード表示: Musikverein / 奏在 / 両方 の3条件 ★★★
                        if musikverein_room_effects and air_particle_layer:
                            label = " Musikverein + Kanzai Mode "   # 両方ON
                            color = "\033[1;43;30m"   # 黄色地・黒文字
                        elif musikverein_room_effects:
                            label = " Musikverein Mode "                          # Musikvereinのみ
                            color = "\033[1;42;30m"   # 緑地・黒文字
                        elif air_particle_layer:
                            label = " Kanzai Mode "             # Air Particle Layerのみ
                            color = "\033[1;45;97m"   # マゼンタ地・白文字
                        else:
                            label = None

                        if label:
                            label_w = get_display_width(label)
                            remain = max(0, 78 - label_w)
                            lpad = remain // 2
                            rpad = remain - lpad
                            header_bar = pad_string_by_width("=" * lpad + label + "=" * rpad, 78)
                            sys.stdout.write(f"{color}{header_bar}\033[0m\n")
                        else:
                            sys.stdout.write(f"\033[1;44;97m{bar}\033[0m\n")

                        title_display = truncate_string_by_width(info['title'], 74)
                        title_line = pad_string_by_width(f"♪ {title_display}", 78)
                        sys.stdout.write(f"\033[1;44;97m{title_line}\033[0m\n")

                        artist_info = []
                        if info['performer']:
                            artist_info.append(f"Performer: {info['performer']}")
                        if info['composer']:
                            artist_info.append(f"Composer: {info['composer']}")
                        if info['conductor']:
                            artist_info.append(f"指揮: {info['conductor']}")

                        if artist_info:
                            artist_text = " | ".join(artist_info)
                            artist_display = truncate_string_by_width(artist_text, 74)
                            artist_line = pad_string_by_width(f"  {artist_display}", 78)
                            sys.stdout.write(f"\033[44;97m{artist_line}\033[0m\n")

                        album_info = []
                        if info['album']:
                            album_info.append(f"💿 {info['album']}")
                        if info['tempo']:
                            album_info.append(f"{info['tempo']} BPM")
                        if info['genre']:
                            album_info.append(f"[{info['genre']}]")

                        if album_info:
                            album_text = " | ".join(album_info)
                            album_display = truncate_string_by_width(album_text, 74)
                            album_line = pad_string_by_width(f"  {album_display}", 78)
                            sys.stdout.write(f"\033[44;97m{album_line}\033[0m\n")

                        status_info = []
                        if info['total_tracks'] > 0:
                            status_info.append(f"Track {info['track_num']}/{info['total_tracks']}")
                        if info['mode']:
                            status_info.append(f"Mode: {info['mode']}")

                        if status_info:
                            status_text = " | ".join(status_info)
                            status_display = truncate_string_by_width(status_text, 74)
                            status_line = pad_string_by_width(f"  {status_display}", 78)
                            sys.stdout.write(f"\033[44;97m{status_line}\033[0m\n")

                        # ★★★ Sonia Intelligence ステータス行 ★★★
                        if SI_AVAILABLE:
                            _si_line = _si_status_line()
                            if _si_line:
                                _si_disp = truncate_string_by_width(_si_line, 74)
                                _si_padded = pad_string_by_width(f"  {_si_disp}", 78)
                                sys.stdout.write(f"\033[44;97m{_si_padded}\033[0m\n")

                        # ★★★ フィルタープリセット表示行（常時更新） ★★★
                        _fp_label = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)
                        if SI_AVAILABLE and _si_instance and _si_instance._last_was_default:
                            _fp_text = f"  🎛️  Filter: {_fp_label}  ✦auto-genre"
                        else:
                            _fp_text = f"  🎛️  Filter: {_fp_label}"
                        _fp_disp = truncate_string_by_width(_fp_text, 76)
                        _fp_padded = pad_string_by_width(_fp_disp, 78)
                        sys.stdout.write(f"\033[44;97m{_fp_padded}\033[0m\n")

                        sys.stdout.write(f"\033[44;97m{bar}\033[0m\n")
                        sys.stdout.write("\033[u")
                        sys.stdout.flush()
                except:
                    pass
            time.sleep(0.5)
    except:
        pass
    finally:
        with terminal_io_lock:
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()

def update_track_info(track_path, mode='', track_num=0, total_tracks=0):
    """曲情報を更新（エンコーディング対応強化版）"""
    global current_track_info
    
    try:
        audio = File(track_path)
        
        with info_display_lock:
            current_track_info['file_path'] = track_path
            current_track_info['mode'] = mode
            current_track_info['track_num'] = track_num
            current_track_info['total_tracks'] = total_tracks
            
            # タイトル
            title = get_tag_safe(audio, 'TIT2', 'title', 'TITLE', '\xa9nam', '©nam', 'Title')
            if not title:
                title = os.path.basename(track_path)
            current_track_info['title'] = title
            
            # アーティスト
            current_track_info['artist'] = get_tag_safe(audio, 'TPE1', 'artist', 'ARTIST', '\xa9ART', '©ART')
            
            # アルバム
            current_track_info['album'] = get_tag_safe(audio, 'TALB', 'album', 'ALBUM', '\xa9alb', '©alb')
            
            # 作曲家
            current_track_info['composer'] = get_tag_safe(audio, 'TCOM', 'composer', 'COMPOSER', '\xa9wrt', '©wrt')
            
            # 指揮者
            current_track_info['conductor'] = get_tag_safe(audio, 'TPE3', 'conductor', 'CONDUCTOR')
            
            # 演奏者
            current_track_info['performer'] = get_tag_safe(audio, 'TPE2', 'performer', 'PERFORMER', 'albumartist', 'ALBUMARTIST')
            
            # ジャンル
            current_track_info['genre'] = get_tag_safe(audio, 'TCON', 'genre', 'GENRE', '\xa9gen', '©gen')
            
            # テンポ
            current_track_info['tempo'] = get_tag_safe(audio, 'TBPM', 'bpm', 'BPM', 'tmpo')
            
            # 長さ
            if audio and hasattr(audio.info, 'length'):
                duration_sec = int(audio.info.length)
                minutes = duration_sec // 60
                seconds = duration_sec % 60
                current_track_info['duration'] = f"{minutes}:{seconds:02d}"
            else:
                current_track_info['duration'] = ''
                
    except Exception as e:
        safe_print(f"⚠️ Track info error: {e}")
        current_track_info['title'] = os.path.basename(track_path)

def clear_track_info():
    """曲情報をクリア"""
    global current_track_info
    
    with info_display_lock:
        current_track_info = {
            'title': '',
            'artist': '',
            'album': '',
            'composer': '',
            'conductor': '',
            'performer': '',
            'genre': '',
            'tempo': '',
            'mode': '',
            'track_num': 0,
            'total_tracks': 0,
            'file_path': '',
            'duration': '',
            'elapsed': 0
        }

def start_info_display():
    """曲情報表示スレッドを開始"""
    global info_display_active, info_display_thread
    
    if not info_display_active:
        info_display_active = True
        info_display_thread = threading.Thread(target=display_track_info_thread, daemon=True)
        info_display_thread.start()

def stop_info_display():
    """曲情報表示スレッドを停止"""
    global info_display_active, info_display_thread
    
    if info_display_active:
        info_display_active = False
        if info_display_thread:
            time.sleep(0.6)  # スレッドの終了を待つ
        clear_track_info()
        # 画面をクリア
        with terminal_io_lock:
            sys.stdout.write("\033[H\033[J")
            sys.stdout.flush()
# ★★★ 曲情報表示機能ここまで ★★★


# 音声認識用のグローバル変数
voice_input_result = None
voice_input_waiting = False
voice_recognition_active = False
input_received = False
USB_MIC_DEVICE_ID = None

# ジャケット選択用グローバル変数
web_selection_result = None
web_server_running = False
web_server_instance = None  # ★★★ 追加 ★★★

# 音響プリセット設定
AUDIO_PRESETS = {
    'none': None,
    'vocal': [
        'equalizer', '40', '1q', '+2.5',
        'equalizer', '60', '1q', '+3.8',
        'equalizer', '80', '1q', '+3.5',
        'equalizer', '120', '1q', '+2.0',
        'equalizer', '250', '1q', '+0',
        'equalizer', '400', '1q', '-1.2',
        'equalizer', '600', '1q', '-1.8',
        'equalizer', '1000', '1q', '+3',
        'equalizer', '2200', '2q', '+2.5',
        'equalizer', '3000', '1q', '+2.5',
        'equalizer', '5000', '2q', '+1.5',
        'equalizer', '8000', '2q', '+0.5',
        'equalizer', '10000', '1q', '+1',
        'equalizer', '12000', '1q', '+0.5',
        'bass', '6.5', '45',
        'treble', '0.5'
    ],
    'soloist': [
        'equalizer', '40', '1q', '+2.5',
        'equalizer', '60', '1q', '+3.8',
        'equalizer', '80', '1q', '+3.5',
        'equalizer', '120', '1q', '+2.0',
        'equalizer', '250', '1q', '+0',
        'equalizer', '400', '1q', '-1.2',
        'equalizer', '500', '1q', '+3.5',
        'equalizer', '600', '1q', '-1.8',
        'equalizer', '1000', '1q', '+2',
        'equalizer', '2000', '2q', '+5.5',
        'equalizer', '2200', '2q', '+2',
        'equalizer', '3000', '1q', '+1.2',
        'equalizer', '5000', '2q', '+1.5',
        'equalizer', '8000', '2q', '+0.5',
        'equalizer', '10000', '1q', '+1',
        'equalizer', '12000', '1q', '+0.5',
        'bass', '6.5', '45',
        'treble', '0.5',
        'compand', '0.3,1', '6:-70,-60,-20', '-5'
    ],
    'hall': [
        'equalizer', '40', '1q', '+2.5',
        'equalizer', '60', '1q', '+3.8',
        'equalizer', '80', '1q', '+3.5',
        'equalizer', '120', '1q', '+2.0',
        'equalizer', '250', '1q', '+0',
        'equalizer', '400', '1q', '-1.2',
        'equalizer', '600', '1q', '-1.8',
        'equalizer', '1000', '1q', '+2',
        'equalizer', '2200', '2q', '+2',
        'equalizer', '3000', '1q', '+1.2',
        'equalizer', '5000', '2q', '+1.5',
        'equalizer', '8000', '2q', '+0.5',
        'equalizer', '10000', '1q', '+1',
        'equalizer', '12000', '1q', '+0.5',
        'bass', '6.5', '45',
        'treble', '0.5',
        'reverb', '30', '50', '100', '100', '0', '5'  # 控えめなホールリバーブ
    ],
    'chamber': [
        'equalizer', '40', '1q', '+2.5',
        'equalizer', '60', '1q', '+3.8',
        'equalizer', '80', '1q', '+3.5',
        'equalizer', '120', '1q', '+2.0',
        'equalizer', '200', '1q', '-2',
        'equalizer', '250', '1q', '+0',
        'equalizer', '400', '1q', '-1.2',
        'equalizer', '600', '1q', '-1.8',
        'equalizer', '1000', '1q', '+2',
        'equalizer', '2200', '2q', '+2',
        'equalizer', '3000', '1q', '+1.2',
        'equalizer', '5000', '2q', '+1.5',
        'equalizer', '8000', '2q', '+0.5',
        'equalizer', '10000', '1q', '+1',
        'equalizer', '12000', '1q', '+0.5',
        'bass', '6.5', '45',
        'treble', '0.5',
        'reverb', '20', '50', '100', '100', '0', '5'
    ],
    'stage': [
    'equalizer', '40', '1q', '+2.5',
    'equalizer', '60', '1q', '+3.8',
    'equalizer', '80', '1q', '+3.5',
    'equalizer', '100', '1q', '+3.5',     # 超低域をさらに強調
    'equalizer', '120', '1q', '+2.0',
    'equalizer', '250', '1q', '+0',
    'equalizer', '400', '1q', '-1.2',
    'equalizer', '600', '1q', '-1.8',
    'equalizer', '1000', '1q', '+2',
    'equalizer', '2200', '2q', '+2',
    'equalizer', '3000', '1q', '+1.2',
    'equalizer', '5000', '2q', '+1.5',
    'equalizer', '8000', '2q', '+0.5',
    'equalizer', '10000', '1q', '+1',
    'equalizer', '12000', '1q', '+0.5',
    'bass', '7.5', '45',     # ベースを強化
    'treble', '0.5',
    'reverb', '15', '20', '40', '40', '2', '0'  # エコーを大幅に削減
],
    'strings': [
        'equalizer', '40', '1q', '+2.5',
        'equalizer', '60', '1q', '+3.8',
        'equalizer', '80', '1q', '+3.5',
        'equalizer', '120', '1q', '+2.0',
        'equalizer', '250', '1q', '+0',
        'equalizer', '400', '1q', '-1.2',
        'equalizer', '600', '1q', '-1.8',
        'equalizer', '1000', '1q', '+2',
        'equalizer', '2200', '2q', '+2',
        'equalizer', '3000', '2q', '+5',
        'equalizer', '5000', '2q', '+1.5',
        'equalizer', '8000', '1q', '+4',
        'equalizer', '10000', '1q', '+1.5',
        'equalizer', '12000', '1q', '+1',
        'bass', '6.5', '45',
        'treble', '1.5',
        'reverb', '15'
    ]
}

# ムードグループ
MOOD_GROUPS = {
    'positive': ['happy', 'energetic'],
    'negative': ['melancholy', 'intense'],
    'neutral': ['calm', 'ambient', 'moderate']
}

# ===== ユーティリティ関数 =====

def detect_usb_microphone():
    """USBマイクを自動検出する（改良版）"""
    global USB_MIC_DEVICE_ID
    if not VOICE_RECOGNITION_AVAILABLE:
        return None
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        print("🎤 Available recording devices:")
        candidates = []
        for i, device in enumerate(devices):
            if device.get('max_input_channels', 0) > 0:
                name = device.get('name', '')
                hostapi = device.get('hostapi', '')
                print(f"   {i}: {name} (input: {device['max_input_channels']}ch, default_samplerate: {device.get('default_samplerate')})")
                # 優先候補: 名前に usb や audio 等が含まれるもの、あるいは hostapi に "ALSA"/"Windows WASAPI" 等
                lname = name.lower()
                if any(k in lname for k in ['usb', 'audio', 'microphone', 'mic', 'alsa', 'wasapi', 'asio']):
                    candidates.append(i)
        # 優先候補があれば先頭を採用、無ければ sd.default.device の input を使う
        chosen = None
        if candidates:
            chosen = candidates[0]
            print(f"   ✅ Selected: {devices[chosen]['name']}")
        else:
            # sd.default.device は (input_idx, output_idx) のタプルを返す場合がある
            try:
                default_dev = sd.default.device
                if isinstance(default_dev, (list, tuple)):
                    chosen = int(default_dev[0]) if default_dev[0] is not None else None
                else:
                    chosen = int(default_dev)
            except Exception:
                chosen = None
            if chosen is not None:
                print(f"   ℹ Using default input device: {devices[chosen]['name']}")
            else:
                print("⚠️ Microphone auto-selection failed. Use --mic-device to specify manually.")
                return None

        USB_MIC_DEVICE_ID = chosen
        print(f"🎤 Microphone device ID: {USB_MIC_DEVICE_ID}")
        return USB_MIC_DEVICE_ID
    except Exception as e:
        print(f"⚠️ Microphone detection error: {e}")
        return None


def safe_load_database():
    """データベースを安全に読み込む"""
    if not os.path.exists(DATABASE_FILE):
        print("⚠ Database file not found")
        print(f"Run 'python3 music_mood_analyzer.py' first to create the database")
        return None
    try:
        with open(DATABASE_FILE, 'r', encoding='utf-8') as f:
            db = json.load(f)
        if not isinstance(db, list):
            print("⚠ Invalid database format")
            return None
        valid_tracks = []
        for track in db:
            if isinstance(track, dict) and 'features' in track and 'path' in track:
                valid_tracks.append(track)
        print(f"✅ Database loaded: {len(valid_tracks)} tracks")
        return valid_tracks
    except Exception as e:
        print(f"⚠ Database load error: {e}")
        return None


def get_metadata(filepath):
    """メタデータを取得"""
    try:
        audio = File(filepath, easy=True)
        if audio is None:
            return {'title': os.path.basename(filepath), 'artist': 'Unknown'}
        return {
            'title': audio.get('title', ['Unknown'])[0],
            'artist': audio.get('artist', ['Unknown'])[0]
        }
    except:
        return {'title': os.path.basename(filepath), 'artist': 'Unknown'}


def get_folder_tracks(folder_path):
    """フォルダ内の音楽ファイル一覧を取得"""
    try:
        if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
            return []
        tracks = []
        for item in sorted(os.listdir(folder_path)):
            item_path = os.path.join(folder_path, item)
            if os.path.isfile(item_path) and item.lower().endswith(SUPPORTED_EXTENSIONS):
                metadata = get_metadata(item_path)
                track_info = {
                    'path': item_path,
                    'title': metadata['title'],
                    'artist': metadata['artist'],
                    'filename': item
                }
                tracks.append(track_info)
        return tracks
    except Exception as e:
        print(f"Error getting folder tracks: {e}")
        return []


def ask_start_track(folder_tracks, auto_play_seconds=7):
    """
    フォルダー内の曲一覧を番号付きで表示し、
    ユーザーが選んだ開始曲番号（1始まり）以降のトラックリストを返す。
    Enterのみ（未入力）または '1' → 先頭から再生。
    auto_play_seconds 秒以内に入力がなければ自動的に先頭から再生開始。
    """
    if not folder_tracks:
        return folder_tracks
    print("\n📋 Track list:")
    print("─" * 60)
    for i, t in enumerate(folder_tracks, 1):
        title    = t.get('title', '') or t.get('filename', '')
        artist   = t.get('artist', '')
        title_disp  = truncate_string_by_width(title,  38)
        artist_disp = truncate_string_by_width(artist, 18)
        if artist_disp:
            print(f"  {i:3d}. {title_disp}  [{artist_disp}]")
        else:
            print(f"  {i:3d}. {title_disp}")
    print("─" * 60)
    total = len(folder_tracks)

    # ★ 1文字ずつ読み取るため、ターミナルをcbreakモード（raw風・非canonical）に設定する。
    #   これまで stty sane（canonicalモードに戻す）を呼んだ後に sys.stdin.read(1) で
    #   1文字ずつ読もうとしていたため、Enterを押すまで入力がバッファリングされてしまい、
    #   番号入力が正しく機能しない不具合があった。
    _old_term_settings = None
    try:
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass
    try:
        _old_term_settings = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
    except Exception:
        _old_term_settings = None

    deadline = time.time() + auto_play_seconds
    typed = ''

    sys.stdout.write(f"▶ Enter start track (1–{total}) — auto-play in {auto_play_seconds}s: ")
    sys.stdout.flush()

    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                print(f"\n⏱️  {auto_play_seconds}s elapsed → auto-playing from the start")
                return folder_tracks

            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch = sys.stdin.read(1)
                if ch in ('\n', '\r'):
                    raw = typed.strip()
                    print()
                    if raw == '' or raw == '0':
                        return folder_tracks
                    if raw.isdigit():
                        n = int(raw)
                        if 1 <= n <= total:
                            if n == 1:
                                return folder_tracks
                            print(f"✅ Starting from track {n}: '{folder_tracks[n-1].get('title', folder_tracks[n-1].get('filename',''))}'")
                            return folder_tracks[n - 1:]
                        else:
                            print(f"⚠️  Enter a number between 1 and {total}")
                            typed = ''
                            deadline = time.time() + auto_play_seconds
                            sys.stdout.write(f"▶ Enter start track (1–{total}) — auto-play in {auto_play_seconds}s: ")
                            sys.stdout.flush()
                    else:
                        print("⚠️  Please enter a number (Enter for track 1)")
                        typed = ''
                        deadline = time.time() + auto_play_seconds
                        sys.stdout.write(f"▶ Enter start track (1–{total}) — auto-play in {auto_play_seconds}s: ")
                        sys.stdout.flush()
                elif ch in ('\x7f', '\x08'):
                    if typed:
                        typed = typed[:-1]
                        sys.stdout.write('\b \b')
                        sys.stdout.flush()
                elif ch.isprintable():
                    typed += ch
                    sys.stdout.write(ch)
                    sys.stdout.flush()
            else:
                # 入力途中でなければ残り秒数を上書き表示
                if not typed:
                    secs_left = max(0, int(remaining) + 1)
                    sys.stdout.write(f"\r▶ Enter start track (1–{total}) — auto-play in {secs_left}s: ")
                    sys.stdout.flush()
    finally:
        # ★ 必ず元のターミナル設定（canonicalモード）に戻す
        if _old_term_settings is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _old_term_settings)
            except Exception:
                pass
        try:
            import subprocess as _sp
            _sp.run(['stty', 'sane'], check=False, timeout=1)
        except Exception:
            pass


def find_cover_image_safe(track_path):
    """ジャケット画像を安全に検索
    ①フォルダー内の画像ファイル → ②タグ埋め込みカバーアート の順で探す"""
    try:
        # ── ① フォルダー内の独立した画像ファイルを探す（従来動作）──
        folder = os.path.dirname(track_path)
        if os.path.exists(folder) and os.path.isdir(folder):
            largest_image = None
            largest_size = 0
            for item in os.listdir(folder):
                item_path = os.path.join(folder, item)
                if not os.path.isfile(item_path):
                    continue
                if not item.lower().endswith(IMAGE_EXTENSIONS):
                    continue
                try:
                    size = os.path.getsize(item_path)
                    if size > largest_size:
                        largest_size = size
                        largest_image = item_path
                except OSError:
                    continue
            if largest_image:
                return largest_image

        # ── ② タグ埋め込みカバーアートを抽出 ──
        return _extract_embedded_cover(track_path)

    except Exception as e:
        print(f"Error searching for cover art: {e}")
        return None


def _extract_embedded_cover(track_path):
    """オーディオタグに埋め込まれたカバーアートを /tmp に書き出して返す。
    対応: MP3 (APIC), M4A/AAC (covr), FLAC (PICTURE), OGG (metadata_block_picture)"""
    try:
        from mutagen import File as MutagenFile
        from mutagen.id3 import ID3NoHeaderError

        audio = MutagenFile(track_path)
        if audio is None:
            return None

        image_data = None
        mime_type  = 'image/jpeg'

        tags = audio.tags
        if tags is None:
            return None

        # ── MP3: ID3 APIC フレーム ──
        # iTunes は通常 APIC:Cover Art (Front) または APIC: に保存する
        for key in list(tags.keys()):
            if key.startswith('APIC'):
                apic = tags[key]
                if hasattr(apic, 'data') and apic.data:
                    image_data = apic.data
                    mime_type  = getattr(apic, 'mime', 'image/jpeg')
                    break

        # ── M4A / AAC: covr アトム ──
        if image_data is None and 'covr' in tags:
            covr = tags['covr']
            if covr and len(covr) > 0:
                image_data = bytes(covr[0])
                # MP4Cover.FORMAT_JPEG=13, FORMAT_PNG=14
                try:
                    from mutagen.mp4 import MP4Cover
                    if covr[0].imageformat == MP4Cover.FORMAT_PNG:
                        mime_type = 'image/png'
                except Exception:
                    pass

        # ── FLAC: mutagen.flac.Picture ──
        if image_data is None and hasattr(audio, 'pictures'):
            pics = audio.pictures
            if pics:
                # type=3 が Front Cover（ITunes準拠）、なければ先頭
                front = next((p for p in pics if p.type == 3), pics[0])
                image_data = front.data
                mime_type  = getattr(front, 'mime', 'image/jpeg')

        # ── OGG Vorbis: METADATA_BLOCK_PICTURE ──
        if image_data is None:
            mbp_list = tags.get('metadata_block_picture', [])
            if mbp_list:
                import base64
                from mutagen.flac import Picture
                try:
                    pic = Picture(base64.b64decode(mbp_list[0]))
                    image_data = pic.data
                    mime_type  = getattr(pic, 'mime', 'image/jpeg')
                except Exception:
                    pass

        if not image_data:
            return None

        # 拡張子を決定
        ext = '.jpg' if 'jpeg' in mime_type or 'jpg' in mime_type else \
              '.png' if 'png'  in mime_type else '.jpg'

        # ファイル名はパスのハッシュで一意にする（毎回上書きしない）
        import hashlib
        h = hashlib.md5(track_path.encode()).hexdigest()[:12]
        out_dir = '/tmp/qji_embedded_covers'
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f'cover_{h}{ext}')

        # 既にキャッシュ済みならそのまま返す
        if os.path.exists(out_path):
            return out_path

        with open(out_path, 'wb') as f:
            f.write(image_data)
        return out_path

    except Exception as e:
        # デバッグ時は下行のコメントを外してください
        # print(f"埋め込みカバーアート抽出エラー: {e}")
        return None


def show_cover_image(image_path):
    """ジャケット画像を表示"""
    global current_image_path
    if image_path is None:
        return None
    current_image_path = image_path
    try:
        subprocess.run(['which', 'feh'], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        print("⚠️ 'feh' command not found — skipping cover art display.")
        return None
    cmd = [
        "feh",
        "--fullscreen",
        "--auto-zoom",
        "--hide-pointer",
        "--borderless",
        "--no-menus",
        "--reload", "1",
        image_path
    ]
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"⚠️ Cover art display error: {e}")
        return None


def create_cover_with_info(image_path, track_info):
    """ジャケット画像に曲情報を重ねた画像を作成"""
    global air_particle_layer, musikverein_room_effects, current_filter_preset, current_gain_preset
    try:
        # ImageMagickの確認
        subprocess.run(['which', 'convert'], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        # ImageMagickがない場合は元の画像をそのまま返す
        return image_path
    
    try:
        # 一時ファイルを作成
        temp_dir = "/tmp/musicaplayer_covers"
        os.makedirs(temp_dir, exist_ok=True)
        output_path = os.path.join(temp_dir, "current_cover_with_info.jpg")
        
        # ★★★ 画像を一定サイズにリサイズ ★★★
        # 標準サイズを設定（例：1200x1200）
        standard_size = 1200
        
        # まず画像を標準サイズにリサイズ
        resized_path = os.path.join(temp_dir, "resized_cover.jpg")
        resize_cmd = [
            'convert', image_path,
            '-resize', f'{standard_size}x{standard_size}',
            '-background', 'black',
            '-gravity', 'center',
            '-extent', f'{standard_size}x{standard_size}',
            resized_path
        ]
        subprocess.run(resize_cmd, capture_output=True, check=True, timeout=5)
        
        # リサイズした画像のサイズを使用
        width = standard_size
        height = standard_size
        
        # テキストエリアの高さを固定
        text_area_height = 200  # 固定値
        
        # フォントサイズを固定
        title_size = 22
        info_size = 16
        small_size = 14
        
        # 曲情報を整形
        title = track_info.get('title', 'Unknown Title')[:60]
        composer = track_info.get('composer', '')
        performer = track_info.get('performer', '')
        conductor = track_info.get('conductor', '')
        album = track_info.get('album', '')[:50]
        tempo = track_info.get('tempo', '')
        genre = track_info.get('genre', '')
        
        # アーティスト情報を組み立て
        artist_line = ""
        if performer:
            artist_line = f"🎻 {performer[:40]}"
        if composer:
            if artist_line:
                artist_line += f" | 🎼 {composer[:30]}"
            else:
                artist_line = f"🎼 {composer[:40]}"
        if conductor:
            if artist_line:
                artist_line += f" | 🎭 {conductor[:25]}"
            else:
                artist_line = f"🎭 {conductor[:40]}"
        
        # アルバム/テンポ/ジャンル情報
        album_line = ""
        if album:
            album_line = f"💿 {album}"
        if tempo:
            if album_line:
                album_line += f" | 🎵 {tempo} BPM"
            else:
                album_line = f"🎵 {tempo} BPM"
        if genre:
            if album_line:
                album_line += f" | 🎸 {genre[:20]}"
            else:
                album_line = f"🎸 {genre}"
        
        # ★★★ リサイズした画像に黒い帯を追加 ★★★
        convert_cmd = [
            'convert', resized_path,
            '-gravity', 'South',
            '-background', 'black',
            '-splice', f'0x{text_area_height}',
        ]
        
        # ★★★ 日本語対応フォントを検出 ★★★
        # 日本語対応フォントの候補
        font_candidates = [
            'Noto-Sans-CJK-JP-Bold',
            'Noto-Sans-CJK-JP',
            'VL-Gothic',
            'TakaoPGothic',
            'IPAGothic',
            'DejaVu-Sans'  # フォールバック
        ]
        
        # 利用可能なフォントを検出
        jp_font = 'DejaVu-Sans'
        try:
            result = subprocess.run(['convert', '-list', 'font'], 
                                   capture_output=True, text=True, timeout=2)
            available_fonts = result.stdout
            for font in font_candidates:
                if font in available_fonts:
                    jp_font = font
                    break
        except:
            pass
        
        # 黒い背景の上に白文字を配置
        # タイトル（一番下から配置）
        y_offset = text_area_height - title_size - 10
        convert_cmd.extend([
            '-fill', 'white',
            '-pointsize', str(title_size),
            '-font', jp_font,
            '-annotate', f'+0+{y_offset}', f"♪ {title}",
        ])
        
        # アーティスト情報
        if artist_line:
            y_offset -= info_size + 8
            convert_cmd.extend([
                '-fill', 'white',
                '-pointsize', str(info_size),
                '-font', jp_font,
                '-annotate', f'+0+{y_offset}', artist_line,
            ])
        
        # アルバム情報
        if album_line:
            y_offset -= small_size + 8
            convert_cmd.extend([
                '-fill', 'white',
                '-pointsize', str(small_size),
                '-font', jp_font,
                '-annotate', f'+0+{y_offset}', album_line,
            ])
        
        convert_cmd.append(output_path)
        
        # 画像を生成
        subprocess.run(convert_cmd, capture_output=True, check=True, timeout=5)
        
        # ★★★ モードバッジをジャケット右上に合成（3条件） ★★★
        badge_label = None
        if musikverein_room_effects and air_particle_layer:
            badge_label = 'Musikverein\u300c\u594f\u5728\u300d\u30e2\u30fc\u30c9'
            badge_bg    = 'rgba(10,10,10,0.82)'
            badge_fg    = '#ffffff'
        elif musikverein_room_effects:
            badge_label = 'Musikverein\u30e2\u30fc\u30c9'
            badge_bg    = 'rgba(10,10,10,0.82)'
            badge_fg    = '#ffffff'
        elif air_particle_layer:
            badge_label = '\u300c\u594f\u5728\u300d\u30e2\u30fc\u30c9'
            badge_bg    = 'rgba(10,10,10,0.82)'
            badge_fg    = '#ffffff'
        
        if badge_label:
            badge_path = os.path.join(temp_dir, "mode_badge.png")
            badge_font = jp_font
            try:
                badge_cmd = [
                    'convert',
                    '-size', '320x48',
                    'xc:none',
                    '-fill', badge_bg,
                    '-draw', 'roundrectangle 0,0 319,47 12,12',
                    '-font', badge_font,
                    '-pointsize', '19',
                    '-fill', badge_fg,
                    '-gravity', 'Center',
                    '-annotate', '+0+0', badge_label,
                    badge_path
                ]
                subprocess.run(badge_cmd, capture_output=True, check=True, timeout=5)
                if os.path.exists(badge_path):
                    composite_cmd = [
                        'convert', output_path,
                        badge_path,
                        '-gravity', 'NorthEast',
                        '-geometry', '+12+12',
                        '-composite',
                        output_path
                    ]
                    subprocess.run(composite_cmd, capture_output=True, check=True, timeout=5)
            except Exception:
                pass  # バッジ生成失敗は無視して通常画像を使用
        
        # ★★★ Soundfield preset badge (top-left): mirrors the preset badge
        #     already shown in the Android/browser Now Playing mirror, on the
        #     PC's own cover-art display too ★★★
        try:
            _gain_labels_for_badge = {
                'classical': 'Classical',
                'general':   'General',
                'jazz_pop':  'Pop',
                'loud':      'Loud',
            }
            _filter_label = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)
            _gain_label = _gain_labels_for_badge.get(current_gain_preset, current_gain_preset)
            preset_badge_label = f"🎼 {_filter_label}  |  🎚 {_gain_label}"

            preset_badge_path = os.path.join(temp_dir, "preset_badge.png")
            preset_badge_cmd = [
                'convert',
                '-size', '440x48',
                'xc:none',
                '-fill', 'rgba(10,10,10,0.82)',
                '-draw', 'roundrectangle 0,0 439,47 12,12',
                '-font', jp_font,
                '-pointsize', '17',
                '-fill', '#ffffff',
                '-gravity', 'Center',
                '-annotate', '+0+0', preset_badge_label,
                preset_badge_path
            ]
            subprocess.run(preset_badge_cmd, capture_output=True, check=True, timeout=5)
            if os.path.exists(preset_badge_path):
                preset_composite_cmd = [
                    'convert', output_path,
                    preset_badge_path,
                    '-gravity', 'NorthWest',
                    '-geometry', '+12+12',
                    '-composite',
                    output_path
                ]
                subprocess.run(preset_composite_cmd, capture_output=True, check=True, timeout=5)
        except Exception:
            pass  # プリセットバッジ生成失敗は無視して通常画像を使用
        
        return output_path if os.path.exists(output_path) else image_path
    
    except Exception as e:
        # エラーが発生した場合は元の画像を返す
        print(f"⚠️ Error generating track-info image: {e}")
        return image_path


def show_cover_image_with_info(image_path, track_info=None):
    """曲情報付きジャケット画像を表示"""
    global current_image_path
    
    if image_path is None:
        return None
    
    # 曲情報がある場合は情報を重ねた画像を作成
    if track_info:
        display_image = create_cover_with_info(image_path, track_info)
    else:
        display_image = image_path
    
    current_image_path = display_image
    
    try:
        subprocess.run(['which', 'feh'], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        print("⚠️ 'feh' command not found — skipping cover art display.")
        return None
    
    cmd = [
        "feh",
        "--fullscreen",
        "--auto-zoom",
        "--hide-pointer",
        "--borderless",
        "--no-menus",
        "--reload", "1",
        display_image
    ]
    
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"⚠️ Cover art display error: {e}")
        return None


def cleanup_processes():
    """再生用プロセス(ffmpeg/aplay/feh)をクリーンアップする。

    ★★★ 重要 ★★★
    CamillaDSP / wobble はプログラム起動時に一度だけ立ち上げる「常駐」プロセスであり、
    ループバック→DACへの音声転送を担っている。ここで毎回killしてしまうと、
    ラジオ視聴やトラック切り替えのたびにDSPそのものが死んでしまい、
    以後どの音源を再生してもDSP経由の音が一切出なくなる（再起動する箇所が無いため）。
    そのためDSP/wobbleの終了処理は shutdown_dsp() に分離し、
    プログラム終了時にのみ呼び出す。
    """
    global current_processes
    
    # ★★★ 曲情報表示を停止 ★★★
    stop_info_display()
    
    for proc_name, proc in current_processes.items():
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except:
                try:
                    proc.kill()
                except:
                    pass
    current_processes = {'ffmpeg': None, 'aplay': None, 'feh': None}


def shutdown_dsp():
    """CamillaDSP + wobble の常駐プロセスを終了する。
    プログラム終了時（atexit）にのみ呼び出すこと。
    cleanup_processes() からは呼ばない。
    ★ ターミナルの自動クローズと連動するよう、即座にSIGKILLで終了させる
      （SIGTERMでの穏やかな終了待ちは行わず、体感の待ち時間を最小化する）"""
    import builtins as _bi_cleanup
    for _proc_name, _proc in [
        ('watchdog',   getattr(_bi_cleanup, '_watchdog_proc', None)),
        ('wobble',     getattr(_bi_cleanup, '_wobble_proc', None)),
        ('camilladsp', getattr(_bi_cleanup, '_cdsp_proc',   None)),
    ]:
        try:
            if _proc and _proc.poll() is None:
                _proc.kill()  # SIGKILLで即座に終了（terminate+waitより高速）
        except Exception:
            pass
    # 念のためpkillでも確実に終了（-9で即座に、タイムアウトも短縮）
    try:
        import subprocess as _sub_cleanup
        _sub_cleanup.run(['pkill', '-9', '-f', 'camilladsp'], capture_output=True, timeout=1)
        _sub_cleanup.run(['pkill', '-9', '-f', 'wobble_simple'], capture_output=True, timeout=1)
        _sub_cleanup.run(['pkill', '-9', '-f', 'wobble_v3'], capture_output=True, timeout=1)
        _sub_cleanup.run(['pkill', '-9', '-f', 'cdsp_watchdog'], capture_output=True, timeout=1)
    except Exception:
        pass
    setattr(_bi_cleanup, '_cdsp_proc', None)
    setattr(_bi_cleanup, '_wobble_proc', None)
    setattr(_bi_cleanup, '_watchdog_proc', None)


def _scan_dac_list():
    """接続中のALSAカード + BlueALSAを走査し、DSP出力先の候補一覧を返す。
    起動時のDAC選択と「DSP出力デバイスの再認識」メニューの両方から使う。"""
    import re as _re2, shutil as _sh2, subprocess as _sp2
    _dac_list = {}
    try:
        with open('/proc/asound/cards', 'r') as _ac:
            for _line in _ac:
                _m2 = _re2.match(r'^\s*(\d+)\s+\[([^\]]+)\]:\s+\S+\s+-\s+(.+)', _line)
                if _m2:
                    _cnum2 = _m2.group(1)
                    _cname2 = _m2.group(2).strip()
                    _clabel2 = _m2.group(3).strip()
                    if 'Loopback' not in _cname2 and 'PCH' not in _cname2 and 'HDMI' not in _cname2:
                        _dac_list[_cnum2] = {
                            'device': f'plughw:{_cnum2},0',
                            'name': _clabel2,
                            'type': 'alsa',
                            'format': 'S32_LE',
                            'samplerate': '48000'
                        }
    except Exception:
        pass
    if _sh2.which('bluealsa-aplay') or os.path.exists('/var/run/bluealsa'):
        try:
            _ba_result = _sp2.run(
                ['bluealsa-aplay', '--list-pcms'],
                capture_output=True, text=True, timeout=5
            )
            _ba_lines = _ba_result.stdout.splitlines()
            _ba_idx = 0
            for _bi2, _bline in enumerate(_ba_lines):
                if _bline.startswith('bluealsa:') and 'a2dp' in _bline.lower():
                    _ba_name = _ba_lines[_bi2 + 1].strip() if _bi2 + 1 < len(_ba_lines) else 'Bluetooth'
                    # キーは常に'b'に統一（複数BTデバイスは最初の1つを使用）
                    _dac_list['b'] = {
                        'device': _bline.strip(),
                        'name': f'🎧 {_ba_name.split(",")[0]}',
                        'type': 'bluealsa',
                        'format': 'S16_LE',
                        'samplerate': '48000'
                    }
                    _ba_idx += 1
                    break  # 最初の1デバイスのみ使用
            if _ba_idx == 0:
                _dac_list['b'] = {
                    'device': 'bluealsa',
                    'name': '🎧 Bluetooth (BlueALSA — connect a device first)',
                    'type': 'bluealsa',
                    'format': 'S16_LE',
                    'samplerate': '48000'
                }
        except Exception:
            _dac_list['b'] = {
                'device': 'bluealsa',
                'name': '🎧 Bluetooth (BlueALSA)',
                'type': 'bluealsa',
                'format': 'S16_LE',
                'samplerate': '48000'
            }
    return _dac_list


def _select_dac_interactive(_dac_list):
    """Qji標準スタイルでDAC選択メニューを表示する。Enterのみでhw:2,0を自動選択。"""
    print('\n' + '=' * 60)
    print('🔊 Select DSP output device')
    print('=' * 60)
    for _k2, _v2 in sorted(_dac_list.items()):
        _marker = '  ◀ default' if _v2['device'] in ('plughw:2,0', 'hw:2,0') else ''
        print(f'  {_k2}. {_v2["device"]:35s}  {_v2["name"]}{_marker}')
    print(f'\n  Enter only → auto-select the default DSP output card (hw:2,0)')
    print('=' * 60)
    try:
        _dac_raw = input('Enter a number (Enter for hw:2,0): ').strip()
    except (EOFError, KeyboardInterrupt):
        _dac_raw = ''
    if _dac_raw == '':
        _dac_device = 'plughw:2,0'
        _dac_info = {
            'device': _dac_device, 'name': 'DSP default output',
            'type': 'alsa', 'format': 'S32_LE', 'samplerate': '48000'
        }
        print(f'✅ Auto-selected the default DSP output card, hw:2,0')
    elif _dac_raw in _dac_list:
        _dac_info = _dac_list[_dac_raw]
        _dac_device = _dac_info['device']
        print(f'🔊 DSP output device: {_dac_device}  ({_dac_info["name"]})')
    else:
        _dac_device = 'plughw:2,0'
        _dac_info = {
            'device': _dac_device, 'name': 'DSP default output',
            'type': 'alsa', 'format': 'S32_LE', 'samplerate': '48000'
        }
        print(f'⚠️ No valid number entered — auto-selected hw:2,0')
    return _dac_device, _dac_info


def _write_dsp_output_device(_dac_device, _dac_info):
    """spatial_final.yml内のplayback deviceを行単位で書き換える（正規表現不使用）"""
    _HOME = os.path.expanduser("~")
    _yml_path = f'{_HOME}/camilladsp_test/spatial_final.yml'
    _is_bluealsa = _dac_info['type'] == 'bluealsa'
    _target_format = 'S16_LE' if _is_bluealsa else 'S32_LE'
    try:
        with open(_yml_path, 'r') as _yf:
            _lines = _yf.readlines()
        _in_playback = False
        _device_replaced = False
        _format_replaced = False
        _new_lines = []
        for _line in _lines:
            _stripped = _line.strip()
            # playbackセクションの開始を検出
            if _stripped == 'playback:':
                _in_playback = True
            elif _stripped.startswith('capture:') or _stripped.startswith('mixers:') or _stripped.startswith('filters:') or _stripped.startswith('pipeline:'):
                _in_playback = False
            # playbackセクション内のdeviceを書き換え
            if _in_playback and _stripped.startswith('device:') and not _device_replaced:
                _indent = len(_line) - len(_line.lstrip())
                _new_lines.append(' ' * _indent + f'device: "{_dac_device}"\n')
                _device_replaced = True
                continue
            # playbackセクション内のformatを書き換え
            if _in_playback and _stripped.startswith('format:') and not _format_replaced:
                _indent = len(_line) - len(_line.lstrip())
                _new_lines.append(' ' * _indent + f'format: {_target_format}\n')
                _format_replaced = True
                continue
            _new_lines.append(_line)
        with open(_yml_path, 'w') as _yf:
            _yf.writelines(_new_lines)
        print(f'🔊 DSP output: {_dac_device} / {_target_format}')
        if not _device_replaced:
            print('⚠️ No device line found')
        return _device_replaced
    except Exception as _e2:
        print(f'⚠️ Device configuration error: {_e2}')
        return False


def reinitialize_dsp_output():
    """CamillaDSP/wobbleだけを再起動し、DACを再検出・選び直す。

    CamillaDSPは起動時に一度だけ出力デバイスを開く常駐プロセスのため、
    起動後にDAC(XMOSなど)を後から接続しても、そのままでは掴み直してくれない。
    これまではQji自体を再起動するしかなかったが、この機能でCamillaDSPと
    wobbleだけを止めて再起動し、その時点で接続されているDACを新たに掴ませる。
    """
    import builtins as _bi3
    _cdsp_proc = getattr(_bi3, '_cdsp_proc', None)
    _wobble_proc = getattr(_bi3, '_wobble_proc', None)
    if _cdsp_proc is None and _wobble_proc is None:
        print("⚠️ DSP is not currently running (this menu doesn't apply if Qji was started without DSP)")
        return

    print("\n" + "=" * 60)
    print("🔄 Re-detect DSP output device")
    print("=" * 60)
    print("Stopping CamillaDSP...")
    shutdown_dsp()

    _dac_list = _scan_dac_list()
    _dac_device, _dac_info = _select_dac_interactive(_dac_list)
    if not _write_dsp_output_device(_dac_device, _dac_info):
        print("❌ Failed to rewrite the configuration — could not restart DSP")
        return

    _HOME = os.path.expanduser("~")
    _cdsp_log = open('/tmp/camilladsp.log', 'w')
    _bi3._cdsp_proc = subprocess.Popen(
        ['camilladsp', f'{_HOME}/camilladsp_test/spatial_final.yml', '--port', '1234'],
        stdout=_cdsp_log, stderr=_cdsp_log
    )
    time.sleep(3.0)

    _wobble_path = getattr(_bi3, '_wobble_script_path', None)
    if _wobble_path and os.path.exists(_wobble_path):
        _wobble_log = open('/tmp/wobble.log', 'w')
        _bi3._wobble_proc = subprocess.Popen(
            ['python3', _wobble_path],
            stdout=_wobble_log, stderr=_wobble_log
        )
        time.sleep(1.0)
    else:
        _bi3._wobble_proc = None
        print("⚠️ Couldn't locate the wobble script, so wobble was not started (the sound field itself will still play)")

    print(f"✅ DSP restarted. New output: {_dac_device}")


def _widen_pipe_buffer(pipe_obj, target_bytes=1048576):
    """ffmpeg → aplay/arecord をつなぐOSパイプのバッファを拡張する（Linux限定）。

    Linuxのパイプはデフォルトで64KB (S32LE 2ch 44.1kHzで約93ミリ秒分) しか
    保持できない。CamillaDSPのwobble制御スレッドや他の作業でCPUが一瞬でも
    競合し、ffmpeg側の書き込みが数百ミリ秒遅れると、aplay側のALSAバッファが
    どれだけ大きく設定されていても供給が追いつかず一瞬無音になる。
    F_SETPIPE_SZ でパイプ自体の容量を広げておくことで、この種の瞬間的な
    処理落ちに対する耐性を大きく上げられる（失敗しても無害・無音でフォールバック）。
    """
    try:
        import fcntl as _fcntl
        _F_SETPIPE_SZ = getattr(_fcntl, 'F_SETPIPE_SZ', 1031)  # Linux固有: python3.10未満には定数が無いため直値でフォールバック
        _fcntl.fcntl(pipe_obj.fileno(), _F_SETPIPE_SZ, target_bytes)
    except Exception:
        pass


# ===========================================================================
# ★★★ ラジオ局別音場プリセット 保存・読み込み ★★★
# ===========================================================================

# ── デフォルト音場設定（リセット用基準値）──
_RADIO_DEFAULT_PRESET = {
    'filter_preset':            'radio',       # ★ ラジオ向け軽量プリセット
    'musikverein_room_effects': True,
    'air_particle_layer':       False,         # ★ Air Particle OFFをデフォルトに
    'echo_mode':                'classical',
    'gain_preset':              'classical',
    'volume':                   12,
    'tinnitus_reduction_mode':  False,
    'loudness_normalization':   False,
    'si_eq':                    {},
    'si_base_preset':           None,
    'si_acoustic_space':        None,
}


def _collect_radio_preset() -> dict:
    """現在のラジオ音場設定を辞書にまとめる。SI EQデルタ・音響空間設定を含む完全版。"""
    data = {
        'filter_preset':            current_filter_preset,
        'musikverein_room_effects': musikverein_room_effects,
        'air_particle_layer':       air_particle_layer,
        'echo_mode':                musikverein_echo_mode,
        'gain_preset':              current_gain_preset,
        'volume':                   CURRENT_VOLUME,
        'tinnitus_reduction_mode':  tinnitus_reduction_mode,
        'loudness_normalization':   loudness_normalization,
        'si_eq':                    {},
        'si_base_preset':           None,
        'si_acoustic_space':        None,
    }
    if SI_AVAILABLE and _si_instance and _si_instance.current_params:
        p = _si_instance.current_params
        if hasattr(p, 'eq') and p.eq:
            data['si_eq'] = {k: v for k, v in p.eq.items() if abs(v) >= 0.05}
        data['si_base_preset']    = (getattr(p, 'preset_name', None)
                                     or getattr(p, 'base_preset', None))
        data['si_acoustic_space'] = getattr(p, 'acoustic_space', None)
    return data


def _apply_radio_preset(p: dict):
    """保存済みプリセット辞書 p の設定を現在のグローバルへ適用する（SI EQデルタも復元）。"""
    global current_filter_preset, musikverein_room_effects, air_particle_layer
    global musikverein_echo_mode, current_gain_preset, CURRENT_VOLUME
    global tinnitus_reduction_mode, loudness_normalization

    current_filter_preset     = p.get('filter_preset',            'radio')
    musikverein_room_effects  = p.get('musikverein_room_effects',  True)
    air_particle_layer        = p.get('air_particle_layer',        False)
    musikverein_echo_mode     = p.get('echo_mode',                 'classical')
    current_gain_preset       = p.get('gain_preset',              'classical')
    CURRENT_VOLUME            = p.get('volume',                    CURRENT_VOLUME)
    tinnitus_reduction_mode   = p.get('tinnitus_reduction_mode',   False)
    loudness_normalization    = p.get('loudness_normalization',     False)

    if SI_AVAILABLE and _si_instance:
        si_eq       = p.get('si_eq', {})
        si_base     = p.get('si_base_preset', None)
        si_acoustic = p.get('si_acoustic_space', None)
        try:
            if si_base or si_eq or si_acoustic:
                from filter_builder import params_from_preset
                params = params_from_preset(si_base or 'default')
                if hasattr(params, 'eq') and si_eq:
                    for band, gain in si_eq.items():
                        params.eq[band] = gain
                if si_acoustic and hasattr(params, 'acoustic_space'):
                    params.acoustic_space = si_acoustic
                _si_instance.current_params = params
            else:
                if _si_instance.current_params and hasattr(_si_instance.current_params, 'eq'):
                    _si_instance.current_params.eq = {}
        except Exception:
            pass


def _radio_tty_input(prompt: str) -> str:
    """ストリーム再生中に /dev/tty 経由でキー入力を受け取るユーティリティ。"""
    result = ''
    try:
        _tty = open("/dev/tty", "r", encoding="utf-8", errors="replace")
        import subprocess as _sp
        _sp.run(["stty", "sane"], stdin=_tty, check=False, timeout=1)
        print(prompt, end="", flush=True)
        result = _tty.readline().rstrip("\n").strip()
        _tty.close()
    except Exception as e:
        print(f"\n⚠️ Input error: {e}")
    return result


def _radio_preset_summary_lines(p: dict) -> list:
    """プリセット辞書を人間が読める行リストに変換する。"""
    _fp  = FILTER_PRESET_LABELS.get(p.get('filter_preset', ''), p.get('filter_preset', '---'))
    _apl = ('Kanzai Full'   if (p.get('musikverein_room_effects') and p.get('air_particle_layer'))
            else 'Musikverein only' if p.get('musikverein_room_effects')
            else 'Air Particle' if p.get('air_particle_layer') else 'OFF')
    _gain_labels = {'classical': 'Classical(0dB)', 'general': 'General(-1.5dB)', 'jazz_pop': 'Pop(-3.5dB)', 'loud': 'Loud(-5dB)'}
    _gain = _gain_labels.get(p.get('gain_preset', ''), p.get('gain_preset', '---'))
    _vol  = p.get('volume', 0)
    _echo = p.get('echo_mode', '---')
    _tin  = 'ON' if p.get('tinnitus_reduction_mode') else 'OFF'
    _loud = 'ON' if p.get('loudness_normalization')  else 'OFF'
    si_eq = p.get('si_eq', {})
    lines = [
        f"  Filter    : {_fp}",
        f"  Air Layer : {_apl}  Echo: {_echo}",
        f"  Input gain: {_gain}  Output: {_vol:+d}dB",
        f"  Tinnitus  : {_tin}  Loudness norm: {_loud}",
    ]
    if si_eq:
        _eq_str = '  '.join(f"{k}:{v:+.1f}" for k, v in si_eq.items() if abs(v) >= 0.1)
        lines.append(f"  SI EQ     : {_eq_str[:50]}")
    return lines


def _save_radio_station_preset(station):
    """
    [s]キー: 現在の音場設定（SI EQデルタ含む）をラジオ局ごとに保存する。
    ストリームは止めずにそのまま継続する。
    保存先: PRESETS_FILE の "radio_stations" セクション（キー=URL）
    """
    url  = station['url']
    name = station.get('name', url)
    preset_data = _collect_radio_preset()

    lines = _radio_preset_summary_lines(preset_data)
    print("\n")
    print("┌──────────────────────────────────────────────────────┐")
    print("│  💾  Save radio station preset                       │")
    print("├──────────────────────────────────────────────────────┤")
    print(f"│  Station : {name[:41]:<41} │")
    print("├──────────────────────────────────────────────────────┤")
    for line in lines:
        print(f"│{line:<54} │")
    print("├──────────────────────────────────────────────────────┤")
    print("│  Save these settings for this station?               │")
    print("│    [y] Save  /  Enter = Cancel                       │")
    print("└──────────────────────────────────────────────────────┘")

    choice = _radio_tty_input("  Select → ")
    if choice.lower() != 'y':
        print("  Cancelled")
        return

    preset_data['station_name'] = name
    preset_data['timestamp']    = time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        presets = load_presets()
        if 'radio_stations' not in presets:
            presets['radio_stations'] = {}
        presets['radio_stations'][url] = preset_data
        with open(PRESETS_FILE, 'w', encoding='utf-8') as f:
            json.dump(presets, f, ensure_ascii=False, indent=2)
        _si_mark = '（SI EQデルタ含む）' if preset_data.get('si_eq') else ''
        print(f"\n  ✅ Audio settings saved for '{name}' {_si_mark}")
        print(f"     Will load automatically next time this station plays")
    except Exception as e:
        print(f"\n  ⚠️ Save failed: {e}")


def _load_radio_station_preset(station) -> bool:
    """起動時専用: 保存済みプリセットを自動読み込みして適用する。"""
    url = station['url']
    try:
        presets = load_presets()
        radio_presets = presets.get('radio_stations', {})
        if url not in radio_presets:
            return False
        _apply_radio_preset(radio_presets[url])
        return True
    except Exception as e:
        print(f"⚠️ Radio preset load error: {e}")
        return False


def _copy_from_station_menu(current_station) -> bool:
    """
    [l]キー: 保存済み局プリセット一覧を表示し、選んだ局の設定を現在の局に適用する。
    戻り値 True → 呼び出し側でストリームを再起動する。
    """
    try:
        presets   = load_presets()
        radio_all = presets.get('radio_stations', {})
    except Exception as e:
        print(f"\n  ⚠️ Preset load error: {e}")
        return False

    current_url    = current_station['url']
    other_stations = [(url, p) for url, p in radio_all.items() if url != current_url]

    if not other_stations:
        print("\n  ℹ️  No presets saved for other stations")
        print("     Save settings for other stations first with [s], then try again")
        _radio_tty_input("  [Press Enter to go back] ")
        return False

    print("\n")
    print("┌──────────────────────────────────────────────────────┐")
    print("│  📋  Saved radio station presets                     │")
    print("│  ─ Select a station number to copy settings from ─   │")
    print("├──────────────────────────────────────────────────────┤")
    for i, (url, p) in enumerate(other_stations, 1):
        sname = p.get('station_name', url)[:38]
        ts    = p.get('timestamp', '')[:10]
        _fp   = FILTER_PRESET_LABELS.get(p.get('filter_preset', ''), '---')
        _apl  = ('Kanzai Full'  if (p.get('musikverein_room_effects') and p.get('air_particle_layer'))
                 else 'Musikverein'  if p.get('musikverein_room_effects')
                 else 'AirLayer'  if p.get('air_particle_layer') else 'OFF')
        _si_mark = ' ✦SI' if p.get('si_eq') else ''
        print(f"│  {i:2d}. {sname:<38} │")
        print(f"│      {_fp} | {_apl}{_si_mark:<20}  ({ts}) │")
        print("│                                                      │")
    print("├──────────────────────────────────────────────────────┤")
    print("│  [Number] to select  /  Enter = Cancel               │")
    print("└──────────────────────────────────────────────────────┘")

    choice = _radio_tty_input("  Select number → ")
    if not choice.strip().isdigit():
        print("  Cancelled")
        return False
    idx = int(choice.strip()) - 1
    if not (0 <= idx < len(other_stations)):
        print("  Invalid number")
        return False

    src_url, src_preset = other_stations[idx]
    src_name = src_preset.get('station_name', src_url)

    print(f"\n  ── Settings from '{src_name}' ──")
    for line in _radio_preset_summary_lines(src_preset):
        print(f" {line}")
    confirm = _radio_tty_input(f"\n  Apply these settings to the current station? [y/Enter=cancel] → ")
    if confirm.lower() != 'y':
        print("  Cancelled")
        return False

    _apply_radio_preset(src_preset)
    print(f"\n  ✅ Settings from '{src_name}' applied → restarting stream")
    return True


def _reset_radio_preset() -> bool:
    """
    [0]キー: ラジオ音場設定をシステムデフォルトに戻す。
    戻り値 True → 呼び出し側でストリームを再起動する。
    """
    print("\n")
    print("┌──────────────────────────────────────────────────────┐")
    print("│  🔄  Reset audio settings to default                 │")
    print("├──────────────────────────────────────────────────────┤")
    print("│  Will revert to:                                     │")
    for line in _radio_preset_summary_lines(_RADIO_DEFAULT_PRESET):
        print(f"│{line:<54} │")
    print("├──────────────────────────────────────────────────────┤")
    print("│    [y] Reset  /  Enter = Cancel                      │")
    print("└──────────────────────────────────────────────────────┘")

    choice = _radio_tty_input("  Select → ")
    if choice.lower() != 'y':
        print("  Cancelled")
        return False

    _apply_radio_preset(_RADIO_DEFAULT_PRESET)
    print("\n  ✅ Reset to defaults → restarting stream")
    return True

# ===========================================================================
# ★★★ ラジオ局別音場プリセット ここまで ★★★
# ===========================================================================


def play_radio_stream(station):
    """
    指定されたラジオステーションをストリーミング再生する。
    _build_audio_filter_args を使用し、Air Particle Layer を含む全音場調整を適用。

    起動時: 保存済みプリセットがあれば自動読み込み（SI EQデルタ含む）
    キー操作:
      [c] フィルタープリセット変更（再起動）
      [x] SI音響プリセット選択（SI有効時のみ / 再起動）
      [s] 現在の音場設定をこの局に保存（SI EQデルタ含む / ストリーム継続）
      [l] 保存済み他局プリセット一覧→選択して適用（再起動）
      [0] デフォルト設定にリセット（再起動）
      [q] 停止してメインメニューへ
    """
    global stop_playback, current_processes
    global tinnitus_reduction_mode, musikverein_room_effects, air_particle_layer
    global current_filter_preset

    url         = station['url']
    name        = station['name']
    description = station.get('description', '')
    country     = station.get('country', '')

    try:
        _normal_term = termios.tcgetattr(sys.stdin)
    except Exception:
        _normal_term = None

    restart_stream = True
    _first_start   = True

    while restart_stream:
        restart_stream = False
        stop_playback  = False
        cleanup_processes()

        if _normal_term is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSANOW, _normal_term)
                termios.tcflush(sys.stdin, termios.TCIFLUSH)
            except Exception:
                pass

        # ── 初回起動時のみ: 保存済みプリセットを自動読み込み ──
        if _first_start:
            _first_start = False
            if _load_radio_station_preset(station):
                print(f"   💾 Loaded saved audio settings for '{name}'")
            else:
                _apply_radio_preset(_RADIO_DEFAULT_PRESET)

        print(f"\n📻 Radio: {country} {name}")
        print(f"   {description}")
        print(f"   URL: {url}")

        if upsampling_target_rate > 0:
            output_sample_rate = str(upsampling_target_rate)
            print(f"   📊 Sample rate: → {upsampling_target_rate} Hz (upsampled)")
        else:
            output_sample_rate = '48000' if dsp_mode_active else '44100'  # ★ DSPモード時は48000Hzに揃える（Loopback→CamillaDSPとレート一致）

        gain_db = GAIN_PRESETS.get(current_gain_preset, 0.0)
        print(f"   🎛️  Gain preset: {current_gain_preset} ({gain_db:+.1f} dB)")

        loudness_filter = ''
        if loudness_normalization:
            loudness_filter = 'loudnorm=I=-16:TP=-2.0:LRA=11,'
            print("   🔊 Loudness norm.: ON")

        if tinnitus_reduction_mode:
            print("   👂 Tinnitus reduction: ON")

        eq_filter = get_equalizer_ffmpeg_filter()
        eq_part   = f'{eq_filter},' if eq_filter else ''

        _fp_label = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)
        if musikverein_room_effects and air_particle_layer:
            _space_label = 'Kanzai Full'
        elif musikverein_room_effects:
            _space_label = 'Musikverein room only'
        elif air_particle_layer:
            _space_label = 'Air Particle only'
        else:
            _space_label = 'OFF'
        print(f"   🏛️  Filter preset: {_fp_label}  |  Air Particle: {_space_label}")

        # SI EQデルタが有効なら表示
        if SI_AVAILABLE and _si_instance and _si_instance.current_params:
            _p = _si_instance.current_params
            if hasattr(_p, 'eq') and _p.eq:
                _eq_active = {k: v for k, v in _p.eq.items() if abs(v) >= 0.1}
                if _eq_active:
                    _eq_str = '  '.join(f"{k}:{v:+.1f}" for k, v in _eq_active.items())
                    print(f"   🎚️  SI EQ: {_eq_str[:55]}")

        print("=" * 60)
        print("⏳ Connecting to stream...")

        filter_args = _build_audio_filter_args(
            gain_db, tinnitus_reduction_mode, musikverein_room_effects,
            loudness_filter, eq_part, CURRENT_VOLUME, air_particle_layer,
            echo_mode=musikverein_echo_mode
        )

        # ★★★ ピークモニターGUI用ログ出力: ラジオ再生でも有効化 ★★★
        # （通常再生と同じ astats+ametadata パススルー方式。音声そのものには影響しない）
        _radio_declip_log_path = f'/tmp/qji_declip_{os.getpid()}.log'
        try:
            if os.path.exists(_radio_declip_log_path):
                os.remove(_radio_declip_log_path)
        except Exception:
            pass
        filter_args = _wrap_filter_args_with_declip_monitor(filter_args, _radio_declip_log_path)

        ffmpeg_cmd = (
            [
                'ffmpeg',
                '-loglevel', 'warning',
                '-reconnect', '1',
                '-reconnect_streamed', '1',
                '-reconnect_delay_max', '10',
                # ★ icecast/shoutcastサーバーが疑似的にEOFを送ってストリームを
                #   区切ってくることがあり、これが「一瞬無音→即再生再開」の主因になる。
                '-reconnect_at_eof', '1',
                '-timeout', '15000000',
                '-i', url,
                '-vn',
                '-ar', output_sample_rate,
                '-acodec', 'pcm_s32le',
            ]
            + filter_args
            + ['-f', 's32le', '-']
        )

        aplay_cmd = [
            'aplay',
            '-D', output_device,
            '-f', 'S32_LE',
            '-r', output_sample_rate,
            '-c', '2',
            '--buffer-size=262144',
            '--period-size=32768'
        ]

        ffmpeg_proc  = None
        aplay_proc   = None
        old_settings = None

        # ★ ffmpeg/aplayのstderrを読み捨てずに放置すると、OSのパイプバッファが
        #   警告メッセージで埋まった時点でffmpeg側のwrite()がブロックし、
        #   音声処理そのものが一時停止してしまう。専用スレッドで常時読み捨てつつ、
        #   reconnect / underrun 等の重要な行だけを画面に出す。
        _ffmpeg_log_lines = []
        _stop_log_threads = threading.Event()

        def _drain_stream_stderr(proc, tag):
            try:
                for _raw_line in iter(proc.stderr.readline, b''):
                    if _stop_log_threads.is_set():
                        break
                    try:
                        _text = _raw_line.decode('utf-8', errors='replace').rstrip()
                    except Exception:
                        continue
                    if not _text:
                        continue
                    _ffmpeg_log_lines.append(f'[{tag}] {_text}')
                    if len(_ffmpeg_log_lines) > 30:
                        _ffmpeg_log_lines.pop(0)
                    _low = _text.lower()
                    if any(k in _low for k in ('reconnect', 'error', 'severe', 'eof', 'underrun', 'overrun')):
                        print(f"\n   ⚠️ [{tag}] {_text}")
            except Exception:
                pass

        try:
            ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            _widen_pipe_buffer(ffmpeg_proc.stdout)  # ★ パイプ容量を拡張(耐性の上限を底上げ)
            current_processes['ffmpeg'] = ffmpeg_proc
            threading.Thread(target=_drain_stream_stderr, args=(ffmpeg_proc, 'ffmpeg'), daemon=True).start()

            # ★★★ 本当の事前バッファリング ★★★
            # 今までは ffmpeg と aplay を同時に起動していたため、パイプの中身は
            # 常にほぼ空で「バッファリング中」の表示は実質的な意味を持っていなかった。
            # ここで aplay を起動する前に ffmpeg だけを少し先行させ、拡張した
            # パイプ(最大約1MB≒3秒弱)に音声を貯めてから再生を始めることで、
            # 開始時点から実質的な「貯金」を持った状態にする。
            _PREBUFFER_SECONDS = 4.0 if dsp_mode_active else 2.0
            print(f"⏳ Pre-buffering ({_PREBUFFER_SECONDS:.0f}s)...", end='', flush=True)
            _prebuffer_deadline = time.time() + _PREBUFFER_SECONDS
            _early_fail = False
            while time.time() < _prebuffer_deadline:
                time.sleep(0.2)
                print('.', end='', flush=True)
                if ffmpeg_proc.poll() is not None:
                    _early_fail = True
                    break

            if _early_fail:
                ret = ffmpeg_proc.poll()
                print(f"\n❌ ffmpeg exited (code: {ret})")
                time.sleep(0.2)
                if _ffmpeg_log_lines:
                    for line in _ffmpeg_log_lines[-8:]:
                        print(f"   {line}")
                print(f"\n💡 Check the URL: {url}")
                _stop_log_threads.set()
                return

            aplay_proc  = subprocess.Popen(aplay_cmd,  stdin=ffmpeg_proc.stdout, stderr=subprocess.PIPE)
            current_processes['aplay']  = aplay_proc
            threading.Thread(target=_drain_stream_stderr, args=(aplay_proc, 'aplay'), daemon=True).start()

            print("\n⏳ Checking connection...", end='', flush=True)
            for i in range(20):
                time.sleep(0.5)
                print('.', end='', flush=True)
                ret = ffmpeg_proc.poll()
                if ret is not None:
                    print(f"\n❌ ffmpeg exited (code: {ret})")
                    time.sleep(0.2)  # ドレインスレッドが最後の行を拾うのを待つ
                    if _ffmpeg_log_lines:
                        for line in _ffmpeg_log_lines[-8:]:
                            print(f"   {line}")
                    print(f"\n💡 Check the URL: {url}")
                    _stop_log_threads.set()
                    return


            _fp_now = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)
            print(f"\n✅ Connected!")
            print(f"🎵 Playing: {country} {name}")
            print(f"   🏛️  {_fp_now}  |  Air Particle: {_space_label}")
            _ctrl_hint = ("[c]Filter | [x]SI preset | [a]Air layer | [s]Save | [l]Copy station | [0]Reset | [q]Stop"
                          if SI_AVAILABLE else
                          "[c]Filter | [a]Air layer | [s]Save | [l]Copy station | [0]Reset | [q]Stop")
            print(f"   {_ctrl_hint}")
            print("=" * 60)

            old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())

            while ffmpeg_proc.poll() is None and aplay_proc.poll() is None:
                if select.select([sys.stdin], [], [], 0.3)[0]:
                    key = sys.stdin.read(1).lower()

                    if key == 'q':
                        print("\n⏹  Stopping...")
                        break

                    elif key in ('c', 'x', 'l', '0', 'a'):
                        # ── 再起動が必要なキー: ターミナル復元 → ストリーム停止 → 処理 ──
                        termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)
                        old_settings = None
                        termios.tcflush(sys.stdin, termios.TCIFLUSH)
                        for _p in (ffmpeg_proc, aplay_proc):
                            try:
                                _p.terminate(); _p.wait(timeout=2)
                            except Exception:
                                try: _p.kill()
                                except Exception: pass

                        if key == 'c':
                            _do_filter_select(station_name=f"{country} {name}".strip())
                            restart_stream = True
                        elif key == 'x' and SI_AVAILABLE:
                            # ── ラジオはon_track_startを通らないため
                            #    current_paramsが未設定の場合がある。
                            #    current_filter_presetに対応するデフォルト値で初期化する。──
                            if _si_instance.current_params is None:
                                try:
                                    from filter_builder import params_from_preset
                                    _fp_to_si = {
                                        'musikverein': 'orchestra',
                                        'piano':       'piano',
                                        'chamber':     'chamber',
                                        'vocal':       'vocal',
                                        'jazz':        'jazz',
                                    }
                                    _si_init_name = _fp_to_si.get(current_filter_preset, 'default')
                                    _si_instance.current_params = params_from_preset(_si_init_name)
                                    print(f"\n  🎛️  SI init: starting with '{_si_init_name}' base preset")
                                except Exception as _e:
                                    print(f"\n  ⚠️ SI init error: {_e}")
                            _si_do_preset_menu()
                            restart_stream = True
                        elif key == 'x' and not SI_AVAILABLE:
                            print("\n  ⚠️ Sonia Intelligence is not available")
                        elif key == 'l':
                            restart_stream = _copy_from_station_menu(station)
                        elif key == '0':
                            restart_stream = _reset_radio_preset()
                        elif key == 'a':
                            air_particle_layer = not air_particle_layer
                            if musikverein_room_effects and air_particle_layer:
                                _apl_new = 'Kanzai Full (Musikverein + Air Particle)'
                            elif musikverein_room_effects:
                                _apl_new = 'Musikverein room only (Air Particle OFF)'
                            elif air_particle_layer:
                                _apl_new = 'Air Particle only'
                            else:
                                _apl_new = 'OFF'
                            print(f"\n   🌿 Air Particle: {_apl_new}")
                            restart_stream = True

                        if _normal_term is not None:
                            try:
                                termios.tcsetattr(sys.stdin, termios.TCSANOW, _normal_term)
                                termios.tcflush(sys.stdin, termios.TCIFLUSH)
                            except Exception:
                                pass
                        if restart_stream:
                            break
                        else:
                            # キャンセルされた場合はストリームを再起動して聴き続ける
                            restart_stream = True
                            break

                    elif key == 's':
                        # ── [s] ストリーム継続したまま保存 ──
                        termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)
                        termios.tcflush(sys.stdin, termios.TCIFLUSH)
                        _save_radio_station_preset(station)
                        termios.tcflush(sys.stdin, termios.TCIFLUSH)
                        tty.setcbreak(sys.stdin.fileno())
                        _fp_now2 = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)
                        print(f"\n   🎵 Still playing: {country} {name}")
                        print(f"   🏛️  {_fp_now2}  |  {_ctrl_hint}")

            if not restart_stream:
                if ffmpeg_proc.poll() is not None and aplay_proc.poll() is not None:
                    print("\n⚠️  Stream disconnected")
                    if _ffmpeg_log_lines:
                        print("   Recent log:")
                        for line in _ffmpeg_log_lines[-8:]:
                            print(f"   {line}")

        except FileNotFoundError as e:
            print(f"\n❌ Command not found: {e}")
            print("   Install with: sudo apt install ffmpeg alsa-utils")
        except Exception as e:
            print(f"\n❌ Radio playback error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            _stop_log_threads.set()
            if old_settings is not None:
                try:
                    termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)
                except Exception:
                    pass
            try:
                termios.tcflush(sys.stdin, termios.TCIFLUSH)
            except Exception:
                pass
            cleanup_processes()

            if restart_stream:
                _new_label = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)
                print(f"\n🔄 Settings changed: {_new_label} → restarting stream...")
                time.sleep(0.5)
            else:
                print("📻 Radio playback ended\n")


# ===== ★★★ AirPlay 受信モード ★★★ =====

def _find_loopback_card():
    """snd-aloop のカード番号を返す。見つからない場合は None。
    日本語ロケール（カード N）・英語ロケール（card N）の両方に対応。"""
    try:
        result = subprocess.run(['aplay', '-l'], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if 'Loopback' in line:
                # 英語: "card 7: Loopback ..."  日本語: "カード 7: Loopback ..."
                m = re.search(r'(?:card|カード)\s+(\d+)', line, re.IGNORECASE)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return None


_AIRPLAY_METADATA_PIPE = '/tmp/qji_shairport_metadata'
_feh_proc       = None   # AirPlay カバーアート表示用 feh プロセス（永続インスタンス）
_last_pict_data = b''    # 最後に受信したカバーアートキャッシュ

def _write_shairport_conf(device_name=None, loopback_card=None):
    """shairport-sync 用設定ファイルを生成する（ALSA ループバック出力）。
    S32 / mmap=no が Amanero Combo384 との相性で必要。
    metadata セクションでカバーアートパイプを有効化する。"""
    name = device_name or _AIRPLAY_DEVICE_NAME
    card = loopback_card if loopback_card is not None else 7
    conf = (
        '// shairport-sync configuration for Qji AirPlay receiver\n\n'
        'general = {\n'
        f'  name = "{name}";\n'
        '  ignore_volume_control = "yes";\n'
        '};\n\n'
        'alsa = {\n'
        f'  output_device = "hw:{card},0";\n'
        '  output_format = "S32";\n'
        '  mmap = "no";\n'
        # mixer_control_name を完全に省略し、CTL アクセスを無効化する。
        # Loopback デバイスはミキサーを持たないため、空文字や省略でも
        # CTL を開こうとして Invalid CTL エラーが発生する。
        # use_hardware_mute_if_available = "no" でこれを防ぐ。
        '  use_hardware_mute_if_available = "no";\n'
        '};\n\n'
        'metadata = {\n'
        '  enabled = "yes";\n'
        '  include_cover_art = "yes";\n'
        f'  pipe_name = "{_AIRPLAY_METADATA_PIPE}";\n'
        '};\n'
    )
    os.makedirs(os.path.dirname(_SHAIRPORT_CONF_PATH), exist_ok=True)
    with open(_SHAIRPORT_CONF_PATH, 'w') as _f:
        _f.write(conf)
    # メタデータパイプが存在しなければ作成（named pipe）
    if not os.path.exists(_AIRPLAY_METADATA_PIPE):
        try:
            os.mkfifo(_AIRPLAY_METADATA_PIPE)
        except OSError:
            pass  # すでに存在する場合など


def _airplay_show_cover(image_data: bytes, title: str, artist: str, album: str):
    """
    カバーアートを表示する。
    - caption:@file で文字幅に関わらず自動フィット
    - feh --auto-reload 2 で永続インスタンス管理（ユーザーが閉じたときのみ再起動）
    """
    global _feh_proc, _last_pict_data

    cover_dir    = '/tmp/qji_airplay_covers'
    orig_path    = os.path.join(cover_dir, 'original_cover.jpg')
    final_path   = os.path.join(cover_dir, 'current_cover.jpg')
    text_bar     = os.path.join(cover_dir, 'text_bar.png')
    caption_file = os.path.join(cover_dir, 'caption.txt')
    tmp_path     = os.path.join(cover_dir, 'tmp_cover.jpg')
    os.makedirs(cover_dir, exist_ok=True)

    try:
        with open(orig_path, 'wb') as _f:
            _f.write(image_data)

        # ── テキスト合成（ImageMagick） ──────────────────────────────────
        if shutil.which('convert') and shutil.which('identify'):
            try:
                id_out = subprocess.run(
                    ['identify', '-format', '%w %h', orig_path],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip().split()
                img_w, img_h = int(id_out[0]), int(id_out[1])
                text_h = max(img_h // 4, 90)   # 3行分の余裕（25%）
                font   = _find_japanese_font()

                # ── フォントサイズを「文字数×幅」と「行高さ」の両方から計算 ──
                # 最も長い行が画像幅(85%)に収まるよう上限を設ける
                max_chars = max(len(title or ''), len(artist or ''),
                                len(album or ''), 10)
                sz_by_width  = int(img_w * 0.85 / (max_chars * 0.58))
                sz_by_height = max(img_h // 28, 12)
                font_sz = max(min(sz_by_width, sz_by_height, 44), 10)

                # 3行を text_h 内に均等配置（gravity South 基準）
                # 下から: album → artist → title の順
                pad  = max(font_sz // 2, 8)
                y_al = pad                       # album  （最下）
                y_ar = pad + font_sz + pad // 2  # artist （中）
                y_ti = pad + (font_sz + pad // 2) * 2  # title（最上）

                tmp_path = os.path.join(cover_dir, 'tmp_cover.jpg')
                cmd = [
                    'convert', orig_path,
                    '-gravity',    'South',
                    '-background', 'black',
                    '-splice',     f'0x{text_h}',
                    '-font',       font,
                    '-fill',       'white',
                    '-pointsize',  str(font_sz),
                ]
                if title:
                    cmd += ['-annotate', f'+0+{y_ti}', title]
                if artist:
                    cmd += ['-annotate', f'+0+{y_ar}', artist]
                if album:
                    cmd += ['-annotate', f'+0+{y_al}', album]
                cmd.append(tmp_path)

                r = subprocess.run(cmd, capture_output=True, timeout=15)
                if r.returncode != 0:
                    _airplay_log(f'annotate error: {r.stderr.decode()[:300]}')

                if os.path.isfile(tmp_path):
                    os.replace(tmp_path, final_path)
                else:
                    shutil.copy2(orig_path, final_path)
            except Exception as _e:
                _airplay_log(f'text compose error: {_e}')
                shutil.copy2(orig_path, final_path)
        else:
            shutil.copy2(orig_path, final_path)

        _airplay_log(f'show_cover: {len(image_data)}B written → {final_path}')

        # ── feh 表示（永続インスタンス） ────────────────────────────────
        env = dict(os.environ)
        env.setdefault('DISPLAY', ':0')

        if _feh_proc is not None and _feh_proc.poll() is None:
            # feh 生存中 → ファイルを書き換えるだけで --auto-reload が更新する
            _airplay_log('feh alive — auto-reload will refresh')
            return

        # feh 死亡（初回 or ユーザーが閉じた）→ 再起動
        subprocess.run(['pkill', '-f', 'feh.*current_cover'], capture_output=True)
        time.sleep(0.1)
        _airplay_log('launching feh --auto-reload 2')
        _feh_proc = subprocess.Popen(
            ['feh', '--fullscreen', '--auto-zoom', '--borderless',
             '--no-menus', '--auto-reload', '2',
             '--title', 'Now Playing', final_path],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    except Exception as _e:
        _airplay_log(f'show_cover exception: {_e}')


_AIRPLAY_COVER_LOG = '/tmp/qji_airplay_cover.log'


def _airplay_log(msg: str):
    """カバーアートポーラー専用デバッグログ（/tmp/qji_airplay_cover.log）。"""
    try:
        ts = time.strftime('%H:%M:%S')
        with open(_AIRPLAY_COVER_LOG, 'a', encoding='utf-8') as _lf:
            _lf.write(f'[{ts}] {msg}\n')
    except Exception:
        pass


def _airplay_metadata_poller(stop_event):
    """
    shairport-sync のメタデータパイプを読み続け、カバーアートを feh で表示する。
    shairport-sync はパイプに XML アイテムのストリームを書き込む:
      <item><type>core</type><code>minm</code><length>N</length><data encoding="base64">...</data></item>
      <item><type>core</type><code>asar</code>...  ← artist
      <item><type>core</type><code>asal</code>...  ← album
      <item><type>ssnc</type><code>PICT</code>...  ← cover art (very large)

    バグ修正点:
      - buf.find(b'</item>') を start から検索（先頭の残骸 </item> を踏まない）
      - <item> より前のゴミバイトを都度 trim してバッファ肥大を防ぐ
      - ライター未接続の即時 EOF をカウントで判定し busy-loop 回避
      - デバッグログを /tmp/qji_airplay_cover.log に記録
    """
    import base64
    import xml.etree.ElementTree as ET
    import select as _sel

    _title        = ''
    _artist       = ''
    _album        = ''
    global _feh_proc, _last_pict_data
    pipe_path     = _AIRPLAY_METADATA_PIPE
    buf           = b''

    # ── 起動時に古いログを消去して新規作成 ─────────────────────────────
    try:
        with open(_AIRPLAY_COVER_LOG, 'w', encoding='utf-8') as _lf:
            _lf.write(f'[{time.strftime("%H:%M:%S")}] === AirPlay cover poller started ===\n')
            _lf.write(f'[{time.strftime("%H:%M:%S")}] pipe={pipe_path}\n')
    except Exception:
        pass

    # ── ヘルパー ────────────────────────────────────────────────────────
    def _decode_data(item_el):
        """<data encoding="base64">...</data> からバイナリを取得。"""
        data_el = item_el.find('data')
        if data_el is None or data_el.text is None:
            return b''
        enc = data_el.get('encoding', '')
        raw = data_el.text.strip()
        if enc == 'base64':
            try:
                return base64.b64decode(raw)
            except Exception as _e:
                _airplay_log(f'base64 decode error: {_e}')
                return b''
        return raw.encode('utf-8', errors='replace')

    def _decode_text(item_el):
        return _decode_data(item_el).decode('utf-8', errors='replace').strip()

    # ── メインループ ────────────────────────────────────────────────────
    while not stop_event.is_set():
        if not os.path.exists(pipe_path):
            _airplay_log(f'pipe not found: {pipe_path}  — waiting...')
            time.sleep(2.0)
            continue

        try:
            _airplay_log('opening pipe...')
            fd = os.open(pipe_path, os.O_RDONLY | os.O_NONBLOCK)
            _airplay_log('pipe fd opened OK')

            with os.fdopen(fd, 'rb', buffering=0) as pipe:
                empty_streak = 0   # 連続空読み取りカウント

                while not stop_event.is_set():
                    ready, _, _ = _sel.select([pipe], [], [], 1.0)
                    if stop_event.is_set():
                        break
                    if not ready:
                        # タイムアウト — ライターがまだ接続待ち or 無音中
                        empty_streak = 0   # selectタイムアウトはEOFではない
                        continue

                    chunk = pipe.read(65536)
                    if not chunk:
                        # select が ready を返したのに空 → ライターなし(EOF)
                        empty_streak += 1
                        if empty_streak >= 3:
                            _airplay_log('pipe EOF (no writer) — reopening in 2s')
                            break
                        time.sleep(0.2)
                        continue

                    empty_streak = 0
                    buf += chunk
                    _airplay_log(f'recv {len(chunk)}B  buf={len(buf)}B')

                    # ── <item>…</item> を buf から切り出してパース ──────
                    while True:
                        start = buf.find(b'<item>')
                        if start == -1:
                            buf = b''          # <item> なし — バッファを空に
                            break
                        if start > 0:
                            buf = buf[start:]  # <item> 前のゴミを捨てる
                            start = 0

                        # </item> を <item> より後ろから検索（★バグ修正）
                        end = buf.find(b'</item>', start)
                        if end == -1:
                            break              # まだ末尾が来ていない — 次の read を待つ

                        xml_bytes = buf[start: end + 7]   # "</item>" = 7文字
                        buf       = buf[end + 7:]

                        try:
                            item = ET.fromstring(xml_bytes)
                        except ET.ParseError as _e:
                            _airplay_log(f'XML parse error: {_e}')
                            continue

                        # shairport-sync は type/code を HEX 文字列で送る
                        # 例: 'minm' → '6d696e6d', 'PICT' → '50494354'
                        def _h(s):
                            try: return bytes.fromhex(s).decode('ascii')
                            except Exception: return s

                        itype = _h(item.findtext('type') or '')
                        code  = _h(item.findtext('code') or '')
                        _airplay_log(f'item: type={itype!r}  code={code!r}')

                        if code == 'minm':
                            _title  = _decode_text(item)
                            _airplay_log(f'  → title  = {_title!r}')
                        elif code == 'asar':
                            _artist = _decode_text(item)
                            _airplay_log(f'  → artist = {_artist!r}')
                        elif code == 'asal':
                            _album  = _decode_text(item)
                            _airplay_log(f'  → album  = {_album!r}')
                        elif code == 'PICT':
                            pic = _decode_data(item)
                            _airplay_log(f'  → PICT   = {len(pic)} bytes')
                            if pic:
                                _last_pict_data = pic   # キャッシュ
                                threading.Thread(
                                    target=_airplay_show_cover,
                                    args=(pic, _title, _artist, _album),
                                    daemon=True,
                                    name='airplay-show-cover',
                                ).start()
                        elif code == 'mden':
                            # メタデータ終端: PICT なし（同一アルバム）かつ
                            # feh が死んでいれば キャッシュ画像で再表示
                            if _last_pict_data and (
                                _feh_proc is None or _feh_proc.poll() is not None
                            ):
                                _airplay_log('mden: feh dead — re-showing cached cover')
                                threading.Thread(
                                    target=_airplay_show_cover,
                                    args=(_last_pict_data, _title, _artist, _album),
                                    daemon=True,
                                    name='airplay-show-cover',
                                ).start()

        except OSError as _e:
            _airplay_log(f'OSError: {_e}')
        except Exception as _e:
            _airplay_log(f'Exception: {type(_e).__name__}: {_e}')

        if not stop_event.is_set():
            time.sleep(2.0)   # 再オープン待ち

    _airplay_log('poller stopped')
    subprocess.run(['pkill', '-f', 'feh.*current_cover'], capture_output=True)


def _stop_shairport_all():
    """systemd サービス含め全ての shairport-sync インスタンスを停止する。"""
    # systemd サービスとして動いている場合は stop（sudo 不要な場合もある）
    subprocess.run(['sudo', 'systemctl', 'stop', 'shairport-sync'],
                   capture_output=True)
    subprocess.run(['systemctl', '--user', 'stop', 'shairport-sync'],
                   capture_output=True)
    # プロセスを直接 kill
    subprocess.run(['pkill', '-f', 'shairport-sync'], capture_output=True)
    time.sleep(1.5)   # ポート 5000 の解放を待つ


def play_airplay_stream():
    """
    shairport-sync (ALSA loopback) → ffmpeg (Musikverein 等フィルター) → aplay
    snd-aloop 経由で Qji フィルターチェーンをフル適用する。

    ── キー操作 ──
      [c] フィルター変更  [h] 楽友協会  [a] Air Particle
      [x] SI プリセット   [+/-] 音量    [q] 停止
    """
    global stop_playback, current_processes
    global tinnitus_reduction_mode, musikverein_room_effects, air_particle_layer
    global current_filter_preset, CURRENT_VOLUME, musikverein_echo_mode

    # ── shairport-sync 確認 ───────────────────────────────────────────────
    if subprocess.run(['which', 'shairport-sync'], capture_output=True).returncode != 0:
        print('\n❌ shairport-sync not found.')
        input('   [Press Enter to return to menu...]')
        return

    # ── avahi-daemon 確認 ─────────────────────────────────────────────────
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'avahi-daemon'],
                      capture_output=True).returncode != 0:
        print('\n⚠️  avahi-daemon is not running.')
        print('   sudo systemctl start avahi-daemon')
        if input('   Continue anyway? [y/N]: ').strip().lower() != 'y':
            return

    # ── ALSA ループバック確認 ─────────────────────────────────────────────
    loopback_card = _find_loopback_card()
    if loopback_card is None:
        print('\n❌ ALSA loopback device not found.')
        print('   Run: sudo modprobe snd-aloop  then try again.')
        print('   To persist: add snd-aloop to /etc/modules')
        input('   [Press Enter to return to menu...]')
        return
    loopback_play = f'hw:{loopback_card},0'   # shairport-sync 出力先
    loopback_cap  = f'hw:{loopback_card},1'   # ffmpeg 入力元

    # ── systemd サービス版を含め全インスタンスを停止 ─────────────────────
    print('\n🔄 Stopping existing shairport-sync (including systemd)...')
    _stop_shairport_all()

    # ポート 5000 が解放されたか確認
    import socket
    for _retry in range(5):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
            _s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if _s.connect_ex(('127.0.0.1', 5000)) != 0:
                break
        print(f'   Waiting for port 5000 to be free... ({_retry+1}/5)')
        time.sleep(1.0)

    # ── デバイス名 ────────────────────────────────────────────────────────
    print(f'\n📡 AirPlay receiver name (default: {_AIRPLAY_DEVICE_NAME})')
    custom_name = input('   Name (Enter to keep default): ').strip()
    device_name = custom_name if custom_name else _AIRPLAY_DEVICE_NAME
    _write_shairport_conf(device_name, loopback_card=loopback_card)

    restart_stream  = True
    _keep_shairport = False   # フィルター変更時 True → shairport-sync の接続を維持

    shairport_proc = None   # while ループをまたいで保持（フィルター変更時に再利用）

    # ── カバーアート ポーラースレッド起動 ────────────────────────────────
    _cover_stop   = threading.Event()
    _cover_thread = threading.Thread(
        target=_airplay_metadata_poller,
        args=(_cover_stop,),
        daemon=True,
        name='airplay-cover-poller',
    )
    _cover_thread.start()

    try:
        while restart_stream:
            restart_stream = False
            _keep_shairport = False   # ループ先頭でリセット
            stop_playback  = False
            cleanup_processes()

            # ── フィルターチェーン構築 ────────────────────────────────────
            gain_db         = GAIN_PRESETS.get(current_gain_preset, 0.0)
            loudness_filter = 'loudnorm=I=-16:TP=-2.0:LRA=11,' if loudness_normalization else ''
            eq_filter       = get_equalizer_ffmpeg_filter()
            eq_part         = f'{eq_filter},' if eq_filter else ''
            _fp_label       = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)

            if musikverein_room_effects and air_particle_layer:
                _space_label = 'Kanzai Full'
            elif musikverein_room_effects:
                _space_label = 'Musikverein room only'
            elif air_particle_layer:
                _space_label = 'Air Particle only'
            else:
                _space_label = 'OFF'

            filter_args = _build_audio_filter_args(
                gain_db, tinnitus_reduction_mode, musikverein_room_effects,
                loudness_filter, eq_part, CURRENT_VOLUME, air_particle_layer,
                echo_mode=musikverein_echo_mode,
            )
            output_sample_rate = str(upsampling_target_rate) if upsampling_target_rate > 0 else ('48000' if dsp_mode_active else '44100')  # ★ DSPモード時は48000Hzに揃える（Loopback→CamillaDSPとレート一致）

            # ★★★ ピークモニターGUI用ログ出力: AirPlay再生でも有効化 ★★★
            _airplay_declip_log_path = f'/tmp/qji_declip_{os.getpid()}.log'
            try:
                if os.path.exists(_airplay_declip_log_path):
                    os.remove(_airplay_declip_log_path)
            except Exception:
                pass
            filter_args = _wrap_filter_args_with_declip_monitor(filter_args, _airplay_declip_log_path)

            # ── arecord → ffmpeg → aplay パイプライン ─────────────────────
            # arecord は常に S32_LE（shairport-sync が S32 で書き込むため）
            # ffmpeg 出力と aplay フォーマットは出力デバイスで分岐する:
            #   bluealsa  → WAV(S16_LE)  Bluetooth A2DP は 16bit が基本
            #   hw デバイス → raw S32_LE  Amanero 等は S32_LE のみ受け付ける
            arecord_cmd = [
                'arecord', '-D', loopback_cap,
                '-f', 'S32_LE', '-r', '44100', '-c', '2', '-t', 'raw',
            ]

            if _is_bluealsa_device(output_device):
                # BlueALSA: S16_LE WAV 出力（WAV ヘッダからフォーマット自動判定）
                # BlueALSA は 44100Hz S16_LE のみ対応。アップサンプリングは無効。
                # --buffer-size/--period-size は BlueALSA では使用しない
                ffmpeg_cmd = (
                    ['ffmpeg', '-loglevel', 'error',
                     '-f', 's32le', '-ar', '44100', '-ac', '2', '-i', 'pipe:0',
                     '-vn', '-ar', '44100', '-acodec', 'pcm_s16le']
                    + filter_args
                    + ['-f', 'wav', '-']
                )
                aplay_cmd = [
                    'aplay', '-D', output_device,
                    # WAV ヘッダでフォーマット確定するため -f/-r/-c 不要
                    # BlueALSA は buffer-size/period-size 指定不可
                ]
                _fmt_label = 'S16_LE WAV → Bluetooth A2DP'
            else:
                # ハードウェア DAC: raw S32_LE（WAV ヘッダなし）
                ffmpeg_cmd = (
                    ['ffmpeg', '-loglevel', 'error',
                     '-f', 's32le', '-ar', '44100', '-ac', '2', '-i', 'pipe:0',
                     '-vn', '-ar', output_sample_rate]
                    + filter_args
                    + ['-f', 's32le', '-']
                )
                aplay_cmd = [
                    'aplay', '-D', output_device,
                    '-f', 'S32_LE', '-r', output_sample_rate, '-c', '2',
                    '--buffer-size=262144', '--period-size=32768',
                ]
                _fmt_label = 'S32_LE raw'

            print(f'\n📡 AirPlay receiver starting: "{device_name}"')
            print(f'   Loopback: {loopback_play} → {loopback_cap} → {output_device} ({_fmt_label})')
            print(f'   🏛️  {_fp_label}  |  Air Particle: {_space_label}')
            _hint = ('[c]Filter|[x]SI preset|[a]Air layer|[h]Musikverein|[i]Image|[+/-]Vol|[q]Stop'
                     if SI_AVAILABLE else
                     '[c]Filter|[a]Air layer|[h]Musikverein|[i]Image|[+/-]Vol|[q]Stop')
            print(f'   {_hint}')
            print('=' * 60)
            print('⏳ Select "Qji" as AirPlay output on your iPhone / Mac...')

            old_settings   = None
            arecord_proc   = None
            ffmpeg_proc    = None
            aplay_proc     = None

            try:
                # shairport-sync: 生きていれば再利用（AirPlay 接続を維持するため）
                # 死んでいる場合（初回・接続断など）のみ新規起動する
                if shairport_proc is None or shairport_proc.poll() is not None:
                    _sp_log = open('/tmp/qji_shairport.log', 'w')
                    shairport_proc = subprocess.Popen(
                        ['shairport-sync', '-o', 'alsa',
                         '--configfile', _SHAIRPORT_CONF_PATH],
                        stdout=subprocess.DEVNULL,
                        stderr=_sp_log,
                    )
                    # 起動確認
                    time.sleep(1.5)
                    if shairport_proc.poll() is not None:
                        print(f'\n❌ shairport-sync failed to start (exit code: {shairport_proc.returncode})')
                        print('   Hint: if port 5000 is in use, run:')
                        print('   sudo systemctl stop shairport-sync')
                        print('   sudo systemctl disable shairport-sync')
                        print('   Details: journalctl -u shairport-sync -n 20')
                        return
                    print(f'\n✅ Waiting — select "{device_name}" via AirPlay and start playing')
                else:
                    print(f'\n🔄 Filter updated — restarting while keeping AirPlay connection...')

                # arecord はループバックキャプチャ側から読む
                _ar_log = open('/tmp/qji_arecord.log', 'w')
                arecord_proc = subprocess.Popen(
                    arecord_cmd,
                    stdout=subprocess.PIPE,
                    stderr=_ar_log,
                )

                # ffmpeg は arecord の stdout を stdin として受け取る
                _ff_log = open('/tmp/qji_ffmpeg.log', 'w')
                ffmpeg_proc = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=arecord_proc.stdout,
                    stdout=subprocess.PIPE,
                    stderr=_ff_log,
                )
                arecord_proc.stdout.close()

                _ap_log = open('/tmp/qji_aplay.log', 'w')
                aplay_proc = subprocess.Popen(
                    aplay_cmd,
                    stdin=ffmpeg_proc.stdout,
                    stderr=_ap_log,
                )
                ffmpeg_proc.stdout.close()

                current_processes['ffmpeg'] = ffmpeg_proc
                current_processes['aplay']  = aplay_proc

                # shairport-sync 起動確認は上の if ブロック内で実施済み

                old_settings = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())

                _user_quit = False
                while not _user_quit:
                    # ── キー入力チェック ──────────────────────────
                    if select.select([sys.stdin], [], [], 0.3)[0]:
                        key = sys.stdin.read(1).lower()

                        if key == 'q':
                            print('\n⏹  Stopping AirPlay receiver...')
                            _user_quit = True
                            break

                        elif key in ('+', '='):
                            CURRENT_VOLUME = min(CURRENT_VOLUME + 1, 6)
                            print(f'\n   🔊 Output gain: {CURRENT_VOLUME:+d} dB')
                            restart_stream = True; break

                        elif key == '-':
                            CURRENT_VOLUME = max(CURRENT_VOLUME - 1, -20)
                            print(f'\n   🔊 Output gain: {CURRENT_VOLUME:+d} dB')
                            restart_stream = True; break

                        elif key == 'i':
                            # ジャケット画像再表示
                            _img = current_image_path
                            if not _img or not os.path.exists(_img):
                                if current_playing_track and current_playing_track.get('path'):
                                    _img = find_cover_image_safe(current_playing_track['path'])
                            # AirPlayはshairport-syncのカバーアートを使う
                            # /tmp/qji_airplay_covers/current_cover.jpg が最新
                            _ap_cover = '/tmp/qji_airplay_covers/current_cover.jpg'
                            if not _img or not os.path.exists(_img):
                                if os.path.exists(_ap_cover):
                                    _img = _ap_cover
                            if _img and os.path.exists(_img):
                                if current_processes['feh'] and current_processes['feh'].poll() is None:
                                    try:
                                        current_processes['feh'].terminate()
                                        current_processes['feh'].wait(timeout=1)
                                    except Exception:
                                        pass
                                current_processes['feh'] = show_cover_image(_img)
                                print(f'\n🖼️ Cover redisplayed: {os.path.basename(_img)}')
                            else:
                                print('\n⚠️ Cover image not found')

                        elif key in ('c', 'x', 'a', 'h'):
                            termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)
                            old_settings = None
                            termios.tcflush(sys.stdin, termios.TCIFLUSH)
                            _keep_shairport = True
                            for _p in (arecord_proc, ffmpeg_proc, aplay_proc):
                                try: _p.terminate(); _p.wait(timeout=2)
                                except Exception:
                                    try: _p.kill()
                                    except Exception: pass

                            if key == 'c':
                                _do_filter_select(airplay_mode=True); restart_stream = True
                            elif key == 'x' and SI_AVAILABLE:
                                if _si_instance and _si_instance.current_params is None:
                                    try:
                                        from filter_builder import params_from_preset
                                        _map = {'musikverein':'orchestra','piano':'piano',
                                                'chamber':'chamber','vocal':'vocal','jazz':'jazz'}
                                        _si_instance.current_params = params_from_preset(
                                            _map.get(current_filter_preset, 'default'))
                                    except Exception as _e:
                                        print(f'\n  ⚠️ SI init error: {_e}')
                                _si_do_preset_menu(); restart_stream = True
                            elif key == 'x':
                                print('\n  ⚠️ Sonia Intelligence is not available')
                                restart_stream = True
                            elif key == 'a':
                                air_particle_layer = not air_particle_layer
                                print(f'\n   🌿 Air Particle: {"ON" if air_particle_layer else "OFF"}')
                                restart_stream = True
                            elif key == 'h':
                                musikverein_room_effects = not musikverein_room_effects
                                print(f'\n   🏛️  Musikverein: {"ON" if musikverein_room_effects else "OFF"}')
                                restart_stream = True
                            break

                    # ── プロセス死活監視・自動再起動 ──────────────
                    # shairport-sync が落ちたら接続断 → 外側ループで再起動
                    if shairport_proc.poll() is not None:
                        print('\n📡 AirPlay connection lost. Restarting...')
                        _stop_shairport_all()
                        restart_stream = True
                        break

                    # arecord/ffmpeg/aplay が落ちた場合は静かに再起動
                    # （iPhoneが未再生のときは arecord が即死するため）
                    _pipe_dead = (
                        arecord_proc.poll() is not None or
                        ffmpeg_proc.poll()  is not None or
                        aplay_proc.poll()   is not None
                    )
                    if _pipe_dead:
                        # プロセスを全部止めてから作り直す
                        for _p in (arecord_proc, ffmpeg_proc, aplay_proc):
                            try: _p.terminate(); _p.wait(timeout=1)
                            except Exception: pass
                        time.sleep(0.5)
                        _ar_log2 = open('/tmp/qji_arecord.log', 'w')
                        arecord_proc = subprocess.Popen(
                            arecord_cmd, stdout=subprocess.PIPE, stderr=_ar_log2)
                        _ff_log2 = open('/tmp/qji_ffmpeg.log', 'w')
                        ffmpeg_proc = subprocess.Popen(
                            ffmpeg_cmd, stdin=arecord_proc.stdout,
                            stdout=subprocess.PIPE, stderr=_ff_log2)
                        arecord_proc.stdout.close()
                        _ap_log2 = open('/tmp/qji_aplay.log', 'w')
                        aplay_proc = subprocess.Popen(
                            aplay_cmd, stdin=ffmpeg_proc.stdout, stderr=_ap_log2)
                        ffmpeg_proc.stdout.close()
                        current_processes['ffmpeg'] = ffmpeg_proc
                        current_processes['aplay']  = aplay_proc

                if not restart_stream and shairport_proc.poll() is not None:
                    print('\n📡 AirPlay connection lost. Restarting...')
                    _stop_shairport_all()
                    restart_stream = True

                # ── 診断: どのプロセスが先に落ちたか ─────────────────────
                if not restart_stream:
                    _dead = [(n, p) for n, p in [
                        ('shairport-sync', shairport_proc),
                        ('arecord',        arecord_proc),
                        ('ffmpeg',         ffmpeg_proc),
                        ('aplay',          aplay_proc),
                    ] if p and p.poll() is not None]
                    if _dead:
                        print('\n🔍 Exited processes:')
                        for _n, _p in _dead:
                            print(f'   {_n}: exit code {_p.returncode}')
                        print('   Logs: /tmp/qji_shairport.log  /tmp/qji_arecord.log  /tmp/qji_ffmpeg.log')

            except FileNotFoundError as e:
                print(f'\n❌ Command not found: {e}')
            except Exception as e:
                print(f'\n❌ AirPlay error: {e}')
                import traceback; traceback.print_exc()
            finally:
                # ── ターミナル復元（確実に行う） ────────────────────────────
                if old_settings is not None:
                    try: termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)
                    except Exception: pass
                try: termios.tcflush(sys.stdin, termios.TCIFLUSH)
                except Exception: pass
                # stty sane でフォールバック（cbreak 等が残った場合の保険）
                try: subprocess.run(['stty', 'sane'], stdin=sys.stdin,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception: pass

                # ── プロセス終了 ────────────────────────────────────────────
                # restart_stream=True（フィルター変更等）のときは shairport-sync を維持
                # restart_stream=False（[q] 停止・エラー）のときは全プロセスを終了
                procs_to_kill = (
                    [arecord_proc, ffmpeg_proc, aplay_proc]
                    if restart_stream
                    else [shairport_proc, arecord_proc, ffmpeg_proc, aplay_proc]
                )
                for _p in procs_to_kill:
                    if _p and _p.poll() is None:
                        try: _p.terminate(); _p.wait(timeout=2)
                        except Exception:
                            try: _p.kill()
                            except Exception: pass
                current_processes['ffmpeg'] = None
                current_processes['aplay']  = None

            if restart_stream:
                _lbl = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)
                if _keep_shairport:
                    print(f'\n🔄 Filter updated: {_lbl} — restarting (AirPlay connection kept)...')
                else:
                    print(f'\n🔄 Restarting: {_lbl} ...')
                    _stop_shairport_all()

    finally:
        # ── カバーアートスレッド停止 ─────────────────────────────────────
        _cover_stop.set()
        _cover_thread.join(timeout=3.0)
        _stop_shairport_all()
        print('\n📡 AirPlay receiver stopped\n')


# ===== ★★★ AirPlay 受信モード ここまで ★★★ =====


# ===== ★★★ UPnP/DLNA レシーバーモード ★★★ =====

def _find_japanese_font():
    """日本語フォントのパスを返す（gmediarender カバーアート用）。"""
    candidates = [
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/fonts-japanese-gothic.ttf',
        '/usr/share/fonts/truetype/takao-gothic/TakaoPGothic.ttf',
        '/usr/share/fonts/truetype/vlgothic/VL-Gothic-Regular.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    if shutil.which('fc-list'):
        try:
            out = subprocess.run(
                ['fc-list', ':lang=ja', 'file'],
                capture_output=True, text=True, timeout=3,
            ).stdout
            line = out.strip().splitlines()[0] if out.strip() else ''
            font = line.split(':')[0].strip()
            if font and os.path.isfile(font):
                return font
        except Exception:
            pass
    return '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'


def _gmrender_show_cover(cover_url: str, title: str, artist: str, album: str):
    """
    カバーアートをダウンロードし、テキストを合成してデスクトップに feh 表示。
    バックグラウンドスレッドから nice -n 19 相当で呼ばれる。
    ImageMagick (convert/identify) がない場合は素の画像のみ表示。
    """
    import urllib.request
    cover_dir = '/tmp/gmediarender_covers'
    os.makedirs(cover_dir, exist_ok=True)
    orig_path  = os.path.join(cover_dir, 'original_cover.jpg')
    final_path = os.path.join(cover_dir, 'current_cover.jpg')

    try:
        # ── カバー画像ダウンロード ──────────────────────────────────────
        req = urllib.request.Request(cover_url,
                                     headers={'User-Agent': 'Qji/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            with open(orig_path, 'wb') as f:
                f.write(resp.read())

        # ── テキスト合成（ImageMagick があれば）────────────────────────
        if shutil.which('convert') and shutil.which('identify'):
            try:
                id_out = subprocess.run(
                    ['identify', '-format', '%w %h', orig_path],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip().split()
                img_w, img_h = int(id_out[0]), int(id_out[1])
                text_h   = img_h // 8
                font     = _find_japanese_font()
                title_sz = max(img_h // 35, 12)
                artst_sz = max(img_h // 45, 10)
                album_sz = max(img_h // 50, 9)
                tmp_path = os.path.join(cover_dir, 'tmp_cover.jpg')
                subprocess.run([
                    'convert', orig_path,
                    '-gravity',    'South',
                    '-background', 'black',
                    '-splice',     f'0x{text_h}',
                    '-font',       font,
                    '-fill',       'white',
                    '-pointsize',  str(title_sz),
                    '-annotate',   f'+0+{text_h - 15}',  title,
                    '-pointsize',  str(artst_sz),
                    '-annotate',   f'+0+{text_h // 2}',  artist,
                    '-pointsize',  str(album_sz),
                    '-annotate',   '+0+15',               album,
                    tmp_path,
                ], capture_output=True, timeout=15)
                if os.path.isfile(tmp_path):
                    os.replace(tmp_path, final_path)
                else:
                    shutil.copy2(orig_path, final_path)
            except Exception:
                shutil.copy2(orig_path, final_path)
        else:
            shutil.copy2(orig_path, final_path)

        # ── feh 表示 ───────────────────────────────────────────────────
        subprocess.run(['pkill', '-f', 'feh.*current_cover'],
                       capture_output=True)
        time.sleep(0.05)
        env = dict(os.environ)
        env.setdefault('DISPLAY', ':0')
        subprocess.Popen(
            ['feh', '--fullscreen', '--auto-zoom', '--borderless',
             '--no-menus', '--title', 'Now Playing', final_path],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass   # カバー表示失敗は無音で無視


def _gmrender_cover_poller(stop_event, port=49494):
    """
    gmrender-resurrect の UPnP GetPositionInfo を 2 秒ごとポーリング。
    ログ出力形式に完全に依存しない方式でメタデータを取得しカバーアートを表示する。

    gmrender は port 49494 で UPnP HTTP サービスを公開しており、
    SOAP 経由で現在再生中のトラック情報を取得できる。
    daemon スレッドとして起動する。stop_event.set() で停止。
    """
    import urllib.request
    import html as _html
    import re as _re
    import hashlib
    import socket as _socket

    # libupnp は LAN IP にバインドするため localhost では繋がらない。
    # ダミー UDP 接続でルーティングに使われる LAN IP を動的取得する。
    def _get_lan_ip():
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as _s:
                _s.connect(('192.168.0.1', 1))   # 送信しない。OSがsrcIPを決める
                return _s.getsockname()[0]
        except Exception:
            return '127.0.0.1'

    _SOAP = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        b' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        b'<s:Body>'
        b'<u:GetPositionInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        b'<InstanceID>0</InstanceID>'
        b'</u:GetPositionInfo>'
        b'</s:Body>'
        b'</s:Envelope>'
    )
    # description.xml から AVTransport の正しい control URL を取得
    # （gmrender-resurrect のバージョンによって URL が異なるため自動検出）
    def _find_ctrl_url(ip):
        try:
            with urllib.request.urlopen(
                f'http://{ip}:{port}/description.xml', timeout=5
            ) as _r:
                _desc = _r.read().decode('utf-8')
            _m = _re.search(
                r'AVTransport.*?<controlURL>([^<]+)</controlURL>',
                _desc, _re.DOTALL
            )
            if _m:
                return f'http://{ip}:{port}{_m.group(1).strip()}'
        except Exception:
            pass
        return f'http://{ip}:{port}/upnp/control/rendertransport1'  # fallback

    _ip     = _get_lan_ip()
    _CTRL   = _find_ctrl_url(_ip)   # 例: http://192.168.11.22:49494/upnp/control/rendertransport1
    _ACTION = '"urn:schemas-upnp-org:service:AVTransport:1#GetPositionInfo"'

    last_hash = ''

    while not stop_event.is_set():
        stop_event.wait(2.0)   # 2 秒待機。stop_event.set() で即時脱出
        if stop_event.is_set():
            break
        try:
            req = urllib.request.Request(
                _CTRL, data=_SOAP,
                headers={
                    'Content-Type': 'text/xml; charset="utf-8"',
                    'SOAPAction': _ACTION,
                }
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = resp.read().decode('utf-8')

            # TrackMetaData タグを取得（DIDL-Lite が HTML エンコードで入っている）
            m = _re.search(r'<TrackMetaData>(.*?)</TrackMetaData>', body, _re.DOTALL)
            if not m:
                continue
            raw = m.group(1).strip()
            if not raw or raw in ('NOT_IMPLEMENTED', 'NO_MEDIA_PRESENT', ''):
                continue

            meta = _html.unescape(raw)   # &lt; → < 等を戻す

            h = hashlib.md5(meta.encode()).hexdigest()
            if h == last_hash:
                continue
            last_hash = h

            title  = _re.search(r'<dc:title>([^<]*)</dc:title>',               meta)
            artist = _re.search(r'<upnp:artist>([^<]*)</upnp:artist>',         meta)
            album  = _re.search(r'<upnp:album>([^<]*)</upnp:album>',           meta)
            cover  = _re.search(r'<upnp:albumArtURI>([^<]*)</upnp:albumArtURI>', meta)

            if not title or not cover:
                continue

            threading.Thread(
                target=_gmrender_show_cover,
                args=(
                    cover.group(1).strip(),
                    title.group(1).strip(),
                    artist.group(1).strip() if artist else '',
                    album.group(1).strip()  if album  else '',
                ),
                daemon=True,
            ).start()

        except Exception:
            pass   # gmrender 起動前・停止後は例外が出るが無視


def _stop_gmediarender_all():
    """実行中の gmediarender プロセスを全て停止する。
    sudo systemctl は capture_output 下でパスワードプロンプトがタイムアウトするため使わない。
    ユーザーサービスは --user で試み、残存プロセスは pkill で確実に止める。
    """
    # ユーザーサービス停止（sudo 不要）
    for svc in ['gmediarender', 'gmediarenderer']:
        subprocess.run(['systemctl', '--user', 'stop', svc],
                       capture_output=True, timeout=3)
    # プロセス直接終了（サービス登録の有無にかかわらず確実に止まる）
    subprocess.run(['pkill', '-x', 'gmediarender'], capture_output=True)
    subprocess.run(['pkill', '-f', 'feh.*current_cover'], capture_output=True)
    time.sleep(0.5)


def play_gmediarender_stream():
    """
    gmediarender (ALSA loopback) → ffmpeg (Musikverein 等フィルター) → aplay
    --gstout-audiopipe で S32_LE を強制し、AirPlay と同一パイプラインを共用する。

    ── キー操作 ──
      [c] フィルター変更  [h] 楽友協会  [a] Air Particle
      [x] SI プリセット   [+/-] 音量    [q] 停止
    """
    global stop_playback, current_processes
    global tinnitus_reduction_mode, musikverein_room_effects, air_particle_layer
    global current_filter_preset, CURRENT_VOLUME, musikverein_echo_mode

    # ── gmediarender 確認 ─────────────────────────────────────────────────
    if subprocess.run(['which', 'gmediarender'], capture_output=True).returncode != 0:
        print('\n❌ gmediarender not found.')
        print('   Install with: sudo apt install gmediarender')
        input('   [Press Enter to return to menu...]')
        return

    # ── ALSA ループバック確認 ─────────────────────────────────────────────
    loopback_card = _find_loopback_card()
    if loopback_card is None:
        print('\n❌ ALSA loopback device not found.')
        print('   Run: sudo modprobe snd-aloop  then try again.')
        print('   To persist: add snd-aloop to /etc/modules')
        input('   [Press Enter to return to menu...]')
        return
    loopback_play = f'hw:{loopback_card},0'   # gmediarender 出力先
    loopback_cap  = f'hw:{loopback_card},1'   # arecord 入力元

    # ── 既存インスタンス停止 ──────────────────────────────────────────────
    print('\n🔄 Stopping existing gmediarender...')
    _stop_gmediarender_all()

    # ── デバイス名 ────────────────────────────────────────────────────────
    print(f'\n📡 UPnP/DLNA receiver name (default: {_GMRENDER_DEVICE_NAME})')
    custom_name = input('   Name (Enter to keep default): ').strip()
    device_name = custom_name if custom_name else _GMRENDER_DEVICE_NAME

    restart_stream  = True
    _keep_gmrender  = False   # フィルター変更時 True → gmediarender 接続を維持

    gmrender_proc   = None   # while ループをまたいで保持
    _cover_stop     = threading.Event()   # カバーアートポーラー停止フラグ
    _cover_started  = False               # ポーラースレッドを一度だけ起動する
    _pending_action = None                # finally後に呼ぶメニュー: 'filter' | 'si' | None

    try:
        while restart_stream:
            restart_stream = False
            _keep_gmrender  = False
            stop_playback   = False
            cleanup_processes()

            # ── フィルターチェーン構築（AirPlay と同一ロジック）────────────
            gain_db         = GAIN_PRESETS.get(current_gain_preset, 0.0)
            loudness_filter = 'loudnorm=I=-16:TP=-2.0:LRA=11,' if loudness_normalization else ''
            eq_filter       = get_equalizer_ffmpeg_filter()
            eq_part         = f'{eq_filter},' if eq_filter else ''
            _fp_label       = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)

            if musikverein_room_effects and air_particle_layer:
                _space_label = 'Kanzai Full'
            elif musikverein_room_effects:
                _space_label = 'Musikverein room only'
            elif air_particle_layer:
                _space_label = 'Air Particle only'
            else:
                _space_label = 'OFF'

            filter_args = _build_audio_filter_args(
                gain_db, tinnitus_reduction_mode, musikverein_room_effects,
                loudness_filter, eq_part, CURRENT_VOLUME, air_particle_layer,
                echo_mode=musikverein_echo_mode,
            )
            output_sample_rate = str(upsampling_target_rate) if upsampling_target_rate > 0 else ('48000' if dsp_mode_active else '44100')  # ★ DSPモード時は48000Hzに揃える（Loopback→CamillaDSPとレート一致）

            # ★★★ ピークモニターGUI用ログ出力: UPnP/DLNA再生でも有効化 ★★★
            _upnp_declip_log_path = f'/tmp/qji_declip_{os.getpid()}.log'
            try:
                if os.path.exists(_upnp_declip_log_path):
                    os.remove(_upnp_declip_log_path)
            except Exception:
                pass
            filter_args = _wrap_filter_args_with_declip_monitor(filter_args, _upnp_declip_log_path)

            # ── arecord → ffmpeg → aplay（AirPlay と同一。S32_LE で統一）──
            arecord_cmd = [
                'arecord', '-D', loopback_cap,
                '-f', 'S32_LE', '-r', '44100', '-c', '2', '-t', 'raw',
            ]

            if _is_bluealsa_device(output_device):
                ffmpeg_cmd = (
                    ['ffmpeg', '-loglevel', 'error',
                     '-f', 's32le', '-ar', '44100', '-ac', '2', '-i', 'pipe:0',
                     '-vn', '-ar', '44100', '-acodec', 'pcm_s16le']
                    + filter_args
                    + ['-f', 'wav', '-']
                )
                aplay_cmd = [
                    'aplay', '-D', output_device,
                ]
                _fmt_label = 'S16_LE WAV → Bluetooth A2DP'
            else:
                ffmpeg_cmd = (
                    ['ffmpeg', '-loglevel', 'error',
                     '-f', 's32le', '-ar', '44100', '-ac', '2', '-i', 'pipe:0',
                     '-vn', '-ar', output_sample_rate]
                    + filter_args
                    + ['-f', 's32le', '-']
                )
                aplay_cmd = [
                    'aplay', '-D', output_device,
                    '-f', 'S32_LE', '-r', output_sample_rate, '-c', '2',
                    '--buffer-size=262144', '--period-size=32768',
                ]
                _fmt_label = 'S32_LE raw'

            print(f'\n📡 UPnP/DLNA receiver starting: "{device_name}"')
            print(f'   Loopback: {loopback_play} → {loopback_cap} → {output_device} ({_fmt_label})')
            print(f'   🏛️  {_fp_label}  |  Space: {_space_label}')
            _hint = ('[c]Filter|[x]SI preset|[a]Air layer|[h]Musikverein|[+/-]Vol|[q]Stop'
                     if SI_AVAILABLE else
                     '[c]Filter|[a]Air layer|[h]Musikverein|[+/-]Vol|[q]Stop')
            print(f'   {_hint}')
            print('=' * 60)

            old_settings  = None
            arecord_proc  = None
            ffmpeg_proc   = None
            aplay_proc    = None

            try:
                # ── gmediarender 起動（フィルター変更時は接続維持して再利用）──
                if gmrender_proc is None or gmrender_proc.poll() is not None:
                    # --gstout-audiopipe: GStreamer pipeline で S32LE を強制
                    # AirPlay (shairport-sync S32_LE) と同一フォーマットに統一する
                    _gst_pipe = (
                        f'audioconvert ! audioresample ! '
                        f'audio/x-raw,format=S32LE,rate=44100,channels=2 ! '
                        f'alsasink device={loopback_play}'
                    )
                    _gm_log = open('/tmp/qji_gmediarender.log', 'w')
                    gmrender_proc = subprocess.Popen(
                        [
                            'gmediarender',
                            '--port', '49494',
                            '--friendly-name', device_name,
                            f'--gstout-audiopipe={_gst_pipe}',
                            '--gstout-initial-volume-db=0',
                        ],
                        stdout=_gm_log,
                        stderr=_gm_log,
                    )
                    # カバーアートポーラースレッドを一度だけ起動
                    # （フィルター変更で gmrender を再利用する間も動き続ける）
                    if not _cover_started:
                        threading.Thread(
                            target=_gmrender_cover_poller,
                            args=(_cover_stop,),
                            daemon=True,
                        ).start()
                        _cover_started = True
                    time.sleep(1.5)
                    if gmrender_proc.poll() is not None:
                        print(f'\n❌ gmediarender failed to start (exit code: {gmrender_proc.returncode})')
                        print('   Log: /tmp/qji_gmediarender.log')
                        input('   [Press Enter to return to menu...]')
                        return
                    print(f'\n✅ Waiting — select "{device_name}" in your UPnP controller and start playing')
                else:
                    print(f'\n🔄 Filter updated — restarting (UPnP connection kept)...')

                # ── arecord ───────────────────────────────────────────────
                _ar_log = open('/tmp/qji_gm_arecord.log', 'w')
                arecord_proc = subprocess.Popen(
                    arecord_cmd,
                    stdout=subprocess.PIPE,
                    stderr=_ar_log,
                )
                # ── ffmpeg ────────────────────────────────────────────────
                _ff_log = open('/tmp/qji_gm_ffmpeg.log', 'w')
                ffmpeg_proc = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=arecord_proc.stdout,
                    stdout=subprocess.PIPE,
                    stderr=_ff_log,
                )
                arecord_proc.stdout.close()
                # ── aplay ─────────────────────────────────────────────────
                _ap_log = open('/tmp/qji_gm_aplay.log', 'w')
                aplay_proc = subprocess.Popen(
                    aplay_cmd,
                    stdin=ffmpeg_proc.stdout,
                    stderr=_ap_log,
                )
                ffmpeg_proc.stdout.close()

                current_processes['ffmpeg'] = ffmpeg_proc
                current_processes['aplay']  = aplay_proc

                # ── キー入力ループ ─────────────────────────────────────────
                old_settings = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())

                while (gmrender_proc.poll() is None and
                       arecord_proc.poll()  is None and
                       ffmpeg_proc.poll()   is None and
                       aplay_proc.poll()    is None):
                    if select.select([sys.stdin], [], [], 0.3)[0]:
                        key = sys.stdin.read(1).lower()

                        if key == 'q':
                            print('\n⏹  Stopping UPnP/DLNA receiver...')
                            break

                        elif key in ('+', '='):
                            CURRENT_VOLUME = min(CURRENT_VOLUME + 1, 6)
                            print(f'\n   🔊 Output gain: {CURRENT_VOLUME:+d} dB')
                            _keep_gmrender = True; restart_stream = True; break

                        elif key == '-':
                            CURRENT_VOLUME = max(CURRENT_VOLUME - 1, -20)
                            print(f'\n   🔊 Output gain: {CURRENT_VOLUME:+d} dB')
                            _keep_gmrender = True; restart_stream = True; break

                        elif key in ('c', 'x', 'a', 'h'):
                            # ★ ターミナル操作・プロセス停止は finally に任せる。
                            #   メニュー呼び出しも finally 後（端末完全復元後）に行う。
                            _keep_gmrender = True
                            restart_stream  = True
                            if key == 'c':
                                _pending_action = 'filter'
                            elif key == 'x':
                                _pending_action = 'si'
                            elif key == 'a':
                                air_particle_layer = not air_particle_layer
                                print(f'\n   🌿 Air Particle: {"ON" if air_particle_layer else "OFF"}')
                            elif key == 'h':
                                musikverein_room_effects = not musikverein_room_effects
                                print(f'\n   🏛️  Musikverein: {"ON" if musikverein_room_effects else "OFF"}')
                            break

                # ── gmediarender 接続断検出 ────────────────────────────────
                if not restart_stream and gmrender_proc.poll() is not None:
                    print('\n📡 gmediarender exited. Restarting...')
                    gmrender_proc = None
                    restart_stream = True

                # ── 診断ログ ──────────────────────────────────────────────
                if not restart_stream:
                    _dead = [(n, p) for n, p in [
                        ('gmediarender', gmrender_proc),
                        ('arecord',      arecord_proc),
                        ('ffmpeg',       ffmpeg_proc),
                        ('aplay',        aplay_proc),
                    ] if p and p.poll() is not None]
                    if _dead:
                        print('\n🔍 Exited processes:')
                        for _n, _p in _dead:
                            print(f'   {_n}: exit code {_p.returncode}')
                        print('   Logs: /tmp/qji_gmediarender.log  /tmp/qji_gm_arecord.log  /tmp/qji_gm_ffmpeg.log')

            except FileNotFoundError as e:
                print(f'\n❌ Command not found: {e}')
            except Exception as e:
                print(f'\n❌ UPnP/DLNA error: {e}')
                import traceback; traceback.print_exc()
            finally:
                if old_settings is not None:
                    try: termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)
                    except Exception: pass
                try: termios.tcflush(sys.stdin, termios.TCIFLUSH)
                except Exception: pass
                try: subprocess.run(['stty', 'sane'], stdin=sys.stdin,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception: pass

                procs_to_kill = (
                    [arecord_proc, ffmpeg_proc, aplay_proc]
                    if _keep_gmrender
                    else [gmrender_proc, arecord_proc, ffmpeg_proc, aplay_proc]
                )
                for _p in procs_to_kill:
                    if _p and _p.poll() is None:
                        try: _p.terminate(); _p.wait(timeout=2)
                        except Exception:
                            try: _p.kill()
                            except Exception: pass
                current_processes['ffmpeg'] = None
                current_processes['aplay']  = None

            if restart_stream:
                # ★ この時点で finally が完走済み → stty sane 適用済み → 入力確実
                if _pending_action == 'filter':
                    _do_filter_select(airplay_mode=True)
                elif _pending_action == 'si':
                    if SI_AVAILABLE:
                        if _si_instance and _si_instance.current_params is None:
                            try:
                                from filter_builder import params_from_preset
                                _map = {'musikverein':'orchestra','piano':'piano',
                                        'chamber':'chamber','vocal':'vocal','jazz':'jazz'}
                                _si_instance.current_params = params_from_preset(
                                    _map.get(current_filter_preset, 'default'))
                            except Exception as _e:
                                print(f'\n  ⚠️ SI init error: {_e}')
                        _si_do_preset_menu()
                    else:
                        print('\n  ⚠️ Sonia Intelligence is not available')
                _pending_action = None

                _lbl = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)
                if _keep_gmrender:
                    print(f'\n🔄 Filter updated: {_lbl} — restarting (UPnP connection kept)...')
                else:
                    print(f'\n🔄 Restarting: {_lbl} ...')
                    _stop_gmediarender_all()

    finally:
        _cover_stop.set()          # カバーアートポーラー停止
        _stop_gmediarender_all()
        print('\n📡 UPnP/DLNA receiver stopped\n')


# ===== ★★★ UPnP/DLNA レシーバーモード ここまで ★★★ =====


def radio_menu():
    """ラジオステーション選択メニュー"""
    while True:
        print("\n📻 Radio stations")
        print("=" * 60)
        for i, station in enumerate(RADIO_STATIONS, 1):
            country = station.get('country', '')
            print(f"  {i}. {country} {station['name']}  —  {station.get('description', '')}")
        print("  0. Return to main menu")
        print("=" * 60)

        choice = input("Select (number): ").strip()

        if choice == '0' or choice.lower() == 'q':
            break

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(RADIO_STATIONS):
                play_radio_stream(RADIO_STATIONS[idx])
                # 再生終了後もメニューに留まる
            else:
                print("⚠️ Invalid number")
        else:
            print("⚠️ Please enter a number")


def get_mood_color(mood):
    """ムードに応じた色コードを返す"""
    colors = {
        'happy': '\033[93m',
        'calm': '\033[94m',
        'energetic': '\033[91m',
        'melancholy': '\033[95m',
        'intense': '\033[31m',
        'ambient': '\033[96m',
        'moderate': '\033[97m'
    }
    reset = '\033[0m'
    return colors.get(mood, ''), reset


def get_mood_emoji(mood):
    """ムードに応じた絵文字を返す"""
    emojis = {
        'happy': '😊',
        'calm': '😌',
        'energetic': '⚡',
        'melancholy': '😢',
        'intense': '🔥',
        'ambient': '🌫️',
        'moderate': '😐'
    }
    return emojis.get(mood, '🎵')
# -*- coding: utf-8 -*-
"""
musicaplayerg27.py - Part 2/4
検索機能と音声認識機能
"""

# ===== 音声認識関連 =====

def extract_number_from_speech(text):
    """音声認識結果から数字を抽出する"""
    japanese_numbers = {
        'ゼロ': '0', 'れい': '0',
        'いち': '1', 'ひとつ': '1', 'いっこ': '1',
        'に': '2', 'ふたつ': '2',
        'さん': '3', 'みっつ': '3',
        'よん': '4', 'し': '4', 'よっつ': '4',
        'ご': '5', 'いつつ': '5',
        'ろく': '6', 'むっつ': '6',
        'なな': '7', 'しち': '7', 'ななつ': '7',
        'はち': '8', 'やっつ': '8',
        'きゅう': '9', 'く': '9', 'ここのつ': '9',
        'じゅう': '10',
    }
    digit_match = re.search(r'\d+', text)
    if digit_match:
        return int(digit_match.group())
    for jp_num, digit in japanese_numbers.items():
        if jp_num in text:
            return int(digit)
    return None


def extract_tempo_from_speech(text):
    """音声認識結果からテンポを抽出する"""
    number = extract_number_from_speech(text)
    if number and 50 <= number <= 200:
        return number

    if any(keyword in text for keyword in ['はやい', '速い', 'アップテンポ']):
        return 140
    elif any(keyword in text for keyword in ['おそい', '遅い', 'スロー']):
        return 80
    elif 'ふつう' in text or '普通' in text:
        return 120

    return None


def extract_search_keyword_from_speech(text, mode):
    """音声認識結果から検索キーワードを抽出"""
    composer_keywords = {
        'ベートーヴェン': 'Beethoven',
        'べーとーべん': 'Beethoven',
        'モーツァルト': 'Mozart',
        'もーつぁると': 'Mozart',
        'バッハ': 'Bach',
        'ばっは': 'Bach',
        'ショパン': 'Chopin',
        'しょぱん': 'Chopin',
        'チャイコフスキー': 'Tchaikovsky',
        'ちゃいこふすきー': 'Tchaikovsky',
        'ドビュッシー': 'Debussy',
        'どびゅっしー': 'Debussy',
        'ブラームス': 'Brahms',
        'ぶらーむす': 'Brahms',
        'リスト': 'Liszt',
        'りすと': 'Liszt',
    }

    performer_keywords = {
        'ベルリン': 'Berlin',
        'べるりん': 'Berlin',
        'ウィーン': 'Vienna',
        'うぃーん': 'Vienna',
        'フィル': 'Phil',
        'ふぃる': 'Phil',
        'カラヤン': 'Karajan',
        'からやん': 'Karajan',
    }

    genre_keywords = {
        'クラシック': 'Classical',
        'くらしっく': 'Classical',
        'ジャズ': 'Jazz',
        'じゃず': 'Jazz',
        'ロック': 'Rock',
        'ろっく': 'Rock',
        'ポップ': 'Pop',
        'ぽっぷ': 'Pop',
    }

    mood_keywords = {
        'ハッピー': 'happy',
        'はっぴー': 'happy',
        '明るい': 'happy',
        'カーム': 'calm',
        'かーむ': 'calm',
        '静か': 'calm',
        '穏やか': 'calm',
        'エナジェティック': 'energetic',
        'えなじぇてぃっく': 'energetic',
        '元気': 'energetic',
        'メランコリー': 'melancholy',
        'めらんこりー': 'melancholy',
        '悲しい': 'melancholy',
        'インテンス': 'intense',
        '激しい': 'intense',
        'アンビエント': 'ambient',
        '環境音': 'ambient',
        'モデレート': 'moderate',
        '普通': 'moderate'
    }

    if mode == 'composer':
        for jp_name, en_name in composer_keywords.items():
            if jp_name in text:
                return en_name
    elif mode in ['performer', 'conductor']:
        for jp_name, en_name in performer_keywords.items():
            if jp_name in text:
                return en_name
    elif mode == 'genre':
        for jp_name, en_name in genre_keywords.items():
            if jp_name in text:
                return en_name
    elif mode == 'mood':
        for jp_name, en_name in mood_keywords.items():
            if jp_name in text:
                return en_name

    words = text.split()
    for word in words:
        if len(word) > 2:
            return word.capitalize()

    return None


def voice_input_listener_thread(timeout_seconds=15, listen_for_tempo=False, listen_for_keyword=False, mode='composer'):
    """音声入力を待機するスレッド関数(改良版)"""
    global voice_input_result, voice_input_waiting, voice_recognition_active, input_received
    if not VOICE_RECOGNITION_AVAILABLE:
        return
    if not os.path.exists(VOSK_MODEL_PATH):
        print("⚠️ Vosk model not found:", VOSK_MODEL_PATH)
        return
    voice_input_result = None
    voice_recognition_active = True
    q = queue.Queue()
    model = Model(VOSK_MODEL_PATH)

    # デバイスとサンプルレートを問い合わせ（外部マイクが 44100/48000 の場合に対応）
    device_id = USB_MIC_DEVICE_ID if USB_MIC_DEVICE_ID is not None else None
    try:
        import sounddevice as sd
        if device_id is not None:
            dev_info = sd.query_devices(device_id)
        else:
            # default input
            default_dev = sd.default.device
            device_id = int(default_dev[0]) if isinstance(default_dev, (list, tuple)) and default_dev[0] is not None else device_id
            dev_info = sd.query_devices(device_id) if device_id is not None else None
        samplerate = int(dev_info['default_samplerate']) if dev_info and 'default_samplerate' in dev_info else 16000
    except Exception:
        samplerate = 16000

    rec = KaldiRecognizer(model, samplerate)

    def callback(indata, frames, time_, status):
        # PortAudio の input overflow 等は負荷時に頻発。stderr へ出すと TTY 上の枠線UIと混線するため既定では出さない
        if status and os.environ.get("SOUNDDEVICE_DEBUG"):
            print(status, file=sys.stderr)
        # indata は RawInputStream だと bytes-like、InputStream だと ndarray のことがあるので安全に処理
        try:
            if hasattr(indata, 'tobytes'):
                q.put(indata.tobytes())
            else:
                q.put(bytes(indata))
        except Exception:
            try:
                q.put(bytes(indata))
            except:
                pass

    start_time = time.time()
    try:
        if device_id is not None:
            print(f"🎤 Using device {device_id}, sample rate: {samplerate}")
        else:
            print("🎤 Using default mic, sample rate: {samplerate}")

        # RawInputStream を使う（生データをそのまま Vosk に渡す）
        with sd.RawInputStream(samplerate=samplerate, blocksize=8000, dtype='int16',
                               channels=1, callback=callback, device=device_id):
            while voice_input_waiting and not input_received and (time.time() - start_time) < timeout_seconds:
                try:
                    data = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if not voice_recognition_active or input_received:
                    break
                # data は bytes のはず（上で統一）
                if rec.AcceptWaveform(data):
                    result = json.loads(rec.Result())
                    command = result.get("text", "").strip()
                    if command:
                        print(f"🎤 Voice recognised: {command}")
                        if listen_for_tempo:
                            tempo = extract_tempo_from_speech(command)
                            if tempo:
                                voice_input_result = tempo
                                input_received = True
                                break
                        elif listen_for_keyword:
                            keyword = extract_search_keyword_from_speech(command, mode)
                            if keyword:
                                voice_input_result = keyword
                                input_received = True
                                break
                        else:
                            number = extract_number_from_speech(command)
                            if number is not None:
                                voice_input_result = number
                                input_received = True
                                break
    except Exception as e:
        print(f"Voice recognition error: {e}")
        print("💡 Hint: try specifying the device ID with --mic-device.")
    finally:
        voice_recognition_active = False


def keyboard_input_listener_thread(listen_for_tempo=False, listen_for_keyword=False, prompt=""):
    """キーボード入力を待機するスレッド関数"""
    global voice_input_result, input_received
    try:
        if prompt:
            print(prompt, end='', flush=True)
        elif listen_for_tempo:
            print("⌨️ Enter tempo (50-200) and press Enter: ", end='', flush=True)
        elif listen_for_keyword:
            print("⌨️ Enter search keyword and press Enter: ", end='', flush=True)
        else:
            print("⌨️ Enter a number and press Enter: ", end='', flush=True)

        while not input_received:
            if select.select([sys.stdin], [], [], 0.1)[0]:
                try:
                    line = sys.stdin.readline().strip()
                    if line == '':
                        continue

                    if listen_for_tempo:
                        if line.isdigit():
                            number = int(line)
                            if number < 50 or number > 200:
                                print(f"⚠ Tempo must be 50-200 (entered: {number})")
                                print("⌨️ Enter tempo (50-200) and press Enter: ", end='', flush=True)
                                continue
                            voice_input_result = number
                            input_received = True
                            print(f"⌨️ Keyboard input: {line}")
                            break
                        else:
                            print(f"⚠ Please enter a number (entered: '{line}')")
                            print("⌨️ Enter tempo (50-200) and press Enter: ", end='', flush=True)
                    elif listen_for_keyword:
                        voice_input_result = line
                        input_received = True
                        print(f"⌨️ Keyboard input: {line}")
                        break
                    else:
                        if line.isdigit():
                            voice_input_result = int(line)
                            input_received = True
                            print(f"⌨️ Keyboard input: {line}")
                            break
                        else:
                            print(f"⚠ Please enter a number (entered: '{line}')")
                            if prompt:
                                print(prompt, end='', flush=True)
                            else:
                                print("⌨️ Enter a number and press Enter: ", end='', flush=True)
                except (ValueError, EOFError):
                    continue
            if input_received:
                break
    except KeyboardInterrupt:
        input_received = True


def get_tempo_input(timeout_seconds=15):
    """テンポの音声・キーボード同時入力"""
    global voice_input_result, voice_input_waiting, input_received
    print("\n🎯 Enter tempo (BPM) by voice or keyboard")
    if VOICE_RECOGNITION_AVAILABLE and os.path.exists(VOSK_MODEL_PATH):
        print("   🎤 Voice: say the tempo (e.g. '120', 'fast', 'slow')")
    print("   ⌨️ Keyboard: type a number (50-200) and press Enter")
    print(f"   ⏱️ Timeout: {timeout_seconds}s")

    voice_input_result = None
    voice_input_waiting = True
    input_received = False
    threads = []

    if VOICE_RECOGNITION_AVAILABLE and os.path.exists(VOSK_MODEL_PATH):
        voice_thread = threading.Thread(target=voice_input_listener_thread,
                                       args=(timeout_seconds, True, False, 'tempo'))
        voice_thread.daemon = True
        voice_thread.start()
        threads.append(voice_thread)
        print("🎤 Voice recognition started...")

    keyboard_thread = threading.Thread(target=keyboard_input_listener_thread, args=(True, False, ""))
    keyboard_thread.daemon = True
    keyboard_thread.start()
    threads.append(keyboard_thread)

    start_time = time.time()
    while not input_received and (time.time() - start_time) < timeout_seconds:
        time.sleep(0.1)

    voice_input_waiting = False
    input_received = True
    time.sleep(0.2)

    if voice_input_result is not None and 50 <= voice_input_result <= 200:
        print(f"✅ Tempo entered: {voice_input_result} BPM")
        return voice_input_result
    elif voice_input_result is not None:
        print(f"⚠ Invalid tempo (must be 50-200)")
        return None
    else:
        print("⏰ Timeout or invalid input")
        return None


def get_keyword_input(mode='composer', timeout_seconds=20):
    """検索キーワードの音声・キーボード同時入力"""
    global voice_input_result, voice_input_waiting, input_received

    mode_names = {
        'composer': 'composer',
        'performer': 'performer',
        'conductor': 'conductor',
        'genre': 'genre',
        'mood': 'mood'
    }
    mode_name = mode_names.get(mode, mode)

    print(f"\n🔍 Enter {mode_name} by voice or keyboard")
    if VOICE_RECOGNITION_AVAILABLE and os.path.exists(VOSK_MODEL_PATH):
        print(f"   🎤 Voice: say the {mode_name} name (e.g. 'Beethoven', 'Berlin')")
    print(f"   ⌨️ Keyboard: type the {mode_name} name and press Enter")
    print(f"   ⏱️ Timeout: {timeout_seconds}s")

    voice_input_result = None
    voice_input_waiting = True
    input_received = False
    threads = []

    if VOICE_RECOGNITION_AVAILABLE and os.path.exists(VOSK_MODEL_PATH):
        voice_thread = threading.Thread(target=voice_input_listener_thread,
                                       args=(timeout_seconds, False, True, mode))
        voice_thread.daemon = True
        voice_thread.start()
        threads.append(voice_thread)
        print("🎤 Voice recognition started...")

    keyboard_thread = threading.Thread(target=keyboard_input_listener_thread,
                                       args=(False, True, ""))
    keyboard_thread.daemon = True
    keyboard_thread.start()
    threads.append(keyboard_thread)

    start_time = time.time()
    while not input_received and (time.time() - start_time) < timeout_seconds:
        time.sleep(0.1)

    voice_input_waiting = False
    input_received = True
    time.sleep(0.2)

    if voice_input_result and isinstance(voice_input_result, str):
        print(f"✅ {mode_name} entered: {voice_input_result}")
        return voice_input_result
    else:
        print("⏰ Timeout or invalid input")
        return None
    # -*- coding: utf-8 -*-
"""
musicaplayerg27.py - Part 3/4
検索・選曲機能とジャケット画像選曲機能
"""

# ===== 選曲・検索機能 =====

def get_available_options(mode='composer'):
    """データベース内の利用可能な選択肢を取得"""
    db = safe_load_database()
    if not db:
        return []

    options = {}

    for track in db:
        if mode == 'composer':
            value = track.get('composer', 'Unknown')
        elif mode == 'performer':
            value = track.get('performer', 'Unknown')
            if value == 'Unknown':
                value = track.get('artist', 'Unknown')
        elif mode == 'conductor':
            value = track.get('conductor', 'Unknown')
        elif mode == 'genre':
            value = track.get('genre', 'Unknown')
        elif mode == 'mood':
            value = track.get('mood', 'Unknown')
        else:
            continue

        if value and value != 'Unknown':
            options[value] = options.get(value, 0) + 1

    sorted_options = sorted(options.items(), key=lambda x: x[1], reverse=True)
    return sorted_options


def curses_search_select(stdscr, options, mode='composer'):
    """cursesを使用したリアルタイム検索UI"""
    curses.curs_set(1)
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
    
    mode_names = {
        'composer': 'composer',
        'performer': 'performer',
        'conductor': 'conductor',
        'genre': 'genre',
        'mood': 'mood'
    }
    mode_name = mode_names.get(mode, mode)
    
    search_text = ""
    selected_idx = 0
    scroll_offset = 0
    
    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        
        if search_text:
            matches = [(name, count) for name, count in options 
                      if search_text.lower() in name.lower()]
        else:
            matches = options
        
        if matches:
            selected_idx = max(0, min(selected_idx, len(matches) - 1))
        else:
            selected_idx = 0
        
        display_count = min(height - 7, 20)
        if selected_idx < scroll_offset:
            scroll_offset = selected_idx
        elif selected_idx >= scroll_offset + display_count:
            scroll_offset = selected_idx - display_count + 1
        
        try:
            header = f"🔍 {mode_name}検索"
            stdscr.addstr(0, 0, header[:width-1], curses.A_BOLD)
            
            search_display = f"検索語: {search_text}_"
            stdscr.addstr(1, 0, search_display[:width-1])
            
            result_info = f"該当: {len(matches)}件"
            if len(matches) > display_count:
                result_info += f" (表示: {scroll_offset+1}-{min(scroll_offset+display_count, len(matches))})"
            stdscr.addstr(2, 0, result_info[:width-1])
            
            stdscr.addstr(3, 0, "="*min(60, width-1))
        except:
            pass
        
        if not matches:
            try:
                stdscr.addstr(5, 0, "❌ 該当する項目がありません", curses.A_DIM)
            except:
                pass
        else:
            for i in range(display_count):
                list_idx = scroll_offset + i
                if list_idx < len(matches):
                    name, count = matches[list_idx]
                    line = f" {list_idx+1:3d}. {name[:45]:45} ({count:3d}曲)"
                    
                    try:
                        if list_idx == selected_idx:
                            stdscr.addstr(4+i, 0, "▶" + line[:width-2], curses.color_pair(1) | curses.A_BOLD)
                        else:
                            stdscr.addstr(4+i, 0, " " + line[:width-2])
                    except:
                        pass
        
        try:
            footer_line = height - 2
            stdscr.addstr(footer_line, 0, "="*min(60, width-1))
            
            controls = "↑↓:選択移動 | Enter:決定 | BackSpace:削除 | Esc/Ctrl+C:戻る"
            stdscr.addstr(footer_line + 1, 0, controls[:width-1], curses.A_REVERSE)
        except:
            pass
        
        stdscr.refresh()
        
        try:
            key = stdscr.getch()
        except:
            continue
        
        if key == 27:
            return None
        elif key == 3:
            return None
        elif key == 10 or key == curses.KEY_ENTER:
            if matches and 0 <= selected_idx < len(matches):
                return matches[selected_idx][0]
        elif key == curses.KEY_UP:
            if selected_idx > 0:
                selected_idx -= 1
        elif key == curses.KEY_DOWN:
            if matches and selected_idx < len(matches) - 1:
                selected_idx += 1
        elif key == curses.KEY_PPAGE:
            selected_idx = max(0, selected_idx - display_count)
        elif key == curses.KEY_NPAGE:
            if matches:
                selected_idx = min(len(matches) - 1, selected_idx + display_count)
        elif key == curses.KEY_HOME:
            selected_idx = 0
            scroll_offset = 0
        elif key == curses.KEY_END:
            if matches:
                selected_idx = len(matches) - 1
        elif key == curses.KEY_BACKSPACE or key == 127 or key == 8:
            if search_text:
                search_text = search_text[:-1]
                selected_idx = 0
                scroll_offset = 0
        elif key == 330:
            search_text = ""
            selected_idx = 0
            scroll_offset = 0
        elif 32 <= key <= 126:
            search_text += chr(key)
            selected_idx = 0
            scroll_offset = 0


def interactive_search_with_curses(options, mode='composer'):
    """cursesラッパー関数(エラーハンドリング付き)"""
    mode_names = {
        'composer': 'composer',
        'performer': 'performer',
        'conductor': 'conductor',
        'genre': 'genre',
        'mood': 'mood'
    }
    mode_name = mode_names.get(mode, mode)
    
    if not options:
        print(f"⚠️ No {mode_name} data in database")
        return None
    
    try:
        result = curses.wrapper(curses_search_select, options, mode)
        print("\n")
        return result
    except KeyboardInterrupt:
        print("\n⚠️ Search cancelled")
        return None
    except Exception as e:
        print(f"\n⚠️ Search UI error: {e}")
        return None


def get_tracks_by_tempo(target_tempo, tolerance=10, limit=200):
    """テンポで楽曲を検索"""
    db = safe_load_database()
    if not db:
        return []

    tracks = []
    min_tempo = target_tempo - tolerance
    max_tempo = target_tempo + tolerance
    unique_paths = set()

    for track in db:
        features = track.get('features', {})
        track_tempo = features.get('tempo', 0)
        if min_tempo <= track_tempo <= max_tempo:
            path = track.get('path')
            if path and path not in unique_paths:
                unique_paths.add(path)
                tracks.append(track)

    random.shuffle(tracks)
    return tracks[:limit]


def get_tracks_by_composer(composer_name, limit=200):
    """作曲家で楽曲を検索"""
    db = safe_load_database()
    if not db:
        return []

    tracks = []
    unique_paths = set()

    for track in db:
        composer = track.get('composer', '')
        if composer.lower() == composer_name.lower():
            path = track.get('path')
            if path and path not in unique_paths:
                unique_paths.add(path)
                tracks.append(track)

    if not tracks:
        for track in db:
            composer = track.get('composer', '')
            composer_words = set(composer.lower().split())
            search_words = set(composer_name.lower().split())
            if search_words.issubset(composer_words) or composer_name.lower() in composer.lower():
                path = track.get('path')
                if path and path not in unique_paths:
                    unique_paths.add(path)
                    tracks.append(track)

    random.shuffle(tracks)
    return tracks[:limit]


def get_tracks_by_performer(performer_name, limit=200):
    """演奏者で楽曲を検索"""
    db = safe_load_database()
    if not db:
        return []

    tracks = []
    unique_paths = set()

    for track in db:
        performer = track.get('performer', '')
        artist = track.get('artist', '')

        if performer.lower() == performer_name.lower() or artist.lower() == performer_name.lower():
            path = track.get('path')
            if path and path not in unique_paths:
                unique_paths.add(path)
                tracks.append(track)

    if not tracks:
        for track in db:
            performer = track.get('performer', '')
            artist = track.get('artist', '')

            performer_words = set(performer.lower().split())
            artist_words = set(artist.lower().split())
            search_words = set(performer_name.lower().split())

            if search_words.issubset(performer_words) or search_words.issubset(artist_words) or performer_name.lower() in performer.lower() or performer_name.lower() in artist.lower():
                path = track.get('path')
                if path and path not in unique_paths:
                    unique_paths.add(path)
                    tracks.append(track)

    random.shuffle(tracks)
    return tracks[:limit]


def get_tracks_by_conductor(conductor_name, limit=200):
    """指揮者で楽曲を検索"""
    db = safe_load_database()
    if not db:
        return []

    tracks = []
    unique_paths = set()

    for track in db:
        conductor = track.get('conductor', '')
        if conductor.lower() == conductor_name.lower():
            path = track.get('path')
            if path and path not in unique_paths:
                unique_paths.add(path)
                tracks.append(track)

    if not tracks:
        for track in db:
            conductor = track.get('conductor', '')
            conductor_words = set(conductor.lower().split())
            search_words = set(conductor_name.lower().split())

            if search_words.issubset(conductor_words) or conductor_name.lower() in conductor.lower():
                path = track.get('path')
                if path and path not in unique_paths:
                    unique_paths.add(path)
                    tracks.append(track)

    random.shuffle(tracks)
    return tracks[:limit]


def get_tracks_by_genre(genre_name, limit=200):
    """ジャンルで楽曲を検索"""
    db = safe_load_database()
    if not db:
        return []

    tracks = []
    unique_paths = set()

    for track in db:
        genre = track.get('genre', '')
        if genre.lower() == genre_name.lower():
            path = track.get('path')
            if path and path not in unique_paths:
                unique_paths.add(path)
                tracks.append(track)

    if not tracks:
        for track in db:
            genre = track.get('genre', '')
            genre_words = set(genre.lower().split())
            search_words = set(genre_name.lower().split())

            if search_words.issubset(genre_words) or genre_name.lower() in genre.lower():
                path = track.get('path')
                if path and path not in unique_paths:
                    unique_paths.add(path)
                    tracks.append(track)

    random.seed(time.time())
    random.shuffle(tracks)
    random.seed()
    return tracks[:limit]


def get_tracks_by_mood(mood_name, limit=200):
    """ムードで楽曲を検索"""
    db = safe_load_database()
    if not db:
        return []

    tracks = []
    unique_paths = set()

    for track in db:
        mood = track.get('mood', '')
        if mood.lower() == mood_name.lower():
            path = track.get('path')
            if path and path not in unique_paths:
                unique_paths.add(path)
                tracks.append(track)

    if not tracks:
        for track in db:
            mood = track.get('mood', '')
            mood_words = set(mood.lower().split())
            search_words = set(mood_name.lower().split())

            if search_words.issubset(mood_words) or mood_name.lower() in mood.lower():
                path = track.get('path')
                if path and path not in unique_paths:
                    unique_paths.add(path)
                    tracks.append(track)

    random.shuffle(tracks)
    return tracks[:limit]


def get_tracks_by_keyword(keyword, limit=200, debug=False):
    """キーワードで楽曲を全フィールドから検索"""
    db = safe_load_database()
    if not db:
        return []

    tracks = []
    unique_paths = set()
    keyword_lower = keyword.lower()
    
    match_fields = {'title': 0, 'artist': 0, 'composer': 0, 'performer': 0, 
                   'conductor': 0, 'genre': 0, 'mood': 0, 'filename': 0, 'path': 0}

    for track in db:
        path = track.get('path', '')
        if path in unique_paths or not path:
            continue
        
        title = (track.get('title') or '').lower()
        artist = (track.get('artist') or '').lower()
        composer = (track.get('composer') or '').lower()
        performer = (track.get('performer') or '').lower()
        conductor = (track.get('conductor') or '').lower()
        genre = (track.get('genre') or '').lower()
        mood = (track.get('mood') or '').lower()
        filename = os.path.basename(path).lower()
        fullpath = path.lower()
        
        matched = False
        match_reason = []
        
        if keyword_lower in title:
            matched = True
            match_reason.append('title')
            match_fields['title'] += 1
        if keyword_lower in artist:
            matched = True
            match_reason.append('artist')
            match_fields['artist'] += 1
        if keyword_lower in composer:
            matched = True
            match_reason.append('composer')
            match_fields['composer'] += 1
        if keyword_lower in performer:
            matched = True
            match_reason.append('performer')
            match_fields['performer'] += 1
        if keyword_lower in conductor:
            matched = True
            match_reason.append('conductor')
            match_fields['conductor'] += 1
        if keyword_lower in genre:
            matched = True
            match_reason.append('genre')
            match_fields['genre'] += 1
        if keyword_lower in mood:
            matched = True
            match_reason.append('mood')
            match_fields['mood'] += 1
        if keyword_lower in filename:
            matched = True
            match_reason.append('filename')
            match_fields['filename'] += 1
        if keyword_lower in fullpath:
            matched = True
            match_reason.append('path')
            match_fields['path'] += 1
        
        if matched:
            unique_paths.add(path)
            track['_match_reason'] = match_reason
            tracks.append(track)
            
            if debug and len(tracks) <= 5:
                print(f"\n  [Match {len(tracks)}] {match_reason}:")
                print(f"    Title: {track.get('title', 'N/A')}")
                print(f"    Artist: {track.get('artist', 'N/A')}")
                print(f"    Filename: {filename}")

    if debug:
        print(f"\n🔍 Search stats (keyword: '{keyword}'):")
        print(f"  Total matches: {len(tracks)}")
        print(f"  By field:")
        for field, count in match_fields.items():
            if count > 0:
                print(f"    {field}: {count}")

    random.shuffle(tracks)
    return tracks[:limit]


def get_tracks_by_mood_group(group_name, limit=200):
    """ムードグループで検索"""
    moods = MOOD_GROUPS.get(group_name, [])
    if not moods:
        print(f"⚠️ Unknown mood group: {group_name}")
        return []
    
    all_tracks = []
    unique_paths = set()
    
    for mood in moods:
        mood_tracks = get_tracks_by_mood(mood, limit=limit*2)
        for track in mood_tracks:
            path = track.get('path')
            if path and path not in unique_paths:
                unique_paths.add(path)
                all_tracks.append(track)
    
    random.shuffle(all_tracks)
    return all_tracks[:limit]


def match_track_with_filters(track, filters):
    """単一トラックが指定された複数フィルタに一致するかを判定する"""
    if 'tempo' in filters and filters['tempo'] is not None:
        target, tolerance = filters['tempo']
        track_tempo = track.get('features', {}).get('tempo', 0)
        if not (target - tolerance <= track_tempo <= target + tolerance):
            return False

    if 'composer' in filters and filters['composer']:
        wanted = filters['composer'].lower()
        composer = (track.get('composer') or '').lower()
        if composer == '' or (wanted not in composer and not set(wanted.split()).issubset(set(composer.split()))):
            return False

    if 'performer' in filters and filters['performer']:
        wanted = filters['performer'].lower()
        performer = (track.get('performer') or '').lower()
        artist = (track.get('artist') or '').lower()
        if performer == '' and artist == '':
            return False
        if wanted not in performer and wanted not in artist and not set(wanted.split()).issubset(set(performer.split())) and not set(wanted.split()).issubset(set(artist.split())):
            return False

    if 'conductor' in filters and filters['conductor']:
        wanted = filters['conductor'].lower()
        conductor = (track.get('conductor') or '').lower()
        if conductor == '' or (wanted not in conductor and not set(wanted.split()).issubset(set(conductor.split()))):
            return False

    if 'genre' in filters and filters['genre']:
        wanted = filters['genre'].lower()
        genre = (track.get('genre') or '').lower()
        if genre == '' or (wanted not in genre and not set(wanted.split()).issubset(set(genre.split()))):
            return False

    if 'mood' in filters and filters['mood']:
        wanted = filters['mood'].lower()
        mood = (track.get('mood') or '').lower()
        if mood == '' or (wanted not in mood and not set(wanted.split()).issubset(set(mood.split()))):
            return False
    
    # ★★★ キーワード検索を追加 ★★★
    if 'keyword' in filters and filters['keyword']:
        keyword = filters['keyword'].lower()
        # タイトル、アーティスト、アルバム、作曲家、演奏者、指揮者、ジャンルから検索
        title = (track.get('title') or '').lower()
        artist = (track.get('artist') or '').lower()
        album = (track.get('album') or '').lower()
        composer = (track.get('composer') or '').lower()
        performer = (track.get('performer') or '').lower()
        conductor = (track.get('conductor') or '').lower()
        genre = (track.get('genre') or '').lower()
        
        # いずれかのフィールドにキーワードが含まれているかチェック
        if not (keyword in title or keyword in artist or keyword in album or 
                keyword in composer or keyword in performer or keyword in conductor or keyword in genre):
            return False
    
    return True


def get_tracks_by_filters(filters, limit=200):
    """複数条件(filters)に一致するトラックを返す"""
    db = safe_load_database()
    if not db:
        return []

    results = []
    unique_paths = set()
    for track in db:
        path = track.get('path')
        if not path or path in unique_paths:
            continue
        try:
            if match_track_with_filters(track, filters):
                unique_paths.add(path)
                results.append(track)
        except Exception:
            continue

    random.shuffle(results)
    return results[:limit]


def show_mood_statistics():
    """データベース内のムード分布を表示"""
    db = safe_load_database()
    if not db:
        print("⚠️ Could not load database")
        return
    
    mood_count = {}
    for track in db:
        mood = track.get('mood', 'Unknown')
        mood_count[mood] = mood_count.get(mood, 0) + 1
    
    total = len(db)
    print("\n" + "="*60)
    print("📊 Mood distribution statistics")
    print("="*60)
    
    mood_names = {
        'happy': '😊 Happy',
        'calm': '😌 Calm', 
        'energetic': '⚡ Energetic',
        'melancholy': '😢 Melancholy',
        'intense': '🔥 Intense',
        'ambient': '🌫️ Ambient',
        'moderate': '😐 Moderate',
        'Unknown': '❓ Unclassified'
    }
    
    for mood, count in sorted(mood_count.items(), key=lambda x: x[1], reverse=True):
        percentage = (count / total) * 100
        mood_display = mood_names.get(mood, mood)
        bar_length = int(percentage / 2)
        bar = '█' * bar_length
        color, reset = get_mood_color(mood)
        print(f"  {mood_display:20} {color}{bar}{reset} {count:4} tracks ({percentage:5.1f}%)")
    
    print("="*60)
    print(f"Total: {total} tracks\n")


# ===== ジャケット画像選曲機能 =====

def collect_album_covers(tracks, limit=500):
    """トラックリストからアルバムジャケット画像を収集（厳格版）"""
    folder_images = {}
    
    print(f"🔍 Collecting cover art from {len(tracks)} tracks...")
    
    # 渡されたトラックだけを使用
    for track in tracks:
        track_path = track.get('path')
        if not track_path or not os.path.exists(track_path):
            continue
        
        folder = os.path.dirname(track_path)
        
        if folder in folder_images:
            # 同じフォルダの曲を追加
            folder_images[folder][1].append(track)
            continue
        
        image_path = find_cover_image_safe(track_path)
        if image_path:
            # 新しいフォルダとして追加（渡されたトラックのみ）
            folder_images[folder] = (image_path, [track])
    
    result = []
    for folder, (image_path, track_list) in folder_images.items():
        # track_listには渡されたtracksの中の曲だけが含まれる
        result.append((image_path, folder, len(track_list)))
    
    # 曲数の多い順にソート
    result.sort(key=lambda x: x[2], reverse=True)
    
    print(f"✅ Found {len(result)} album folder(s)")
    
    # デバッグ：最初の5個を表示
    if result:
        print("📁 Discovered albums (top 5):")
        for i, (img, folder, count) in enumerate(result[:5], 1):
            folder_name = os.path.basename(folder)
            print(f"   {i}. {folder_name[:50]} ({count} tracks)")
    
    return result[:limit]


# 修正箇所: display_album_covers_with_feh関数内
# ブラウザキャッシュクリア機能を追加

def clear_browser_cache():
    """Chromeのキャッシュと閲覧履歴を完全にクリア"""
    try:
        import subprocess
        import os
        import glob
        
        print("\n🧹 Clearing browser cache...")
        
        # Chromeプロセスを全て終了
        subprocess.run(['pkill', '-f', 'chrome'], stderr=subprocess.DEVNULL)
        subprocess.run(['pkill', '-f', 'chromium'], stderr=subprocess.DEVNULL)
        time.sleep(1)
        
        # Chromeキャッシュディレクトリパス
        home = os.path.expanduser('~')
        cache_dirs = [
            f"{home}/.cache/google-chrome",
            f"{home}/.cache/chromium",
            f"{home}/.config/google-chrome/Default/Cache",
            f"{home}/.config/google-chrome/Default/Code Cache",
            f"{home}/.config/chromium/Default/Cache",
            f"{home}/.config/chromium/Default/Code Cache",
        ]
        
        # 履歴ファイルパス
        history_files = [
            f"{home}/.config/google-chrome/Default/History",
            f"{home}/.config/google-chrome/Default/History-journal",
            f"{home}/.config/chromium/Default/History",
            f"{home}/.config/chromium/Default/History-journal",
        ]
        
        # キャッシュディレクトリを削除
        for cache_dir in cache_dirs:
            if os.path.exists(cache_dir):
                try:
                    shutil.rmtree(cache_dir)
                    print(f"  ✓ Deleted: {cache_dir}")
                except Exception as e:
                    print(f"  ⚠️ Failed to delete {cache_dir}: {e}")
        
        # 履歴ファイルを削除
        for hist_file in history_files:
            if os.path.exists(hist_file):
                try:
                    os.remove(hist_file)
                    print(f"  ✓ Deleted: {hist_file}")
                except Exception as e:
                    print(f"  ⚠️ Failed to delete {hist_file}: {e}")
        
        print("✅ Browser cache cleared")
        
    except Exception as e:
        print(f"⚠️ Cache clear error: {e}")


def _kill_stale_chrome_for_profile(profile_dir):
    """
    ★★★ 取り残されたChromeプロセス対策 ★★★
    ターミナルを強制終了した際などに、専用プロファイル(profile_dir)を
    握ったままのChromeプロセスが残っていると、次回起動時にそのプロセスへ
    新しいタブが追加され「タブが2つ開く」原因になる。
    新しいChromeを起動する直前に、同じプロファイルを使っている
    既存プロセスを確実に終了させておく。
    """
    try:
        result = subprocess.run(
            ['pgrep', '-f', f'user-data-dir={profile_dir}'],
            capture_output=True, text=True, timeout=2
        )
        pids = [p for p in result.stdout.split() if p.isdigit()]
        if not pids:
            return
        print(f"🧹 Terminating leftover Chrome processes holding the dedicated profile ({len(pids)})")
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                pass
        time.sleep(0.5)
        # まだ残っていればSIGKILL
        result2 = subprocess.run(
            ['pgrep', '-f', f'user-data-dir={profile_dir}'],
            capture_output=True, text=True, timeout=2
        )
        for pid in result2.stdout.split():
            if pid.isdigit():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except Exception:
                    pass
    except FileNotFoundError:
        # pgrepが無い環境では諦める(致命的ではない)
        pass
    except Exception as e:
        print(f"⚠️ Error while checking for existing Chrome processes: {e}")


def display_album_covers_with_feh(album_covers, keep_browser_open=False, existing_server=None):
    """HTMLギャラリーでアルバムジャケット画像を表示し、クリックまたは番号入力で再生"""
    global web_selection_result, web_server_running, web_server_instance, next_album_selection
    
    if not album_covers:
        print("⚠️ No cover art available to display")
        return None, None
    
    # 一時ディレクトリを作成
    temp_dir = os.path.expanduser('~/music_covers_gallery')
    if not keep_browser_open and os.path.exists(temp_dir):
        try:
            shutil.rmtree(temp_dir)
        except:
            pass
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir, mode=0o755)
    
    try:
        os.chmod(temp_dir, 0o755)
    except:
        pass
    
    try:
        print(f"\n🖼️  Preparing {len(album_covers)} album covers...")
        
        # 画像をコピー
        image_files = []
        timestamp = int(time.time())
        
        for i, (image_path, folder, track_count) in enumerate(album_covers, 1):
            ext = os.path.splitext(image_path)[1]
            new_name = f"{i:03d}_{timestamp}{ext}"
            new_path = os.path.join(temp_dir, new_name)
            # ★ シンボリックリンクで即座に完了（コピー不要）
            try:
                if os.path.lexists(new_path):
                    os.remove(new_path)
                os.symlink(image_path, new_path)
            except OSError:
                try:
                    shutil.copy2(image_path, new_path)
                    os.chmod(new_path, 0o644)
                except Exception:
                    pass
            # フォルダーパスを追加
            image_files.append((i, new_name, os.path.basename(folder), track_count, folder))
        
        # HTMLギャラリーを生成
        html_content = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>アルバムジャケット選択</title>
    <style>
        body {
            background-color: #1a1a1a;
            color: #ffffff;
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 20px;
        }
        .header {
            text-align: center;
            padding: 20px;
            background-color: #2a2a2a;
            border-radius: 10px;
            margin-bottom: 30px;
        }
        .header h1 {
            margin: 0;
            color: #4a9eff;
        }
        .header p {
            margin: 10px 0 0 0;
            color: #aaaaaa;
        }
        .status {
            text-align: center;
            padding: 10px;
            margin-bottom: 20px;
            background-color: #2a2a2a;
            border-radius: 8px;
            color: #4a9eff;
            font-weight: bold;
            display: none;
        }
        .status.show {
            display: block;
        }
        .gallery {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(100px, 1fr));
            gap: 15px;
            padding: 20px;
        }
        .album {
            position: relative;
            cursor: pointer;
            border-radius: 8px;
            overflow: hidden;
            transition: transform 0.2s, box-shadow 0.2s;
            background-color: #2a2a2a;
        }
        .album:hover {
            transform: scale(1.05);
            box-shadow: 0 8px 16px rgba(74, 158, 255, 0.4);
        }
        .album.selected {
            border: 3px solid #4a9eff;
            transform: scale(1.1);
            box-shadow: 0 12px 24px rgba(74, 158, 255, 0.6);
        }
        .album.played {
            opacity: 0.5;
            border: 2px solid #666;
        }
        .album img {
            width: 100%;
            height: 100px;
            object-fit: cover;
            display: block;
        }
        .album-number {
            position: absolute;
            top: 5px;
            right: 5px;
            background-color: rgba(0, 0, 0, 0.8);
            color: #ffffff;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: bold;
            font-size: 14px;
            border: 2px solid #4a9eff;
        }
        .album-info {
            padding: 10px;
            text-align: center;
            font-size: 12px;
            color: #aaaaaa;
        }
        .played-mark {
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            font-size: 48px;
            color: #4a9eff;
            display: none;
        }
        .album.played .played-mark {
            display: block;
        }
        /* キューパネル */
        .queue-panel {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background-color: #1e2a3a;
            border: 2px solid #4a9eff;
            border-radius: 12px;
            padding: 14px 18px;
            min-width: 240px;
            max-width: 320px;
            max-height: 60vh;
            overflow-y: auto;
            z-index: 500;
            box-shadow: 0 8px 24px rgba(0,0,0,0.6);
        }
        .queue-panel h3 {
            margin: 0 0 10px 0;
            color: #4a9eff;
            font-size: 14px;
        }
        .queue-item {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 5px 0;
            border-bottom: 1px solid #333;
            font-size: 12px;
            color: #ccc;
        }
        .queue-item:last-child { border-bottom: none; }
        .queue-pos {
            background: #4a9eff;
            color: #fff;
            border-radius: 50%;
            width: 20px;
            height: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            flex-shrink: 0;
        }
        .queue-empty {
            color: #666;
            font-size: 12px;
            text-align: center;
        }
        .queue-clear {
            margin-top: 10px;
            width: 100%;
            background: #333;
            color: #aaa;
            border: 1px solid #555;
            border-radius: 6px;
            padding: 4px 8px;
            cursor: pointer;
            font-size: 11px;
        }
        .queue-clear:hover { background: #444; color: #fff; }
        /* コンテキストメニューのスタイル */
        .context-menu {
            display: none;
            position: fixed;
            background-color: #2a2a2a;
            border: 2px solid #4a9eff;
            border-radius: 8px;
            padding: 5px 0;
            z-index: 1000;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
            min-width: 200px;
        }
        .context-menu-item {
            padding: 12px 20px;
            cursor: pointer;
            color: #ffffff;
            transition: background-color 0.2s;
        }
        .context-menu-item:hover {
            background-color: #4a9eff;
            color: #1a1a1a;
        }
        .context-menu-item i {
            margin-right: 10px;
            width: 20px;
            display: inline-block;
            text-align: center;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🎵 Album Cover Browser</h1>
        <p>Click an album cover to play it</p>
        <p>Right-click an image to open its folder</p>
        <p>全 """ + str(len(album_covers)) + """ albums</p>
    </div>
    <div id="status" class="status">Waiting...</div>
    
    <!-- キューパネル -->
    <div class="queue-panel">
        <h3>📋 Playback Queue</h3>
        <div id="queueList"><div class="queue-empty">Click to add</div></div>
        <button class="queue-clear" onclick="clearQueue()">🗑 Clear queue</button>
    </div>
    
    <!-- コンテキストメニュー -->
    <div id="contextMenu" class="context-menu">
        <div class="context-menu-item" onclick="openFolder()">
            <i>📁</i>Open folder
        </div>
    </div>
    
    <div class="gallery">
"""
        
        for i, filename, folder_name, track_count, folder_path in image_files:
            cache_buster = f"?v={timestamp}"
            # フォルダーパスをエスケープしてdata属性に追加
            escaped_folder = folder_path.replace("'", "\\'")
            html_content += f"""
        <div class="album" id="album-{i}" onclick="selectAlbum({i}, this)" data-folder="{escaped_folder}" data-name="{folder_name[:40]}">
            <img src="{filename}{cache_buster}" alt="Album {i}">
            <div class="album-number">{i}</div>
            <div class="album-info">{track_count} tracks</div>
            <div class="played-mark">✓</div>
        </div>
"""
        
        html_content += """
    </div>
    <script>
        let playedAlbums = [];
        let queueList = [];       // { num, name } の順序付きキュー
        let currentContextFolder = '';
        
        function renderQueue() {
            const el = document.getElementById('queueList');
            if (queueList.length === 0) {
                el.innerHTML = '<div class="queue-empty">Click to add</div>';
                return;
            }
            el.innerHTML = queueList.map((item, idx) =>
                `<div class="queue-item">
                    <span class="queue-pos">${idx + 1}</span>
                    <span>${item.name || ('Album ' + item.num)}</span>
                </div>`
            ).join('');
        }
        
        function clearQueue() {
            queueList = [];
            renderQueue();
        }
        
        function selectAlbum(number, el) {
            const name = el ? el.getAttribute('data-name') : ('Album ' + number);
            
            // キューに追加
            queueList.push({ num: number, name: name });
            renderQueue();
            
            // ステータス表示
            document.getElementById('status').textContent =
                'Added album ' + number + ' to queue (total: ' + queueList.length + ')';
            document.getElementById('status').classList.add('show');
            
            // 選択ハイライト
            document.querySelectorAll('.album').forEach(function(a) {
                a.classList.remove('selected');
            });
            if (el) el.classList.add('selected');
            
            playedAlbums.push(number);
            document.getElementById('album-' + number).classList.add('played');
            
            localStorage.setItem('selected_album', number);
            
            // サーバーに送信
            fetch('select.php?num=' + number, {method: 'POST'})
                .catch(function() {});
            
            setTimeout(function() {
                document.getElementById('status').textContent =
                'Queue: ' + queueList.length + ' — click more to add';
            }, 2000);
        }
        
        // コンテキストメニューの表示制御
        function showContextMenu(event, folder) {
            event.preventDefault();
            event.stopPropagation();
            
            const menu = document.getElementById('contextMenu');
            currentContextFolder = folder;
            
            menu.style.display = 'block';
            menu.style.left = event.pageX + 'px';
            menu.style.top = event.pageY + 'px';
        }
        
        // コンテキストメニューを閉じる
        function hideContextMenu() {
            document.getElementById('contextMenu').style.display = 'none';
        }
        
        // フォルダーを開く
        function openFolder() {
            if (currentContextFolder) {
                fetch('open_folder.php?path=' + encodeURIComponent(currentContextFolder), {method: 'POST'})
                    .then(response => {
                        document.getElementById('status').textContent = 'Folder opened';
                        document.getElementById('status').classList.add('show');
                        setTimeout(function() {
                            document.getElementById('status').classList.remove('show');
                        }, 2000);
                    })
                    .catch(function() {});
            }
            hideContextMenu();
        }
        
        // すべてのアルバムに右クリックイベントを追加
        document.querySelectorAll('.album').forEach(function(albumEl) {
            albumEl.addEventListener('contextmenu', function(e) {
                showContextMenu(e, this.getAttribute('data-folder'));
            });
        });
        
        // ページ全体でクリックしたらメニューを閉じる
        document.addEventListener('click', hideContextMenu);
        
        localStorage.removeItem('selected_album');
        renderQueue();
    </script>
</body>
</html>
"""
        
        html_file = os.path.join(temp_dir, 'gallery.html')
        with open(html_file, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        try:
            os.chmod(html_file, 0o644)
        except:
            pass
        
        # ★★★ サーバーの再利用ロジック ★★★
        server_port = 8765
        
        # 既存のサーバーがあればそれを使用
        if existing_server is None:
            class SelectionHandler(SimpleHTTPRequestHandler):
                def do_POST(self):
                    global web_selection_result, next_album_selection
                    if 'select.php' in self.path:
                        import urllib.parse
                        query = urllib.parse.urlparse(self.path).query
                        params = urllib.parse.parse_qs(query)
                        if 'num' in params:
                            num = int(params['num'][0])
                            next_album_selection.append(num)
                            web_selection_result = num  # 互換性のため残す
                            q_len = len(next_album_selection)
                            print(f"\n🖱️ Album {num} added to queue via browser (queue: {q_len})")
                    
                    # フォルダーを開く処理
                    elif 'open_folder.php' in self.path:
                        import urllib.parse
                        query = urllib.parse.urlparse(self.path).query
                        params = urllib.parse.parse_qs(query)
                        if 'path' in params:
                            folder_path = params['path'][0]
                            print(f"\n📁 Opening folder: {folder_path}")
                            try:
                                # ファイルマネージャーでフォルダーを開く
                                if os.path.exists(folder_path):
                                    # Linuxの一般的なファイルマネージャーを試行
                                    for fm in ['xdg-open', 'nautilus', 'dolphin', 'thunar', 'pcmanfm', 'nemo']:
                                        if shutil.which(fm):
                                            subprocess.Popen([fm, folder_path], 
                                                           stdout=subprocess.DEVNULL, 
                                                           stderr=subprocess.DEVNULL)
                                            print(f"✅ Folder opened with {fm}")
                                            break
                                else:
                                    print(f"⚠️ Folder not found: {folder_path}")
                            except Exception as e:
                                print(f"⚠️ Could not open folder: {e}")
                    
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'OK')
                
                def log_message(self, format, *args):
                    pass
            
            web_server_running = True
            web_selection_result = None
            
            def run_server():
                global web_server_instance
                os.chdir(temp_dir)
                # ★★★ SO_REUSEADDRオプションを追加 ★★★
                socketserver.TCPServer.allow_reuse_address = True
                try:
                    with socketserver.TCPServer(("", server_port), SelectionHandler) as httpd:
                        web_server_instance = httpd
                        httpd.timeout = 1
                        while web_server_running:
                            httpd.handle_request()
                except OSError as e:
                    if e.errno == 98:  # Address already in use
                        print(f"⚠️ Port {server_port} is already in use — reusing existing server.")
                    else:
                        raise
            
            server_thread = threading.Thread(target=run_server, daemon=True)
            server_thread.start()
            time.sleep(0.5)  # サーバー起動待ち
        else:
            print("♻️ Reusing existing server")
        
        print("=" * 60)
        print("📋 Album list (number and track count):")
        for i, (image_path, folder, track_count) in enumerate(album_covers, 1):
            folder_name = os.path.basename(folder)
            print(f"  {i:3d}. {folder_name[:60]:60} ({track_count} tracks)")
        
        print("\n" + "=" * 60)
        print(f"💡 Open in browser: http://localhost:{server_port}/gallery.html")
        print("=" * 60)
        print("🎵 How to use:")
        print("   1. Open the URL above in a browser")
        print("   2. Click a cover art image → playback starts immediately")
        print("   3. Or type a number directly in this terminal")
        print("   4. Enter 'q' or 'back' to exit")
        print("=" * 60)
        
        browser_url = f"http://localhost:{server_port}/gallery.html"
        browser_process = None
        
        profile_dir = os.path.expanduser('~/music_player_chrome_profile')
        
        chrome_options = [
            f'--user-data-dir={profile_dir}',
            '--disable-cache',
            '--disk-cache-size=1',
            '--no-first-run',
            '--no-default-browser-check',
            f'--app={browser_url}'
        ]
        
        if not keep_browser_open:
            # ★ 新規起動前に、取り残された同一プロファイルのChromeを掃除
            #   (プロセスが残っていた場合の保険として引き続き実施)
            _kill_stale_chrome_for_profile(profile_dir)
            for browser in ['google-chrome', 'chromium-browser', 'chromium', 'chrome']:
                browser_path = shutil.which(browser)
                if browser_path:
                    try:
                        browser_process = subprocess.Popen(
                            [browser_path] + chrome_options,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True
                        )
                        print(f"\n🌐 Browser ({browser}) opened with dedicated profile")
                        break
                    except Exception as e:
                        print(f"⚠️ Failed to launch {browser}: {e}")
                        continue
        
        if not browser_process and not keep_browser_open:
            print("\n💡 Browser did not open automatically")
            print(f"   Open manually: {browser_url}")
        
        print("\n⏳ Waiting for a click or number input...")
        
        selected_index = None
        
        try:
            while selected_index is None:
                if next_album_selection:
                    num = next_album_selection.pop(0)
                    candidate = num - 1
                    if 0 <= candidate < len(album_covers):
                        selected_folder = album_covers[candidate][1]
                        remaining = len(next_album_selection)
                        print(f"✅ Browser selected: {os.path.basename(selected_folder)} ({album_covers[candidate][2]} tracks)")
                        if remaining > 0:
                            print(f"   📋 {remaining} item(s) in queue")
                        selected_index = candidate
                        break
                    else:
                        print(f"⚠️ Invalid selection: {num}")
                        selected_index = None
                
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    try:
                        choice = sys.stdin.readline().strip()
                        
                        if choice.lower() == 'q' or choice.lower() == 'back':
                            print("⚠️ Selection cancelled")
                            break
                        
                        if choice.isdigit():
                            idx = int(choice) - 1
                            if 0 <= idx < len(album_covers):
                                selected_index = idx
                                selected_folder = album_covers[idx][1]
                                print(f"✅ Selected by number: {os.path.basename(selected_folder)} ({album_covers[idx][2]} tracks)")
                                break
                            else:
                                print(f"⚠️ Enter a number from 1 to {len(album_covers)}")
                        else:
                            if choice:
                                print("⚠️ Please enter a number")
                    
                    except (EOFError, KeyboardInterrupt):
                        print("\n⚠️ Selection cancelled")
                        break
                
                time.sleep(0.1)
        
        finally:
            pass  # サーバーとブラウザは維持
        
        return selected_index, browser_process
    
    except Exception as e:
        print(f"⚠️ Error: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def get_recently_added_folders(n=10):
    """
    MUSIC_DIRSの中から最近追加されたアルバムフォルダを最大n個返す。
    ★高速化版★ 2段階方式:
      Phase1: フォルダのmtime(stat1回)だけで候補を絞る
      Phase2: 上位候補フォルダのみファイルctimeを確認
    さらに重複パスを除去してスキャンを最小化。
    ~/A のような明示指定フォルダは必ずPhase2対象に含める。
    """
    EXPAND_ROOTS = {'/media', '/mnt', os.path.expanduser('~/Desktop')}

    # Step0: MUSIC_DIRSを展開しつつ重複・包含関係を除去
    raw_dirs = []
    for d in MUSIC_DIRS:
        if not os.path.isdir(d):
            continue
        if d in EXPAND_ROOTS:
            try:
                for sub in os.listdir(d):
                    sub_path = os.path.join(d, sub)
                    if os.path.isdir(sub_path):
                        raw_dirs.append(sub_path)
            except OSError:
                pass
        else:
            raw_dirs.append(d)

    raw_dirs = sorted(set(raw_dirs))
    search_dirs = []
    for d in raw_dirs:
        dominated = any(
            d != other and d.startswith(other.rstrip('/') + '/')
            for other in raw_dirs
        )
        if not dominated:
            search_dirs.append(d)

    print(f"  📂 Scanning {len(search_dirs)} drive(s)")

    # Phase1: フォルダmtime(stat1回)だけで候補を絞る
    PHASE1_CANDIDATE_MULT = 8
    folder_dir_mtime = {}

    for search_dir in search_dirs:
        for root, dirs, files in os.walk(search_dir):
            dirs.sort()
            if files:
                try:
                    dir_mt = os.stat(root).st_mtime
                    folder_dir_mtime[root] = dir_mt
                except OSError:
                    pass

    if not folder_dir_mtime:
        return []

    sorted_by_dir_mtime = sorted(
        folder_dir_mtime.items(), key=lambda x: x[1], reverse=True
    )
    top_candidate_folders = [f for f, _ in sorted_by_dir_mtime[:n * PHASE1_CANDIDATE_MULT]]

    print(f"  🔍 Phase 1 candidates: {len(top_candidate_folders)} folder(s) → Phase 2 detail check")

    # Phase2: 候補フォルダのみファイルctimeを確認
    folder_ctimes = {}
    for folder in top_candidate_folders:
        try:
            files_in_folder = os.listdir(folder)
        except OSError:
            continue
        newest_ctime = 0.0
        has_audio = False
        for fname in files_in_folder:
            if fname.lower().endswith(SUPPORTED_EXTENSIONS):
                fpath = os.path.join(folder, fname)
                try:
                    ct = os.stat(fpath).st_ctime
                    has_audio = True
                    if ct > newest_ctime:
                        newest_ctime = ct
                except OSError:
                    pass
        if has_audio:
            folder_ctimes[folder] = newest_ctime

    if not folder_ctimes:
        print("  ⚠️ Phase 2 found no music folders — falling back to full scan...")
        for search_dir in search_dirs:
            for root, dirs, files in os.walk(search_dir):
                newest_ctime = 0.0
                has_audio = False
                for fname in files:
                    if fname.lower().endswith(SUPPORTED_EXTENSIONS):
                        fpath = os.path.join(root, fname)
                        try:
                            ct = os.stat(fpath).st_ctime
                            has_audio = True
                            if ct > newest_ctime:
                                newest_ctime = ct
                        except OSError:
                            pass
                if has_audio:
                    folder_ctimes[root] = newest_ctime

    if not folder_ctimes:
        return []

    sorted_folders = sorted(folder_ctimes.items(), key=lambda x: x[1], reverse=True)
    return sorted_folders[:n]


def select_recently_added_jacket_mode(n=10):
    """
    最近追加された音源 n セットをジャケット画像で表示し、
    ジャケットモードに準じて再生する（オプション N）。
    """
    global web_server_running, web_server_instance, web_selection_result, next_album_selection

    print(f"\n🆕  Recently added (latest {n} sets) — cover-art browser")
    print("=" * 60)
    print(f"📂 Searching for recently added album folders...")

    recent_folders = get_recently_added_folders(n=n)

    if not recent_folders:
        print("⚠️ No recently added folders found")
        return

    print(f"✅ Found {len(recent_folders)} recent folder(s):")
    for i, (folder, mtime) in enumerate(recent_folders, 1):
        dt_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))
        print(f"   {i:2d}. [{dt_str}]  {os.path.basename(folder)}")

    # 各フォルダからトラックを収集してジャケット用データを作る
    all_tracks = []
    for folder, _ in recent_folders:
        folder_tracks = get_folder_tracks(folder)
        all_tracks.extend(folder_tracks)

    if not all_tracks:
        print("⚠️ No music files found")
        return

    album_covers = collect_album_covers(all_tracks, limit=n)

    if not album_covers:
        print("⚠️ No cover art found (the folder may not contain image files)")
        return

    # ===== 以下はジャケットモードと同じループ =====
    # グローバル状態リセット
    next_album_selection.clear()
    web_selection_result = None

    def clear_input_buffer():
        try:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except:
            pass
        while select.select([sys.stdin], [], [], 0.0)[0]:
            try:
                sys.stdin.readline()
            except:
                break

    browser_process = None

    try:
        selected_index, browser_proc = display_album_covers_with_feh(
            album_covers,
            keep_browser_open=False,
            existing_server=None
        )

        if browser_proc:
            browser_process = browser_proc

        if selected_index is None:
            print("\n📱 Album selection cancelled")
            return

        while True:
            selected_folder = album_covers[selected_index][1]
            folder_tracks = get_folder_tracks(selected_folder)

            if not folder_tracks:
                print("⚠️ No music files found in the selected folder")
                break

            # 追加日時を表示
            folder_mtime_map = {f: mt for f, mt in recent_folders}
            mt = folder_mtime_map.get(selected_folder, 0)
            dt_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(mt)) if mt else "不明"

            print(f"\n📂 Folder: {os.path.basename(selected_folder)}")
            print(f"📅 Added: {dt_str}")
            print(f"🎵 Playing {len(folder_tracks)} tracks in folder mode")
            q_pending = len(next_album_selection)
            if q_pending > 0:
                print(f"📋 You can click more albums in the browser during playback (queue: {q_pending})")
            else:
                print(f"💡 You can click the next album in the browser during playback")

            # ★ 開始曲番号の選択
            folder_tracks = ask_start_track(folder_tracks)

            play_music_with_mode_switching({'mode': 'folder', 'tracks': folder_tracks})

            print("\n" + "=" * 60)
            print("✅ Album playback complete")

            time.sleep(0.5)
            clear_input_buffer()

            if next_album_selection:
                num = next_album_selection.pop(0)
                idx = num - 1
                if 0 <= idx < len(album_covers):
                    selected_index = idx
                    remaining = len(next_album_selection)
                    print(f"▶️  Playing queued album #{num} (remaining: {remaining})")
                    continue
                else:
                    print(f"⚠️ Invalid queue number: {num}")

            print("\n💡 Select the next album from the browser")
            print("   or type a number in this terminal")
            print("   Enter 'q' to return to the menu")
            print("=" * 60)

            # ★ 修正: readline()前にターミナルをcanonicalモードに戻す
            try:
                import subprocess as _sp
                _sp.run(['stty', 'sane'], check=False, timeout=1)
            except Exception:
                pass
            try:
                termios.tcflush(sys.stdin, termios.TCIFLUSH)
            except Exception:
                pass

            user_quit = False
            timeout_counter = 0
            max_timeout = 3000

            while timeout_counter < max_timeout:
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    try:
                        choice = sys.stdin.readline().strip()
                        if choice.lower() in ('q', 'back'):
                            print("\n📱 Exiting recently-added mode")
                            user_quit = True
                            break
                        if choice.isdigit():
                            idx = int(choice) - 1
                            if 0 <= idx < len(album_covers):
                                selected_index = idx
                                print(f"✅ Album #{choice} selected")
                                break
                            else:
                                print(f"⚠️ Enter a number from 1 to {len(album_covers)}")
                    except Exception as e:
                        print(f"⚠️ Input error: {e}")

                if next_album_selection:
                    num = next_album_selection.pop(0)
                    idx = num - 1
                    if 0 <= idx < len(album_covers):
                        selected_index = idx
                        remaining = len(next_album_selection)
                        print(f"\n▶️  Album #{num} selected (remaining: {remaining})")
                        break
                    else:
                        print(f"⚠️ Invalid selection: {num}")

                timeout_counter += 1

            if user_quit:
                break
            if timeout_counter >= max_timeout:
                print("\n⏰ Timeout: returning to menu")
                break

    except KeyboardInterrupt:
        print("\n⚠️ Ctrl+C pressed")
    except Exception as e:
        print(f"\n⚠️ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n🧹 Exiting recently-added mode...")
        web_server_running = False
        time.sleep(1.0)
        if web_server_instance:
            try:
                web_server_instance.server_close()
            except Exception as e:
                print(f"⚠️ Warning while stopping server: {e}")
        web_server_instance = None
        if browser_process and browser_process.poll() is None:
            try:
                browser_process.terminate()
                browser_process.wait(timeout=2)
            except:
                try:
                    browser_process.kill()
                except:
                    pass
        clear_input_buffer()
        try:
            import termios
            old_settings = termios.tcgetattr(sys.stdin)
            termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)
        except:
            pass
        time.sleep(0.3)
        next_album_selection.clear()
        web_selection_result = None
        print("✅ Recently-added mode exited\n")


def select_album_by_cover_image_loop(mode='all', filters=None):
    """ジャケット画像から選曲するメイン関数(ループ対応版)"""
    global web_server_running, web_server_instance, web_selection_result, next_album_selection
    
    print("\n🖼️  Cover-art browser mode")
    print("=" * 60)
    
    # トラック一覧を取得
    if mode == 'all':
        print("📦 Collecting all album covers from the database...")
        db = safe_load_database()
        if not db:
            return
        tracks = db
    elif mode == 'filtered' and filters:
        print(f"📦 Collecting cover art matching {filters}...")
        tracks = get_tracks_by_filters(filters, limit=10000)
        if not tracks:
            print("⚠️ No tracks matched the filter")
            return
    else:
        print("⚠️ Invalid mode")
        return
    
    album_covers = collect_album_covers(tracks, limit=1000)
    
    if not album_covers:
        print("⚠️ No cover art found")
        return
    
    print(f"✅ Found {len(album_covers)} album cover(s)")
    
    browser_process = None
    
    # ★★★ グローバル状態を完全にリセット ★★★
    next_album_selection.clear()
    web_selection_result = None
    
    # ★★★ 入力バッファをクリアする関数 ★★★
    def clear_input_buffer():
        """標準入力バッファをクリア"""
        try:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except:
            pass
        while select.select([sys.stdin], [], [], 0.0)[0]:
            try:
                sys.stdin.readline()
            except:
                break
    
    try:
        # 初回の選択
        selected_index, browser_proc = display_album_covers_with_feh(
            album_covers, 
            keep_browser_open=False,
            existing_server=None
        )
        
        if browser_proc:
            browser_process = browser_proc
        
        if selected_index is None:
            print("\n📱 Album selection cancelled")
            return
        
        # ★★★ メインループ ★★★
        while True:
            # 現在のアルバムを再生
            selected_folder = album_covers[selected_index][1]
            folder_tracks = get_folder_tracks(selected_folder)
            
            if not folder_tracks:
                print("⚠️ No music files found in the selected folder")
                break
            
            print(f"\n📂 Folder: {os.path.basename(selected_folder)}")
            print(f"🎵 Playing {len(folder_tracks)} tracks in folder mode")
            q_pending = len(next_album_selection)
            if q_pending > 0:
                print(f"📋 You can click more albums in the browser during playback (queue: {q_pending})")
            else:
                print(f"💡 You can click the next album in the browser during playback")
            
            # ★ 開始曲番号の選択
            folder_tracks = ask_start_track(folder_tracks)
            
            # 再生開始
            play_music_with_mode_switching({'mode': 'folder', 'tracks': folder_tracks})
            
            print("\n" + "=" * 60)
            print("✅ Album playback complete")
            
            time.sleep(0.5)
            clear_input_buffer()
            
            # キューに予約されたアルバムをすべて順番に処理
            if next_album_selection:
                num = next_album_selection.pop(0)
                idx = num - 1
                if 0 <= idx < len(album_covers):
                    selected_index = idx
                    remaining = len(next_album_selection)
                    print(f"▶️  Playing queued album #{num} (remaining: {remaining})")
                    continue
                else:
                    print(f"⚠️ Invalid queue number: {num}")
            
            # キューが空のとき次の選択を待つ
            print("\n💡 Select the next album from the browser")
            print("   or type a number in this terminal")
            print("   Enter 'q' to return to the menu")
            print("=" * 60)
            
            # ★ 修正: readline()を呼ぶ前にターミナルをcanonical（通常）モードに確実に戻す
            # play_music_with_mode_switching → keyboard_listener がrawモードを
            # 使用するため、ここで明示的にリセットしないとreadline()がブロックする
            try:
                import subprocess as _sp
                _sp.run(['stty', 'sane'], check=False, timeout=1)
            except Exception:
                pass
            try:
                termios.tcflush(sys.stdin, termios.TCIFLUSH)
            except Exception:
                pass
            
            # ★★★ シンプルな入力待機 ★★★
            user_quit = False
            timeout_counter = 0
            max_timeout = 3000
            
            while timeout_counter < max_timeout:
                # キーボード入力チェック
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    try:
                        choice = sys.stdin.readline().strip()
                        
                        if choice.lower() == 'q' or choice.lower() == 'back':
                            print("\n📱 Exiting cover-art browser")
                            user_quit = True
                            break
                        
                        if choice.isdigit():
                            idx = int(choice) - 1
                            if 0 <= idx < len(album_covers):
                                selected_index = idx
                                print(f"✅ Album #{choice} selected")
                                break
                            else:
                                print(f"⚠️ Enter a number from 1 to {len(album_covers)}")
                    except Exception as e:
                        print(f"⚠️ Input error: {e}")
                
                # キューからのクリックチェック
                if next_album_selection:
                    num = next_album_selection.pop(0)
                    idx = num - 1
                    if 0 <= idx < len(album_covers):
                        selected_index = idx
                        remaining = len(next_album_selection)
                        print(f"\n▶️  Album #{num} selected (remaining: {remaining})")
                        break
                    else:
                        print(f"⚠️ Invalid selection: {num}")
                
                timeout_counter += 1
            
            if user_quit:
                break
            
            if timeout_counter >= max_timeout:
                print("\n⏰ Timeout: returning to menu")
                break
    
    except KeyboardInterrupt:
        print("\n⚠️ Ctrl+C pressed")
    except Exception as e:
        print(f"\n⚠️ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n🧹 Exiting cover-art browser...")
        
        # ★★★ 重要: サーバー停止フラグを先に立てる ★★★
        web_server_running = False
        
        # ★★★ サーバースレッドが終了するまで少し待つ ★★★
        time.sleep(1.0)
        
        # ★★★ サーバーインスタンスの処理（shutdown()は使わない） ★★★
        if web_server_instance:
            try:
                # shutdown()の代わりにserver_close()だけを使う
                web_server_instance.server_close()
                print("✓ Server stopped")
            except Exception as e:
                print(f"⚠️ Warning while stopping server: {e}")
        web_server_instance = None
        
        # ブラウザプロセス終了
        if browser_process and browser_process.poll() is None:
            try:
                browser_process.terminate()
                browser_process.wait(timeout=2)
                print("✓ Browser closed")
            except:
                try:
                    browser_process.kill()
                except:
                    pass
        
        # 入力バッファクリア
        clear_input_buffer()
        
        # ターミナルリセット
        try:
            import termios
            old_settings = termios.tcgetattr(sys.stdin)
            termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)
        except:
            pass
        
        time.sleep(0.3)
        
        # リセット
        next_album_selection.clear()
        web_selection_result = None
        
        print("✅ Cover-art browser exited\n")
        print("💡 Please wait a moment for the menu to appear...")


# ===== 音楽再生機能 =====

def apply_audio_preset_via_sox(input_file, output_device, preset_name='none'):
    """SoXを使ってプリセットを適用しながらリアルタイム再生"""
    preset = AUDIO_PRESETS.get(preset_name, None)
    
    if preset is None:
        return None
    
    try:
        # ★★★ 修正: soxiが失敗した場合のフォールバック処理を追加 ★★★
        orig_rate = None
        try:
            result = subprocess.run(['soxi', '-r', input_file], 
                                  capture_output=True, text=True, timeout=2)
            if result.returncode == 0 and result.stdout.strip():
                orig_rate = result.stdout.strip()
                print(f"📊 soxi sample rate: {orig_rate} Hz")
        except Exception as e:
            print(f"⚠️ soxi warning: {e} — trying fallback")
        
        # ★★★ soxiが失敗した場合、get_sample_rate関数を使用 ★★★
        if not orig_rate:
            print("📊 Getting sample rate via fallback...")
            orig_rate = str(get_sample_rate(input_file))
            print(f"📊 Sample rate: {orig_rate} Hz")

        vol_minus_8db = ["vol", "-8dB"]
        
        sox_cmd = [
            'sox', input_file,
            '--buffer', '16384',
            '-t', 'alsa',
            '-b', '32',
            '-r', orig_rate,
            output_device
        ] + vol_minus_8db + preset
        
        # ★★★ イコライザー統合 ★★★
        eq_filters = get_equalizer_sox_filters()
        if eq_filters:
            sox_cmd.extend(eq_filters)
        
        # ── ピーククリッピング防止（alimiter相当: 0.98dBFS 超をカット）──
        # compand attack,decay  dB_in:dB_out_pairs  gain
        sox_cmd.extend(['compand', '0,0.005', '2:-inf,-inf,-0.18,-0.18', '0'])
        
        print(f"🎵 Running SoX...")
        # ★★★ 修正: エラー出力を確認できるようにする ★★★
        sox_process = subprocess.Popen(sox_cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
        
        # ★★★ 修正: プロセスが正常に起動したか確認 ★★★
        time.sleep(0.3)  # プロセス起動を待つ
        if sox_process.poll() is not None:
            # プロセスがすぐに終了した場合
            stderr_output = sox_process.stderr.read().decode('utf-8', errors='replace')
            print(f"⚠️ SoX process exited immediately")
            print(f"⚠️ Error details: {stderr_output}")
            return None
        
        print(f"✅ SoX process started (PID: {sox_process.pid})")
        return sox_process
        
    except Exception as e:
        print(f"⚠️ SoX preset error: {e}")
        import traceback
        traceback.print_exc()
        return None



# ===== ★★★ Sonia Intelligence 補助関数 ★★★ =====

_SI_EQ_BANDS = {
    "sub_bass":  (40,   60),   "bass":      (80,   100),
    "low_mid":   (200,  160),  "mid":       (500,  300),
    "upper_mid": (1000, 500),  "presence":  (2000, 800),
    "high_mid":  (4000, 1500), "air_low":   (6000, 2000),
    "air":       (10000,3000), "air_high":  (16000,4000),
}

def _si_get_eq_filters(params) -> str:
    if not SI_AVAILABLE or not params or not params.eq:
        return ""
    parts = []
    for band, gain in params.eq.items():
        if abs(gain) < 0.1:
            continue
        if band in _SI_EQ_BANDS:
            freq, width = _SI_EQ_BANDS[band]
            parts.append(f"equalizer=f={freq}:t=h:width={width}:g={gain:.1f}")
    return ",".join(parts)

def _si_status_line() -> str:
    if not SI_AVAILABLE or not _si_instance or not _si_instance.current_params:
        return ""
    p = _si_instance.current_params
    try:
        space = _si_get_space(p.acoustic_space)
        rt60 = p.rt60_override or (space.reverb.rt60_sec if space else 0)
        space_short = space.display_name.split("(")[0].strip()[:18] if space else p.acoustic_space
    except:
        rt60 = 0; space_short = p.acoustic_space
    if _si_instance.current_profile_id:
        prof = _si_instance.db.get(_si_instance.current_profile_id)
        pname = prof.display_name[:18] if prof else "---"
    else:
        pname = "---"
    return f"🎵SI: {pname} | {space_short} | RT60={rt60:.1f}s"

def _si_readline(prompt="  → ") -> str:
    """
    SI専用の入力読み取り。
    /dev/ttyを直接開いてkeyboard_listenerスレッドの干渉を避ける。
    日本語入力・コピペ両方に対応。
    """
    global si_input_active
    si_input_active = True
    _si_display_event.clear()   # 情報表示スレッドを完全停止
    result = ""
    tty_fd = None
    try:
        import subprocess as _sp
        # keyboard_listenerがraw modeを手放すのを待つ（display_threadはEvent停止済み）
        time.sleep(0.15)
        # /dev/ttyを直接開く（sys.stdinとは独立）
        tty_fd = open("/dev/tty", "r", encoding="utf-8", errors="replace")
        # canonicalモード + echoを確実に有効化
        _sp.run(["stty", "sane"], stdin=tty_fd, check=False, timeout=1)
        # ★★★ 修正: プロンプト表示の直前にもう一度flush ★★★
        # 呼び出し元（特にラジオ再生の[c]キー等）ではキー押下からここに来るまでに
        # プロセス停止処理やsleepで数秒かかることがあり、その間にユーザーが焦って
        # 続けて数字やEnterを叩くと端末の入力キューに溜まってしまう。
        # プロンプトを出す直前にflushすることで、それらの「フライング入力」を
        # 確実に読み捨て、メニュー表示後に打った入力だけを拾うようにする。
        try:
            termios.tcflush(tty_fd, termios.TCIFLUSH)
        except Exception:
            pass
        print(prompt, end="", flush=True)
        result = tty_fd.readline().rstrip("\n").strip()
    except Exception as e:
        print(f"\n⚠️ Input error: {e}")
    finally:
        if tty_fd:
            try: tty_fd.close()
            except: pass
        si_input_active = False
        _si_display_event.set()   # 情報表示スレッドを再開
        # keyboard_listenerのraw modeを復元
        try:
            time.sleep(0.05)
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            tty.setraw(sys.stdin.fileno())
        except: pass
    return result


def _si_do_feedback():
    """[z]キー: Sonia Intelligence フィードバック入力"""
    if not SI_AVAILABLE or not _si_instance:
        return
    time.sleep(0.60)  # info_display_threadのsleep(0.5)より長く待つ
    sys.stdout.write("\033[2J\033[H")  # 画面全体クリア＋先頭
    sys.stdout.flush()
    print("\n")
    print("┌──────────────────────────────────────────────────┐")
    print("│  🎵 Sonia Intelligence — Enter your listening feedback │")
    print("├──────────────────────────────────────────────────┤")
    print("│  e.g.: more piano forward / more hall depth           │")
    print("│       bass too heavy / boost string brilliance        │")
    print("│  [Empty Enter = cancel]                               │")
    print("└──────────────────────────────────────────────────┘")
    text = _si_readline("  → ")
    if not text:
        print("  Cancelled")
        return
    try:
        _, explanation = _si_instance.on_feedback(text)
        print()
        for line in explanation.split("\n")[:6]:
            if line.strip():
                print(f"  ✓ {line}")
    except Exception as e:
        print(f"  ⚠️ Error: {e}")
    _si_readline("  [Press Enter to resume] ")


def _si_do_hall_select():
    """[h]キー: ホール（音響空間）選択"""
    if not SI_AVAILABLE or not _si_instance:
        return
    try:
        from acoustic_spaces import list_spaces
        spaces = list_spaces()
    except Exception as e:
        print(f"\n⚠️ Error getting acoustic space list: {e}")
        return
    sys.stdout.write("\033[2J\033[H")  # 画面全体クリア＋先頭（Eventで停止済み）
    sys.stdout.flush()
    cur = _si_instance.current_params.acoustic_space if _si_instance.current_params else ""
    print("\n")
    print("┌───────────────────────────────────────────────────────┐")
    print("│  🏛️  Sonia Intelligence — Acoustic space selection       │")
    print("├───────────────────────────────────────────────────────┤")
    for i, s in enumerate(spaces, 1):
        mark = "●" if s["name"] == cur else " "
        rt = s["rt60"]; bar = "█" * int(rt * 3)
        print(f"│  {mark} {i}. {s['display_name'][:36]:<36} RT={rt:.1f}s {bar:<6} │")
    print("└───────────────────────────────────────────────────────┘")
    choice = _si_readline("  選択 → ")
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(spaces) and _si_instance.current_params:
            sn = spaces[idx]["name"]
            _si_instance.current_params.acoustic_space = sn
            for attr in ("rt60_override","wet_override","pre_delay_override","side_ratio_override"):
                setattr(_si_instance.current_params, attr, None)
            print(f"\n  ✓ Acoustic space changed: {spaces[idx]['display_name']}")
    _si_readline("  [Press Enter to resume] ")


def _si_do_profile_select():
    """[p]キー: SIプロファイル選択"""
    if not SI_AVAILABLE or not _si_instance:
        return
    try:
        from genre_presets import list_presets
        from filter_builder import params_from_preset
        profiles = _si_instance.db.list_profiles()
        presets = list_presets()
    except Exception as e:
        print(f"\n⚠️ Error getting profile list: {e}")
        return
    sys.stdout.write("\033[2J\033[H")  # 画面全体クリア＋先頭（Eventで停止済み）
    sys.stdout.flush()
    print("\n")
    print("┌──────────────────────────────────────────────┐")
    print("│  🎵 Sonia Intelligence — Profile selection    │")
    print("├──────────────────────────────────────────────┤")
    if profiles:
        for i, p in enumerate(profiles[:8], 1):
            fb = f"({len(p.tuning_history)}回)" if p.tuning_history else ""
            mark = "●" if p.profile_id == _si_instance.current_profile_id else " "
            print(f"│  {mark} {i}. {p.display_name[:28]:<28} {fb:<6} │")
        print("├──────────────────────────────────────────────┤")
    offset = len(profiles[:8])
    for i, p in enumerate(presets, offset + 1):
        print(f"│    {i}. {p['display_name'][:38]:<38} │")
    print("└──────────────────────────────────────────────┘")
    choice = _si_readline("  選択 → ")
    if choice.isdigit():
        idx = int(choice) - 1
        try:
            if 0 <= idx < len(profiles):
                prof = profiles[idx]
                params = _si_instance.db._dict_to_params(prof.current_params, prof.base_preset)
                _si_instance.current_params = params
                _si_instance.current_profile_id = prof.profile_id
                print(f"\n  ✓ Profile applied: {prof.display_name}")
            elif offset <= idx < offset + len(presets):
                preset = presets[idx - offset]
                params = params_from_preset(preset["name"])
                _si_instance.current_params = params
                _si_instance.current_profile_id = None
                print(f"\n  ✓ Preset applied: {preset['display_name']}")
        except Exception as e:
            print(f"\n  ⚠️ Apply error: {e}")
    _si_readline("  [Press Enter to resume] ")




def _si_write_back_to_music_db(album: str, folder: str, preset_name: str):
    """
    music_mood_db.json の該当アルバム全トラックに
    si_preset フィールドを書き戻す。次回再生時に自動適用される。
    """
    try:
        if not os.path.exists(DATABASE_FILE):
            return
        with open(DATABASE_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
        if not isinstance(db, list):
            return
        folder_norm = os.path.normpath(folder) if folder else ""
        updated = 0
        for track in db:
            track_album  = track.get("album", "")
            track_folder = os.path.normpath(os.path.dirname(track.get("path", "")))
            if ((album and track_album == album)
                    or (folder_norm and track_folder == folder_norm)):
                track["si_preset"] = preset_name
                updated += 1
        if updated > 0:
            with open(DATABASE_FILE, "w", encoding="utf-8") as f:
                json.dump(db, f, ensure_ascii=False, indent=2)
            print(f"  📝 music_mood_db: recorded si_preset=\'{preset_name}\' for {updated} track(s)")
    except Exception as e:
        print(f"  ⚠️ DB write-back error: {e}")


def _si_do_album_preset():
    """
    [a]キー: このアルバム/フォルダにプリセットを手動登録。
    次回から自動適用される。
    """
    if not SI_AVAILABLE or not _si_instance:
        return
    try:
        from genre_presets import list_presets, get_preset
        presets = list_presets()
    except Exception as e:
        print(f"\n⚠️ Error getting preset list: {e}")
        return

    album = _si_instance._pending_album
    folder = os.path.basename(_si_instance._pending_folder.rstrip("/")) if _si_instance._pending_folder else ""
    display_name = album or folder or "（不明）"

    time.sleep(0.60)
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

    print("\n")
    print("┌─────────────────────────────────────────────────────┐")
    print("│  📀 Sonia Intelligence — Register album preset       │")
    print("├─────────────────────────────────────────────────────┤")
    album_disp = display_name[:45]
    print(f"│  Album: {album_disp:<46} │")
    print("│  Select the acoustic preset for this album           │")
    print("├─────────────────────────────────────────────────────┤")

    # カテゴリを整理して表示
    CATEGORIES = [
        ("クラシック — ピアノ", [
            "piano_solo_intimate", "piano_concerto_romantic", "piano_concerto_modern"
        ]),
        ("クラシック — 弦楽・管弦楽", [
            "string_quartet", "violin_concerto", "symphony_full"
        ]),
        ("クラシック — 声楽・オペラ", [
            "opera_aria"
        ]),
        ("ジャズ", [
            "jazz_trio", "jazz_vocal"
        ]),
        ("汎用", [
            "default"
        ]),
    ]

    num = 0
    num_to_preset = {}
    for cat_name, preset_names in CATEGORIES:
        print(f"│  ── {cat_name:<47} │")
        for pname in preset_names:
            p = get_preset(pname)
            num += 1
            num_to_preset[num] = pname
            cur_mark = "●" if (not _si_instance._last_was_default and
                               _si_instance._pending_album and
                               _si_instance.db.get_album_preset(album, _si_instance._pending_folder) == pname) else " "
            print(f"│  {cur_mark} {num:2d}. {p.display_name[:44]:<44} │")

    print("├─────────────────────────────────────────────────────┤")
    print("│  [Enter only] Cancel                                 │")
    print("└─────────────────────────────────────────────────────┘")

    choice = _si_readline("  選択番号 → ")
    if not choice or not choice.strip().isdigit():
        print("  Cancelled")
        return

    idx = int(choice.strip())
    if idx not in num_to_preset:
        print("  Invalid number")
        return

    preset_name = num_to_preset[idx]
    try:
        _si_instance.apply_album_preset(preset_name)
        p = get_preset(preset_name)
        print(f"\n  ✅ Registered: {display_name}")
        print(f"     → {p.display_name}")
        print(f"     Will be applied automatically from next playback")
        # music_mood_db.jsonにも書き戻す
        _si_write_back_to_music_db(album, _si_instance._pending_folder, preset_name)
    except Exception as e:
        print(f"\n  ⚠️ Registration error: {e}")

    _si_readline("\n  [Press Enter to resume] ")


def _si_do_preset_menu():
    """
    [x]キー: Sonia Intelligence 音響プリセット番号選択メニュー
    自然言語を入力しなくても番号を選ぶだけで音響調整ができる。
    """
    if not SI_AVAILABLE or not _si_instance:
        return

    time.sleep(0.60)  # info_display_threadのsleep(0.5)より長く待つ
    sys.stdout.write("\033[2J\033[H")  # 画面全体クリア＋先頭
    sys.stdout.flush()

    # ── プリセット定義 ────────────────────────────────────────────
    # (表示ラベル, フィードバックテキスト, カテゴリ, eq_delta)
    # eq_delta が非空の項目 → current_params.eq に直接加算（NLP解釈バイパス）
    # eq_delta が空 {}     → on_feedback 経由（残響・空間系など）
    # バンド: sub_bass/bass/low_mid/mid/upper_mid/presence/high_mid/air_low/air/air_high
    PRESETS = [
        # ── リセット ─────────────────────────────
        ("リセット",    None,                          None,    None),
        ("EQ全解除（SI補正をクリア）",  None,          "reset", "RESET"),
        # ── 空間・残響 ──────────────────────────
        ("空間・残響",  None,                          None,    None),
        ("部屋を広く（残響+）",          "ホールの奥行きをもっと",         "space", {}),
        ("部屋を狭く（残響−）",          "エコーを少なく",                 "space", {}),
        ("残響をかなり増やす",           "残響をもっと",                   "space", {}),
        ("残響をかなり減らす",           "残響を減らして",                 "space", {}),
        ("包まれ感を強める",             "包まれ感をもっと",               "space", {}),
        # ── 低域 ────────────────────────────────
        ("低域",        None,                          None,    None),
        ("低音を強く",                   "低音をもっと",                   "bass",  {"sub_bass": +2.0, "bass": +3.0}),
        ("低音を弱く",                   "低音が重すぎる",                 "bass",  {"sub_bass": -2.0, "bass": -3.0}),
        ("ベースをもっと前に",           "ベースをもっと",                 "bass",  {"bass": +2.0, "low_mid": +1.0}),
        ("低域のこもりを解消",           "低域がぼわぼわする",             "bass",  {"low_mid": -2.5, "bass": -1.0}),
        # ── 中高域・存在感 ──────────────────────
        ("中高域・存在感", None,                       None,    None),
        ("ピアノをもっと前に",           "ピアノをもっと前に",             "mid",   {"upper_mid": +2.0, "presence": +1.5}),
        ("弦をもっと前に",               "弦をもっと",                     "mid",   {"presence": +2.0, "high_mid": +1.0}),
        ("ヴォーカルをもっと前に",       "声を前に",                       "mid",   {"mid": +2.0, "upper_mid": +1.5}),
        ("明瞭度を上げる",               "もっとクリアに",                 "mid",   {"presence": +2.0, "high_mid": +1.0, "low_mid": -1.0}),
        ("温かさを出す",                 "温かみをもっと",                 "mid",   {"mid": +1.5, "bass": +1.0, "air": -1.0}),
        ("籠り感を減らす",               "籠り感がある",                   "mid",   {"low_mid": -2.5, "presence": +1.5}),
        # ── 高域・空気感 ────────────────────────
        ("高域・空気感", None,                         None,    None),
        ("高音を強く",                   "高音を強く",                     "air",   {"air": +3.0, "air_high": +2.0, "air_low": +1.0}),
        ("空気感を増やす",               "空気感をもっと",                 "air",   {"air": +2.0, "air_high": +2.5, "air_low": +1.0}),
        ("透明感を高く",                 "透明感をもっと",                 "air",   {"air": +1.8, "air_low": +0.8, "high_mid": +0.4}),
        ("高域の刺さりを緩和",           "高音が刺さる",                   "air",   {"air": -2.0, "air_high": -1.5, "presence": -1.0}),
        ("艶を加える",                   "艶をもっと",                     "air",   {"presence": +1.5, "upper_mid": +1.0, "air_low": +0.5}),
        # ── 全体バランス・キャラクター ──────────
        ("全体バランス・キャラクター", None,           None,    None),
        ("もっとクールに",               "クールにして",                   "dyn",   {"mid": -1.0, "bass": -1.0, "air": +1.0}),
        ("ダイナミクスを強調",           "もっと迫力を",                   "dyn",   {"sub_bass": +1.5, "presence": +1.5}),
        ("聴き疲れを軽減",               "聴き疲れる",                     "dyn",   {"presence": -2.0, "air": -1.0, "air_high": -1.0}),
        ("もっと自然な音に",             "もっと自然な音に",               "dyn",   {}),
        ("うるさい感じを緩和",           "うるさい",                       "dyn",   {"presence": -2.0, "upper_mid": -1.0, "air": -0.5}),
    ]

    # ── 表示 ─────────────────────────────────────────────────────
    print("\n")
    print("┌─────────────────────────────────────────────────────┐")
    print("│  🎛️  Sonia Intelligence — Acoustic preset selection  │")
    print("├─────────────────────────────────────────────────────┤")

    num = 0
    num_map = {}   # 番号 → PRESETSインデックス
    for i, (label, text, cat, *_) in enumerate(PRESETS):
        if cat is None:
            # カテゴリヘッダー
            print(f"│  ── {label:<45} │")
        else:
            num += 1
            num_map[num] = i
            print(f"│  {num:2d}. {label:<43} │")

    print("├─────────────────────────────────────────────────────┤")
    print("│  Multi-select: enter numbers separated by spaces     │")
    print("│  [Enter only] Cancel                                 │")
    print("└─────────────────────────────────────────────────────┘")

    choice = _si_readline("  選択 → ")
    if not choice:
        print("  Cancelled")
        return

    # 番号をパース（複数可）
    selected_nums = []
    for token in choice.replace("、", " ").replace(",", " ").split():
        if token.isdigit():
            n = int(token)
            if n in num_map:
                selected_nums.append(n)

    if not selected_nums:
        print("  No valid number entered")
        return

    # 選択されたプリセットを順番に適用
    print()
    for n in selected_nums:
        idx = num_map[n]
        label, feedback_text, _, eq_delta = PRESETS[idx]

        if eq_delta == "RESET":
            # ── EQ全解除 ──
            if _si_instance.current_params:
                _si_instance.current_params.eq = {}
                print(f"  ✓ {label}")
                print(f"    Cleared all SI EQ corrections")
            else:
                print(f"  ⚠️ {label}: current_params not set")
        elif eq_delta:
            # ── EQデルタ定義あり → NLP解釈を完全バイパスして直接適用 ──
            if _si_instance.current_params:
                p = _si_instance.current_params
                if not hasattr(p, 'eq') or p.eq is None:
                    p.eq = {}
                for band, delta in eq_delta.items():
                    p.eq[band] = round(p.eq.get(band, 0.0) + delta, 2)
                print(f"  ✓ {label}")
                changed = [(b, v) for b, v in p.eq.items() if abs(v) >= 0.5]
                if changed:
                    bands_str = "  ".join(f"{b}:{v:+.1f}dB" for b, v in changed[:5])
                    print(f"    Accumulated EQ: {bands_str}")
            else:
                print(f"  ⚠️ {label}: current_params not set")
        else:
            # ── eq_delta 空 → on_feedback 経由（残響・空間系） ──
            try:
                _, explanation = _si_instance.on_feedback(feedback_text)
                print(f"  ✓ {label}")
                for line in explanation.split("\n")[:3]:
                    if line.strip() and "解釈:" not in line:
                        print(f"    {line.strip()}")
            except Exception as e:
                print(f"  ⚠️ {label}: {e}")

    _si_readline("\n  [Press Enter to resume] ")


# ===== ★★★ Sonia Intelligence 補助関数ここまで ★★★ =====

# ===========================================================================
# ★★★ 音響プリセット定義（sonia_filter_presets 埋め込み版） ★★★
# ===========================================================================

def _preset_musikverein(gain_db, current_volume, eq_part, musikverein_room_effects,
                        air_particle_layer, echo_mode, tinnitus_reduction_mode, loudness_filter):
    """楽友協会ホール音場。オーケストラ全般・管弦楽・交響曲を対象とした標準プリセット。"""
    parts = []
    parts.append(f'volume={gain_db}dB')
    
    # ─── 倍音生成（エキサイター）の追加 ───
    # level_in/out=1 (原音維持), amount=3 (倍音量), drive=6 (倍音の歪み/深さ), blend=5 (高域ブレンド)
    # これにより、ホール残響に送る前の原音に「脳を錯覚させる倍音の種」を蒔きます。
    #parts.append('aexciter=level_in=1:level_out=1:amount=3:drive=6:blend=5')
    # driveを6→3へ下げ、blendをマイナスにしてチリチリ感を排除
    #parts.append('aexciter=level_in=1:level_out=1:amount=2:drive=2:blend=-5')
    # 4500Hz以上をターゲットにして、中高域のチリチリ感を物理的に避ける
    #parts.append('aexciter=level_in=1:level_out=1:amount=2:drive=3:blend=-2:freq=4500')
    # freqを6500に引き上げ、blendを-5まで下げることで、ザラツキの「種」を物理的に耳の可聴域から遠ざけます
    parts.append('aexciter=level_in=1:level_out=1:amount=1.2:drive=1.5:blend=-5:freq=6500')

    
    parts += [
        'equalizer=f=40:t=q:w=0.75:g=2.2',
        'equalizer=f=60:t=q:w=0.8:g=3.0',
        'equalizer=f=80:t=q:w=0.85:g=2.6',
        'equalizer=f=2500:t=q:w=2.0:g=-0.8',  # ヴァイオリンのザラつく芯を消す
        'equalizer=f=3800:t=q:w=2.0:g=-0.6',  # ピアノのアタックの硬さ・濁りを取る
        'equalizer=f=6200:t=q:w=3.0:g=-1.0',  # エキサイターのチリチリする開始点を叩く
        'equalizer=f=7400:t=q:w=3.0:g=-1.2',  # 耳障りな高域の擦れ音をピンポイントで消す
        'equalizer=f=110:t=q:w=0.9:g=-0.6',
        'equalizer=f=140:t=q:w=0.95:g=-0.6',
        'equalizer=f=95:t=q:w=0.85:g=-0.6',
        'equalizer=f=125:t=q:w=0.9:g=-0.5',
        'equalizer=f=60:t=q:w=0.8:g=0.2',
        'equalizer=f=40:t=q:w=0.75:g=0.2',
        'equalizer=f=250:t=q:w=1.0:g=-0.5',
        'equalizer=f=400:t=q:w=1.0:g=-1.4',
        'equalizer=f=600:t=q:w=1.0:g=-2.0',
        'equalizer=f=1000:t=q:w=1.0:g=2.0',
        'equalizer=f=2200:t=q:w=1.4:g=1.0',
        'equalizer=f=3000:t=q:w=1.2:g=-0.4',
        'equalizer=f=5000:t=q:w=1.5:g=0.8',
        'equalizer=f=8000:t=q:w=1.5:g=0.0',
        'equalizer=f=6500:t=q:w=1.3:g=-0.5',
    ]
    if musikverein_room_effects:
        if echo_mode == 'jazz_vocal':
            parts.append('aecho=0.88:0.92:17:0.13')
            parts.append('aecho=0.78:0.85:31:0.11')
        else:
            parts.append('aecho=0.88:0.92:17:0.22')
            parts.append('aecho=0.78:0.85:31:0.18')
        parts.append('acompressor=threshold=-27dB:ratio=1.15:attack=40:release=120:makeup=1.15')
        if echo_mode == 'jazz_vocal':
            parts.append('aecho=0.6:0.7:29:0.08')
        else:
            parts.append('aecho=0.6:0.7:29:0.13')
        parts.append('bass=g=2.8:f=45:w=0.6')
        parts.append('bass=g=2.0:f=80:w=0.6')
        if echo_mode == 'jazz_vocal':
            parts.append('aecho=0.55:0.65:65:0.05')
        else:
            parts.append('aecho=0.55:0.65:65:0.09')
        if tinnitus_reduction_mode:
            parts.append('highshelf=f=10000:g=-1.5:w=0.5')
        parts.append('treble=g=1.0')
        if not tinnitus_reduction_mode:
            parts.append('highshelf=f=7000:g=1.4:w=0.7')
    parts.append('alimiter=level_in=1.0:level_out=1.0:limit=0.98:attack=2:release=50')
    if musikverein_room_effects:
        parts.append('equalizer=f=2800:t=q:w=1.3:g=0.6')
        parts.append('equalizer=f=1800:t=q:w=1.2:g=0.25')
    if loudness_filter:
        parts.append(loudness_filter.rstrip(','))
    # ★ loudnorm使用時は既に-2.0dBTPまで詰まっているため、この段の出力ゲインは
    #   0dB超（＝さらなるブースト）を許可しない。減衰(マイナス側)のみ有効。
    _post_gain = current_volume if not loudness_filter else min(current_volume, 0)
    parts.append(
        f'{eq_part}volume={_post_gain}dB,'
        'alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=50'
    )
    main_chain = ','.join(parts)
    if musikverein_room_effects and air_particle_layer:
        _se = ('[str]aecho=0.65:0.75:11:0.019[side];' if echo_mode == 'jazz_vocal'
               else '[str]aecho=0.65:0.75:11:0.031[side];')
        _ce = ('[side]aecho=0.55:0.65:6:0.012[diff];' if echo_mode == 'jazz_vocal'
               else '[side]aecho=0.55:0.65:6:0.02[diff];')
        fc = (
            f'[0:a]{main_chain}[main];'
            + '[main]equalizer=f=250:t=q:w=1.3:g=0.4[str];'
            + _se + _ce
            + '[diff]volume=14dB[diffg];'
            + 'anoisesrc=color=pink:amplitude=0.00070:r=48000:d=86400[air];'
            + '[air]highpass=f=130,lowpass=f=10500[airband];'
            + '[airband]volume=0.035,apulsator=hz=0.14:amount=0.12:mode=sine[airbed];'
            + 'anoisesrc=color=brown:amplitude=0.00016:r=48000:d=86400[aud];'
            + '[aud]highpass=f=180,volume=0.018[audience];'
            + "[diffg][airbed][audience]amix=inputs=3:weights='1 0.018 0.007':normalize=0:duration=first[premix];"
            + '[premix]alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=50[out]'
        )
        return ['-filter_complex', fc, '-map', '[out]']
    else:
        return ['-af', main_chain]


def _preset_piano(gain_db, current_volume, eq_part, musikverein_room_effects,
                  air_particle_layer, echo_mode, tinnitus_reduction_mode, loudness_filter):
    """ピアノソロ専用プリセット。打鍵トランジェントとダイナミクスを最大限に再現。"""
    parts = []
    parts.append(f'volume={gain_db}dB')
    
    # freqを6500に引き上げ、blendを-5まで下げることで、ザラツキの「種」を物理的に耳の可聴域から遠ざけます
    parts.append('aexciter=level_in=1:level_out=1:amount=1.2:drive=1.5:blend=-5:freq=6500')
    
    parts += [
        'equalizer=f=40:t=q:w=0.75:g=2.2',
        'equalizer=f=60:t=q:w=0.8:g=3.0',
        'equalizer=f=2500:t=q:w=2.0:g=-0.8',  # ヴァイオリンのザラつく芯を消す
        'equalizer=f=3800:t=q:w=2.0:g=-0.6',  # ピアノのアタックの硬さ・濁りを取る
        'equalizer=f=6200:t=q:w=3.0:g=-1.0',  # エキサイターのチリチリする開始点を叩く
        'equalizer=f=7400:t=q:w=3.0:g=-1.2',  # 耳障りな高域の擦れ音をピンポイントで消す
        'equalizer=f=80:t=q:w=0.85:g=2.6',
        'equalizer=f=110:t=q:w=0.9:g=-0.6',
        'equalizer=f=140:t=q:w=0.95:g=-0.6',
        'equalizer=f=95:t=q:w=0.85:g=-0.6',
        'equalizer=f=125:t=q:w=0.9:g=-0.5',
        'equalizer=f=60:t=q:w=0.8:g=0.2',
        'equalizer=f=40:t=q:w=0.75:g=0.2',
        'equalizer=f=250:t=q:w=1.0:g=-0.5',
        'equalizer=f=220:t=q:w=1.1:g=0.5',
        'equalizer=f=320:t=q:w=1.1:g=-0.3',
        'equalizer=f=400:t=q:w=1.0:g=-1.4',
        'equalizer=f=600:t=q:w=1.0:g=-2.0',
        'equalizer=f=700:t=q:w=1.1:g=0.5',
        'equalizer=f=1000:t=q:w=1.0:g=2.0',
        'equalizer=f=1800:t=q:w=1.1:g=0.6',
        'equalizer=f=2200:t=q:w=1.4:g=1.0',
        'equalizer=f=3000:t=q:w=1.1:g=-0.2',
        'equalizer=f=5000:t=q:w=1.5:g=0.8',
        'equalizer=f=8000:t=q:w=1.5:g=0.0',
        'equalizer=f=6500:t=q:w=1.3:g=-0.5',
    ]
    if musikverein_room_effects:
        if echo_mode == 'jazz_vocal':
            parts.append('aecho=0.88:0.92:17:0.13')
            parts.append('aecho=0.78:0.85:31:0.11')
        else:
            parts.append('aecho=0.88:0.92:17:0.22')
            parts.append('aecho=0.78:0.85:31:0.18')
        parts.append('acompressor=threshold=-27dB:ratio=1.14:attack=38:release=110')
        if echo_mode == 'jazz_vocal':
            parts.append('aecho=0.6:0.7:29:0.08')
        else:
            parts.append('aecho=0.6:0.7:29:0.13')
        parts.append('bass=g=2.8:f=45:w=0.6')
        parts.append('bass=g=2.0:f=80:w=0.6')
        if echo_mode == 'jazz_vocal':
            parts.append('aecho=0.55:0.65:65:0.05')
        else:
            parts.append('aecho=0.55:0.65:68:0.11')
        if tinnitus_reduction_mode:
            parts.append('highshelf=f=10000:g=-1.5:w=0.5')
        parts.append('treble=g=1.0')
        if not tinnitus_reduction_mode:
            parts.append('highshelf=f=7000:g=1.4:w=0.7')
    parts.append('alimiter=level_in=1.0:level_out=1.0:limit=0.98:attack=2:release=65')
    if musikverein_room_effects:
        parts.append('equalizer=f=2800:t=q:w=1.3:g=0.6')
        parts.append('equalizer=f=1800:t=q:w=1.2:g=0.25')
    if loudness_filter:
        parts.append(loudness_filter.rstrip(','))
    # ★ loudnorm使用時は既に-2.0dBTPまで詰まっているため、この段の出力ゲインは
    #   0dB超（＝さらなるブースト）を許可しない。減衰(マイナス側)のみ有効。
    _post_gain = current_volume if not loudness_filter else min(current_volume, 0)
    parts.append(
        f'{eq_part}volume={_post_gain}dB,'
        'alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=65'
    )
    main_chain = ','.join(parts)
    if musikverein_room_effects and air_particle_layer:
        _se = ('[str]aecho=0.65:0.75:11:0.019[side];' if echo_mode == 'jazz_vocal'
               else '[str]aecho=0.65:0.75:11:0.031[side];')
        _ce = ('[side]aecho=0.55:0.65:6:0.012[diff];' if echo_mode == 'jazz_vocal'
               else '[side]aecho=0.55:0.65:6:0.02[diff];')
        fc = (
            f'[0:a]{main_chain}[main];'
            + '[main]equalizer=f=250:t=q:w=1.3:g=0.4[str];'
            + _se + _ce
            + '[diff]volume=16dB[diffg];'
            + 'anoisesrc=color=pink:amplitude=0.00070:r=48000:d=86400[air];'
            + '[air]highpass=f=130,lowpass=f=10500[airband];'
            + '[airband]volume=0.042,apulsator=hz=0.14:amount=0.12:mode=sine[airbed];'
            + 'anoisesrc=color=brown:amplitude=0.00016:r=48000:d=86400[aud];'
            + '[aud]highpass=f=180,volume=0.018[audience];'
            + "[diffg][airbed][audience]amix=inputs=3:weights='1 0.018 0.007':normalize=0:duration=first[premix];"
            + '[premix]alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=65[out]'
        )
        return ['-filter_complex', fc, '-map', '[out]']
    else:
        return ['-af', main_chain]


def _preset_chamber(gain_db, current_volume, eq_part, musikverein_room_effects,
                    air_particle_layer, echo_mode, tinnitus_reduction_mode, loudness_filter):
    """室内楽プリセット。弦楽四重奏・ピアノトリオ等。ピアノフォルテのクリップを二段コンプで防止。"""
    parts = []
    parts.append(f'volume={gain_db}dB')
    
    # freqを6500に引き上げ、blendを-5まで下げることで、ザラツキの「種」を物理的に耳の可聴域から遠ざけます
    parts.append('aexciter=level_in=1:level_out=1:amount=1.2:drive=1.5:blend=-5:freq=6500')
    
    parts += [
        'equalizer=f=40:t=q:w=0.75:g=1.0',
        'equalizer=f=60:t=q:w=0.8:g=1.2',
        'equalizer=f=2500:t=q:w=2.0:g=-0.8',  # ヴァイオリンのザラつく芯を消す
        'equalizer=f=3800:t=q:w=2.0:g=-0.6',  # ピアノのアタックの硬さ・濁りを取る
        'equalizer=f=6200:t=q:w=3.0:g=-1.0',  # エキサイターのチリチリする開始点を叩く
        'equalizer=f=7400:t=q:w=3.0:g=-1.2',  # 耳障りな高域の擦れ音をピンポイントで消す
        'equalizer=f=80:t=q:w=0.85:g=1.0',
        'equalizer=f=90:t=q:w=0.8:g=0.4',
        'equalizer=f=100:t=q:w=0.9:g=-0.4',
        'equalizer=f=220:t=q:w=1.1:g=0.35',
        'equalizer=f=250:t=q:w=1.0:g=-0.3',
        'equalizer=f=400:t=q:w=1.0:g=-1.8',
        'equalizer=f=600:t=q:w=1.0:g=-1.9',
        'equalizer=f=1000:t=q:w=1.0:g=2.0',
        'equalizer=f=950:t=q:w=0.9:g=0.3',
        'equalizer=f=1300:t=q:w=0.85:g=1.6',
        'equalizer=f=1800:t=q:w=1.1:g=0.6',
        'equalizer=f=2200:t=q:w=1.2:g=1.6',
        'equalizer=f=3000:t=q:w=1.2:g=0.15',
        'equalizer=f=4200:t=q:w=1.3:g=0.25',
        'equalizer=f=5000:t=q:w=1.4:g=0.8',
        'equalizer=f=8000:t=q:w=1.5:g=0.2',
        'equalizer=f=6500:t=q:w=1.3:g=-0.5',
    ]
    if musikverein_room_effects:
        parts.append('aecho=0.85:0.88:5:0.04')
        parts.append('aecho=0.90:0.92:3:0.035')
        parts.append('aecho=0.86:0.92:7:0.18')
        parts.append('aecho=0.75:0.82:17:0.08')
        parts.append('aecho=0.60:0.70:58:0.07')
        parts.append('acompressor=threshold=-20dB:ratio=1.45:attack=3:release=60:makeup=1.06')
        parts.append('acompressor=threshold=-30dB:ratio=1.32:attack=10:release=120:makeup=1.10')
        parts.append('treble=g=1.1')
        if tinnitus_reduction_mode:
            parts.append('highshelf=f=10000:g=-1.2:w=0.5')
        else:
            parts.append('highshelf=f=7000:g=0.9:w=0.7')
    parts.append('acompressor=threshold=-18dB:ratio=1.08:attack=1:release=25:makeup=1.0')
    parts.append('alimiter=level_in=1.0:level_out=1.0:limit=0.96:attack=1:release=50')
    if loudness_filter:
        parts.append(loudness_filter.rstrip(','))
    # ★ loudnorm使用時は既に-2.0dBTPまで詰まっているため、この段の出力ゲインは
    #   0dB超（＝さらなるブースト）を許可しない。減衰(マイナス側)のみ有効。
    _post_gain = current_volume if not loudness_filter else min(current_volume, 0)
    parts.append(
        f'{eq_part}volume={_post_gain}dB,'
        'alimiter=level_in=1.0:level_out=1.0:limit=0.92:attack=1:release=50'
    )
    main_chain = ','.join(parts)
    if musikverein_room_effects and air_particle_layer:
        _se = ('[str]aecho=0.65:0.75:11:0.019[side];' if echo_mode == 'jazz_vocal'
               else '[str]aecho=0.65:0.75:11:0.031[side];')
        _ce = ('[side]aecho=0.55:0.65:6:0.012[diff];' if echo_mode == 'jazz_vocal'
               else '[side]aecho=0.55:0.65:6:0.02[diff];')
        fc = (
            f'[0:a]{main_chain}[main];'
            + '[main]equalizer=f=250:t=q:w=1.3:g=0.4[str];'
            + _se + _ce
            + '[diff]volume=16dB[diffg];'
            + 'anoisesrc=color=pink:amplitude=0.00040:r=48000:d=86400[air];'
            + '[air]highpass=f=180,lowpass=f=8000[airband];'
            + '[airband]volume=0.020,apulsator=hz=0.12:amount=0.08[airbed];'
            + 'anoisesrc=color=brown:amplitude=0.00010:r=48000:d=86400[aud];'
            + '[aud]highpass=f=220,volume=0.010[audience];'
            + "[diffg][airbed][audience]amix=inputs=3:weights='1 0.012 0.005':normalize=0:duration=first[premix];"
            + '[premix]alimiter=level_in=1.0:level_out=1.0:limit=0.92:attack=1:release=50[out]'
        )
        return ['-filter_complex', fc, '-map', '[out]']
    else:
        return ['-af', main_chain]


def _preset_vocal(gain_db, current_volume, eq_part, musikverein_room_effects,
                  air_particle_layer, echo_mode, tinnitus_reduction_mode, loudness_filter):
    """声楽プリセット。オペラ・リート・ポップスボーカル。声帯マイクロモジュレーション付き。"""
    parts = []
    parts.append(f'volume={gain_db}dB')
    
    # freqを6500に引き上げ、blendを-5まで下げることで、ザラツキの「種」を物理的に耳の可聴域から遠ざけます
    parts.append('aexciter=level_in=1:level_out=1:amount=1.2:drive=1.5:blend=-5:freq=6500')
    
    parts += [
        'equalizer=f=40:t=q:w=0.75:g=2.2',
        'equalizer=f=60:t=q:w=0.8:g=3.0',
        'equalizer=f=2500:t=q:w=2.0:g=-0.8',  # ヴァイオリンのザラつく芯を消す
        'equalizer=f=3800:t=q:w=2.0:g=-0.6',  # ピアノのアタックの硬さ・濁りを取る
        'equalizer=f=6200:t=q:w=3.0:g=-1.0',  # エキサイターのチリチリする開始点を叩く
        'equalizer=f=7400:t=q:w=3.0:g=-1.2',  # 耳障りな高域の擦れ音をピンポイントで消す
        'equalizer=f=80:t=q:w=0.85:g=2.6',
        'equalizer=f=110:t=q:w=0.9:g=-0.6',
        'equalizer=f=140:t=q:w=0.95:g=-0.6',
        'equalizer=f=95:t=q:w=0.85:g=-0.6',
        'equalizer=f=125:t=q:w=0.9:g=-0.5',
        'equalizer=f=60:t=q:w=0.8:g=0.2',
        'equalizer=f=40:t=q:w=0.75:g=0.2',
        'equalizer=f=250:t=q:w=1.0:g=-0.5',
        'equalizer=f=350:t=q:w=0.9:g=0.6',
        'equalizer=f=400:t=q:w=1.0:g=-0.5',
        'equalizer=f=500:t=q:w=0.9:g=1.0',
        'equalizer=f=600:t=q:w=1.0:g=-0.8',
        'equalizer=f=900:t=q:w=0.9:g=2.0',
        'equalizer=f=1000:t=q:w=1.0:g=2.0',
        'equalizer=f=1500:t=q:w=1.0:g=1.8',
        'equalizer=f=2000:t=q:w=0.9:g=1.2',
        'equalizer=f=2200:t=q:w=1.0:g=0.8',
        'equalizer=f=2500:t=q:w=1.2:g=2.1',
        'equalizer=f=2800:t=q:w=1.2:g=1.6',
        'equalizer=f=3000:t=q:w=1.2:g=-0.4',
        'equalizer=f=4000:t=q:w=1.3:g=1.2',
        'equalizer=f=5000:t=q:w=1.5:g=0.8',
        'equalizer=f=6500:t=q:w=1.3:g=-0.3',
        'equalizer=f=6500:t=q:w=1.3:g=-0.5',
        'equalizer=f=8000:t=q:w=1.5:g=0.6',
    ]
    parts.append('apulsator=hz=6.2:amount=0.040:mode=sine')
    parts.append('apulsator=hz=0.7:amount=0.018:mode=sine')
    parts.append('aecho=0.92:0.96:22:0.035')
    parts.append('treble=g=1.4')
    parts.append('equalizer=f=7000:t=q:w=1.4:g=-0.3')
    if musikverein_room_effects:
        if echo_mode == 'jazz_vocal':
            parts.append('aecho=0.88:0.92:17:0.13')
            parts.append('aecho=0.78:0.85:31:0.11')
        else:
            parts.append('aecho=0.7:0.8:9:0.08')
            parts.append('aecho=0.6:0.7:18:0.05')
        parts.append('acompressor=threshold=-26dB:ratio=1.08:attack=8:release=70:makeup=1.04')
        if echo_mode == 'jazz_vocal':
            parts.append('aecho=0.55:0.65:32:0.04')
        else:
            parts.append('aecho=0.6:0.7:29:0.13')
        parts.append('bass=g=2.8:f=45:w=0.6')
        parts.append('bass=g=2.0:f=80:w=0.6')
        if echo_mode == 'jazz_vocal':
            parts.append('aecho=0.55:0.65:65:0.05')
        else:
            parts.append('aecho=0.55:0.65:48:0.04')
        if tinnitus_reduction_mode:
            parts.append('highshelf=f=10000:g=-1.5:w=0.5')
        parts.append('treble=g=0.6')
        if not tinnitus_reduction_mode:
            parts.append('highshelf=f=7500:g=0.8:w=0.7')
    parts.append('alimiter=level_in=1.0:level_out=1.0:limit=0.98:attack=2:release=50')
    if musikverein_room_effects:
        parts.append('equalizer=f=3200:t=q:w=1.0:g=2.0')
        parts.append('equalizer=f=1800:t=q:w=1.0:g=1.2')
    if loudness_filter:
        parts.append(loudness_filter.rstrip(','))
    # ★ loudnorm使用時は既に-2.0dBTPまで詰まっているため、この段の出力ゲインは
    #   0dB超（＝さらなるブースト）を許可しない。減衰(マイナス側)のみ有効。
    _post_gain = current_volume if not loudness_filter else min(current_volume, 0)
    parts.append(
        f'{eq_part}volume={_post_gain}dB,'
        'alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=50'
    )
    main_chain = ','.join(parts)
    if musikverein_room_effects and air_particle_layer:
        _se = ('[str]aecho=0.65:0.75:11:0.019[side];' if echo_mode == 'jazz_vocal'
               else '[str]aecho=0.65:0.75:11:0.031[side];')
        _ce = ('[side]aecho=0.55:0.65:6:0.012[diff];' if echo_mode == 'jazz_vocal'
               else '[side]aecho=0.55:0.65:6:0.02[diff];')
        fc = (
            f'[0:a]{main_chain}[main];'
            + '[main]equalizer=f=250:t=q:w=1.3:g=0.4[str];'
            + _se + _ce
            + '[diff]volume=14dB[diffg];'
            + 'anoisesrc=color=pink:amplitude=0.00070:r=48000:d=86400[air];'
            + '[air]highpass=f=130,lowpass=f=10500[airband];'
            + '[airband]volume=0.014,apulsator=hz=0.11:amount=0.07:mode=sine[airbed];'
            + 'anoisesrc=color=brown:amplitude=0.00016:r=48000:d=86400[aud];'
            + '[aud]highpass=f=180,volume=0.014[audience];'
            + "[diffg][airbed][audience]amix=inputs=3:weights='1 0.018 0.007':normalize=0:duration=first[premix];"
            + '[premix]alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=50[out]'
        )
        return ['-filter_complex', fc, '-map', '[out]']
    else:
        return ['-af', main_chain]


def _preset_jazz(gain_db, current_volume, eq_part, musikverein_room_effects,
                 air_particle_layer, echo_mode, tinnitus_reduction_mode, loudness_filter):
    """ジャズプリセット。ウッドベースの芯（140/180Hz）とシンバル解像度（8kHz+）を両立。
    ★ 歪み対策：元のEQ/コンプ/makeup を完全復元。
       デジタルクリップのみ asoftclip=type=tanh で阻止。
       tanh 関数は真空管飽和特性に近く、音楽的倍音を保ちながら角を丸める。
    """
    parts = []
    # ★ EQ補償オフセット：ジャズEQの累積ブースト（低域+2.2dB、中域+2.2dB、makeup+1.3dB相当）
    #    を先に引いておくことで asoftclip への入力を適正レベルに保つ。
    #    音楽的な豊かさ・ダイナミクスはそのまま。外部ゲインプリセットには影響しない。
    _jazz_eq_offset = -0.7  # -1.8 → -0.7：音量感を戻しつつ歪みを抑制  # EQ累積ブーストの実測ピーク補正値
    #_jazz_eq_offset = -1.8  # -1.8 → -0.7：音量感を戻しつつ歪みを抑制  # EQ累積ブーストの実測ピーク補正値
    parts.append(f'volume={gain_db + _jazz_eq_offset}dB')
    
    # freqを6500に引き上げ、blendを-5まで下げることで、ザラツキの「種」を物理的に耳の可聴域から遠ざけます
    parts.append('aexciter=level_in=1:level_out=1:amount=1.2:drive=1.5:blend=-5:freq=6500')
    
    #parts += [
        #'equalizer=f=30:t=q:w=0.7:g=2.0',
        #'equalizer=f=40:t=q:w=0.75:g=2.2',
        #'equalizer=f=2500:t=q:w=2.0:g=-0.8',  # ヴァイオリンのザラつく芯を消す
        #'equalizer=f=3800:t=q:w=2.0:g=-0.6',  # ピアノのアタックの硬さ・濁りを取る
        #'equalizer=f=6200:t=q:w=3.0:g=-1.0',  # エキサイターのチリチリする開始点を叩く
        #'equalizer=f=7400:t=q:w=3.0:g=-1.2',  # 耳障りな高域の擦れ音をピンポイントで消す
        #'equalizer=f=60:t=q:w=0.8:g=0.8',
        #'equalizer=f=80:t=q:w=0.85:g=0.2',
        #'equalizer=f=90:t=q:w=0.8:g=0.0',
        #'equalizer=f=100:t=q:w=0.9:g=-1.2',
        #'equalizer=f=100:t=q:w=0.9:g=-0.8',
        #'equalizer=f=140:t=q:w=1.0:g=0.6',
        #'equalizer=f=180:t=q:w=1.0:g=0.4',
        #'equalizer=f=220:t=q:w=1.1:g=0.5',
        #'equalizer=f=250:t=q:w=1.0:g=-0.5',
        #'equalizer=f=400:t=q:w=1.0:g=-1.8',
        #'equalizer=f=600:t=q:w=1.0:g=-1.9',
        #'equalizer=f=950:t=q:w=0.9:g=0.3',
        #'equalizer=f=1000:t=q:w=1.0:g=2.2',
        #'equalizer=f=1300:t=q:w=0.85:g=1.6',
        #'equalizer=f=1800:t=q:w=1.1:g=0.6',
        #'equalizer=f=2200:t=q:w=1.2:g=1.6',
        #'equalizer=f=3000:t=q:w=1.2:g=-0.5',
        #'equalizer=f=4200:t=q:w=1.3:g=0.25',
        #'equalizer=f=5000:t=q:w=1.4:g=0.8',
        #'equalizer=f=8000:t=q:w=0.9:g=0.6',
        #'equalizer=f=6500:t=q:w=1.3:g=-0.5',
    #]
    parts += [
        'equalizer=f=30:t=q:w=0.7:g=0.8',     # 0.5 -> 0.8 超低域の空気感をわずかに戻す
        'equalizer=f=40:t=q:w=0.75:g=1.2',    # 1.0 -> 1.5 重低音のズッシリ感を強化
        'equalizer=f=2500:t=q:w=2.0:g=-0.8',  # ヴァイオリンのザラつく芯を消す
        'equalizer=f=3800:t=q:w=2.0:g=-0.6',  # ピアノのアタックの硬さ・濁りを取る
        'equalizer=f=6200:t=q:w=3.0:g=-1.0',  # エキサイターのチリチリする開始点を叩く
        'equalizer=f=7400:t=q:w=3.0:g=-1.2',  # 耳障りな高域の擦れ音をピンポイントで消す
        'equalizer=f=60:t=q:w=0.8:g=0.8',     # 1.2 -> 2.5 キックの重量感と押し出しを大幅強化
        'equalizer=f=80:t=q:w=0.85:g=2.2',    # 0.5 -> 2.2 ベースの硬い芯とアタック感を強調
        'equalizer=f=90:t=q:w=0.8:g=1.8',     # 0.0 -> 1.5 押し出し感の輪郭をさらにハッキリと
        'equalizer=f=100:t=q:w=0.9:g=-0.8',   # -1.5 -> -0.6 パワーが痩せすぎないようカットを緩和
        'equalizer=f=100:t=q:w=0.9:g=-0.4',   # -1.0 -> -0.4 （重複箇所の調整）
        'equalizer=f=140:t=q:w=1.0:g=-0.5',   # -0.4 -> -0.5 モヤつきの原因なのでしっかりカット
        'equalizer=f=180:t=q:w=1.0:g=-0.4',   # -0.2 -> -0.4 濁りを抑えて引き締まりをキープ
        'equalizer=f=220:t=q:w=1.1:g=0.2',
        'equalizer=f=250:t=q:w=1.0:g=-0.5',
        'equalizer=f=400:t=q:w=1.0:g=-1.8',
        'equalizer=f=600:t=q:w=1.0:g=-1.9',
        'equalizer=f=950:t=q:w=0.9:g=0.3',
        'equalizer=f=1000:t=q:w=1.0:g=2.2',
        'equalizer=f=1300:t=q:w=0.85:g=1.6',
        'equalizer=f=1800:t=q:w=1.1:g=0.6',
        'equalizer=f=2200:t=q:w=1.2:g=1.6',
        'equalizer=f=3000:t=q:w=1.2:g=-0.5',
        'equalizer=f=4200:t=q:w=1.3:g=0.25',
        'equalizer=f=5000:t=q:w=1.4:g=0.8',
        'equalizer=f=8000:t=q:w=0.9:g=0.6',
        'equalizer=f=6500:t=q:w=1.3:g=-0.5',
    ]
    if musikverein_room_effects:
        parts.append('aecho=0.85:0.88:5:0.04')
        parts.append('aecho=0.90:0.92:3:0.035')
        parts.append('aecho=0.86:0.92:7:0.18')
        parts.append('aecho=0.75:0.82:17:0.08')
        parts.append('bass=g=0.8:f=85:w=0.5')
        parts.append('aecho=0.60:0.70:58:0.07')
        parts.append('acompressor=threshold=-20dB:ratio=1.45:attack=3:release=60:makeup=1.06')
        parts.append('acompressor=threshold=-30dB:ratio=1.32:attack=10:release=120:makeup=1.10')
        parts.append('acompressor=threshold=-24dB:ratio=1.2:attack=8:release=120')
        parts.append('treble=g=1.1')
        if tinnitus_reduction_mode:
            parts.append('highshelf=f=10000:g=-1.2:w=0.5')
        else:
            parts.append('highshelf=f=7000:g=0.9:w=0.7')
    parts.append('acompressor=threshold=-18dB:ratio=1.08:attack=1:release=25:makeup=1.0')
    # ★ ソフトクリッパー：tanh特性で真空管的に角を丸める。
    #    デジタルの硬いクリップだけを防ぎ、音楽的な倍音・ダイナミクスは一切手を触れない。
    #    threshold=0.95：-0.45dBFS を超えた瞬間だけ作動。ピアニッシモには完全無干渉。
    parts.append('asoftclip=type=tanh:threshold=0.95:output=0.95')
    if loudness_filter:
        parts.append(loudness_filter.rstrip(','))
    # ★ loudnorm使用時は既に-2.0dBTPまで詰まっているため、この段の出力ゲインは
    #   0dB超（＝さらなるブースト）を許可しない。減衰(マイナス側)のみ有効。
    _post_gain = current_volume if not loudness_filter else min(current_volume, 0)
    parts.append(
        f'{eq_part}volume={_post_gain}dB,'
        'asoftclip=type=tanh:threshold=0.95:output=0.95'
    )
    main_chain = ','.join(parts)
    if musikverein_room_effects and air_particle_layer:
        _se = ('[str]aecho=0.65:0.75:11:0.019[side];' if echo_mode == 'jazz_vocal'
               else '[str]aecho=0.65:0.75:11:0.031[side];')
        _ce = ('[side]aecho=0.55:0.65:6:0.012[diff];' if echo_mode == 'jazz_vocal'
               else '[side]aecho=0.55:0.65:6:0.02[diff];')
        fc = (
            f'[0:a]{main_chain}[main];'
            + 'anoisesrc=color=white:amplitude=0.00005:r=48000:d=86400[n1];'
            + '[n1]highpass=f=1500,lowpass=f=9000[n1b];'
            + '[n1b]apulsator=hz=0.27:amount=0.15[n1c];'
            + '[main]asplit=2[dry][tap];'
            + '[tap]adelay=1|2[tapd];'
            + "[dry][tapd]amix=inputs=2:weights='1 0.06'[body];"
            + "[body][n1c]amix=inputs=2:weights='1 0.018':normalize=0:duration=first[pres];"
            + '[pres]asoftclip=type=tanh:threshold=0.95:output=0.95[out]'
        )
        return ['-filter_complex', fc, '-map', '[out]']
    else:
        return ['-af', main_chain]


def _preset_radio(gain_db, current_volume, eq_part, musikverein_room_effects,
                  air_particle_layer, echo_mode, tinnitus_reduction_mode, loudness_filter):
    """ラジオ向け標準プリセット。旧バージョン (qji-q.py) の軽量フィルターチェーンに準拠。
    Air Particle Layer は使用せず、常にシンプルな -af チェーンで動作する。
    コンプレッサーは ratio=1.28 / release=360ms（旧版設定）、treble=0.4（穏やか）。
    """
    parts = []
    parts.append(f'volume={gain_db}dB')
    
    # freqを6500に引き上げ、blendを-5まで下げることで、ザラツキの「種」を物理的に耳の可聴域から遠ざけます
    parts.append('aexciter=level_in=1:level_out=1:amount=1.2:drive=1.5:blend=-5:freq=6500')
    
    parts += [
        'equalizer=f=40:t=q:w=0.75:g=2.2',
        'equalizer=f=60:t=q:w=0.8:g=3.0',
        'equalizer=f=2500:t=q:w=2.0:g=-0.8',  # ヴァイオリンのザラつく芯を消す
        'equalizer=f=3800:t=q:w=2.0:g=-0.6',  # ピアノのアタックの硬さ・濁りを取る
        'equalizer=f=6200:t=q:w=3.0:g=-1.0',  # エキサイターのチリチリする開始点を叩く
        'equalizer=f=7400:t=q:w=3.0:g=-1.2',  # 耳障りな高域の擦れ音をピンポイントで消す
        'equalizer=f=80:t=q:w=0.85:g=2.6',
        'equalizer=f=110:t=q:w=0.9:g=-0.6',
        'equalizer=f=140:t=q:w=0.95:g=-0.6',
        'equalizer=f=95:t=q:w=0.85:g=-0.6',
        'equalizer=f=125:t=q:w=0.9:g=-0.5',
        'equalizer=f=60:t=q:w=0.8:g=0.2',
        'equalizer=f=40:t=q:w=0.75:g=0.2',
        'equalizer=f=250:t=q:w=1.0:g=-0.5',
        'equalizer=f=400:t=q:w=1.0:g=-1.4',
        'equalizer=f=600:t=q:w=1.0:g=-2.0',
        'equalizer=f=1000:t=q:w=1.0:g=2.0',
        'equalizer=f=2200:t=q:w=1.4:g=1.0',
        'equalizer=f=3000:t=q:w=1.2:g=-0.4',
        'equalizer=f=5000:t=q:w=1.5:g=0.8',
        'equalizer=f=8000:t=q:w=1.5:g=0.0',
        'equalizer=f=6500:t=q:w=1.3:g=-0.5',
    ]
    if musikverein_room_effects:
        parts.append('aecho=0.88:0.92:17:0.22')
        parts.append('aecho=0.78:0.85:31:0.18')
        parts.append('acompressor=threshold=-27dB:ratio=1.28:attack=40:release=360:makeup=1.15')
        parts.append('aecho=0.6:0.7:29:0.13')
        parts.append('bass=g=2.8:f=45:w=0.6')
        parts.append('bass=g=2.0:f=80:w=0.6')
        if tinnitus_reduction_mode:
            parts.append('highshelf=f=10000:g=-1.5:w=0.5')
        parts.append('treble=g=0.4')
        parts.append('equalizer=f=2800:t=q:w=1.3:g=0.6')
        parts.append('equalizer=f=1800:t=q:w=1.2:g=0.25')
    parts.append('alimiter=level_in=1.0:level_out=1.0:limit=0.98:attack=2:release=50')
    if loudness_filter:
        parts.append(loudness_filter.rstrip(','))
    # ★ loudnorm使用時は既に-2.0dBTPまで詰まっているため、この段の出力ゲインは
    #   0dB超（＝さらなるブースト）を許可しない。減衰(マイナス側)のみ有効。
    _post_gain = current_volume if not loudness_filter else min(current_volume, 0)
    parts.append(
        f'{eq_part}volume={_post_gain}dB,'
        'alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=50'
    )
    main_chain = ','.join(parts)
    return ['-af', main_chain]


def _preset_spatial(gain_db, current_volume, eq_part, musikverein_room_effects,
                    air_particle_layer, echo_mode, tinnitus_reduction_mode, loudness_filter):
    """🌐 Spatial（3D空間音響）プリセット。
    ヘッドホン・イヤホンでの「ながら聴き」向け立体音響。
    extrastereoによるステレオ拡張と、左右・前後に遅延差を持たせた
    多段階aechoで「ふあーっと漂う」包まれ感を演出する。
    音割れ防止のため入力音圧を-4dB補正済み。
    """
    parts = []
    parts.append(f'volume={gain_db}dB')
    parts.append('volume=-2dB')
    
    # freqを6500に引き上げ、blendを-5まで下げることで、ザラツキの「種」を物理的に耳の可聴域から遠ざけます
    parts.append('aexciter=level_in=1:level_out=1:amount=1.2:drive=1.5:blend=-5:freq=6500')
    
    parts += [
        'equalizer=f=40:t=q:w=0.75:g=2.2',
        'equalizer=f=60:t=q:w=0.8:g=3.0',
        'equalizer=f=2500:t=q:w=2.0:g=-0.8',  # ヴァイオリンのザラつく芯を消す
        'equalizer=f=3800:t=q:w=2.0:g=-0.6',  # ピアノのアタックの硬さ・濁りを取る
        'equalizer=f=6200:t=q:w=3.0:g=-1.0',  # エキサイターのチリチリする開始点を叩く
        'equalizer=f=7400:t=q:w=3.0:g=-1.2',  # 耳障りな高域の擦れ音をピンポイントで消す
        'equalizer=f=80:t=q:w=0.85:g=2.6',
        'equalizer=f=110:t=q:w=0.9:g=-0.6',
        'equalizer=f=140:t=q:w=0.95:g=-0.6',
        'equalizer=f=95:t=q:w=0.85:g=-0.6',
        'equalizer=f=125:t=q:w=0.9:g=-0.5',
        'equalizer=f=60:t=q:w=0.8:g=0.2',
        'equalizer=f=40:t=q:w=0.75:g=0.2',
        'equalizer=f=250:t=q:w=1.0:g=-0.5',
        'equalizer=f=400:t=q:w=1.0:g=-1.4',
        'equalizer=f=600:t=q:w=1.0:g=-2.0',
        'equalizer=f=1000:t=q:w=1.0:g=2.0',
        'equalizer=f=2200:t=q:w=1.4:g=1.0',
        'equalizer=f=3000:t=q:w=1.2:g=-0.4',
        'equalizer=f=5000:t=q:w=1.5:g=0.8',
        'equalizer=f=8000:t=q:w=1.5:g=0.0',
        'equalizer=f=6500:t=q:w=1.3:g=-0.5',
    ]
    if musikverein_room_effects:
        if echo_mode == 'jazz_vocal':
            parts.append('aecho=0.88:0.92:17:0.13')
            parts.append('aecho=0.78:0.85:31:0.11')
        else:
            parts.append('aecho=0.88:0.92:17:0.22')
            parts.append('aecho=0.78:0.85:31:0.18')
        parts.append('acompressor=threshold=-27dB:ratio=1.15:attack=40:release=120:makeup=1.15')
        if echo_mode == 'jazz_vocal':
            parts.append('aecho=0.6:0.7:29:0.08')
        else:
            parts.append('aecho=0.6:0.7:29:0.13')
        parts.append('bass=g=2.8:f=45:w=0.6')
        parts.append('bass=g=2.0:f=80:w=0.6')
        parts.append('extrastereo=m=1.03')
        parts.append('stereotools=phase=0.02')
        if echo_mode == 'jazz_vocal':
            parts.append('aecho=0.55:0.65:65:0.05')
        else:
            parts.append('aecho=0.55:0.65:65:0.09')

        # ★ 微弱後方反射（Musikverein Late Reflection）
        parts.append(
            'aecho=0.90:0.82:'
            '18|60|180|320:'
            '0.015|0.010|0.006|0.002'
        )

        parts.append(
           #'apulsator=hz=0.08:amount=0.03:mode=sine'
           #'apulsator=hz=0.12:amount=0.05:mode=sine'
           'apulsator=hz=0.04:amount=0.08:mode=sine'
        )
        
        parts.append('volume=8dB'
        )

        if tinnitus_reduction_mode:
            parts.append('highshelf=f=10000:g=-1.5:w=0.5')
        parts.append('treble=g=1.0')
        if not tinnitus_reduction_mode:
            parts.append('highshelf=f=7000:g=1.4:w=0.7')
    parts.append('alimiter=level_in=1.0:level_out=1.0:limit=0.98:attack=2:release=50')
    if musikverein_room_effects:
        parts.append('equalizer=f=2800:t=q:w=1.3:g=0.6')
        parts.append('equalizer=f=1800:t=q:w=1.2:g=0.35')
    if loudness_filter:
        parts.append(loudness_filter.rstrip(','))
    # ★ loudnorm使用時は既に-2.0dBTPまで詰まっているため、この段の出力ゲインは
    #   0dB超（＝さらなるブースト）を許可しない。減衰(マイナス側)のみ有効。
    _post_gain = current_volume if not loudness_filter else min(current_volume, 0)
    parts.append(
        f'{eq_part}volume={_post_gain}dB,'
        'alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=50'
    )
    main_chain = ','.join(parts)
    if musikverein_room_effects and air_particle_layer:
        _se = ('[str]aecho=0.65:0.75:11:0.019[side];' if echo_mode == 'jazz_vocal'
               else '[str]aecho=0.65:0.75:11:0.031[side];')
        _ce = ('[side]aecho=0.55:0.65:6:0.012[diff];' if echo_mode == 'jazz_vocal'
               else '[side]aecho=0.55:0.65:6:0.02[diff];')
        fc = (
            f'[0:a]{main_chain}[main];'
            + '[main]equalizer=f=250:t=q:w=1.3:g=0.4[str];'
            + _se + _ce
            + '[diff]volume=14dB[diffg];'
            + 'anoisesrc=color=pink:amplitude=0.00070:r=48000:d=86400[air];'
            + '[air]highpass=f=130,lowpass=f=10500[airband];'
            + '[airband]volume=0.035,apulsator=hz=0.14:amount=0.12:mode=sine[airbed];'
            + 'anoisesrc=color=brown:amplitude=0.00016:r=48000:d=86400[aud];'
            + '[aud]highpass=f=180,volume=0.018[audience];'
            + "[diffg][airbed][audience]amix=inputs=3:weights='1 0.018 0.007':normalize=0:duration=first[premix];"
            + '[premix]alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=50[out]'
        )
        return ['-filter_complex', fc, '-map', '[out]']
    else:
        return ['-af', main_chain]


def _preset_bypass(gain_db, current_volume, eq_part, musikverein_room_effects,
                   air_particle_layer, echo_mode, tinnitus_reduction_mode, loudness_filter):
    """バイパスモード: EQ・リバーブ・APL・コンプなど全音響処理なし。
    ffmpeg はデコード専用として使用し、入力ゲイン・出力音量・alimiter のみ適用する。
    Musikverein 等の音響効果を比較するためのリファレンス再生。

    ※ 他プリセットと同様に gain_db（入力ゲイン）と alimiter を適用することで
       音割れを防ぎつつ、EQ/リバーブ/APL のみを取り除いた純粋な比較ができる。
    """
    chain = (
        f'volume={gain_db}dB,'          # 入力ゲイン（他プリセットと同条件）
        f'{eq_part}'                    # ユーザー手動EQのみ（空の場合は無影響）
        f'volume={current_volume}dB,'   # 出力音量（[+]/[-]キー）
        'alimiter=level_in=1.0:level_out=1.0:limit=0.98:attack=2:release=50'  # 音割れ防止
    )
    return ['-af', chain]


def _preset_vinyl_emotion(gain_db, current_volume, eq_part, musikverein_room_effects,
                          air_particle_layer, echo_mode, tinnitus_reduction_mode, loudness_filter):
    """Vinyl Emotion（ヴァイナル・エモーション）音場 ★v6。
    バイパスモードに準じ、Musikverein の残響・Air Particle Layer 等の
    空間演出は一切適用しない、固定フィルターチェーンのみのプリセット。

    ★ v5→v6 の変更理由：
       密度の高いフォルティッシモ（ピアノ等）で「音が中央に寄って
       団子になる／ぐじゃっとする」との報告。原因調査のため、この
       コードベースの他プリセット（Musikverein等）のacompressor設定を
       全て確認したところ、比率(ratio)はどれも1.08〜1.45に収まって
       いた。対してv5の第1段は ratio=3:1 という、このコードベース内
       で唯一の強い圧縮だった。強い比率の圧縮は密度の高い箇所で音場を
       押し潰し、団子状の窮屈な印象を生みやすい。v6では他プリセットと
       同じ「穏やかな多段グルー」に揃え、ピーク保護の最終防衛は
       asoftclip/alimiter（本来の役割）に委ねる設計に戻した。
       あわせてbassシェルフも少し軽くし、密度の高い低中域の
       混雑感を減らした。

    ★ sticky プリセット（_STICKY_FILTER_PRESETS）に含まれるため、
       一度 [c]（再生中）/ [F]（起動画面）でこのプリセットを選択すると、
       改めて [c]/[F] で他のプリセットに変更するまで、SI/ジャンル自動
       判定やトラック別保存プロファイルによる自動上書きを受けず維持
       される。
    """
    chain = (
        f'volume={gain_db}dB,'           # 入力ゲイン
        'volume=-2.1dB,'                 # EQ前の歪み対策
        'asubcut=cutoff=18:order=4,'     # 【変更】22Hzから18Hzへ下げ、ホールの超低域（空気振動）を残す
        # 【削除】lowpassを完全に削除。レコードの持つ最高域の倍音成分をすべて活かします
        'equalizer=f=600:width_type=q:width=0.5:g=-0.8,'  # 【変更】中音域の曇りをもう少しカット（-0.6 → -0.8）
        'equalizer=f=1600:width_type=q:width=0.4:g=-0.4,' # 張り付き感除去
        'equalizer=f=2500:width_type=q:width=0.6:g=0.8,'  # 【変更】管楽器・ピアノの輪郭を少し前へ（0.6 → 0.8）
        'equalizer=f=3000:width_type=q:width=0.5:g=-0.8,' # 【変更】頭割れ防止を最低限に緩和（-1.2 → -0.8）
        'equalizer=f=3800:width_type=q:width=0.7:g=-0.4,' # 【変更】バイオリンのトゲを抑えつつ艶を残す（-0.6 → -0.4）
        'equalizer=f=6000:width_type=q:width=0.8:g=1.5,'  # 【変更】フルートやシンバルの透明感をアップ（1.2 → 1.5）
        'equalizer=f=12000:width_type=q:width=1.2:g=1.4,' # 【変更】10kHzから12kHz（超高域の輝き・空気感）へシフトして増強
        'equalizer=f=315:width_type=q:width=0.35:g=0.3,'  # 【変更】籠りの原因になりやすい帯域を微調整（350Hz → 315Hz、1.4dB → 0.3dB）
        'extrastereo=m=1.12:c=1,'        # 左右の広がり
        'acompressor=threshold=-24dB:ratio=1.4:attack=2:release=70:makeup=1,' # コンプレッサー
        'volume=-12.0dB,'                 # 中間ゲイン調整
        f'{eq_part}'                     # ユーザー手動EQ
        f'volume={current_volume}dB,'    # 出力音量
        'alimiter=level_in=1.0:level_out=1.0:limit=0.98:attack=0.5:release=80' # 最終保険
    )
    return ['-af', chain]



def _preset_calm(gain_db, current_volume, eq_part, musikverein_room_effects,
                 air_particle_layer, echo_mode, tinnitus_reduction_mode, loudness_filter):
    """Calm（安らぎ）音場。
    心の安定と深い休息をもたらす、穏やかで包み込むような音場。
    Musikverein の黄金反射をベースに、EQ の上限を穏やかに丸め、
    Air Particle Layer の揺らぎを超低周波（hz=0.055）・小振幅（amount=0.065）に抑えることで
    静水面のような安定した空間を作り出す。
    就寝前の音楽鑑賞、瞑想的聴取、長時間の内省的リスニングに最適。
    """
    parts = []
    parts.append(f'volume={gain_db}dB')
    
    # freqを6500に引き上げ、blendを-5まで下げることで、ザラツキの「種」を物理的に耳の可聴域から遠ざけます
    parts.append('aexciter=level_in=1:level_out=1:amount=1.2:drive=1.5:blend=-5:freq=6500')
    
    parts += [
        # ── 基幹 EQ（Musikverein ベース、高域を穏やかに抑制） ──
        'equalizer=f=40:t=q:w=0.75:g=2.2',
        'equalizer=f=60:t=q:w=0.8:g=3.0',
        'equalizer=f=2500:t=q:w=2.0:g=-0.8',  # ヴァイオリンのザラつく芯を消す
        'equalizer=f=3800:t=q:w=2.0:g=-0.6',  # ピアノのアタックの硬さ・濁りを取る
        'equalizer=f=6200:t=q:w=3.0:g=-1.0',  # エキサイターのチリチリする開始点を叩く
        'equalizer=f=7400:t=q:w=3.0:g=-1.2',  # 耳障りな高域の擦れ音をピンポイントで消す
        'equalizer=f=80:t=q:w=0.85:g=2.6',
        'equalizer=f=110:t=q:w=0.9:g=-0.6',
        'equalizer=f=140:t=q:w=0.95:g=-0.6',
        'equalizer=f=95:t=q:w=0.85:g=-0.6',
        'equalizer=f=125:t=q:w=0.9:g=-0.5',
        'equalizer=f=60:t=q:w=0.8:g=0.2',
        'equalizer=f=40:t=q:w=0.75:g=0.2',
        'equalizer=f=250:t=q:w=1.0:g=-0.5',
        'equalizer=f=400:t=q:w=1.0:g=-1.4',
        'equalizer=f=600:t=q:w=1.0:g=-2.0',
        'equalizer=f=1000:t=q:w=1.0:g=2.0',
        'equalizer=f=2200:t=q:w=1.4:g=1.0',
        'equalizer=f=3000:t=q:w=1.2:g=-0.4',
        'equalizer=f=5000:t=q:w=1.5:g=0.8',
        'equalizer=f=8000:t=q:w=1.5:g=0.0',
        'equalizer=f=6500:t=q:w=1.3:g=-0.5',
        # Calm 独自: 3kHz 帯域をやや引き下げ刺激を減らす
        'equalizer=f=3000:t=q:w=1.0:g=-1.5',
        # Calm 独自: 5.2kHz を微細に持ち上げ（0.34dB）→ 倍音の温かみを保つ
        'equalizer=f=5200:t=q:w=0.5:g=0.34',
        # Calm 独自: 3.5kHz の僅かな上乗せ（0.5dB）→ 声の柔らかな輪郭
        'equalizer=f=3500:t=q:w=0.35:g=0.5',
    ]
    if musikverein_room_effects:
        # 初期反射は Musikverein 標準（17ms, 31ms）を踏襲し温かみを維持
        parts.append('aecho=0.88:0.92:17:0.22')
        parts.append('aecho=0.78:0.85:31:0.18')
        parts.append('acompressor=threshold=-27dB:ratio=1.15:attack=40:release=120:makeup=1.15')
        parts.append('aecho=0.6:0.7:29:0.13')
        parts.append('bass=g=2.8:f=45:w=0.6')
        parts.append('bass=g=2.0:f=80:w=0.6')
        parts.append('aecho=0.55:0.65:65:0.09')
        if tinnitus_reduction_mode:
            parts.append('highshelf=f=10000:g=-1.5:w=0.5')
        # Calm: treble を控えめに（Musikverein=1.0 → 0.5）
        parts.append('treble=g=0.5')
        if not tinnitus_reduction_mode:
            # Calm: 高域の艶も控えめに（+0.7dB → フラット気味）
            parts.append('highshelf=f=7000:g=0.6:w=0.7')
    parts.append('alimiter=level_in=1.0:level_out=1.0:limit=0.98:attack=2:release=50')
    if musikverein_room_effects:
        parts.append('equalizer=f=2800:t=q:w=1.3:g=0.6')
        parts.append('equalizer=f=1800:t=q:w=1.2:g=0.25')
    if loudness_filter:
        parts.append(loudness_filter.rstrip(','))
    # ★ loudnorm使用時は既に-2.0dBTPまで詰まっているため、この段の出力ゲインは
    #   0dB超（＝さらなるブースト）を許可しない。減衰(マイナス側)のみ有効。
    _post_gain = current_volume if not loudness_filter else min(current_volume, 0)
    parts.append(
        f'{eq_part}volume={_post_gain}dB,'
        'alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=50'
    )
    main_chain = ','.join(parts)
    if musikverein_room_effects and air_particle_layer:
        # Calm の Air Particle Layer:
        #   - diffusion volume: 11.5dB（やや穏やか）
        #   - apulsator hz=0.055（超低速の揺らぎ → 静水面のような安定感）
        #   - apulsator amount=0.065（小振幅 → 穏やかな揺れ）
        fc = (
            f'[0:a]{main_chain}[main];'
            + '[main]equalizer=f=250:t=q:w=1.3:g=0.4[str];'
            + '[str]aecho=0.60:0.70:11:0.037[side];'
            + '[side]aecho=0.50:0.60:6:0.025[diff];'
            + '[diff]volume=11.5dB[diffg];'
            + 'anoisesrc=color=pink:amplitude=0.00070:r=48000:d=86400[air];'
            + '[air]highpass=f=130,lowpass=f=9000[airband];'
            + '[airband]volume=0.0465,apulsator=hz=0.055:amount=0.065:mode=sine[airbed];'
            + 'anoisesrc=color=brown:amplitude=0.00016:r=48000:d=86400[aud];'
            + '[aud]highpass=f=180,volume=0.018[audience];'
            + "[diffg][airbed][audience]amix=inputs=3:weights='1 0.018 0.007':normalize=0:duration=first[premix];"
            + '[premix]alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=50[out]'
        )
        return ['-filter_complex', fc, '-map', '[out]']
    else:
        return ['-af', main_chain]


def _preset_deep(gain_db, current_volume, eq_part, musikverein_room_effects,
                 air_particle_layer, echo_mode, tinnitus_reduction_mode, loudness_filter):
    """Deep（深淵）音場。
    音楽の奥底に沈み込むような、濃密で内省的な音場。
    短い初期反射（11ms, 6ms）で音の近接感・密度を高め、
    Air Particle Layer の揺らぎを最低速（hz=0.040）・中程度振幅（amount=0.085）に設定することで
    深海底の静寂のような厚みある空間を作り出す。
    深夜の独奏聴取、ピアノ小品、弦楽の内省的作品に最適。
    """
    parts = []
    parts.append(f'volume={gain_db}dB')
    
    # freqを6500に引き上げ、blendを-5まで下げることで、ザラツキの「種」を物理的に耳の可聴域から遠ざけます
    parts.append('aexciter=level_in=1:level_out=1:amount=1.2:drive=1.5:blend=-5:freq=6500')
    
    parts += [
        # ── 基幹 EQ（低域の重みを活かしつつ高域を抑制） ──
        'equalizer=f=40:t=q:w=0.75:g=2.2',
        'equalizer=f=60:t=q:w=0.8:g=3.0',
        'equalizer=f=2500:t=q:w=2.0:g=-0.8',  # ヴァイオリンのザラつく芯を消す
        'equalizer=f=3800:t=q:w=2.0:g=-0.6',  # ピアノのアタックの硬さ・濁りを取る
        'equalizer=f=6200:t=q:w=3.0:g=-1.0',  # エキサイターのチリチリする開始点を叩く
        'equalizer=f=7400:t=q:w=3.0:g=-1.2',  # 耳障りな高域の擦れ音をピンポイントで消す
        'equalizer=f=80:t=q:w=0.85:g=2.6',
        'equalizer=f=110:t=q:w=0.9:g=-0.6',
        'equalizer=f=140:t=q:w=0.95:g=-0.6',
        'equalizer=f=95:t=q:w=0.85:g=-0.6',
        'equalizer=f=125:t=q:w=0.9:g=-0.5',
        'equalizer=f=60:t=q:w=0.8:g=0.2',
        'equalizer=f=40:t=q:w=0.75:g=0.2',
        'equalizer=f=250:t=q:w=1.0:g=-0.5',
        'equalizer=f=400:t=q:w=1.0:g=-1.4',
        'equalizer=f=600:t=q:w=1.0:g=-2.0',
        'equalizer=f=1000:t=q:w=1.0:g=2.0',
        'equalizer=f=2200:t=q:w=1.4:g=1.0',
        'equalizer=f=3000:t=q:w=1.2:g=-0.4',
        'equalizer=f=5000:t=q:w=1.5:g=0.8',
        'equalizer=f=8000:t=q:w=1.5:g=0.0',
        'equalizer=f=6500:t=q:w=1.3:g=-0.5',
        # Deep 独自: 3kHz 帯域を引き下げ（-1.5dB）→ 中高域の刺激を除去
        'equalizer=f=3000:t=q:w=1.0:g=-1.5',
        # Deep 独自: 5.2kHz フラット → 倍音の輝きを抑え沈み込む質感
        'equalizer=f=5200:t=q:w=0.5:g=0.0',
        # Deep 独自: 3.5kHz を極小上乗せ（+0.2dB）→ 弦の基音輪郭だけ残す
        'equalizer=f=3500:t=q:w=0.4:g=0.2',
    ]
    if musikverein_room_effects:
        # Deep の初期反射: 短い遅延（11ms, 6ms）→ 音の近接感・密度・包囲感
        parts.append('aecho=0.58:0.68:11:0.042')
        parts.append('aecho=0.48:0.58:6:0.030')
        parts.append('acompressor=threshold=-27dB:ratio=1.15:attack=40:release=120:makeup=1.15')
        parts.append('aecho=0.6:0.7:29:0.13')
        parts.append('bass=g=2.8:f=45:w=0.6')
        parts.append('bass=g=2.0:f=80:w=0.6')
        parts.append('aecho=0.55:0.65:65:0.09')
        if tinnitus_reduction_mode:
            parts.append('highshelf=f=10000:g=-1.5:w=0.5')
        # Deep: treble をさらに抑制（Musikverein=1.0 → 0.3）
        parts.append('treble=g=0.3')
        if not tinnitus_reduction_mode:
            # Deep: 高域の艶を最小限に（0.3dB）→ 暗くて深みのある音質
            parts.append('highshelf=f=7000:g=0.3:w=0.7')
    parts.append('alimiter=level_in=1.0:level_out=1.0:limit=0.98:attack=2:release=50')
    if musikverein_room_effects:
        parts.append('equalizer=f=2800:t=q:w=1.3:g=0.6')
        parts.append('equalizer=f=1800:t=q:w=1.2:g=0.25')
    if loudness_filter:
        parts.append(loudness_filter.rstrip(','))
    # ★ loudnorm使用時は既に-2.0dBTPまで詰まっているため、この段の出力ゲインは
    #   0dB超（＝さらなるブースト）を許可しない。減衰(マイナス側)のみ有効。
    _post_gain = current_volume if not loudness_filter else min(current_volume, 0)
    parts.append(
        f'{eq_part}volume={_post_gain}dB,'
        'alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=50'
    )
    main_chain = ','.join(parts)
    if musikverein_room_effects and air_particle_layer:
        # Deep の Air Particle Layer:
        #   - diffusion volume: 10.5dB（やや抑え気味）
        #   - apulsator hz=0.040（最低速の揺らぎ → 深海底の揺れ）
        #   - apulsator amount=0.085（中振幅 → 厚みある包囲感）
        fc = (
            f'[0:a]{main_chain}[main];'
            + '[main]equalizer=f=250:t=q:w=1.3:g=0.4[str];'
            + '[str]aecho=0.60:0.70:11:0.037[side];'
            + '[side]aecho=0.50:0.60:6:0.025[diff];'
            + '[diff]volume=10.5dB[diffg];'
            + 'anoisesrc=color=pink:amplitude=0.00070:r=48000:d=86400[air];'
            + '[air]highpass=f=130,lowpass=f=9000[airband];'
            + '[airband]volume=0.052,apulsator=hz=0.040:amount=0.085:mode=sine[airbed];'
            + 'anoisesrc=color=brown:amplitude=0.00016:r=48000:d=86400[aud];'
            + '[aud]highpass=f=180,volume=0.018[audience];'
            + "[diffg][airbed][audience]amix=inputs=3:weights='1 0.018 0.007':normalize=0:duration=first[premix];"
            + '[premix]alimiter=level_in=1.0:level_out=1.0:limit=0.95:attack=2:release=50[out]'
        )
        return ['-filter_complex', fc, '-map', '[out]']
    else:
        return ['-af', main_chain]


# プリセットディスパッチャー
_FILTER_PRESET_MAP = {
    'musikverein': _preset_musikverein,
    'piano':       _preset_piano,
    'chamber':     _preset_chamber,
    'vocal':       _preset_vocal,
    'jazz':        _preset_jazz,
    'calm':        _preset_calm,     # ★ 心の安定①: 静水面の安らぎ
    'deep':        _preset_deep,     # ★ 心の安定②: 深淵の沈潜
    'spatial':     _preset_spatial,  # ★ 3D空間音響: ヘッドホン向け立体音響
    'radio':       _preset_radio,
    'bypass':      _preset_bypass,   # ★ 音響処理なし（リファレンス再生）
    'vinyl_emotion': _preset_vinyl_emotion,  # ★ アナログレコード風の質感・空気感
}

FILTER_PRESET_LABELS = {
    'musikverein': '🎻 Musikverein (Orchestra)',
    'piano':       '🎹 Piano',
    'chamber':     '🏠 Chamber',
    'vocal':       '🎙 Vocal',
    'jazz':        '🎷 Jazz',
    'calm':        '🌿 Calm (soothing)',
    'deep':        '🌊 Deep (abyssal)',
    'spatial':     '🌐 Spatial (3D spatial audio)',
    'radio':       '📻 Radio (standard)',
    'bypass':      '⚪ Bypass (no processing / reference)',
    'vinyl_emotion': '💿 Vinyl Emotion (analog texture)',
}

# SI プリセット → フィルタープリセット マッピング
SI_PRESET_TO_FILTER_PRESET = {
    'default':               'musikverein',
    'piano_solo_intimate':   'piano',
    'piano_concerto_romantic':'piano',
    'piano_concerto_modern': 'piano',
    'string_quartet':        'chamber',
    'violin_concerto':       'musikverein',
    'symphony_full':         'musikverein',
    'opera_aria':            'vocal',
    'jazz_trio':             'jazz',
    'jazz_vocal':            'vocal',
}

# ★★★ ジャンルキーワード → フィルタープリセット 自動マッピング ★★★
# 音源情報が不完全な場合でも、ジャンルタグからフィルターを自動選択する
GENRE_TO_FILTER_PRESET = {
    # Jazz系
    'jazz':       'jazz',
    'ジャズ':     'jazz',
    'swing':      'jazz',
    'bebop':      'jazz',
    'blues':      'jazz',
    'bossa':      'jazz',
    'soul':       'jazz',
    'funk':       'jazz',
    'ブルース':   'jazz',
    # Vocal系
    'vocal':      'vocal',
    'ボーカル':   'vocal',
    'opera':      'vocal',
    'オペラ':     'vocal',
    'singer':     'vocal',
    'pop':        'vocal',
    'ポップ':     'vocal',
    'rock':       'vocal',
    'ロック':     'vocal',
    'folk':       'vocal',
    'フォーク':   'vocal',
    # Chamber系
    'chamber':    'chamber',
    '室内楽':     'chamber',
    'quartet':    'chamber',
    'trio':       'chamber',
    'duo':        'chamber',
    # Piano系（ソロ）
    'piano':      'piano',
    'ピアノ':     'piano',
    # Orchestra系 → musikverein
    'orchestra':  'musikverein',
    'symphony':   'musikverein',
    'classical':  'musikverein',
    'クラシック': 'musikverein',
    '交響':       'musikverein',
    'concerto':   'musikverein',
    'baroque':    'musikverein',
    'バロック':   'musikverein',
    # Calm系（安らぎ）
    'calm':       'calm',
    'ambient':    'calm',
    'meditation': 'calm',
    'sleep':      'calm',
    'relax':      'calm',
    '安らぎ':     'calm',
    '瞑想':       'calm',
    'アンビエント': 'calm',
    # Deep系（深淵）
    'deep':       'deep',
    '深夜':       'deep',
    'nocturne':   'deep',
    'nocturnal':  'deep',
}

def _genre_to_filter_preset(genre_str: str) -> str:
    """ジャンル文字列からフィルタープリセットを自動判定する。
    マッチしない場合は '' を返す（呼び出し側でデフォルト維持）。"""
    if not genre_str:
        return ''
    genre_lower = genre_str.lower()
    for keyword, preset in GENRE_TO_FILTER_PRESET.items():
        if keyword.lower() in genre_lower:
            return preset
    return 


# ★★★ タイトル・アーティスト名キーワード → フィルタープリセット ★★★
# ジャンルタグが空・未認識のときの補助判定。ジャンルタグより優先度は低い。
TITLE_KEYWORDS_TO_FILTER_PRESET = {
    # ── Jazz系: 著名アーティスト名 ──
    'miles davis':      'jazz',
    'coltrane':         'jazz',
    'john coltrane':    'jazz',
    'bill evans':       'jazz',
    'charlie parker':   'jazz',
    'dizzy gillespie':  'jazz',
    'oscar peterson':   'jazz',
    'dave brubeck':     'jazz',
    'thelonious monk':  'jazz',
    'herbie hancock':   'jazz',
    'pat metheny':      'jazz',
    'chick corea':      'jazz',
    'wes montgomery':   'jazz',
    'art tatum':        'jazz',
    'stan getz':        'jazz',
    'cannonball adderley': 'jazz',
    'gerry mulligan':   'jazz',
    'chet baker':       'jazz',
    'clifford brown':   'jazz',
    'lee morgan':       'jazz',
    'freddie hubbard':  'jazz',
    'wayne shorter':    'jazz',
    'sonny rollins':    'jazz',
    'charles mingus':   'jazz',
    'keith jarrett':    'jazz',
    'mccoy tyner':      'jazz',
    'milt jackson':     'jazz',
    'django reinhardt': 'jazz',
    'django':           'jazz',
    'kind of blue':     'jazz',
    # ── Jazz系: タイトルキーワード ──
    'jazz':             'jazz',
    'ジャズ':           'jazz',
    'swing':            'jazz',
    'bebop':            'jazz',
    'bossa nova':       'jazz',
    'ブルース':         'jazz',
    # ── Vocal/Opera系: 著名歌手名 ──
    'pavarotti':        'vocal',
    'domingo':          'vocal',
    'carreras':         'vocal',
    'callas':           'vocal',
    'netrebko':         'vocal',
    'gheorghiu':        'vocal',
    'sutherland':       'vocal',
    'bartoli':          'vocal',
    'villazon':         'vocal',
    'villazón':         'vocal',
    'kaufmann':         'vocal',
    'hampson':          'vocal',
    'fischer-dieskau':  'vocal',
    'schwarzkopf':      'vocal',
    'flagstad':         'vocal',
    'bjorling':         'vocal',
    'björling':         'vocal',
    'gigli':            'vocal',
    'tebaldi':          'vocal',
    'bergonzi':         'vocal',
    'freni':            'vocal',
    'te kanawa':        'vocal',
    # ── Vocal系: タイトルキーワード ──
    'aria':             'vocal',
    'arie':             'vocal',
    'lieder':           'vocal',
    'lied ':            'vocal',
    'recital':          'vocal',
    'soprano':          'vocal',
    'tenor':            'vocal',
    'baritone':         'vocal',
    'mezzo':            'vocal',
    # ── Chamber系: タイトルキーワード ──
    'string quartet':   'chamber',
    '弦楽四重奏':       'chamber',
    'piano trio':       'chamber',
    'ピアノ三重奏':     'chamber',
    'piano quartet':    'chamber',
    'string trio':      'chamber',
    'string quintet':   'chamber',
    # ── Piano系: タイトルキーワード ──
    'nocturne':         'piano',
    'nocturnes':        'piano',
    'ノクターン':       'piano',
    'ballade':          'piano',
    'バラード':         'piano',
    'impromptu':        'piano',
    'étude':            'piano',
    'piano sonata':     'piano',
    'ピアノソナタ':     'piano',
}


def _title_to_filter_preset(title: str, artist: str = '', album: str = '',
                              composer: str = '', performer: str = '') -> str:
    """タイトル・アーティスト・アルバム名等からフィルタープリセットを補助判定する。
    ジャンルタグで判定できなかった場合のみ呼び出す。
    マッチしない場合は '' を返す。"""
    combined = ' '.join(filter(None, [title, artist, album, composer, performer])).lower()
    if not combined.strip():
        return ''
    for keyword, preset in TITLE_KEYWORDS_TO_FILTER_PRESET.items():
        if keyword.lower() in combined:
            return preset
    return ''
''

# ===========================================================================
# ★★★ 音響プリセット定義 ここまで ★★★
# ===========================================================================


def _build_audio_filter_args(gain_db, tinnitus_reduction_mode, musikverein_room_effects,
                             loudness_filter, eq_part, current_volume, air_particle_layer=True,
                             echo_mode='classical'):
    """
    ffmpeg オーディオフィルター引数リストを構築して返す。
    current_filter_preset に応じた埋め込みプリセット関数にディスパッチする。

    Returns:
        list: ffmpeg コマンドに追加するフィルター引数のリスト
    """
    _builder = _FILTER_PRESET_MAP.get(current_filter_preset, _preset_musikverein)
    return _builder(
        gain_db=gain_db,
        current_volume=current_volume,
        eq_part=eq_part,
        musikverein_room_effects=musikverein_room_effects,
        air_particle_layer=air_particle_layer,
        echo_mode=echo_mode,
        tinnitus_reduction_mode=tinnitus_reduction_mode,
        loudness_filter=loudness_filter,
    )



# ===========================================================================
# ★★★ 自動歪み軽減装置（Auto De-Clip） ★★★
# ffmpegの最終出力段を asplit で分岐し、astats+ametadata でピークレベルを
# ファイルへ書き出す監視用の枝を追加する。再生される音声そのものには
# 一切手を加えない（分岐先は -map しない）。
#
# ログ出力（ピークモニターGUI向け）は通常再生・ランダム再生・フォルダー
# 再生・ジャケットモードのすべてで常に有効。
# 一方、しきい値超え検出時に入力ゲインを自動で下げる「トリガー」動作は
# 従来通り通常/ランダム再生時のみに限定する（フォルダー再生では
# 曲送り・リプレイ制御と競合するため作動させない）。
# ===========================================================================

def _wrap_filter_args_with_declip_monitor(filter_args, log_path):
    """既存の filter_args（-af または -filter_complex）の最終出力の直前に
    astats+ametadata（パススルー型の解析フィルター）を差し込み、
    ピークレベルを監視ログへ書き出す。astats/ametadataは音声を変化させず
    そのまま通過させるフィルターなので、分岐(asplit)は不要かつ安全。"""
    if not filter_args:
        return filter_args
    mon = (
        'astats=metadata=1:reset=1,'
        f'ametadata=print:key=lavfi.astats.Overall.Peak_level:file={log_path}:direct=1'
    )
    try:
        if filter_args[0] == '-filter_complex':
            fc = filter_args[1]
            if '[out]' not in fc:
                return filter_args  # 想定外の形式は安全側でスキップ
            fc = fc.replace('[out]', f',{mon}[out]', 1)
            return ['-filter_complex', fc, '-map', '[out]']
        elif filter_args[0] == '-af':
            chain = filter_args[1]
            return ['-af', f'{chain},{mon}']
    except Exception:
        pass
    return filter_args


def _declip_trigger_step():
    """歪み検出時に呼ばれる：入力ゲインプリセットを次の段階へ進めて曲を再生し直す。
    [g]キーと全く同じ current_gain_preset / replay_requested の経路を使う。"""
    global current_gain_preset, replay_requested, _declip_baseline_preset, _declip_auto_step
    if not auto_declip_enabled:
        return
    if _declip_baseline_preset is None:
        _declip_baseline_preset = current_gain_preset  # 初回トリガー時点の値を退避
    idx = _DECLIP_GAIN_ORDER.index(current_gain_preset) if current_gain_preset in _DECLIP_GAIN_ORDER else 0
    if idx >= len(_DECLIP_GAIN_ORDER) - 1:
        terminal_print("\n⚠️ Auto De-Clip: distortion persists even at the final stage (Loud, -5dB). Consider adjusting manually.")
        return
    current_gain_preset = _DECLIP_GAIN_ORDER[idx + 1]
    _declip_auto_step += 1
    _labels = {'classical': 'Classical (0dB)', 'general': 'General (-1.5dB)',
               'jazz_pop': 'Pop (-3.5dB)', 'loud': 'Loud (-5dB)'}
    terminal_print(f"\n🛡️ Auto De-Clip: output distortion detected → auto-adjusting input gain to {_labels[current_gain_preset]} and replaying (stage {_declip_auto_step})")
    replay_requested = True


def _declip_monitor_thread_func(log_path, stop_event, threshold_db=-0.38, hits_needed=3, window_sec=2.0):
    """astats ログを監視し、しきい値超えが window_sec 内に hits_needed 回起きたら
    _declip_trigger_step() を呼ぶ。トリガー後は自身が停止されるまで待機のみ
    （replay により新しいスレッドが起動されるため）。"""
    pos = 0
    hits = []
    while not stop_event.is_set():
        try:
            if os.path.exists(log_path):
                with open(log_path, 'r', errors='ignore') as f:
                    f.seek(pos)
                    new_data = f.read()
                    pos = f.tell()
                for line in new_data.splitlines():
                    if 'lavfi.astats.Overall.Peak_level=' not in line:
                        continue
                    try:
                        val = float(line.split('=', 1)[1].strip())
                    except ValueError:
                        continue
                    now = time.time()
                    if val >= threshold_db:
                        hits.append(now)
                    hits[:] = [t for t in hits if now - t <= window_sec]
                    if len(hits) >= hits_needed:
                        _declip_trigger_step()
                        return  # このトラック分の監視は終了（replayで再度起動される）
        except Exception:
            pass
        stop_event.wait(0.2)


def play_one_track(track, show_controls=True):
    """1曲を再生(プリセット対応版) - 音切れ改善版"""
    global stop_playback, current_processes, current_playing_track, current_image_path
    global next_track_requested, prev_track_requested, current_audio_preset
    global mode_change_requested, current_playlist, replay_requested  # ★★★ replay_requestedを追加 ★★★
    global tinnitus_reduction_mode, musikverein_room_effects, air_particle_layer
    global current_filter_preset, current_gain_preset  # ★★★ 追加: フィルター・ゲインプリセット ★★★
    global _declip_baseline_preset, _declip_auto_step, _declip_last_track_path  # ★★★ 自動歪み軽減装置 ★★★
    global _gain_preset_auto_linked_jazz  # ★★★ 追加: jazz自動ゲイン連動の引き継ぎ防止フラグ ★★★

    current_playing_track = track
    
    # ★★★ 曲情報を更新 ★★★
    track_path = track.get('path', '')
    track_num = current_playlist.index(track) + 1 if track in current_playlist else 0
    total_tracks = len(current_playlist)
    update_track_info(track_path, mode=current_playback_mode, track_num=track_num, total_tracks=total_tracks)

    # ★★★ 自動歪み軽減装置: 「新しい曲」に切り替わった場合のみ初期設定へ復元 ★★★
    # （replay_requested による同曲リプレイの場合は track_path が変わらないため復元しない）
    if track_path != _declip_last_track_path:
        if _declip_baseline_preset is not None:
            current_gain_preset = _declip_baseline_preset
        _declip_baseline_preset = None
        _declip_auto_step = 0
        _declip_last_track_path = track_path

    # ★★★ 【最優先】music_mood_dbの audio_profile を復元（全設定を一括ロード） ★★★
    _profile_loaded = _load_audio_profile_from_track(track)

    # ★★★ Sonia Intelligence: トラック開始時にプロファイル自動選択 ★★★
    # audio_profile が保存されていない場合のみ SI / ジャンル自動判定を行う
    # ★ バイパスモード中はSI/ジャンル自動判定をスキップ（ユーザー選択を維持）
    # ★★★ 修正: [c]キーでDBに明示的に保存した filter_preset がある場合も、
    # audio_profile保存分と同様にSI自動判定をスキップし、保存済み設定を優先する。
    # track['filter_preset'] の有無をチェックしないと、SI自動判定（ジャンル判定等）が
    # 先に current_filter_preset を上書きしてしまい、[c]キーで保存した設定が
    # 再生開始時に反映されない不具合が起きる。
    _has_saved_filter_preset = bool(track.get('filter_preset', '')) and track.get('filter_preset', '') in _FILTER_PRESET_MAP
    if not _profile_loaded and not _has_saved_filter_preset and SI_AVAILABLE and _si_instance and current_filter_preset != 'bypass':
        with info_display_lock:
            _ti = current_track_info.copy()
        # music_mood_dbのsi_presetフィールドを参照
        _si_preset_hint = track.get('si_preset', '') if track else ''
        _si_instance.on_track_start(
            genre=_ti.get('genre', ''),
            title=_ti.get('title', ''),
            artist=_ti.get('artist', ''),
            album=_ti.get('album', ''),
            composer=_ti.get('composer', ''),
            performer=_ti.get('performer', ''),
            si_preset=_si_preset_hint,
        )
        # 汎用プリセットになった場合は通知
        if _si_instance._last_was_default:
            with terminal_io_lock:
                sys.stdout.write("\033[s\033[10;1H  💡 [a]: register preset for this album\033[K\033[u")
                sys.stdout.flush()

        # ★★★ SI→フィルタープリセット 自動連動（2段階）★★★
        # 優先度: SI登録プロファイル > ジャンル自動判定 > 現状維持
        # ★ bypass/calm/deep/spatial(3D)中はユーザー選択を維持（自動変更しない）
        _si_preset_resolved = current_filter_preset in _STICKY_FILTER_PRESETS
        try:
            # 【第1段階】SI登録プロファイルが明示的に決まっている場合はそれを使う
            if not _si_preset_resolved and _si_instance.current_profile_id and not _si_instance._last_was_default:
                _si_prof = _si_instance.db.get(_si_instance.current_profile_id)
                if _si_prof and hasattr(_si_prof, 'base_preset') and _si_prof.base_preset in SI_PRESET_TO_FILTER_PRESET:
                    _new_fp = SI_PRESET_TO_FILTER_PRESET[_si_prof.base_preset]
                    if _new_fp != 'default':
                        current_filter_preset = _new_fp
                        _si_preset_resolved = True
            elif not _si_preset_resolved and _si_instance.current_params and not _si_instance._last_was_default:
                _bp = getattr(_si_instance.current_params, 'preset_name', None) or \
                      getattr(_si_instance.current_params, 'base_preset', None)
                if _bp and _bp in SI_PRESET_TO_FILTER_PRESET and SI_PRESET_TO_FILTER_PRESET[_bp] != 'default':
                    current_filter_preset = SI_PRESET_TO_FILTER_PRESET[_bp]
                    _si_preset_resolved = True
        except Exception:
            pass

        # 【第2段階】SI未登録 or デフォルトプロファイルの場合はジャンルで自動判定
        # ジャンルタグで判定できない場合はタイトル・アーティスト名を補助参照
        if not _si_preset_resolved:
            _genre_str = _ti.get('genre', '') or track.get('genre', '')
            _genre_fp = _genre_to_filter_preset(_genre_str)
            if _genre_fp:
                current_filter_preset = _genre_fp
            else:
                _title_fp = _title_to_filter_preset(
                    _ti.get('title', '')     or track.get('title', ''),
                    _ti.get('artist', '')    or track.get('artist', ''),
                    _ti.get('album', '')     or track.get('album', ''),
                    _ti.get('composer', '')  or track.get('composer', ''),
                    _ti.get('performer', '') or track.get('performer', ''),
                )
                if _title_fp:
                    current_filter_preset = _title_fp
                else:
                    # ★★★ 修正: ジャンル・タイトルどちらにも一致しなかった場合、
                    # current_filter_preset を前の曲の値のまま放置しない。
                    # （例: 前曲がボーカル物でvocalに設定された後、
                    #  ジャンル情報の薄いストラヴィンスキーやヴィヴァルディの
                    #  ようなオーケストラ曲が続くと、誤ってvocal/pianoのまま
                    #  再生されてしまっていた。判定不能時は既定のオーケストラ
                    #  プリセットへ明示的にリセットする。）
                    current_filter_preset = 'musikverein'

    # ★★★ 【第3段階】music_mood_dbに手動設定した filter_preset があればそれを優先 ★★★
    # audio_profile がない場合のフォールバック
    # ★★★ 修正: [c]キーで明示的にDBへ保存したfilter_presetは、
    # audio_profile保存分と同様、常に最優先で復元する（stickyチェックを外す）。
    # stickyチェックは「SI自動判定による上書き防止」が目的であり、
    # ユーザーが[c]キーで明示的に保存した設定の復元を妨げるべきではない。
    if not _profile_loaded and current_filter_preset not in _STICKY_FILTER_PRESETS:
        _db_fp = track.get('filter_preset', '')
        if _db_fp and _db_fp in _FILTER_PRESET_MAP:
            current_filter_preset = _db_fp

    # ★★★ 【ジャズ自動ゲイン連動】★★★
    # 保存済みプロファイルがない場合のみ filter_preset に応じて入力ゲインを自動切替。
    # jazz → classical だった場合のみ jazz_pop (-3.5dB) に自動昇格。
    # 手動で general/jazz_pop/loud を選んでいる場合はそのまま維持する。
    # ★★★ 修正: ランダム再生等でJazzの次にMusikverein等クラシック系プリセットへ
    # 切り替わった際、この自動連動で上げたjazz_popゲインが引き継がれてしまう不具合を修正。
    # 自動昇格した場合のみ _gain_preset_auto_linked_jazz を立て、jazz以外に
    # 切り替わったタイミングでclassicalへ自動的に戻す。[g]キーでの手動変更時は
    # フラグをクリアするため、ユーザーが明示的に選んだゲインは上書きしない。
    if not _profile_loaded:
        if current_filter_preset == 'jazz':
            if current_gain_preset == 'classical':
                current_gain_preset = 'jazz_pop'
                _gain_preset_auto_linked_jazz = True
        else:
            if _gain_preset_auto_linked_jazz and current_gain_preset == 'jazz_pop':
                current_gain_preset = 'classical'
            _gain_preset_auto_linked_jazz = False

    terminal_print(f"\n▶ Now playing: {track.get('title', 'Unknown')}")
    terminal_print(f"   Artist: {track.get('artist', 'Unknown')}")
    if _profile_loaded:
        terminal_print(f"   💾 Applied saved profile")

    # ★★★ Sonia Intelligence 音響設定表示 ★★★
    if SI_AVAILABLE and _si_instance and _si_instance.current_params:
        _si_line = _si_status_line()
        if _si_line:
            terminal_print(f"   {_si_line}")
        _fp_label = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)
        # ジャンル自動判定かSI登録かを示す
        if SI_AVAILABLE and _si_instance and _si_instance._last_was_default:
            terminal_print(f"   🎛️  Filter: {_fp_label}  ✦auto-genre")
        else:
            terminal_print(f"   🎛️  Filter: {_fp_label}")
    elif not SI_AVAILABLE:
        # SI非使用時もジャンル自動判定を表示
        # ジャンルタグで判定できない場合はタイトル・アーティスト名を補助参照
        # ★ バイパス・calm・deep・spatial(3D)モード中はジャンル自動判定をスキップ（ユーザー選択を維持）
        if current_filter_preset not in _STICKY_FILTER_PRESETS:
            _genre_str = track.get('genre', '')
            _genre_fp = _genre_to_filter_preset(_genre_str)
            if _genre_fp:
                current_filter_preset = _genre_fp
            else:
                _title_fp = _title_to_filter_preset(
                    track.get('title', ''),
                    track.get('artist', ''),
                    track.get('album', ''),
                    track.get('composer', ''),
                    track.get('performer', ''),
                )
                if _title_fp:
                    current_filter_preset = _title_fp
                else:
                    # ★★★ 修正: 前の曲のフィルターを誤って引き継がないよう、
                    # 判定不能時は既定のオーケストラプリセットへリセットする ★★★
                    current_filter_preset = 'musikverein'
            # ★★★ 手動設定 filter_preset を優先 ★★★
            _db_fp = track.get('filter_preset', '')
            if _db_fp and _db_fp in _FILTER_PRESET_MAP:
                current_filter_preset = _db_fp
        _fp_label = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)
        terminal_print(f"   🎛️  Filter: {_fp_label}  ✦auto-genre")

    if current_audio_preset != 'none':
        preset_names = {
            'vocal': '🎤 Vocal preset',
            'soloist': '🎻 Soloist preset',
            'hall': '🏛️ Hall preset',
            'chamber': '🎼 Chamber preset',
            'stage': '🎭 Stage preset',
            'strings': '🎸 Strings preset'
        }
        terminal_print(f"   Sound: {preset_names.get(current_audio_preset, current_audio_preset)}")

    composer = track.get('composer', 'Unknown')
    conductor = track.get('conductor', 'Unknown')
    performer = track.get('performer', 'Unknown')

    if composer != 'Unknown':
        terminal_print(f"   Composer: {composer}")
    if conductor != 'Unknown':
        terminal_print(f"   Conductor: {conductor}")
    if performer != 'Unknown' and performer != track.get('artist', 'Unknown'):
        terminal_print(f"   Performer: {performer}")

    if current_playback_mode == 'tempo':
        features = track.get('features', {})
        tempo = features.get('tempo', 0)
        terminal_print(f"   Tempo: {tempo:.0f} BPM")

    mood = track.get('mood', 'Unknown')
    if mood != 'Unknown':
        color, reset = get_mood_color(mood)
        emoji = get_mood_emoji(mood)
        mood_names = {
            'happy': 'Happy',
            'calm': 'Calm',
            'energetic': 'Energetic',
            'melancholy': 'Melancholy',
            'intense': 'Intense',
            'ambient': 'Ambient',
            'moderate': 'Moderate'
        }
        mood_en = mood_names.get(mood, mood)
        terminal_print(f"   Mood: {color}{emoji} {mood_en}{reset}")
    
    if show_controls:
        terminal_print("🎹 [r]Restart | [f]Play folder in order | [n]Next | [b]Prev | [i]Show image again | [o]Add to favorites | [q]Quit to menu")
        _gp_labels = {'classical': 'Classical(0dB)', 'general': 'General(-1.5dB)', 'jazz_pop': 'Pop(-3.5dB)', 'loud': 'Loud(-5dB)'}
        terminal_print(f"🔊 [+][-]Output gain ({CURRENT_VOLUME:+d} dB) | [g]Input gain ({_gp_labels.get(current_gain_preset, current_gain_preset)}) | [c]Filter | [s]Save", end="")
        if SI_AVAILABLE:
            terminal_print(" | [z]Feedback | [x]Preset | [a]Register album | [h]Hall | [p]Profile")
        else:
            terminal_print()

    track_path = track.get('path', '')
    if not os.path.exists(track_path):
        terminal_print(f"⚠ File not found: {track_path}")
        return False

    # ★★★ 修正1: 前回のプロセスを完全に終了 ★★★
    if current_processes['ffmpeg'] and current_processes['ffmpeg'].poll() is None:
        try:
            current_processes['ffmpeg'].terminate()
            current_processes['ffmpeg'].wait(timeout=1)
        except:
            try:
                current_processes['ffmpeg'].kill()
                current_processes['ffmpeg'].wait(timeout=0.5)
            except:
                pass
        time.sleep(0.15)  # プロセス終了後の安定化

    if current_processes['aplay'] and current_processes['aplay'].poll() is None:
        try:
            current_processes['aplay'].terminate()
            current_processes['aplay'].wait(timeout=1)
        except:
            try:
                current_processes['aplay'].kill()
                current_processes['aplay'].wait(timeout=0.5)
            except:
                pass
        time.sleep(0.15)  # プロセス終了後の安定化

    image_path = find_cover_image_safe(track_path)
    if image_path:
        feh_running = current_processes['feh'] and current_processes['feh'].poll() is None
        if not feh_running or current_image_path != image_path:
            if feh_running:
                try:
                    current_processes['feh'].terminate()
                    current_processes['feh'].wait(timeout=1)
                except:
                    pass
            # ★★★ 曲情報付きで画像を表示 ★★★
            with info_display_lock:
                track_info_copy = current_track_info.copy()
            current_processes['feh'] = show_cover_image_with_info(image_path, track_info_copy)
            if current_processes['feh']:
                with terminal_io_lock:
                    sys.stdout.write(f"\033[s\033[9;1H🖼️ Cover: {os.path.basename(image_path)[:40]}\033[K\033[u")
                    sys.stdout.flush()

    try:
        subprocess.run(['which', 'ffmpeg'], check=True, capture_output=True)
        subprocess.run(['which', 'aplay'], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        terminal_print(f"⚠ Required command not found: {e}")
        return False

    # ★★★ 自動歪み軽減装置: finally節で安全に参照できるよう既定値を用意 ★★★
    _declip_monitor_log_active = False
    _declip_trigger_active = False
    _declip_log_path = None
    _declip_stop_event = None
    _declip_thread = None

    try:
        if current_audio_preset != 'none':
            terminal_print(f"🎵 Playing with SoX (preset: {current_audio_preset})")
            sox_process = apply_audio_preset_via_sox(track_path, output_device, current_audio_preset)
            if sox_process:
                current_processes['aplay'] = sox_process
                time.sleep(0.2)  # ★★★ SoX起動後の安定化 ★★★
                
                while not stop_playback and not next_track_requested and not prev_track_requested and not mode_change_requested and not replay_requested:
                    try:
                        result = sox_process.poll()
                        if result is not None:
                            break
                        time.sleep(0.1)
                    except:
                        break
                
                if mode_change_requested:
                    try:
                        sox_process.terminate()
                        sox_process.wait(timeout=1)
                    except:
                        try:
                            sox_process.kill()
                        except:
                            pass
                return True
            else:
                # ★★★ 修正: SoXが失敗した場合のエラーメッセージを明確化 ★★★
                terminal_print("⚠️ SoX playback failed. Trying FFmpeg instead.")
        
        pass  # 🎵 FFmpeg再生中（情報バーに表示済み）
        
        # ★★★ 元のファイルのサンプリングレートを取得 ★★★
        original_sample_rate = get_sample_rate(track_path)
        
        # ★★★ アップサンプリング設定に応じて出力サンプリングレートを決定 ★★★
        if upsampling_target_rate > 0:
            output_sample_rate = upsampling_target_rate
            pass  # サンプリングレート情報（情報バーに表示済み）
        else:
            output_sample_rate = 48000 if dsp_mode_active else original_sample_rate
            pass  # サンプリングレート情報
        
        # ★★★ イコライザー統合 ★★★
        eq_filter = get_equalizer_ffmpeg_filter()
        eq_part = f'{eq_filter},' if eq_filter else ''

        # ★★★ Sonia Intelligence: SI EQデルタをeq_partに追加 ★★★
        if SI_AVAILABLE and _si_instance and _si_instance.current_params:
            _si_eq_str = _si_get_eq_filters(_si_instance.current_params)
            if _si_eq_str:
                eq_part = eq_part + _si_eq_str + ','
        
        # ★★★ ゲインプリセットを取得 ★★★
        gain_db = GAIN_PRESETS.get(current_gain_preset, 0.0)
        
        # ★★★ 音量一定化フィルターを構築 ★★★
        # ※ かつては2パス実測方式が「解析待ちで再生が止まる」問題があり保留していたが、
        #    今回：キャッシュに実測値があれば即座に2パス(linear=true)の正確な正規化を使い、
        #    無ければ従来の1パス(dynamic)で再生しつつ、裏でこの曲と次の曲を先読み実測する
        #    方式に変更。再生開始は一切ブロックしない。
        loudness_filter = ''
        if loudness_normalization:
            _measured = _peek_cached_loudness(track_path) if track_path else None
            if _measured:
                loudness_filter = _format_linear_loudnorm(_measured)
            else:
                loudness_filter = 'loudnorm=I=-16:TP=-2.0:LRA=11,'
                if track_path:
                    _prefetch_loudness_async(track_path)  # 次回のために裏で今すぐ実測

            # 次に再生される曲も先読みで実測しておく（フォルダー再生は対象外）
            if current_playback_mode != 'folder':
                try:
                    _idx = current_playlist.index(track)
                    if _idx + 1 < len(current_playlist):
                        _next_path = current_playlist[_idx + 1].get('path', '')
                        if _next_path:
                            _prefetch_loudness_async(_next_path)
                except ValueError:
                    pass
        
        # ★★★ フィルター引数を構築（Air Particle Layer 対応） ★★★
        filter_args = _build_audio_filter_args(
            gain_db, tinnitus_reduction_mode, musikverein_room_effects,
            loudness_filter, eq_part, CURRENT_VOLUME, air_particle_layer,
            echo_mode=musikverein_echo_mode
        )

        # ★★★ ピークモニターGUI用ログ出力: 全再生モードで常時有効 ★★★
        # （ジャケットモード／フォルダー再生モードでもピークモニターGUIが表示できるように、
        #  astats+ametadataによるログ書き出し自体は再生モードを問わず常に行う。
        #  一方、自動歪み軽減装置の「ゲイン自動調整トリガー」は、従来通り
        #  フォルダー再生では作動させない（[f]や曲送りとの競合を避けるため）。）
        _declip_monitor_log_active = True
        _declip_trigger_active = auto_declip_enabled and current_playback_mode != 'folder'
        _declip_log_path = f'/tmp/qji_declip_{os.getpid()}.log'
        _declip_stop_event = None
        _declip_thread = None
        if _declip_monitor_log_active:
            try:
                if os.path.exists(_declip_log_path):
                    os.remove(_declip_log_path)
            except Exception:
                pass
            filter_args = _wrap_filter_args_with_declip_monitor(filter_args, _declip_log_path)

        # ── 頭切れ防止: パイプライン起動ラグ吸収のため微小な無音を先頭に注入 ──
        # ffmpeg起動(t=0) → aplay起動(t+100ms) → ALSAデバイス初期化(t+数十ms) の
        # 合計ラグ (~200ms) を吸収する。adelay はサンプリングレート非依存。
        # ★ バイパスモードは filter_args = ['-af', 'volume=...'] なので通常通り処理される。
        HEAD_PAD_MS = 300  # 頭切れ防止パディング(ミリ秒) ← 環境に応じて調整可
        if filter_args:  # バイパス含む全モード: filter_args が空でないことを確認
            if filter_args[0] == '-filter_complex':
                # Air Particle Layer ON: [0:a] の直後に adelay を挿入
                filter_args[1] = filter_args[1].replace(
                    '[0:a]',
                    f'[0:a]adelay={HEAD_PAD_MS}|{HEAD_PAD_MS},',
                    1  # 最初の1箇所のみ置換
                )
            else:
                # -af モード: フィルターチェーンの先頭に adelay を追加
                filter_args[1] = f'adelay={HEAD_PAD_MS}|{HEAD_PAD_MS},' + filter_args[1]

        ffmpeg_cmd = (
            ['ffmpeg', '-i', track_path, '-vn',
             '-ar', str(output_sample_rate),  # ★★★ サンプリングレートを明示的に指定 ★★★
             '-acodec', 'pcm_s32le']
            + filter_args
            + ['-f', 'wav', '-']
        )

        # ★★★ aplayコマンドにもサンプリングレートを明示的に指定 ★★★
        aplay_cmd = [
            'aplay', 
            '-D', output_device, 
            '-r', '48000' if dsp_mode_active else str(original_sample_rate),  # ★★★
            '--buffer-size=262144', 
            '--period-size=32768'
        ]

        # ★★★ 診断用: Vinyl Emotion再生中に限り、ffmpeg/aplayのstderrを
        #     ログファイルに残す（通常はDEVNULLで握りつぶしていたため、
        #     "ぐじゃる"症状がバッファアンダーラン/ffmpeg側エラーなのか
        #     判別できなかった）。原因特定後はこの分岐は不要になる。
        _vinyl_debug = (current_filter_preset == 'vinyl_emotion')
        _ffmpeg_stderr = open('/tmp/qji_ffmpeg_debug.log', 'ab') if _vinyl_debug else subprocess.DEVNULL
        _aplay_stderr  = open('/tmp/qji_aplay_debug.log',  'ab') if _vinyl_debug else subprocess.DEVNULL
        if _vinyl_debug:
            _ts = time.strftime('%Y-%m-%d %H:%M:%S')
            for _f, _lbl in ((_ffmpeg_stderr, 'ffmpeg'), (_aplay_stderr, 'aplay')):
                _f.write(f'\n===== {_ts} [{_lbl}] track start: {track_path} =====\n'.encode())
                _f.flush()

        current_processes['ffmpeg'] = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=_ffmpeg_stderr
        )
        _widen_pipe_buffer(current_processes['ffmpeg'].stdout)  # ★ CPU競合時の音切れ耐性を上げる
        # adelay で起動ラグを吸収するため、ffmpeg→aplay 間の待機は最小限に短縮
        time.sleep(0.05)

        current_processes['aplay'] = subprocess.Popen(
            aplay_cmd,
            stdin=current_processes['ffmpeg'].stdout,
            stderr=_aplay_stderr
        )
        current_processes['ffmpeg'].stdout.close()

        # パイプライン安定化 (adelayが吸収するため短縮)
        time.sleep(0.05)

        # ★★★ 自動歪み軽減装置: 監視スレッド開始（ゲイン自動調整トリガーは通常/ランダム再生のみ） ★★★
        if _declip_trigger_active:
            _declip_stop_event = threading.Event()
            _declip_thread = threading.Thread(
                target=_declip_monitor_thread_func,
                args=(_declip_log_path, _declip_stop_event),
                daemon=True
            )
            _declip_thread.start()

        playback_start_time = time.time()
        while not stop_playback and not next_track_requested and not prev_track_requested and not mode_change_requested and not replay_requested:
            try:
                aplay_result = current_processes['aplay'].poll()
                if aplay_result is not None:
                    elapsed = time.time() - playback_start_time
                    ffmpeg_still_running = (
                        current_processes['ffmpeg'] and
                        current_processes['ffmpeg'].poll() is None
                    )
                    # ★★★ 修正: aplayが異常終了(非ゼロ)かつffmpegがまだ動いている場合は
                    # XRUNなどのデバイスエラー → ffmpegも停止して再生エラー扱いにする ★★★
                    if aplay_result != 0 and ffmpeg_still_running:
                        terminal_print(f"⚠️ aplay exited abnormally (code={aplay_result}, elapsed={elapsed:.1f}s) — possible device error")
                        try:
                            current_processes['ffmpeg'].terminate()
                            current_processes['ffmpeg'].wait(timeout=1)
                        except:
                            pass
                    break
                time.sleep(0.1)
            except:
                break

        if mode_change_requested:
            if current_processes['ffmpeg'] and current_processes['ffmpeg'].poll() is None:
                try:
                    current_processes['ffmpeg'].terminate()
                    current_processes['ffmpeg'].wait(timeout=1)
                except:
                    pass
            if current_processes['aplay'] and current_processes['aplay'].poll() is None:
                try:
                    current_processes['aplay'].terminate()
                    current_processes['aplay'].wait(timeout=1)
                except:
                    pass

        return True

    except Exception as e:
        terminal_print(f"⚠ Playback error: {e}")
        return False
    finally:
        # ★★★ 自動歪み軽減装置: 監視スレッド停止＆ログ後始末 ★★★
        if _declip_stop_event is not None:
            try:
                _declip_stop_event.set()
                if _declip_thread is not None:
                    _declip_thread.join(timeout=0.5)
            except Exception:
                pass
        if _declip_log_path:
            try:
                if os.path.exists(_declip_log_path):
                    os.remove(_declip_log_path)
            except Exception:
                pass

        if not next_track_requested and not prev_track_requested and not mode_change_requested:
            if current_processes['ffmpeg'] and current_processes['ffmpeg'].poll() is None:
                try:
                    current_processes['ffmpeg'].terminate()
                    current_processes['ffmpeg'].wait(timeout=2)
                except:
                    pass
            if current_processes['aplay'] and current_processes['aplay'].poll() is None:
                try:
                    current_processes['aplay'].terminate()
                    current_processes['aplay'].wait(timeout=2)
                except:
                    pass
        
        if _is_bluealsa_device(output_device):
            time.sleep(0.4)
        else:
            time.sleep(0.1)

# ===========================================================================
# ★★★ 音源別オーディオプロファイル 保存・読み込み ★★★
# ===========================================================================

def _collect_audio_profile() -> dict:
    """現在の全音響設定を辞書にまとめて返す。"""
    profile = {
        'filter_preset':          current_filter_preset,
        'gain_preset':            current_gain_preset,
        'musikverein_room_effects': musikverein_room_effects,
        'air_particle_layer':     air_particle_layer,
        'echo_mode':              musikverein_echo_mode,
        'tinnitus_reduction_mode': tinnitus_reduction_mode,
        'si_preset':              None,
        'si_params':              None,
    }
    # SI情報を追加
    if SI_AVAILABLE and _si_instance:
        # si_preset（アルバム登録プリセット名）
        if _si_instance.current_profile_id:
            prof = _si_instance.db.get(_si_instance.current_profile_id)
            if prof:
                profile['si_preset'] = getattr(prof, 'base_preset', None)
        # si_params（現在のパラメーター状態を直列化）
        if _si_instance.current_params:
            p = _si_instance.current_params
            profile['si_params'] = {
                'acoustic_space':    getattr(p, 'acoustic_space', None),
                'rt60_override':     getattr(p, 'rt60_override', None),
                'wet_override':      getattr(p, 'wet_override', None),
                'pre_delay_override':getattr(p, 'pre_delay_override', None),
                'side_ratio_override':getattr(p, 'side_ratio_override', None),
                'eq':                dict(p.eq) if hasattr(p, 'eq') and p.eq else {},
                'base_preset':       getattr(p, 'preset_name', None)
                                     or getattr(p, 'base_preset', None),
            }
    return profile


def _save_audio_profile_to_db(album: str, folder: str, profile: dict):
    """
    music_mood_db.json の該当アルバム全トラックに audio_profile を書き戻す。
    album または folder でマッチング（どちらかが一致すれば対象）。
    """
    try:
        if not os.path.exists(DATABASE_FILE):
            print("  ⚠️ music_mood_db.json not found")
            return 0
        with open(DATABASE_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
        if not isinstance(db, list):
            return 0
        folder_norm = os.path.normpath(folder) if folder else ""
        updated = 0
        for track in db:
            t_album  = track.get("album", "")
            t_folder = os.path.normpath(os.path.dirname(track.get("path", "")))
            if ((album and t_album == album)
                    or (folder_norm and t_folder == folder_norm)):
                track["audio_profile"] = profile
                # 互換性のため個別フィールドも更新
                if profile.get('filter_preset'):
                    track["filter_preset"] = profile['filter_preset']
                if profile.get('si_preset'):
                    track["si_preset"] = profile['si_preset']
                updated += 1
        if updated > 0:
            with open(DATABASE_FILE, "w", encoding="utf-8") as f:
                json.dump(db, f, ensure_ascii=False, indent=2)
        return updated
    except Exception as e:
        print(f"  ⚠️ Profile save error: {e}")
        return 0


def _load_audio_profile_from_track(track: dict):
    """
    トラックの audio_profile フィールドを読み込み、全設定を復元する。
    呼び出しは play_one_track の冒頭で行う。
    """
    global current_filter_preset, current_gain_preset
    global musikverein_room_effects, air_particle_layer, musikverein_echo_mode
    global tinnitus_reduction_mode

    profile = track.get('audio_profile', None)
    if not profile or not isinstance(profile, dict):
        return False  # プロファイルなし

    # 基本設定を復元
    # ★★★ 修正: 保存済みプロファイルは常に最優先で復元する ★★★
    # stickyチェック（bypass/calm/deep/spatial維持）は「SI自動判定による上書き防止」が
    # 目的であり、ユーザーが明示的に保存したプロファイルの復元を妨げるべきではない。
    # ランダム再生等で直前の曲の処理中にcurrent_filter_presetがたまたま
    # stickyプリセットの値になっていた場合でも、保存済みプロファイルは必ず復元する。
    if 'filter_preset' in profile and profile['filter_preset'] in _FILTER_PRESET_MAP:
        current_filter_preset = profile['filter_preset']
    if 'gain_preset' in profile and profile['gain_preset'] in GAIN_PRESETS:
        current_gain_preset = profile['gain_preset']
    if 'musikverein_room_effects' in profile:
        musikverein_room_effects = bool(profile['musikverein_room_effects'])
    if 'air_particle_layer' in profile:
        air_particle_layer = bool(profile['air_particle_layer'])
    if 'echo_mode' in profile:
        musikverein_echo_mode = profile['echo_mode']
    if 'tinnitus_reduction_mode' in profile:
        tinnitus_reduction_mode = bool(profile['tinnitus_reduction_mode'])

    # SI パラメーターを復元
    if SI_AVAILABLE and _si_instance:
        si_params_dict = profile.get('si_params', None)
        if si_params_dict and isinstance(si_params_dict, dict):
            try:
                from filter_builder import params_from_preset
                base = si_params_dict.get('base_preset') or 'default'
                params = params_from_preset(base)
                # 各オーバーライドを適用
                for attr in ('acoustic_space', 'rt60_override', 'wet_override',
                             'pre_delay_override', 'side_ratio_override'):
                    val = si_params_dict.get(attr)
                    if val is not None:
                        setattr(params, attr, val)
                # EQデルタを復元
                eq_saved = si_params_dict.get('eq', {})
                if eq_saved and hasattr(params, 'eq'):
                    for band, gain in eq_saved.items():
                        params.eq[band] = gain
                _si_instance.current_params = params
            except Exception as e:
                pass  # SI復元失敗は無視して続行

    return True


def _suggest_save_granularity(title: str, genre: str, album: str) -> str:
    """
    曲タイトル・ジャンル・アルバム名から保存粒度の推奨を返す。
    'album'  → アルバム単位推奨
    'track'  → 曲単位推奨
    """
    haystack = ' '.join([title, genre, album]).lower()

    album_keywords = [
        'symphony', 'symphonie', 'sinfonie', 'sinfonia',
        '交響曲', '交響詩',
        'concerto', 'konzert', '協奏曲',
        'sonata', 'sonate', 'ソナタ',
        'quartet', 'quintett', 'string quartet',
        '弦楽四重奏', '弦楽五重奏', '室内楽',
        'suite', 'partita', 'セレナーデ', 'serenade',
        'mass', 'messe', 'missa', 'requiem', 'レクイエム',
        'oratorio', 'cantata', 'カンタータ',
        'opera', 'オペラ', 'overture', '序曲',
        'trio', 'piano trio', '三重奏',
        'classical', 'クラシック',
        'op.', 'no.', 'k.',
    ]

    track_keywords = [
        'nocturne', '夜想曲', 'ノクターン',
        'etude', 'étude', '練習曲', 'エチュード',
        'prelude', '前奏曲', 'プレリュード',
        'impromptu', '即興曲',
        'ballade', 'バラード',
        'mazurka', 'マズルカ',
        'waltz', 'valse', 'ワルツ',
        'jazz', 'ジャズ', 'blues', 'ブルース',
        'pop', 'ポップ', 'rock', 'ロック',
        'vocal', 'ヴォーカル', 'song', '歌',
        'folk', 'フォーク', 'bossa', 'samba',
    ]

    album_score = sum(1 for kw in album_keywords if kw in haystack)
    track_score = sum(1 for kw in track_keywords if kw in haystack)

    if track_score > album_score:
        return 'track'
    else:
        return 'album'  # 同点・判断不能はアルバム単位をデフォルト


def _save_audio_profile_to_track(file_path: str, profile: dict) -> bool:
    """
    music_mood_db.json の指定ファイルパスの1曲のみに
    audio_profile を書き戻す（曲単位保存）。
    """
    try:
        if not os.path.exists(DATABASE_FILE):
            print("  ⚠️ music_mood_db.json not found")
            return False
        with open(DATABASE_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
        if not isinstance(db, list):
            return False
        norm_path = os.path.normpath(file_path)
        for track in db:
            if os.path.normpath(track.get("path", "")) == norm_path:
                track["audio_profile"] = profile
                if profile.get('filter_preset'):
                    track["filter_preset"] = profile['filter_preset']
                if profile.get('si_preset'):
                    track["si_preset"] = profile['si_preset']
                with open(DATABASE_FILE, "w", encoding="utf-8") as f:
                    json.dump(db, f, ensure_ascii=False, indent=2)
                return True
        print("  ⚠️ Target track not found in database")
        return False
    except Exception as e:
        print(f"  ⚠️ Profile save error: {e}")
        return False


def _sync_profile_to_memory(profile: dict, file_path: str = None,
                             album: str = None, folder: str = None):
    """
    [s]キーでDBへ保存したプロファイルを、メモリ上のトラック辞書
    （current_playing_track / current_playlist / current_folder_tracks）
    にも即座に反映する。
    ★★★ 修正: これまでDB書き込みのみ行い、メモリ上のtrack辞書を
    更新していなかったため、保存直後にリプレイしても
    audio_profileが空のままSI/ジャンル自動判定が再び走ってしまい、
    「保存したはずのフィルターが元に戻る」不具合の原因になっていた。 ★★★
    file_path 指定時は曲単位、album/folder 指定時はアルバム単位でマッチする。
    """
    global current_playing_track, current_playlist, current_folder_tracks

    norm_path = os.path.normpath(file_path) if file_path else None
    folder_norm = os.path.normpath(folder) if folder else ""

    def _matches(t):
        if norm_path is not None:
            return os.path.normpath(t.get("path", "")) == norm_path
        t_album = t.get("album", "")
        t_folder = os.path.normpath(os.path.dirname(t.get("path", "")))
        return ((album and t_album == album)
                or (folder_norm and t_folder == folder_norm))

    def _apply(t):
        t["audio_profile"] = profile
        if profile.get('filter_preset'):
            t["filter_preset"] = profile['filter_preset']
        if profile.get('si_preset'):
            t["si_preset"] = profile['si_preset']

    try:
        if current_playing_track is not None and _matches(current_playing_track):
            _apply(current_playing_track)
        for t in current_playlist:
            if _matches(t):
                _apply(t)
        for t in current_folder_tracks:
            if _matches(t):
                _apply(t)
    except Exception:
        pass


def _do_save_profile():
    """
    [s]キー: 現在の全音響設定を音源プロファイルとして保存。
    曲単位 / アルバム単位をジャンル・タイトルから推奨し、ユーザーが選択する。
    """
    _si_display_event.clear()
    try:
        time.sleep(0.60)

        with info_display_lock:
            _ti = current_track_info.copy()
        title     = _ti.get('title',     '---')
        album     = _ti.get('album',     '')
        genre     = _ti.get('genre',     '')
        file_path = _ti.get('file_path', '')
        folder    = os.path.dirname(file_path)

        profile   = _collect_audio_profile()
        _fp_label = FILTER_PRESET_LABELS.get(profile['filter_preset'], profile['filter_preset'])

        suggestion = _suggest_save_granularity(title, genre, album)

        with terminal_io_lock:
            sys.stdout.write("\n" * 3)
            sys.stdout.flush()
            print("\n")
            print("┌──────────────────────────────────────────────────────┐")
            print("│  💾  Save audio profile                              │")
            print("├──────────────────────────────────────────────────────┤")
            print(f"│  Track : {title[:43]:<43} │")
            print(f"│  Album : {(album or '---')[:43]:<43} │")
            print(f"│  Genre : {(genre or '---')[:43]:<43} │")
            print("├──────────────────────────────────────────────────────┤")
            print(f"│  Filter        : {_fp_label:<38} │")
            print(f"│  Input gain    : {profile['gain_preset']:<38} │")
            print(f"│  Output gain   : {CURRENT_VOLUME:+d} dB{'':<35} │")
            print(f"│  Musikverein   : {'ON' if profile['musikverein_room_effects'] else 'OFF':<38} │")
            print(f"│  Air Layer     : {'ON' if profile['air_particle_layer'] else 'OFF':<38} │")
            print(f"│  Echo mode     : {profile['echo_mode']:<38} │")
            print(f"│  Tinnitus red. : {'ON' if profile['tinnitus_reduction_mode'] else 'OFF':<38} │")
            if profile.get('si_params') and profile['si_params'].get('acoustic_space'):
                print(f"│  Acoustic space: {profile['si_params']['acoustic_space']:<38} │")
            if profile.get('si_params') and profile['si_params'].get('eq'):
                eq_str = str({k: f"{v:+.1f}" for k, v in profile['si_params']['eq'].items()
                              if abs(v) >= 0.1})[:38]
                print(f"│  EQ delta      : {eq_str:<38} │")
            print("├──────────────────────────────────────────────────────┤")
            t_mark = "  ← ★ recommended" if suggestion == 'track' else ""
            a_mark = "  ← ★ recommended" if suggestion == 'album' else ""
            print(f"│  Select save scope:                                  │")
            print(f"│    1. 🎵 This track only{t_mark:<27} │")
            print(f"│    2. 🏛️  Entire album{a_mark:<29} │")
            print(f"│    Enter = Cancel                                    │")
            print("└──────────────────────────────────────────────────────┘")
            sys.stdout.flush()

        choice = _si_readline("  Select (1/2): ").strip()

        if choice == '1':
            if not file_path:
                with terminal_io_lock:
                    print("  ⚠️ File path unavailable")
            else:
                ok = _save_audio_profile_to_track(file_path, profile)
                if ok:
                    _sync_profile_to_memory(profile, file_path=file_path)
                with terminal_io_lock:
                    if ok:
                        print(f"\n  ✅ Profile saved for '{title[:40]}'")
                        print(f"     Will be applied automatically next time this track plays")
                    else:
                        print("  ⚠️ Could not save")

        elif choice == '2':
            updated = _save_audio_profile_to_db(album, folder, profile)
            if updated > 0:
                _sync_profile_to_memory(profile, album=album, folder=folder)
            with terminal_io_lock:
                if updated > 0:
                    tgt = album or os.path.basename(folder) or 'Unknown'
                    print(f"\n  ✅ Profile saved for {updated} track(s) in '{tgt[:40]}'")
                    print(f"     Will be applied automatically from next playback")
                else:
                    print("  ⚠️ Could not save (no matching tracks found in the database)")

        else:
            with terminal_io_lock:
                print("  Cancelled")

        _si_readline("  [Press Enter to resume] ")
    finally:
        _si_display_event.set()


# ===========================================================================
# ★★★ 音源別オーディオプロファイル ここまで ★★★
# ===========================================================================


def _do_filter_select(airplay_mode=False, station_name=None):
    """
    [c]キー: 再生中フィルタープリセットを手動変更。
    選択後にmusic_mood_db.jsonへ書き戻すオプションあり。
    airplay_mode=True のときは Enter 待ちをスキップして自動再開する。
    station_name が指定された場合（ラジオ再生時）は、見出しとサブタイトルを
    「ラジオ音場選択」表記に切り替え、曲名ではなく局名を表示する。
    """
    global current_filter_preset, current_playlist

    _si_display_event.clear()
    try:
        time.sleep(0.60)  # info_display_threadのsleep(0.5)より長く待つ

        # 現在再生中の曲情報を取得
        with info_display_lock:
            _ti = current_track_info.copy()
        title  = _ti.get('title', '---')[:40]
        album  = _ti.get('album', '')
        folder = os.path.dirname(_ti.get('file_path', ''))

        _PRESETS_LIST = [
            ('musikverein', '🎻  Musikverein (Orchestra)   Standard orchestral'),
            ('piano',       '🎹  Piano                    Solo piano'),
            ('chamber',     '🏠  Chamber                  Chamber music / string quartet'),
            ('vocal',       '🎙  Vocal                    Vocal / opera'),
            ('jazz',        '🎷  Jazz                     Jazz'),
            ('calm',        '🌿  Calm                     Calm, still water'),
            ('deep',        '🌊  Deep                     Deep, submerged'),
            ('spatial',     '🌐  Spatial                  3D spatial audio, for headphones'),
            ('vinyl_emotion','💿  Vinyl Emotion            Analog texture / warmth'),
            ('radio',       '📻  Radio                    Lightweight, stable ffmpeg field for radio'),
        ]

        with terminal_io_lock:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            print("\n")
            print("┌──────────────────────────────────────────────────────┐")
            if station_name:
                print("│  🎛️  Select radio sound field                        │")
            else:
                print("│  🎛️  Change filter preset                            │")
            print("├──────────────────────────────────────────────────────┤")
            if station_name:
                print(f"│  Station: {station_name[:40]:<41} │")
            else:
                print(f"│  Track: {title:<43} │")
            print("├──────────────────────────────────────────────────────┤")

            for i, (key, label) in enumerate(_PRESETS_LIST, 1):
                cur = "●" if key == current_filter_preset else " "
                print(f"│  {cur} {i}. {label:<47} │")

            print("├──────────────────────────────────────────────────────┤")
            print("│  [Enter only] Cancel                                 │")
            print("└──────────────────────────────────────────────────────┘")
            sys.stdout.flush()

        choice = _si_readline("  Select a number → ")
        if not choice or not choice.strip().isdigit():
            with terminal_io_lock:
                print("  Cancelled")
            return

        idx = int(choice.strip()) - 1
        if not (0 <= idx < len(_PRESETS_LIST)):
            with terminal_io_lock:
                print("  Invalid number")
            return

        new_preset, new_label = _PRESETS_LIST[idx]
        old_preset = current_filter_preset
        current_filter_preset = new_preset
        with terminal_io_lock:
            print(f"\n  ✅ Filter changed: {FILTER_PRESET_LABELS.get(new_preset, new_preset)}")

        # ── music_mood_db への書き戻しを確認 ──
        if album or folder:
            save_choice = _si_readline("  Remember this for the whole album? (y/Enter=skip) → ")
            if save_choice.strip().lower() == 'y':
                try:
                    if not os.path.exists(DATABASE_FILE):
                        with terminal_io_lock:
                            print("  ⚠️ music_mood_db.json not found")
                    else:
                        with open(DATABASE_FILE, "r", encoding="utf-8") as f:
                            db = json.load(f)
                        if isinstance(db, list):
                            folder_norm = os.path.normpath(folder) if folder else ""
                            updated = 0
                            for track in db:
                                t_album  = track.get("album", "")
                                t_folder = os.path.normpath(os.path.dirname(track.get("path", "")))
                                if ((album and t_album == album)
                                        or (folder_norm and t_folder == folder_norm)):
                                    track["filter_preset"] = new_preset
                                    # ★★★ 修正: audio_profile内にも独自のfilter_presetが
                                    # 保存されている場合、それが再生時に無条件で最優先されて
                                    # しまい、今回のアルバム一括変更が反映されない不具合が
                                    # あった（[s]で個別保存済みの曲だけ再現）。
                                    # audio_profile側のfilter_presetも同時に揃えることで、
                                    # 「曲によっては適用されない」現象を防ぐ。
                                    _ap = track.get("audio_profile")
                                    if isinstance(_ap, dict) and "filter_preset" in _ap:
                                        _ap["filter_preset"] = new_preset
                                    updated += 1
                            if updated > 0:
                                with open(DATABASE_FILE, "w", encoding="utf-8") as f:
                                    json.dump(db, f, ensure_ascii=False, indent=2)
                                with terminal_io_lock:
                                    print(f"  📝 Recorded filter_preset='{new_preset}' for {updated} track(s)")
                                # ★ 修正: メモリ上の current_playlist のトラック辞書も更新する。
                                # DB書き込みだけではリプレイ時の track.get('filter_preset') が
                                # 空のままになり、SI が上書きするバグを防ぐ。
                                try:
                                    _fn = os.path.normpath(folder) if folder else ""
                                    for _t in current_playlist:
                                        _ta = _t.get("album", "")
                                        _tf = os.path.normpath(os.path.dirname(_t.get("path", "")))
                                        if ((album and _ta == album)
                                                or (_fn and _tf == _fn)):
                                            _t["filter_preset"] = new_preset
                                            _tap = _t.get("audio_profile")
                                            if isinstance(_tap, dict) and "filter_preset" in _tap:
                                                _tap["filter_preset"] = new_preset
                                    # ★★★ 修正: フォルダー/ジャケットモード再生中の
                                    # current_folder_tracks も同様に更新する。
                                    # ここを更新しないと、フォルダー再生中に[c]で
                                    # アルバム一括保存しても同フォルダー内の次の曲で
                                    # 反映されない不具合が起きる。 ★★★
                                    for _t in current_folder_tracks:
                                        _ta = _t.get("album", "")
                                        _tf = os.path.normpath(os.path.dirname(_t.get("path", "")))
                                        if ((album and _ta == album)
                                                or (_fn and _tf == _fn)):
                                            _t["filter_preset"] = new_preset
                                            _tap = _t.get("audio_profile")
                                            if isinstance(_tap, dict) and "filter_preset" in _tap:
                                                _tap["filter_preset"] = new_preset
                                except Exception:
                                    pass
                            else:
                                with terminal_io_lock:
                                    print("  ⚠️ No matching tracks found in the database")
                except Exception as e:
                    with terminal_io_lock:
                        print(f"  ⚠️ Error writing back to database: {e}")

        if not airplay_mode:
            _si_readline("  [Press Enter to resume playback with new filter] ")
    finally:
        _si_display_event.set()


def keyboard_listener():
    """キーボードリスナー"""
    global stop_playback, next_track_requested, prev_track_requested, current_image_path
    global current_playback_mode, mode_change_requested, current_folder_tracks, replay_requested  # ★★★ replay_requestedを追加 ★★★
    global air_particle_layer, si_feedback_requested, si_hall_requested, si_input_active
    global current_filter_preset  # ★★★ 追加: 再生中フィルター変更対応 ★★★
    global CURRENT_VOLUME, current_gain_preset  # ★★★ 追加: 再生中ゲイン調整対応 ★★★
    global _gain_preset_auto_linked_jazz  # ★★★ 追加: 手動変更時はjazz自動連動フラグを解除 ★★★
    # ★ 修正: setraw()を呼ぶ前にターミナル設定を保存（より安全）
    try:
        old_settings = termios.tcgetattr(sys.stdin)
    except Exception:
        old_settings = None
    try:
        tty.setraw(sys.stdin.fileno())
        # ★★★ 修正: 起動時にstdinの残留バッファをフラッシュ（メニュー操作の残り入力を無視） ★★★
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
        # バッファに溜まっていた入力を読み捨てる（最大50文字）
        _flush_count = 0
        while _flush_count < 50 and select.select([sys.stdin], [], [], 0)[0]:
            sys.stdin.read(1)
            _flush_count += 1
        while not stop_playback:
            # ★★★ SI入力中はkeyboard_listenerを一時停止 ★★★
            if si_input_active:
                time.sleep(0.05)
                continue
            if select.select([sys.stdin], [], [], 0.1)[0]:
                key = sys.stdin.read(1).lower()
                if key == 'r':
                    # ★★★ 追加: 曲を頭から再生し直す ★★★
                    replay_requested = True
                    terminal_print("\n🔄 Restarting track from beginning")
                elif key == 'f':
                    if current_playing_track and current_playing_track.get('path'):
                        folder_path = os.path.dirname(current_playing_track['path'])
                        current_folder_tracks = get_folder_tracks(folder_path)
                        if current_folder_tracks:
                            current_playback_mode = 'folder'
                            mode_change_requested = True
                            terminal_print(f"\n📁 Switched to folder mode: {len(current_folder_tracks)} tracks")
                elif key == 'i':
                    if current_image_path and os.path.exists(current_image_path):
                        if current_processes['feh'] and current_processes['feh'].poll() is None:
                            try:
                                current_processes['feh'].terminate()
                                current_processes['feh'].wait(timeout=1)
                            except:
                                pass
                        current_processes['feh'] = show_cover_image(current_image_path)
                        terminal_print(f"🖼️ Cover image redisplayed: {os.path.basename(current_image_path)}")
                elif key == 'q':
                    stop_playback = True
                    cleanup_processes()
                    break
                elif key == 'n':
                    next_track_requested = True
                elif key == 'b':
                    prev_track_requested = True
                elif key == 'w':
                    # ★★★ 音場調整（Air Particle Layer）ON/OFF トグル ★★★
                    air_particle_layer = not air_particle_layer
                    state = 'ON 🌿' if air_particle_layer else 'OFF'
                    terminal_print(f"\n👂 Air Particle Layer: {state}")
                    if air_particle_layer:
                        terminal_print("   Pink-noise layer enabled → applied from next track (press R to restart now)")
                    replay_requested = True  # 即座に現在曲に反映

                elif key == 'o':
                    # ★★★ [o] Add the currently playing track to Favorites ★★★
                    if current_playing_track and current_playing_track.get('path'):
                        _ok, _msg = add_favorite(current_playing_track)
                        terminal_print(f"\n{_msg}")
                    else:
                        terminal_print("\n⚠️ Couldn't get info for the currently playing track")

                # ━━ Sonia Intelligence キー ━━━━━━━━━━━━━━━
                elif key == 'z' and SI_AVAILABLE:
                    # ★★★ [z] Sonia Intelligence フィードバック入力 ★★★
                    _si_do_feedback()
                    replay_requested = True  # フィードバック適用後に現在曲を再開

                elif key == 'h' and SI_AVAILABLE:
                    # ★★★ [h] ホール（音響空間）選択 ★★★
                    _si_do_hall_select()
                    replay_requested = True

                elif key == 'p' and SI_AVAILABLE:
                    # ★★★ [p] SIプロファイル選択 ★★★
                    _si_do_profile_select()
                    replay_requested = True

                elif key == 'x' and SI_AVAILABLE:
                    # ★★★ [x] SI音響プリセット番号選択 ★★★
                    _si_do_preset_menu()
                    replay_requested = True

                elif key == 'a' and SI_AVAILABLE:
                    # ★★★ [a] アルバムプリセット手動登録 ★★★
                    _si_do_album_preset()
                    replay_requested = True

                elif key == 'c':
                    # ★★★ [c] フィルタープリセット手動変更 ★★★
                    _do_filter_select()
                    replay_requested = True  # 変更を即座に反映

                elif key in ('+', '='):
                    # ★★★ [+] 再生中ゲイン（音量）を +1dB ★★★
                    CURRENT_VOLUME = min(CURRENT_VOLUME + 1, 30)
                    terminal_print(f"\n🔊 Output gain: {CURRENT_VOLUME:+d} dB  ([+] up / [-] down)")
                    replay_requested = True  # 現在曲に即反映

                elif key == '-':
                    # ★★★ [-] 再生中ゲイン（音量）を -1dB ★★★
                    CURRENT_VOLUME = max(CURRENT_VOLUME - 1, -30)
                    terminal_print(f"\n🔉 Output gain: {CURRENT_VOLUME:+d} dB  ([+] up / [-] down)")
                    replay_requested = True  # 現在曲に即反映

                elif key == 'g':
                    # ★★★ [g] 入力ゲインプリセット循環 ★★★
                    _gain_order = ['classical', 'general', 'jazz_pop', 'loud']
                    _gain_labels = {
                        'classical': 'Classical (0 dB)',
                        'general':   'General (-1.5 dB)',
                        'jazz_pop':  'Pop (-3.5 dB)',
                        'loud':      'Loud (-5 dB)',
                    }
                    _cur_idx = _gain_order.index(current_gain_preset) if current_gain_preset in _gain_order else 0
                    current_gain_preset = _gain_order[(_cur_idx + 1) % len(_gain_order)]
                    _gain_preset_auto_linked_jazz = False  # ★ 手動選択なのでjazz自動連動の対象から外す
                    terminal_print(f"\n🎚️  Input gain preset: {_gain_labels[current_gain_preset]}  ([g] cycle / [s] save)")
                    replay_requested = True  # 現在曲に即反映

                elif key == 's':
                    # ★★★ [s] 現在の全音響設定をプロファイルとして保存 ★★★
                    _do_save_profile()
                    # 保存のみ。再生は続行（replay不要）
                    # 画面クリア後にコントロール表示を再描画
                    try:
                        time.sleep(0.1)
                        terminal_print("\n🎹 [r]Restart | [f]Folder | [n]Next | [b]Prev | [i]Show image | [o]Favorite | [q]Menu")
                        _gp_l = {'classical': 'Classical(0dB)', 'general': 'General(-1.5dB)', 'jazz_pop': 'Pop(-3.5dB)', 'loud': 'Loud(-5dB)'}
                        terminal_print(f"🔊 [+][-]Output gain ({CURRENT_VOLUME:+d} dB) | [g]Gain ({_gp_l.get(current_gain_preset, current_gain_preset)}) | [c]Filter | [s]Save", end="")
                        if SI_AVAILABLE:
                            terminal_print(" | [z]Feedback | [x]Preset | [a]Album | [h]Hall | [p]Profile")
                        else:
                            terminal_print()
                    except:
                        pass
                    # ── 画面クリア後にキーガイドを再表示 ──
                    time.sleep(0.1)
                    terminal_print("\n🎹 [r]Restart | [f]Folder | [n]Next | [b]Prev | [i]Show image | [o]Favorite | [q]Menu")
                    _fp = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)
                    _gp_l2 = {'classical': 'Classical(0dB)', 'general': 'General(-1.5dB)', 'jazz_pop': 'Pop(-3.5dB)', 'loud': 'Loud(-5dB)'}
                    terminal_print(f"🔊 [+][-]Output gain ({CURRENT_VOLUME:+d} dB) | [g]Gain ({_gp_l2.get(current_gain_preset, current_gain_preset)}) | [c]Filter ({_fp}) | [s]Save", end="")
                    if SI_AVAILABLE:
                        terminal_print(" | [z]Feedback | [x]Preset | [a]Album | [h]Hall | [p]Profile")
                    else:
                        terminal_print()
    except Exception as e:
        terminal_print(f"Keyboard listener error: {e}")
    finally:
        # ★ 修正: TCSADRAIN（出力バッファ待ち）→ TCSANOW（即時復元）
        # TCSADRAIN はffmpeg/aplayがまだ書き込み中だと長時間ブロックし
        # ターミナルがrawモードのまま固まる原因になる
        if old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)
            except Exception:
                pass


def voice_listener():
    """再生中の音声コマンド認識（改良版）"""
    global stop_playback, next_track_requested, prev_track_requested
    global current_playback_mode, mode_change_requested, current_folder_tracks, replay_requested  # ★★★ replay_requestedを追加 ★★★
    if not VOICE_RECOGNITION_AVAILABLE:
        return
    if not os.path.exists(VOSK_MODEL_PATH):
        print("⚠️ Vosk model not found:", VOSK_MODEL_PATH)
        return

    q = queue.Queue()
    model = Model(VOSK_MODEL_PATH)

    device_id = USB_MIC_DEVICE_ID if USB_MIC_DEVICE_ID is not None else None
    try:
        import sounddevice as sd
        if device_id is not None:
            dev_info = sd.query_devices(device_id)
        else:
            default_dev = sd.default.device
            device_id = int(default_dev[0]) if isinstance(default_dev, (list, tuple)) and default_dev[0] is not None else device_id
            dev_info = sd.query_devices(device_id) if device_id is not None else None
        samplerate = int(dev_info['default_samplerate']) if dev_info and 'default_samplerate' in dev_info else 16000
    except Exception:
        samplerate = 16000

    rec = KaldiRecognizer(model, samplerate)

    def callback(indata, frames, time_, status):
        if status and os.environ.get("SOUNDDEVICE_DEBUG"):
            print(status, file=sys.stderr)
        try:
            if hasattr(indata, 'tobytes'):
                q.put(indata.tobytes())
            else:
                q.put(bytes(indata))
        except Exception:
            try:
                q.put(bytes(indata))
            except:
                pass

    try:
        with sd.RawInputStream(samplerate=samplerate, blocksize=8000, dtype='int16',
                               channels=1, callback=callback, device=device_id):
            terminal_print("🎤 Voice recognition started (offline Vosk)")
            terminal_print(f"🎤 Sample rate: {samplerate} Hz, device: {device_id}")
            while not stop_playback:
                try:
                    data = q.get(timeout=1)
                except queue.Empty:
                    continue
                if rec.AcceptWaveform(data):
                    result = json.loads(rec.Result())
                    command = result.get("text", "").strip()
                    if not command:
                        continue
                    terminal_print(f"🎤 Recognised: {command}")
                    # （既存のコマンド判定ロジックをそのまま使用）
                    if "フォルダ" in command or "フォルダー" in command:
                        if current_playing_track and current_playing_track.get('path'):
                            folder_path = os.path.dirname(current_playing_track['path'])
                            current_folder_tracks = get_folder_tracks(folder_path)
                            if current_folder_tracks:
                                current_playback_mode = 'folder'
                                mode_change_requested = True
                                terminal_print(f"\n📁 Switched to folder mode: {len(current_folder_tracks)} tracks")
                    elif "テンポ" in command:
                        current_playback_mode = 'tempo'
                        mode_change_requested = True
                        terminal_print("\n🎵 Switched to tempo mode")
                    elif "作曲家" in command or "さっきょくか" in command:
                        current_playback_mode = 'composer'
                        mode_change_requested = True
                        terminal_print("\n🎼 Switched to composer mode")
                    elif "演奏" in command or "えんそう" in command or "オーケストラ" in command:
                        current_playback_mode = 'performer'
                        mode_change_requested = True
                        terminal_print("\n🎻 Switched to performer mode")
                    elif "指揮" in command or "しき" in command:
                        current_playback_mode = 'conductor'
                        mode_change_requested = True
                        terminal_print("\n🎺 Switched to conductor mode")
                    elif "ジャンル" in command or "じゃんる" in command:
                        current_playback_mode = 'genre'
                        mode_change_requested = True
                        terminal_print("\n🎭 Switched to genre mode")
                    elif "ムード" in command or "むーど" in command:
                        current_playback_mode = 'mood'
                        mode_change_requested = True
                        terminal_print("\n🎭 Switched to mood mode")
                    elif "次" in command:
                        next_track_requested = True
                    elif "前" in command:
                        prev_track_requested = True
                    elif "もう一度" in command or "もういちど" in command or "リプレイ" in command or "最初から" in command:
                        # ★★★ 追加: 曲を頭から再生し直す音声コマンド ★★★
                        replay_requested = True
                        terminal_print("\n🔄 Restarting track from beginning")
                    elif "終了" in command or "ストップ" in command or "停止" in command:
                        stop_playback = True
                        cleanup_processes()
                        break
                    elif "画像" in command or "ジャケット" in command:
                        if current_image_path and os.path.exists(current_image_path):
                            if current_processes['feh'] and current_processes['feh'].poll() is None:
                                try:
                                    current_processes['feh'].terminate()
                                    current_processes['feh'].wait(timeout=1)
                                except:
                                    pass
                            current_processes['feh'] = show_cover_image(current_image_path)
                            terminal_print(f"🖼️ Cover image redisplayed: {os.path.basename(current_image_path)}")
    except Exception as e:
        terminal_print(f"Voice recognition error: {e}")
        terminal_print("💡 Hint: check that your USB microphone is connected")


# ===== ギャップレス再生機能 =====

def create_concat_file_for_gapless(tracks, temp_dir):
    """
    FFmpegのconcatデマルチプレクサ用のファイルリストを作成

    Args:
        tracks: 再生する曲のリスト (track辞書のリスト)
        temp_dir: 一時ファイルを保存するディレクトリ

    Returns:
        concat_file_path: 作成されたconcatファイルのパス
    """
    concat_file_path = os.path.join(temp_dir, 'gapless_playlist.txt')

    with open(concat_file_path, 'w', encoding='utf-8') as f:
        for track in tracks:
            track_path = track.get('path', '')
            if os.path.exists(track_path):
                # ファイルパスにシングルクォートが含まれる場合のエスケープ
                safe_path = track_path.replace("'", "\'\''")
                f.write(f"file '{safe_path}'\n")

    return concat_file_path

def play_tracks_gapless(tracks, start_index=0):
    """
    ギャップレス再生で複数の曲を連続再生
    
    Args:
        tracks: 再生する曲のリスト (track辞書のリスト)
        start_index: 開始インデックス
    
    Returns:
        最後に再生された曲のインデックス
    """
    global stop_playback, current_processes, next_track_requested, prev_track_requested
    global current_playing_track, gapless_current_index, current_image_path
    
    if not tracks or start_index >= len(tracks):
        return start_index
    
    temp_dir = tempfile.mkdtemp()
    current_batch_start = start_index

    # ★★★ ピークモニターGUI用ログ出力: finally節で安全に参照できるよう既定値を用意 ★★★
    _declip_log_path = None

    try:
        # 再生する曲のリストを作成
        batch_tracks = tracks[start_index:]
        
        if not batch_tracks:
            return start_index
        
        # ★★★ 元のファイルのサンプリングレートを取得（最初の曲から） ★★★
        original_sample_rate = 48000  # デフォルト値
        if batch_tracks:
            first_track_path = batch_tracks[0].get('path', '')
            if os.path.exists(first_track_path):
                original_sample_rate = get_sample_rate(first_track_path)

        # Concatファイルを作成
        concat_file = create_concat_file_for_gapless(batch_tracks, temp_dir)

        # ★★★ アップサンプリング設定に応じて出力サンプリングレートを決定 ★★★
        if upsampling_target_rate > 0:
            output_sample_rate = upsampling_target_rate
            pass  # サンプリングレート情報（情報バーに表示済み）
        else:
            output_sample_rate = 48000 if dsp_mode_active else original_sample_rate
            pass  # サンプリングレート情報
        
        # FFmpegコマンドを構築
        ffmpeg_cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', concat_file]
        
        # ★★★ イコライザー統合 ★★★
        eq_filter = get_equalizer_ffmpeg_filter()
        eq_part = f'{eq_filter},' if eq_filter else ''

        # ★★★ Sonia Intelligence: SI EQデルタをeq_partに追加 ★★★
        if SI_AVAILABLE and _si_instance and _si_instance.current_params:
            _si_eq_str = _si_get_eq_filters(_si_instance.current_params)
            if _si_eq_str:
                eq_part = eq_part + _si_eq_str + ','
        
        # ★★★ ゲインプリセットを取得 ★★★
        gain_db = GAIN_PRESETS.get(current_gain_preset, 0.0)
        
        # ★★★ 音量一定化フィルターを構築 ★★★
        # ※ ギャップレス再生は複数曲を1本のffmpegストリームに連結するため、
        #    曲ごとの2パス実測(build_loudnorm_filter)は適用できない。
        #    従来通りのリアルタイム1パス(dynamic)方式を使用する。
        loudness_filter = ''
        if loudness_normalization:
            loudness_filter = 'loudnorm=I=-16:TP=-2.0:LRA=11,'
        
        # ★★★ フィルター引数を構築（Air Particle Layer 対応） ★★★
        filter_args = _build_audio_filter_args(
            gain_db, tinnitus_reduction_mode, musikverein_room_effects,
            loudness_filter, eq_part, CURRENT_VOLUME, air_particle_layer,
            echo_mode=musikverein_echo_mode
        )

        # ★★★ ピークモニターGUI用ログ出力（フォルダー/ジャケットモードのギャップレス再生でも
        #     ピークモニターGUIを使えるように、astats+ametadataの監視分岐を追加する。
        #     このログ書き出しは音声を変化させないパススルー処理で、
        #     自動歪み軽減装置のゲイン自動調整トリガーはここでは行わない） ★★★
        _declip_log_path = f'/tmp/qji_declip_{os.getpid()}.log'
        try:
            if os.path.exists(_declip_log_path):
                os.remove(_declip_log_path)
        except Exception:
            pass
        filter_args = _wrap_filter_args_with_declip_monitor(filter_args, _declip_log_path)

        # ★★★ リクロッカー安定化: adelay で先頭7秒の無音を注入 ★★★
        # concatデマルチプレクサは全ファイルが同一コーデックである必要があるため
        # 無音WAVを混在させる代わりに ffmpeg フィルターで遅延（無音）を付加する。
        # adelay はサンプリングレート非依存で 44.1/48/96/192kHz すべてに対応。
        RECLOCKER_SILENCE_MS = 7000  # DCD-8 安定化に必要な無音長（ミリ秒）
        if filter_args[0] == '-filter_complex':
            # Air Particle Layer ON: [0:a] の直後に adelay を挿入
            filter_args[1] = filter_args[1].replace(
                '[0:a]',
                f'[0:a]adelay={RECLOCKER_SILENCE_MS}|{RECLOCKER_SILENCE_MS},',
                1  # 最初の1箇所のみ置換
            )
        else:
            # -af モード: フィルターチェーンの先頭に adelay を追加
            filter_args[1] = f'adelay={RECLOCKER_SILENCE_MS}|{RECLOCKER_SILENCE_MS},' + filter_args[1]
        print(f"🔇 Reclocker stabilisation: adding {RECLOCKER_SILENCE_MS // 1000}s silence at start")
        ffmpeg_cmd.extend(
            ['-vn', '-ar', str(output_sample_rate), '-acodec', 'pcm_s32le']
            + filter_args
            + ['-f', 'wav', '-']
        )
        
        # ★★★ aplayコマンドにもサンプリングレートとバッファサイズを指定 ★★★
        aplay_cmd = [
            'aplay', 
            '-D', output_device, 
            '-r', '48000' if dsp_mode_active else str(original_sample_rate),
            '--buffer-size=262144', 
            '--period-size=32768'
        ]
        
        print(f"\n🎵 Gapless mode: playing {len(batch_tracks)} tracks continuously")
        print("=" * 60)
        for i, track in enumerate(batch_tracks[:5]):  # 最初の5曲だけ表示
            track_name = track.get('title', os.path.basename(track.get('path', '')))
            print(f"  [{current_batch_start + i + 1}] {track_name}")
        if len(batch_tracks) > 5:
            print(f"  ... and {len(batch_tracks) - 5} more")
        print("=" * 60)
        
        # FFmpegとaplayをパイプで接続
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        
        aplay_proc = subprocess.Popen(
            aplay_cmd,
            stdin=ffmpeg_proc.stdout,
            stderr=subprocess.DEVNULL
        )
        # ★ 親プロセスのstdout参照を閉じる（パイプ衛生）
        # これをしないとaplayが死んでもffmpegのパイプバッファが詰まりハングする
        ffmpeg_proc.stdout.close()

        current_processes['ffmpeg'] = ffmpeg_proc
        current_processes['aplay'] = aplay_proc
        
        # 曲の進行を追跡(簡易版)
        gapless_current_index = start_index
        
        # プロセスの完了を待つ(ユーザー操作も監視)
        while ffmpeg_proc.poll() is None:
            time.sleep(0.1)
            
            # 現在再生中の曲の画像を表示(簡易実装)
            if gapless_current_index < len(tracks):
                current_track = tracks[gapless_current_index]
                current_playing_track = current_track
                
                # ジャケット画像の表示
                track_path = current_track.get('path', '')
                if track_path:
                    image_path = find_cover_image_safe(track_path)
                    if image_path and image_path != current_image_path:
                        # 画像を更新
                        if current_processes['feh'] and current_processes['feh'].poll() is None:
                            try:
                                current_processes['feh'].terminate()
                            except:
                                pass
                        current_processes['feh'] = show_cover_image(image_path)
                        current_image_path = image_path
            
            # ユーザー操作チェック
            if stop_playback:
                ffmpeg_proc.terminate()
                aplay_proc.terminate()
                return gapless_current_index
            
            if next_track_requested:
                # スキップ機能は実装が複雑なため、現在は全体を停止
                next_track_requested = False
                ffmpeg_proc.terminate()
                aplay_proc.terminate()
                return min(gapless_current_index + 1, len(tracks) - 1)
            
            if prev_track_requested:
                # 前の曲へ戻る(全体を停止して再開始)
                prev_track_requested = False
                ffmpeg_proc.terminate()
                aplay_proc.terminate()
                return max(0, gapless_current_index - 1)
        
        # プロセスの終了待ち
        ffmpeg_proc.wait()
        aplay_proc.wait()
        
        # 全曲再生完了
        return len(tracks)
    
    except Exception as e:
        print(f"⚠️ Gapless playback error: {e}")
        import traceback
        traceback.print_exc()
        return start_index
    
    finally:
        # 一時ディレクトリを削除
        try:
            shutil.rmtree(temp_dir)
        except:
            pass

        # ★★★ ピークモニターGUI用ログの後始末 ★★★
        if _declip_log_path:
            try:
                if os.path.exists(_declip_log_path):
                    os.remove(_declip_log_path)
            except Exception:
                pass

        current_processes['ffmpeg'] = None
        current_processes['aplay'] = None


def play_music_with_mode_switching(initial_playlist):
    """音楽再生(フォルダーモード切替対応)"""
    global stop_playback, next_track_requested, prev_track_requested, mode_change_requested
    global current_playback_mode, current_folder_tracks, current_playlist, replay_requested  # ★★★ replay_requestedを追加 ★★★
    
    # ★★★ 曲情報表示スレッドを開始 ★★★
    start_info_display()
    
    # 辞書形式でフォルダモードが指定されている場合
    folder_mode_start = False
    if isinstance(initial_playlist, dict) and initial_playlist.get('mode') == 'folder':
        folder_mode_start = True
        folder_tracks = initial_playlist.get('tracks', [])
        initial_playlist = folder_tracks.copy()
        current_folder_tracks = folder_tracks.copy()
        current_playback_mode = 'folder'
        print(f"📁 Starting in folder mode: {len(initial_playlist)} tracks")
    
    if not initial_playlist:
        print("⚠ Nothing to play")
        stop_playback = True
        cleanup_processes()
        return
    
    # フォルダモードの場合はシャッフルしない
    if not folder_mode_start:
        random.shuffle(initial_playlist)
        print(f"🎧 Starting playback: {len(initial_playlist)} tracks")
    else:
        print(f"🎧 Playing folder tracks in order")
    
    print(f"💡 Press [q] during playback to return to the menu")
    stop_playback = False
    next_track_requested = False
    prev_track_requested = False
    mode_change_requested = False
    replay_requested = False  # ★★★ 追加: リプレイリクエストを初期化 ★★★
    
    # ★ 修正: スレッドを変数に保存してfinally内でjoinできるようにする
    # またターミナル設定をここで保存する（keyboard_listenerより先）
    _saved_term = None
    try:
        _saved_term = termios.tcgetattr(sys.stdin)
    except Exception:
        pass

    _kb_thread = threading.Thread(target=keyboard_listener, daemon=True)
    _kb_thread.start()
    if VOICE_RECOGNITION_AVAILABLE and VOICE_RECOGNITION_ENABLED:
        threading.Thread(target=voice_listener, daemon=True).start()
    elif VOICE_RECOGNITION_AVAILABLE and not VOICE_RECOGNITION_ENABLED:
        terminal_print("🔇 Voice recognition disabled (--no-voice)")
    
    # ★★★ フォルダモードの場合はギャップレス再生を使用 ★★★
    if folder_mode_start and gapless_mode_enabled:
        print("\n🎵 Starting gapless playback mode")
        try:
            last_index = play_tracks_gapless(initial_playlist, start_index=0)
            print(f"\n✅ Gapless playback complete ({last_index} tracks)")
        except KeyboardInterrupt:
            print("\n⏸️ Playback stopped")
            stop_playback = True
        finally:
            stop_playback = True
            cleanup_processes()
            if current_playback_mode == 'folder':
                current_playback_mode = 'tempo'
                current_folder_tracks = []
        return
    
    # ★★★ 通常再生モード（従来の動作） ★★★
    try:
        current_track_index = 0
        # ★ 修正: current_playlist をメモリ上のトラック辞書の実体に紐付ける。
        # base_playlist は shallow copy なので辞書オブジェクトは共有される。
        # これにより change_filter_preset 等がトラック辞書を更新すると
        # base_playlist 経由の再生にも即座に反映される。
        current_playlist = initial_playlist
        base_playlist = initial_playlist.copy()
        
        while not stop_playback:
            if current_playback_mode == 'folder':
                playlist = current_folder_tracks
            else:
                playlist = base_playlist

            if not playlist or current_track_index >= len(playlist) or current_track_index < 0:
                # ★★★ 修正: フォルダモードの場合は終了する ★★★
                if current_playback_mode == 'folder' and folder_mode_start:
                    print("📁 All folder tracks played")
                    stop_playback = True
                    break
                elif current_playback_mode == 'folder':
                    # フォルダモードから通常モードに戻る場合
                    current_playback_mode = 'tempo'
                    current_folder_tracks = []
                    playlist = base_playlist
                    current_track_index = 0
                    print("📁 Folder playback complete — returning to original playlist")
                    if not playlist:
                        stop_playback = True
                        break
                else:
                    # 通常モードで曲が終了
                    print("✅ All tracks in playlist played")
                    stop_playback = True
                    break

            track = playlist[current_track_index]

            success = play_one_track(track, show_controls=True)
            if not success:
                print(f"⚠ Playback failed: {track.get('title', 'Unknown')}")
                # ★★★ 修正: 連続エラーによる高速スキップを防ぐため少し待機 ★★★
                time.sleep(0.5)
                current_track_index += 1
                continue

            if mode_change_requested:
                mode_change_requested = False
                if current_playback_mode == 'folder':
                    current_track_index = 0
                    playlist = current_folder_tracks.copy()
                else:
                    current_track_index = 0
                    playlist = base_playlist
                continue

            # ★★★ 追加: replay_requestedの処理 ★★★
            if replay_requested:
                replay_requested = False
                # 同じ曲をもう一度再生（current_track_indexは変えない）
                print("🔄 Restarting current track")
                continue
            # ★★★ ここまで追加 ★★★

            if next_track_requested:
                next_track_requested = False
                current_track_index += 1
            elif prev_track_requested:
                prev_track_requested = False
                current_track_index = max(0, current_track_index - 1)
            else:
                current_track_index += 1

            # ★★★ 修正: フォルダモード終了判定を改善 ★★★
            if current_playback_mode == 'folder' and current_track_index >= len(current_folder_tracks):
                if folder_mode_start:
                    # ジャケット選択から開始したフォルダモードは終了
                    print("📁 All folder tracks played")
                    stop_playback = True
                    break
                else:
                    # [f]キーで切り替えたフォルダモードは元に戻る
                    current_playback_mode = 'tempo'
                    current_folder_tracks = []
                    current_track_index = 0
                    playlist = base_playlist
                    print("📁 Folder playback complete — returning to original playlist")

    except KeyboardInterrupt:
        print("\n⏸️ Playback stopped")
        stop_playback = True
    finally:
        stop_playback = True
        cleanup_processes()
        # ★ 修正: keyboard_listenerスレッドの終了を待ち（最大0.5秒）、
        # その後ターミナルを確実に復元する。
        # これにより rawモードのままreadline()が呼ばれるフリーズを防ぐ。
        try:
            _kb_thread.join(timeout=0.5)
        except Exception:
            pass
        if _saved_term is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSANOW, _saved_term)
            except Exception:
                pass
        # バッファに残った入力を捨てる
        try:
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except Exception:
            pass
        # フォルダモードをリセット
        if current_playback_mode == 'folder':
            current_playback_mode = 'tempo'
            current_folder_tracks = []


# ===== メイン処理 =====

# ★★★ 起動時スプラッシュスクリーン ★★★

def _detect_jp_font():
    """日本語対応フォントを自動検出して返す（フォールバック: DejaVu-Sans）"""
    candidates = [
        'Noto-Sans-CJK-JP-Bold', 'Noto-Sans-CJK-JP',
        'NotoSansCJK-Bold', 'NotoSansCJK',
        'VL-Gothic', 'TakaoPGothic', 'IPAGothic',
        'DejaVu-Sans-Bold', 'DejaVu-Sans',
    ]
    try:
        result = subprocess.run(['convert', '-list', 'font'],
                                capture_output=True, text=True, timeout=3)
        available = result.stdout
        for f in candidates:
            if f in available:
                return f
    except Exception:
        pass
    return 'DejaVu-Sans'


def create_splash_image():
    """ImageMagick convert でスプラッシュ画像を生成して一時パスを返す。
    失敗した場合は None を返す。"""
    tmp_path = tempfile.mktemp(suffix='.png')
    jp_font  = _detect_jp_font()
    # 「Sonia」はラテン文字なので DejaVu-Sans-Bold を優先
    en_font  = 'DejaVu-Sans-Bold' if 'DejaVu-Sans-Bold' != jp_font else jp_font
    try:
        cmd = [
            'convert',
            '-size', '2160x960',
            'xc:#050518',                       # 深夜ブルー背景
            # --- "Sonia"（ラテン大文字・シアン）---
            '-font', en_font,
            '-pointsize', '270',
            '-fill', '#00cfff',
            '-gravity', 'Center',
            '-annotate', '+0-240', 'Sonia',
            # --- 「奏在」（日本語フォント・白）---
            '-font', jp_font,
            '-pointsize', '156',
            '-fill', '#ffffff',
            '-annotate', '+0+54', '\u300c\u594f\u5728\u300d',
            # --- サブタイトル（日本語フォント・グレー）---
            '-font', jp_font,
            '-pointsize', '51',
            '-fill', '#7799bb',
            '-annotate', '+0+300',
            '\u9ad8\u97f3\u8cea\u97f3\u697d\u518d\u751f\u30b7\u30b9\u30c6\u30e0'
            '  |  Musikverein Concert Hall Acoustics',
            tmp_path
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=15)
        if result.returncode == 0 and os.path.exists(tmp_path):
            return tmp_path
    except Exception:
        pass
    return None


def _find_splash_logo():
    """Qjilogo.png のパスを探して返す。見つからなければ None。
    優先順: スクリプトと同じディレクトリ → ~/Qjilogo.png"""
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Qjilogo.png'),
        os.path.expanduser('~/Qjilogo.png'),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _get_bluealsa_device() -> str:
    """
    bluealsa-aplay --list-pcms から A2DP 再生デバイスのフル PCM 文字列を返す。
    複数接続されている場合は最初のものを返す。見つからない場合は空文字列。
    例: 'bluealsa:DEV=B0:67:2F:1C:E6:2B,PROFILE=a2dp,SRV=org.bluealsa'
    """
    try:
        result = subprocess.run(
            ['bluealsa-aplay', '--list-pcms'],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith('bluealsa:') and 'PROFILE=a2dp' in line.upper():
                return line
    except Exception:
        pass
    return ''


def _is_bluealsa_device(dev: str) -> bool:
    """output_device が BlueALSA デバイスかどうかを判定する。"""
    return dev.startswith('bluealsa')


def select_output_device_interactive():
    """/proc/asound/cards を読んでサウンドカード一覧を表示し、
    カード番号そのままで出力デバイスを手動選択する。"""
    global output_device

    devices = {}  # {card_num_str: {'hw': ..., 'name': ...}}
    try:
        with open('/proc/asound/cards', 'r') as f:
            for line in f:
                m = re.match(r'^\s*(\d+)\s+\[([^\]]+)\]:\s+\S+\s+-\s+(.+)', line)
                if m:
                    card_num  = m.group(1)
                    card_name = m.group(3).strip()
                    hw_str    = f"hw:{card_num},0"
                    devices[card_num] = {'hw': hw_str, 'name': card_name}
    except Exception as e:
        print(f"⚠️ Failed to read /proc/asound/cards: {e}")

    # BlueALSA を追加 — 実際に接続中のデバイスのフル PCM 文字列を取得
    _ba_dev  = _get_bluealsa_device()
    _ba_name = 'Bluetooth (BlueALSA)'
    if _ba_dev:
        # 接続中デバイスが取得できた場合はデバイス名も表示
        try:
            _ba_info = subprocess.run(
                ['bluealsa-aplay', '--list-pcms'],
                capture_output=True, text=True, timeout=5,
            )
            _lines = _ba_info.stdout.splitlines()
            for i, ln in enumerate(_lines):
                if ln.strip() == _ba_dev and i + 1 < len(_lines):
                    _ba_name = f'Bluetooth: {_lines[i+1].strip().split(",")[0]}'
                    break
        except Exception:
            pass
        devices['b'] = {'hw': _ba_dev, 'name': _ba_name}
    elif shutil.which('bluealsa-aplay') or os.path.exists('/var/run/bluealsa'):
        # bluealsa-aplay はあるが接続中デバイスなし
        devices['b'] = {'hw': 'bluealsa', 'name': 'Bluetooth (BlueALSA — no device connected)'}

    if not devices:
        print("\n" + "=" * 60)
        print("⚠️ No sound card found.")
        print(f"\n  Enter only → use the default (hw:2,0)")
        print("=" * 60)
        try:
            raw = input("Enter number (Enter for default): ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = ''
        if raw == '':
            output_device = 'hw:2,0'
            print(f"✅ Auto-selected the default DSP output card, hw:2,0")
        else:
            print(f"⚠️ No valid number entered — auto-selected the default DSP output card, hw:2,0")
            output_device = 'hw:2,0'
        return output_device

    print("\n" + "=" * 60)
    print("🔊 Select an output sound card")
    print("=" * 60)
    for key in sorted(devices.keys()):
        d = devices[key]
        marker = "  ◀ current" if d['hw'] == output_device else ""
        print(f"  {key}. {d['hw']:14s}  {d['name']}{marker}")
    print(f"\n  Enter only → keep current setting ({output_device})")
    print("=" * 60)

    for _ in range(3):
        try:
            raw = input("Enter number: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if raw == '':
            if output_device == 'hw:2,0':
                print(f"✅ Auto-selected the default DSP output card, hw:2,0")
            else:
                print(f"✅ Keeping device: {output_device}")
            return output_device
        if raw in devices:
            chosen = devices[raw]
            output_device = chosen['hw']
            print(f"✅ Output device: {output_device}  ({chosen['name']})")
            return output_device
        print("⚠️ Please enter a valid number")

    if output_device == 'hw:2,0':
        print(f"✅ Auto-selected the default DSP output card, hw:2,0")
    else:
        print(f"✅ Keeping device: {output_device}")
    return output_device


def select_voice_recognition_interactive():
    """Let the user enable/disable voice recognition at startup.
    Does nothing if VOICE_RECOGNITION_AVAILABLE is False.
    Returns True (enabled) / False (disabled).
    """
    global VOICE_RECOGNITION_ENABLED

    if not VOICE_RECOGNITION_AVAILABLE:
        return True  # Voskが入っていなければそもそも関係なし

    print("\n" + "=" * 60)
    print("🎤 Voice recognition (microphone) setup")
    print("=" * 60)
    current_label = "Enabled 🎤" if VOICE_RECOGNITION_ENABLED else "Disabled 🔇"
    print(f"  1. Enable  🎤  (voice commands available)")
    print(f"  2. Disable  🔇  (noisy environment / no microphone)")
    print(f"\n  Enter only → keep current setting ({current_label})")
    print("=" * 60)

    for _ in range(3):
        try:
            raw = input("Enter number: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if raw == '':
            print(f"✅ Keeping voice recognition setting: {current_label}")
            return VOICE_RECOGNITION_ENABLED
        if raw == '1':
            VOICE_RECOGNITION_ENABLED = True
            print("✅ Voice recognition: Enabled 🎤")
            return True
        if raw == '2':
            VOICE_RECOGNITION_ENABLED = False
            print("🔇 Voice recognition: Disabled")
            return False
        print("⚠️ Please enter 1 or 2")

    print(f"✅ Keeping voice recognition setting: {current_label}")
    return VOICE_RECOGNITION_ENABLED


def show_splash_screen():
    """Startup splash:
        - Display Sonia 奏在 ASCII art in the terminal
        - Show Souzailogo.png fullscreen via feh
        - Auto-clear after ~4 s and return to the main screen
    """
    os.system('clear')

    # ---- Terminal ASCII art ----
    art = [
        "",
        "  \033[1;36m ██████╗ \033[0m\033[1;34m ██████╗ \033[0m\033[1;36m███╗   ██╗\033[0m"
        "\033[1;34m██╗\033[0m\033[1;36m █████╗ \033[0m",
        "  \033[1;36m██╔════╝ \033[0m\033[1;34m██╔═══██╗\033[0m\033[1;36m████╗  ██║\033[0m"
        "\033[1;34m██║\033[0m\033[1;36m██╔══██╗\033[0m",
        "  \033[1;97m███████╗ \033[0m\033[1;97m██║   ██║\033[0m\033[1;97m██╔██╗ ██║\033[0m"
        "\033[1;97m██║\033[0m\033[1;97m███████║\033[0m",
        "  \033[1;36m╚════██║ \033[0m\033[1;34m██║   ██║\033[0m\033[1;36m██║╚██╗██║\033[0m"
        "\033[1;34m██║\033[0m\033[1;36m██╔══██║\033[0m",
        "  \033[1;97m███████║ \033[0m\033[1;97m╚██████╔╝\033[0m\033[1;97m██║ ╚████║\033[0m"
        "\033[1;97m██║\033[0m\033[1;97m██║  ██║\033[0m",
        "  \033[1;36m╚══════╝ \033[0m\033[1;34m ╚═════╝ \033[0m\033[1;36m╚═╝  ╚═══╝\033[0m"
        "\033[1;34m╚═╝\033[0m\033[1;36m╚═╝  ╚═╝\033[0m",
        "",
        "  \033[1;33m           ♪   奏   在   ♪\033[0m",
        "",
        "  \033[0;37m  高音質音楽再生システム  |"
        "  Musikverein Concert Hall Acoustics\033[0m",
        "",
    ]
    for line in art:
        print(line)
    sys.stdout.flush()

    # ---- Display logo image fullscreen via feh ----
    feh_proc = None
    logo_path = _find_splash_logo()
    if logo_path and shutil.which('feh'):
        try:
            feh_proc = subprocess.Popen(
                [
                    'feh',
                    '--fullscreen',       # デスクトップ全体
                    '--auto-zoom',        # アスペクト比を保ってズーム
                    '--hide-pointer',     # マウスカーソルを隠す
                    '--borderless',       # 枠なし
                    '--no-menus',
                    '--title', 'Sonia \u300c\u594f\u5728\u300d',
                    logo_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            feh_proc = None

    # ---- Wait 4 seconds ----
    time.sleep(4)

    # ---- Cleanup ----
    if feh_proc is not None and feh_proc.poll() is None:
        feh_proc.terminate()

    os.system('clear')

# ★★★ スプラッシュスクリーンここまで ★★★


def interactive_mode():
    """Interactive mode (with cover-art album browser)"""
    global output_device, current_playback_mode, current_audio_preset, current_gain_preset, loudness_normalization, tinnitus_reduction_mode, gapless_mode_enabled, upsampling_target_rate, musikverein_room_effects, air_particle_layer, CURRENT_VOLUME, musikverein_echo_mode, current_filter_preset

    db = safe_load_database()
    if db is None:
        return

    print(f"\n==== Qji Music Player (output: {output_device}) ====")

    # ★★★ Now Playingミラーサーバーを自動起動 ★★★
    start_now_playing_server()

    # ★★★ ターミナルを初期化 ★★★
    try:
        import termios
        import tty
        old_settings = termios.tcgetattr(sys.stdin)
        # 念のため標準設定に戻す
        termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)
    except:
        pass

    while True:
        # ★★★ ループの最初でもバッファクリア ★★★
        try:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except:
            pass
        
        print("\n🎵 Qji Music Player")
        print("=" * 60)
        print("Select a playback mode:")
        print("  0. Random shuffle (all tracks)")
        print("  1. Tempo-based playback")
        print("  2. Composer-based playback")
        print("  3. Performer-based playback")
        print("  4. Conductor-based playback")
        print("  5. Genre-based playback")
        print("  6. Mood-based playback")
        print("  7. 🔍 Keyword search")
        print("  8. 🔷 Multi-filter search")
        print("  9. 📊 Mood statistics")
        print("  J. 🖼️  Browse by cover art")
        print("  N. 🆕  Recently added (cover-art browser)")  # ★★★ 追加: 最新アルバム ★★★
        print("  R. 📻 Radio stations")  # ★★★ 追加: ラジオ ★★★
        print("  P. 💾 Preset management")  # ★★★ 追加 ★★★
        print(f"  K. ⭐ Favorites ({len(load_favorites())} tracks)")  # ★★★ Favorites feature ★★★
        print("  A. 🎚️ Audio preset")
        print("  G. 🎛️ Gain preset")
        print("  L. 🔊 Loudness normalisation")  # ★★★ 追加 ★★★
        print("  T. 👂 Tinnitus-reduction mode (high-freq rolloff)")  # ★★★ 追加 ★★★
        print(f"  W. 🌿 Air Particle Layer: {'ON 🌿' if air_particle_layer else 'OFF'}")  # ★★★ 追加 ★★★
        print(f"  V. 🎻 Musikverein room effects: {'ON' if musikverein_room_effects else 'OFF'} (below golden reflections)")  # ★★★ 追加 ★★★
        _fp_label = FILTER_PRESET_LABELS.get(current_filter_preset, current_filter_preset)
        print(f"  F. 🎼 Filter preset (genre-based): {_fp_label}")  # ★★★ 追加 ★★★
        echo_mode_label = 'Classical 🎻' if musikverein_echo_mode == 'classical' else 'Jazz/Vocal 🎷'
        print(f"  E. 🎷 Echo mode: {echo_mode_label}")
        print(f"  Z. 🔗 Gapless playback: {'ON' if gapless_mode_enabled else 'OFF'}")  # ★★★ 追加 ★★★
        upsampling_status = 'OFF' if upsampling_target_rate == 0 else f'{upsampling_target_rate//1000}kHz'
        print(f"  U. 🎼 Upsampling: {upsampling_status}")  # ★★★ 追加 ★★★
        print(f"  M. 📱 Now Playing mirror: {'ON ' + get_local_ip() + ':' + str(NOW_PLAYING_PORT) if now_playing_server_running else 'OFF'}")  # ★★★ 追加 ★★★
        print("  QB. 🎵 Qobuz streaming")
        print("  S. 🟠 SoundCloud streaming")  # ← この1行を追加 
        print("  Y. 🔴 YouTube Music streaming")  # ★★★ 追加 ★★★
        print("  AP. 📡 AirPlay receiver (from iPhone / Mac)")  # ★★★ AirPlay ★★★
        print("  DL. 📻 UPnP/DLNA receiver (BubbleUPnP etc.)")  # ★★★ UPnP/DLNA ★★★
        print("  Q. Quit")
        print("=" * 60)
        print("💡 Press [q] during playback to return to this menu")

        try:
            choice = input("\nChoice (0-9, J, N, M, R, P, K, A, G, L, T, W, V, F, E, Z, U, QB, S, Y, AP, DL, X, Q): ").strip().lower()

            if choice == '0':
                print("\n🎵 Random shuffle mode — all tracks")
                db = safe_load_database()
                if db:
                    all_tracks = db.copy()
                    random.shuffle(all_tracks)
                    playlist = all_tracks[:500] if len(all_tracks) > 500 else all_tracks
                    print(f"✅ Playing {len(playlist)} tracks selected at random from {len(db)} in the database")
                    play_music_with_mode_switching(playlist)
                else:
                    print("⚠️ Could not load database")
                continue

            elif choice == 'a':
                print("\n🎚️ Audio preset:")
                print("  0. None (default)")
                print("  1. Vocal forward")
                print("  2. Soloist forward")
                print("  3. Wide hall (subtle)")
                print("  4. Natural chamber")
                print("  5. Stage presence")
                print("  6. Strings clarity")
                
                preset_choice = input("\nSelect (0-6): ").strip()
                preset_map = {
                    '0': 'none',
                    '1': 'vocal',
                    '2': 'soloist',
                    '3': 'hall',
                    '4': 'chamber',
                    '5': 'stage',
                    '6': 'strings'
                }
                
                if preset_choice in preset_map:
                    current_audio_preset = preset_map[preset_choice]
                    if current_audio_preset == 'none':
                        print("✅ Preset: None (standard EQ)")
                    else:
                        print(f"✅ Preset: {current_audio_preset}")
                else:
                    print("⚠️ Invalid selection")
                
                continue
            
            # ★★★ ゲインプリセット設定メニューを追加 ★★★
            elif choice == 'g':
                print("\n🎛️ Gain preset (input-stage level adjust):")
                print("  1. Classical (0 dB — ideal for delicate strings etc.)")
                print("  2. General (-1.5 dB — balanced, suits most genres)")
                print("  3. Jazz / Pop (-3.5 dB — prevents distortion on loud material)")
                print("  4. Loud material (-5 dB — high-level recordings, live, etc.)")
                print(f"\nCurrent setting: {current_gain_preset}")
                
                gain_choice = input("\nSelect (1-4): ").strip()
                
                if gain_choice == '1':
                    current_gain_preset = 'classical'
                    print("✅ Gain preset: Classical (0 dB)")
                elif gain_choice == '2':
                    current_gain_preset = 'general'
                    print("✅ Gain preset: General (-1.5 dB)")
                elif gain_choice == '3':
                    current_gain_preset = 'jazz_pop'
                    print("✅ Gain preset: Jazz / Pop (-3.5 dB)")
                elif gain_choice == '4':
                    current_gain_preset = 'loud'
                    print("✅ Gain preset: Loud material (-5 dB)")
                else:
                    print("⚠️ Invalid selection")
                
                continue
            
            # ★★★ 音量一定化オプション ★★★
            elif choice == 'l':
                print("\n🔊 Loudness normalisation")
                print("=" * 60)
                print("Reduces volume differences between tracks for a comfortable listening experience.")
                print("")
                print("⚠️  Note:")
                print("  - Slight reduction in dynamic punch is possible")
                print("  - Dynamic range is compressed")
                print("  - May not suit delicate material such as classical")
                print("")
                print(f"Current state: {'ON' if loudness_normalization else 'OFF'}")
                print("")
                print("  1. Turn ON (normalise volume)")
                print("  2. Turn OFF (keep original volume)")
                
                loud_choice = input("\nSelect (1-2): ").strip()
                
                if loud_choice == '1':
                    loudness_normalization = True
                    print("✅ Loudness normalisation: ON")
                    print("💡 Applied from the next track")
                elif loud_choice == '2':
                    loudness_normalization = False
                    print("✅ Loudness normalisation: OFF")
                    print("💡 Original volume restored from next track")
                else:
                    print("⚠️ Invalid selection")
                
                continue
            
            # ★★★ 耳鳴り低減モード ★★★
            elif choice == 't':
                print("\n👂 Tinnitus-reduction mode (high-frequency rolloff)")
                print("=" * 60)
                print("Gently rolls off frequencies above 10 kHz to reduce ear fatigue.")
                print("Designed for listeners sensitive to high frequencies or experiencing tinnitus.")
                print("")
                print("🎵 Changes:")
                print("  - 10 kHz, 12 kHz boost → cut")
                print("  - High-shelf filter added above 10 kHz (−1.5 dB)")
                print("  - Preserves tonal character while reducing harshness")
                print("")
                print(f"Current state: {'ON (HF rolloff active)' if tinnitus_reduction_mode else 'OFF (normal)'}")
                print("")
                print("  1. Turn ON (roll off high frequencies)")
                print("  2. Turn OFF (normal HF response)")
                
                tinnitus_choice = input("\nSelect (1-2): ").strip()
                
                if tinnitus_choice == '1':
                    tinnitus_reduction_mode = True
                    print("✅ Tinnitus-reduction mode: ON")
                    print("💡 Applied from the next track")
                    print("👂 High-frequency harshness reduced")
                elif tinnitus_choice == '2':
                    tinnitus_reduction_mode = False
                    print("✅ Tinnitus-reduction mode: OFF")
                    print("💡 Normal HF response restored")
                else:
                    print("⚠️ Invalid selection")
                
                continue
            
            # ★★★ 音響プリセット（ジャンル別フィルター）切り替え ★★★
            elif choice == 'f':
                print("\n🎼 Filter preset (genre-based)")
                print("=" * 60)
                print("Select a filter chain to match the genre of your music.")
                print("Applied from the next track.\n")
                _fp_keys = list(FILTER_PRESET_LABELS.keys())
                for i, k in enumerate(_fp_keys, 1):
                    mark = '▶' if k == current_filter_preset else ' '
                    print(f"  {i}. {mark} {FILTER_PRESET_LABELS[k]}")
                print("")
                fp_choice = input("Select number (Enter to cancel): ").strip()
                if fp_choice.isdigit() and 1 <= int(fp_choice) <= len(_fp_keys):
                    _old_fp = current_filter_preset
                    current_filter_preset = _fp_keys[int(fp_choice) - 1]
                    print(f"✅ Filter preset → {FILTER_PRESET_LABELS[current_filter_preset]}")
                    # ★ Bypass（無処理リファレンス再生）は他プリセットのような
                    #   EQ/コンプでの音量抑制が一切無いため、+12dB等の一般的な
                    #   出力音量のままだとハイレゾ／大音量ソースで音割れしやすい。
                    #   Bypass に切り替えた瞬間だけ出力音量を 0dB にリセットする
                    #   （[+]/[-]キーでの再調整は従来通り可能）。
                    if current_filter_preset == 'bypass' and _old_fp != 'bypass':
                        CURRENT_VOLUME = 0
                        print(f"🔊 Reset output volume to {CURRENT_VOLUME:+d}dB for Bypass")
                    print("💡 Applied from the next track")
                elif fp_choice == '':
                    print("⚪ Cancelled")
                else:
                    print("⚠️ Invalid selection")
                continue

            # ★★★ 楽友協会ルームエフェクト ON/OFF ★★★
            elif choice == 'v':
                print("\n🎻 Musikverein room-effect settings")
                print("=" * 60)
                print("Golden reflections (early reflections ①②) are kept;")
                print("everything below them is toggled on/off together.")
                print("")
                print("[Disabled when OFF]")
                print("  - Room volume (acompressor)")
                print("  - Low-end: floor and box resonance (aecho 29 ms)")
                print("  - Bass damping (45 Hz / 80 Hz)")
                print("  - Tinnitus-reduction high-shelf")
                print("  - High-end sheen (treble)")
                print("  - Stage brightness (EQ 2800 Hz)")
                print("  - Conductor presence (EQ 1800 Hz)")
                print("")
                print(f"Current state: {'ON' if musikverein_room_effects else 'OFF'}")
                print("")
                print("  1. Turn ON (all room effects active)")
                print("  2. Turn OFF (golden reflections only)")
                
                v_choice = input("\nSelect (1-2): ").strip()
                
                if v_choice == '1':
                    musikverein_room_effects = True
                    CURRENT_VOLUME = 12
                    print("✅ Musikverein room effects: ON")
                    print(f"🔊 Output volume set to {CURRENT_VOLUME} dB")
                    print("💡 Applied from the next track")
                elif v_choice == '2':
                    musikverein_room_effects = False
                    CURRENT_VOLUME = 6
                    print("✅ Musikverein room effects: OFF (early reflections only)")
                    print(f"🔊 Output volume auto-adjusted to {CURRENT_VOLUME} dB")
                    print("💡 Applied from the next track")
                else:
                    print("⚠️ Invalid selection")
                
                continue
            
            elif choice == 'e':  # ★★★ エコーモード切替 ★★★
                print("\n🎷 Echo mode (Musikverein echo level):")
                print("=" * 60)
                print("  Classical: full echo (best for strings / orchestra)")
                print("  Jazz/Vocal: ~40 % echo reduction (best for vocals / jazz)")
                print("")
                print(f"Current setting: {'Classical 🎻' if musikverein_echo_mode == 'classical' else 'Jazz/Vocal 🎷'}")
                print("")
                print("  1. Classical 🎻 (default)")
                print("  2. Jazz/Vocal 🎷")

                echo_choice = input("\nSelect (1-2): ").strip()
                if echo_choice == '1':
                    musikverein_echo_mode = 'classical'
                    print("✅ Echo mode: Classical 🎻")
                elif echo_choice == '2':
                    musikverein_echo_mode = 'jazz_vocal'
                    print("✅ Echo mode: Jazz/Vocal 🎷")
                else:
                    print("⚠️ Invalid selection")
                continue

            elif choice == 'w':
                # ★★★ 音場調整（Air Particle Layer）メニュー ★★★
                print("\n🌿 Air Particle Layer settings")
                print("=" * 60)
                print("Mixes an ultra-faint trace of pink noise (air-particle layer) to")
                print("widen the soundstage and ease high-frequency tinnitus.")
                print("※ Only active when V (Musikverein effects) is ON.")
                print("")
                print(f"Current state: {'ON 🌿' if air_particle_layer else 'OFF'}")
                print(f"Musikverein effects (V): {'ON' if musikverein_room_effects else 'OFF (must be ON)'}")
                print("")
                print("  1. Turn ON")
                print("  2. Turn OFF")

                apl_choice = input("\nSelect (1-2): ").strip()

                if apl_choice == '1':
                    air_particle_layer = True
                    print("✅ Air Particle Layer: ON 🌿")
                    print("💡 Applied from the next track")
                elif apl_choice == '2':
                    air_particle_layer = False
                    print("✅ Air Particle Layer: OFF")
                    print("💡 Applied from the next track")
                else:
                    print("⚠️ Invalid selection")

                continue

            elif choice == 'z':
                print("=" * 60)
                print("In folder mode and cover-art mode,")
                print("plays tracks continuously with no silence between them.")
                print("")
                print(f"Current state: {'ON' if gapless_mode_enabled else 'OFF'}")
                print("")
                print("  1. Turn ON (gapless)")
                print("  2. Turn OFF (normal)")
                
                gapless_choice = input("\nSelect (1-2): ").strip()
                
                if gapless_choice == '1':
                    gapless_mode_enabled = True
                    print("✅ Gapless mode: ON")
                    print("💡 Applied in folder mode / cover-art mode")
                    print("🎵 Continuous playback with no silence between tracks")
                elif gapless_choice == '2':
                    gapless_mode_enabled = False
                    print("✅ Gapless mode: OFF")
                    print("💡 Returning to normal playback")
                else:
                    print("⚠️ Invalid selection")
                
                continue
            
            elif choice == 'u':
                print("\n🎼 Upsampling settings")
                print("=" * 60)
                print("Upsamples all audio to a higher sample rate before playback.")
                print("")
                print("✨ Benefits:")
                print("  - May reduce roughness in the upper midrange")
                print("  - May allow the DAC to operate in a more optimal range")
                print("  - Effective for 44.1 kHz, 48 kHz, 96 kHz and similar sources")
                print("")
                print("⚠️  Note:")
                print("  - Does not add information beyond what is in the source")
                print("  - Higher rates increase CPU load")
                print("")
                current_status = 'OFF (original rate)' if upsampling_target_rate == 0 else f'converting to {upsampling_target_rate//1000} kHz'
                print(f"Current state: {current_status}")
                print("")
                print("  1. Upsample to 192 kHz")
                print("  2. Upsample to 384 kHz (smoother)")
                print("  3. OFF (keep original sample rate)")
                
                upsample_choice = input("\nSelect (1-3): ").strip()
                
                if upsample_choice == '1':
                    upsampling_target_rate = 192000
                    print("✅ Upsampling: 192 kHz")
                    print("💡 192 kHz from next track")
                    print("🎵 Upper-midrange smoothing expected")
                elif upsample_choice == '2':
                    upsampling_target_rate = 384000
                    print("✅ Upsampling: 384 kHz")
                    print("💡 384 kHz from next track")
                    print("🎵 Further smoothing expected")
                    print("⚠️  High CPU load")
                elif upsample_choice == '3':
                    upsampling_target_rate = 0
                    print("✅ Upsampling: OFF")
                    print("💡 Playing at original sample rate")
                else:
                    print("⚠️ Invalid selection")
                
                continue

            elif choice == '1':
                current_playback_mode = 'tempo'
                tempo = get_tempo_input(timeout_seconds=20)
                if tempo:
                    print(f"\n🔍 Searching for tracks at {tempo} ±10 BPM...")
                    playlist = get_tracks_by_tempo(tempo, tolerance=10, limit=200)
                    if playlist:
                        print(f"✅ Found {len(playlist)} track(s)")
                        play_music_with_mode_switching(playlist)
                    else:
                        print(f"⚠ No tracks found at {tempo} ±10 BPM")

            elif choice == '2':
                current_playback_mode = 'composer'
                options = get_available_options(mode='composer')
                if options:
                    composer = interactive_search_with_curses(options, mode='composer')
                else:
                    print("⚠️ No composer data in database")
                    composer = None
    
                if composer:
                    print(f"\n🔍 Searching for tracks by composer '{composer}'...")
                    playlist = get_tracks_by_composer(composer, limit=200)
                    if playlist:
                        print(f"✅ Found {len(playlist)} track(s)")
                        play_music_with_mode_switching(playlist)
                    else:
                        print(f"⚠️ No tracks found for composer '{composer}'")

            elif choice == '3':
                current_playback_mode = 'performer'
                options = get_available_options(mode='performer')
                if options:
                    performer = interactive_search_with_curses(options, mode='performer')
                else:
                    print("⚠️ No performer data in database")
                    performer = None
    
                if performer:
                    print(f"\n🔍 Searching for tracks by performer '{performer}'...")
                    playlist = get_tracks_by_performer(performer, limit=200)
                    if playlist:
                        print(f"✅ Found {len(playlist)} track(s)")
                        play_music_with_mode_switching(playlist)
                    else:
                        print(f"⚠️ No tracks found for performer '{performer}'")

            elif choice == '4':
                current_playback_mode = 'conductor'
                options = get_available_options(mode='conductor')
                if options:
                    conductor = interactive_search_with_curses(options, mode='conductor')
                else:
                    print("⚠️ No conductor data in database")
                    conductor = None
    
                if conductor:
                    print(f"\n🔍 Searching for tracks by conductor '{conductor}'...")
                    playlist = get_tracks_by_conductor(conductor, limit=200)
                    if playlist:
                        print(f"✅ Found {len(playlist)} track(s)")
                        play_music_with_mode_switching(playlist)
                    else:
                        print(f"⚠️ No tracks found for conductor '{conductor}'")

            elif choice == '5':
                current_playback_mode = 'genre'
                options = get_available_options(mode='genre')
                if options:
                    genre = interactive_search_with_curses(options, mode='genre')
                else:
                    print("⚠️ No genre data in database")
                    genre = None
    
                if genre:
                    print(f"\n🔍 Searching for tracks in genre '{genre}'...")
                    playlist = get_tracks_by_genre(genre, limit=200)
                    if playlist:
                        print(f"✅ Found {len(playlist)} track(s)")
                        play_music_with_mode_switching(playlist)
                    else:
                        print(f"⚠️ No tracks found for genre '{genre}'")

            elif choice == '6':
                current_playback_mode = 'mood'
                
                print("\n🎭 Mood selection:")
                print("  1. Select by individual mood")
                print("  2. Select by mood group")
                
                mood_choice = input("Select (1/2): ").strip()
                
                if mood_choice == '2':
                    print("\n🔍 Mood groups:")
                    print("  1. Positive (bright, energetic)")
                    print("  2. Negative (melancholy, intense)")
                    print("  3. Neutral (calm, ambient, moderate)")
                    
                    group_choice = input("Select (1-3): ").strip()
                    group_map = {'1': 'positive', '2': 'negative', '3': 'neutral'}
                    group_name = group_map.get(group_choice)
                    
                    if group_name:
                        print(f"\n🔍 Searching for tracks in mood group '{group_name}'...")
                        playlist = get_tracks_by_mood_group(group_name, limit=200)
                        if playlist:
                            print(f"✅ Found {len(playlist)} track(s)")
                            play_music_with_mode_switching(playlist)
                        else:
                            print(f"⚠️ No tracks found in mood group '{group_name}'")
                else:
                    options = get_available_options(mode='mood')
                    if options:
                        mood = interactive_search_with_curses(options, mode='mood')
                    else:
                        print("⚠️ No mood data in database")
                        mood = None
        
                    if mood:
                        print(f"\n🔍 Searching for tracks with mood '{mood}'...")
                        playlist = get_tracks_by_mood(mood, limit=200)
                        if playlist:
                            print(f"✅ Found {len(playlist)} track(s)")
                            play_music_with_mode_switching(playlist)
                        else:
                            print(f"⚠️ No tracks found with mood '{mood}'")

            elif choice == '7':
                current_playback_mode = 'keyword'
                print("\n🔍 Keyword search mode")
                print("=" * 60)
                print("Searches: title, artist, composer, performer, conductor,")
                print("         genre, mood, filename, folder path")
                print("=" * 60)
                print("e.g.: disney, bach, beethoven, symphony, etc.")
                print("💡 Enter 'debug:keyword' for verbose output")
                
                keyword_input = input("\nEnter keyword: ").strip()
                
                if keyword_input:
                    debug_mode = False
                    if keyword_input.lower().startswith('debug:'):
                        debug_mode = True
                        keyword = keyword_input[6:].strip()
                    else:
                        keyword = keyword_input
                    
                    print(f"\n🔍 Searching for '{keyword}'...")
                    playlist = get_tracks_by_keyword(keyword, limit=200, debug=debug_mode)
                    
                    if playlist:
                        print(f"\n✅ Found {len(playlist)} track(s)")
                        
                        reason_count = {}
                        for track in playlist:
                            reasons = track.get('_match_reason', [])
                            for r in reasons:
                                reason_count[r] = reason_count.get(r, 0) + 1
                        
                        if reason_count:
                            print("\n📊 Match breakdown:")
                            reason_names = {
                                'title': 'Title',
                                'artist': 'Artist',
                                'composer': 'Composer',
                                'performer': 'Performer',
                                'conductor': 'Conductor',
                                'genre': 'Genre',
                                'mood': 'Mood',
                                'filename': 'Filename',
                                'path': 'Folder path'
                            }
                            for reason, count in sorted(reason_count.items(), key=lambda x: x[1], reverse=True):
                                print(f"  {reason_names.get(reason, reason)}: {count}")
                        
                        print("\n📋 Sample results (first 10):")
                        for i, track in enumerate(playlist[:10], 1):
                            reasons = track.get('_match_reason', [])
                            reason_str = ','.join(reasons)
                            print(f"  {i:2d}. {track.get('title', 'Unknown'):40} [{reason_str}]")
                            print(f"      {track.get('artist', 'Unknown')}")
                        if len(playlist) > 10:
                            print(f"  ... and {len(playlist) - 10} more")
                        
                        confirm = input(f"\n▶️ Play these {len(playlist)} track(s)? (y/n): ").strip().lower()
                        if confirm == 'y' or confirm == '':
                            play_music_with_mode_switching(playlist)
                    else:
                        print(f"\n⚠️ No tracks matched '{keyword}'")
                        print("\n💡 Troubleshooting:")
                        print("  1. Try 'debug:keyword' for verbose output")
                        print("  2. Check that metadata is correctly registered in the database")
                        print("  3. Check that the filename or folder name contains the search string")
                else:
                    print("⚠️ No keyword entered")

            elif choice == '8':
                filters = {}
                print("\n🔷 Multi-filter search. Available filters:")
                print("   1: Tempo")
                print("   2: Composer")
                print("   3: Performer")
                print("   4: Conductor")
                print("   5: Genre")
                print("   6: Mood")
                print("💡 e.g. enter '1,2' to combine tempo and composer")
                sel = input("Enter filter numbers separated by commas (e.g. 1,2) or 'back': ").strip()
                if sel.lower() == 'back' or sel == '':
                    continue
                selected = [s.strip() for s in sel.split(',') if s.strip() in ['1','2','3','4','5','6']]
                if not selected:
                    print("⚠ Invalid selection")
                    continue

                for s in selected:
                    if s == '1':
                        tempo = get_tempo_input(timeout_seconds=20)
                        if tempo:
                            tol_input = input("Tempo tolerance in BPM (default 10): ").strip()
                            try:
                                tol = int(tol_input) if tol_input != '' else 10
                            except:
                                tol = 10
                            filters['tempo'] = (tempo, tol)
                        else:
                            print("⚠ No tempo entered — filter removed.")
                    elif s == '2':
                        options = get_available_options(mode='composer')
                        if options:
                            composer = interactive_search_with_curses(options, mode='composer')
                            if composer:
                                filters['composer'] = composer
                            else:
                                print("⚠ No composer entered — filter removed.")
                        else:
                            print("⚠️ No composer data — filter removed.")
                    elif s == '3':
                        options = get_available_options(mode='performer')
                        if options:
                            performer = interactive_search_with_curses(options, mode='performer')
                            if performer:
                                filters['performer'] = performer
                            else:
                                print("⚠ No performer entered — filter removed.")
                        else:
                            print("⚠️ No performer data — filter removed.")
                    elif s == '4':
                        options = get_available_options(mode='conductor')
                        if options:
                            conductor = interactive_search_with_curses(options, mode='conductor')
                            if conductor:
                                filters['conductor'] = conductor
                            else:
                                print("⚠ No conductor entered — filter removed.")
                        else:
                            print("⚠️ No conductor data — filter removed.")
                    elif s == '5':
                        options = get_available_options(mode='genre')
                        if options:
                            genre = interactive_search_with_curses(options, mode='genre')
                            if genre:
                                filters['genre'] = genre
                            else:
                                print("⚠ No genre entered — filter removed.")
                        else:
                            print("⚠️ No genre data — filter removed.")
                    elif s == '6':
                        options = get_available_options(mode='mood')
                        if options:
                            mood = interactive_search_with_curses(options, mode='mood')
                            if mood:
                                filters['mood'] = mood
                            else:
                                print("⚠ No mood entered — filter removed.")
                        else:
                            print("⚠️ No mood data — filter removed.")

                if not filters:
                    print("⚠ No filters specified — returning.")
                    continue

                print(f"\n🔍 Searching with filters: {filters}")
                playlist = get_tracks_by_filters(filters, limit=500)
                if playlist:
                    print(f"✅ Found {len(playlist)} tracks (shuffled, up to 500)")
                    play_music_with_mode_switching(playlist)
                else:
                    print("⚠ No tracks matched the specified filters")

            elif choice == '9':
                show_mood_statistics()
                input("\nPress Enter to return to menu...")
                continue

            elif choice == 'j':  # ← ジャケット画像選曲
                # グローバル状態をリセット
                cleanup_processes()
                current_folder_tracks = []
                current_playlist = []
                
                print("\n🖼️  Browse by cover art")
                print("=" * 60)
                print("Select:")
                print("  1. Browse all albums by cover art")
                print("  2. Filter first, then browse by cover art")
                
                cover_choice = input("\nSelect (1/2): ").strip()
                
                if cover_choice == '1':
                    # ★★★ 修正箇所 ★★★
                    select_album_by_cover_image_loop(mode='all')
                
                elif cover_choice == '2':
                    print("\n📋 Select a filter:")
                    print("  1. Filter by composer")
                    print("  2. Filter by genre")
                    print("  3. Filter by mood")
                    print("  4. Filter by keyword search")  # ★★★ 追加 ★★★
        
                    filter_choice = input("\nSelect (1-4): ").strip()
                    filters = {}
        
                    if filter_choice == '1':
                        options = get_available_options(mode='composer')
                        if options:
                            composer = interactive_search_with_curses(options, mode='composer')
                            if composer:
                                filters['composer'] = composer
        
                    elif filter_choice == '2':
                        options = get_available_options(mode='genre')
                        if options:
                            genre = interactive_search_with_curses(options, mode='genre')
                            if genre:
                                filters['genre'] = genre
        
                    elif filter_choice == '3':
                        options = get_available_options(mode='mood')
                        if options:
                            mood = interactive_search_with_curses(options, mode='mood')
                            if mood:
                                filters['mood'] = mood
                    
                    # ★★★ キーワード検索を追加 ★★★
                    elif filter_choice == '4':
                        keyword = input("\n🔍 Enter keyword: ").strip()
                        if keyword:
                            filters['keyword'] = keyword
                            print(f"✓ Filtering by keyword '{keyword}'")
                        else:
                            print("⚠️ No keyword entered")
        
                    if filters:
                        # ★★★ 修正箇所 ★★★
                        select_album_by_cover_image_loop(mode='filtered', filters=filters)
                    else:
                        print("⚠️ No filter specified")

            elif choice == 'n':  # ★★★ 最近追加した音源 ジャケット選曲 ★★★
                cleanup_processes()
                current_folder_tracks = []
                current_playlist = []

                print("\n🆕  Recently added — cover-art browser")
                print("=" * 60)
                n_input = input("Number of sets to show (default 10): ").strip()
                try:
                    n_count = int(n_input) if n_input.isdigit() and int(n_input) > 0 else 10
                except:
                    n_count = 10
                select_recently_added_jacket_mode(n=n_count)

            elif choice == 'm':  # ★★★ Now Playing ミラーサーバー ★★★
                if now_playing_server_running:
                    stop_now_playing_server()
                    print("\n📱 Now Playing mirror stopped")
                else:
                    start_now_playing_server()
                continue

            elif choice == 'r':  # ★★★ ラジオステーション ★★★
                radio_menu()
                continue

            elif choice == 'ap':  # ★★★ AirPlay レシーバー ★★★
                play_airplay_stream()
                continue

            elif choice == 'dl':  # ★★★ UPnP/DLNA レシーバー ★★★
                play_gmediarender_stream()
                continue

            elif choice == 'p':  # ★★★ プリセット管理 ★★★
                preset_management_menu()
                continue

            elif choice == 'k':  # ★★★ Favorites ★★★
                favorites_menu()
                continue

            elif choice == 'qb':
                # ★★★ Qobuz ストリーミング ★★★
                import qji_qobuzdsp as qji_qobuz
                _gain_db = GAIN_PRESETS.get(current_gain_preset, 0.0)
                _loudness = "loudnorm=I=-16:TP=-2.0:LRA=11," if loudness_normalization else ""
                qji_qobuz.run(
                    build_filter_func=_build_audio_filter_args,
                    gain_preset=current_gain_preset,
                    tinnitus=tinnitus_reduction_mode,
                    musikverein=musikverein_room_effects,
                    loudness_filter=_loudness,
                    eq_part="",
                    volume=CURRENT_VOLUME,
                    air_layer=air_particle_layer,
                    echo_mode=musikverein_echo_mode,
                    output_device=output_device,
                )

            elif choice == 's':   # ★★★ SoundCloud ストリーミング ★★★
                import qji_soundcloud
                _loudness = "loudnorm=I=-16:TP=-2.0:LRA=11," if loudness_normalization else ""
                qji_soundcloud.run(
                    build_filter_func=_build_audio_filter_args,
                    gain_preset=current_gain_preset,
                    tinnitus=tinnitus_reduction_mode,
                    musikverein=musikverein_room_effects,
                    loudness_filter=_loudness,
                    eq_part="",
                    volume=CURRENT_VOLUME,
                    air_layer=air_particle_layer,
                    echo_mode=musikverein_echo_mode,
                    output_device=output_device,
                )
 
            elif choice == 'y':   # ★★★ YouTube Music ストリーミング ★★★
                import qji_ytmusic
                _loudness = "loudnorm=I=-16:TP=-2.0:LRA=11," if loudness_normalization else ""
                qji_ytmusic.run(
                    build_filter_func=_build_audio_filter_args,
                    gain_preset=current_gain_preset,
                    tinnitus=tinnitus_reduction_mode,
                    musikverein=musikverein_room_effects,
                    loudness_filter=_loudness,
                    eq_part="",
                    volume=CURRENT_VOLUME,
                    air_layer=air_particle_layer,
                    echo_mode=musikverein_echo_mode,
                    output_device=output_device,
                )

            elif choice == 'x':
                reinitialize_dsp_output()
                continue

            elif choice == 'q':
                print("👋 Exiting Qji Music Player")
                stop_now_playing_server()  # ★★★ ミラーサーバー停止 ★★★
                break

            else:
                print("⚠️ Invalid selection")
        
        except (EOFError, KeyboardInterrupt):
            print("\n👋 Exiting Qji Music Player")
            break
        except Exception as e:
            print(f"⚠️ Error: {e}")
            # エラー時もバッファクリア
            try:
                import termios
                termios.tcflush(sys.stdin, termios.TCIFLUSH)
            except:
                pass

    # ★★★ 終了時の完全クリーンアップ ★★★
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
        old_settings = termios.tcgetattr(sys.stdin)
        termios.tcsetattr(sys.stdin, termios.TCSANOW, old_settings)
    except:
        pass
    
    cleanup_processes()
    # ★ os._exit() は atexit ハンドラを実行しないため、ここで明示的にDSPを停止する
    shutdown_dsp()
    print("✅ System exited cleanly")
    # ★★★ サウンドデバイス等の内部スレッドによるハングを防ぐため強制終了 ★★★
    os._exit(0)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Qji Music Player (with cover-art browser)")
    parser.add_argument('--tempo', type=int, help='Specify tempo (BPM) directly (can be combined)')
    parser.add_argument('--tempo-tol', type=int, default=10, help='Tempo tolerance (±BPM, default 10)')
    parser.add_argument('--composer', type=str, help='Specify composer name (can be combined)')
    parser.add_argument('--performer', type=str, help='Specify performer name (can be combined)')
    parser.add_argument('--conductor', type=str, help='Specify conductor name (can be combined)')
    parser.add_argument('--genre', type=str, help='Specify genre name (can be combined)')
    parser.add_argument('--mood', type=str, help='Specify mood (can be combined)')
    parser.add_argument('--device', type=str, choices=['hw:0,0', 'hw:1,0', 'hw:2,0', 'hw:3,0', 'bluealsa'],
                        help='Specify output device')
    parser.add_argument('--mic-device', type=int, help='Manually specify microphone device ID')
    parser.add_argument('--no-voice', dest='voice', action='store_false',
                        help='Disable voice recognition at startup (no microphone)')
    parser.set_defaults(voice=True)
    args = parser.parse_args()

    try:
        # --no-voice 指定時はコマンドラインで即座に無効化
        if not args.voice:
            VOICE_RECOGNITION_ENABLED = False
            print("🔇 Voice recognition: disabled (--no-voice)")

        if args.device:
            output_device = args.device
            print(f"🔊 Output device: {output_device}")

        # ★★★ 起動スプラッシュ ★★★
        show_splash_screen()

        # ★★★ 起動時サウンドカード選択（--device 未指定時のみ） ★★★
        if not args.device:
            select_output_device_interactive()

        # ★★★ DSPモード選択 ★★★
        import subprocess as _sp, time as _time, builtins as _bi, shutil as _sh
        _HOME = os.path.expanduser("~")  # ユーザーホームディレクトリ（汎用化）
        _bi._cdsp_proc = None
        _bi._wobble_proc = None
        # ★ Loopbackデバイスかどうかを/proc/asound/cardsで確認（カード番号に依存しない）
        def _is_loopback(dev):
            if 'Loopback' in dev:
                return True
            import re as _re_lb
            m = _re_lb.match(r'hw:(\d+)', dev)
            if not m:
                return False
            card_num = m.group(1)
            try:
                with open('/proc/asound/cards', 'r') as _f:
                    for _line in _f:
                        _m = _re_lb.match(r'^\s*(\d+)\s+\[([^\]]+)\]', _line)
                        if _m and _m.group(1) == card_num and 'Loopback' in _m.group(2):
                            return True
            except Exception:
                pass
            return False
        if _is_loopback(output_device):
            _yml_map = {
                '1': f'{_HOME}/qjidsp_backup_v1/spatial_final.yml',
                '2': f'{_HOME}/qjidsp_backup_v2/spatial_final.yml',
                '3': f'{_HOME}/qjidsp_backup_v3/spatial_final.yml',
                '4': f'{_HOME}/qjidsp_backup_v4/spatial_final.yml',
                '5': f'{_HOME}/qjidsp_backup_v5/spatial_final.yml',
                '6': f'{_HOME}/qjidsp_backup_v6/spatial_final.yml',
            }
            _wobble_map = {
                '1': f'{_HOME}/camilladsp_test/wobble_v1.py',
                '2': f'{_HOME}/camilladsp_test/wobble_v2.py',
                '3': f'{_HOME}/camilladsp_test/wobble_v3_static.py',
                '4': f'{_HOME}/camilladsp_test/wobble_v4.py',
                '5': f'{_HOME}/camilladsp_test/wobble_v5.py',
                '6': f'{_HOME}/camilladsp_test/wobble_v5_harmonics_hp.py',
            }
            # ★ Loopback選択時はDSPモードの使用が前提のため、「DSPなしで起動」は
            #   選択肢として提示しない（音が出ない矛盾した状態を防ぐ）。
            #   1〜6のいずれかを選ぶまでループする。
            _dsp_choice = ''
            while _dsp_choice not in _yml_map:
                print("\n" + "=" * 60)
                print("🎛️  3D DSP mode detected")
                print("=" * 60)
                print("  1) v1 Rich hall (still)   — full EQ, calm reverberance")
                print("  2) v2 Rich hall (moving)  — full EQ, sense of air movement")
                print("  3) v3 Natural tone (still)   — simple, focus on realism")
                print("  4) v4 Natural tone (moving)  — simple, washitsu-style panning")
                print("  5) v5 Harmonics mode        — violin resonance / shakuhachi-style overtones")
                print("  6) v6 Harmonics mode (headphones) — tuned for closed/open headphones")
                print("=" * 60)
                _dsp_choice = input("Select DSP mode (1/2/3/4/5/6) → ").strip()
                if _dsp_choice not in _yml_map:
                    print("⚠️ Please select 1–6 (DSP startup is required when Loopback is selected)")
            if True:
                # ★ {HOME}プレースホルダを実際のホームパスへ展開してからコピーする
                #   （単純な shutil.copy だと {HOME} が文字列のまま残り、
                #    別環境ではCamillaDSPがファイルを見つけられずに即死するため）
                with open(_yml_map[_dsp_choice], 'r', encoding='utf-8') as _f_src:
                    _yml_content = _f_src.read()
                _yml_content = _yml_content.replace('{HOME}', _HOME)
                with open(f'{_HOME}/camilladsp_test/spatial_final.yml', 'w', encoding='utf-8') as _f_dst:
                    _f_dst.write(_yml_content)
                # ★ DAC一覧の取得・選択・yml書き換えは共通関数を使う
                #   （「DSP出力デバイスの再認識」メニューと処理を共通化するため）
                _dac_list = _scan_dac_list()
                _dac_device, _dac_info = _select_dac_interactive(_dac_list)
                _write_dsp_output_device(_dac_device, _dac_info)

                _cdsp_log = open('/tmp/camilladsp.log', 'w')
                _bi._cdsp_proc = _sp.Popen(
                    ['camilladsp', f'{_HOME}/camilladsp_test/spatial_final.yml', '--port', '1234'],
                    stdout=_cdsp_log, stderr=_cdsp_log
                )
                _time.sleep(3.0)
                _wobble_log = open('/tmp/wobble.log', 'w')
                _bi._wobble_proc = _sp.Popen(
                    ['python3', _wobble_map[_dsp_choice]],
                    stdout=_wobble_log, stderr=_wobble_log
                )
                _bi._wobble_script_path = _wobble_map[_dsp_choice]  # ★ 再認識メニューで再利用
                # ★ ウォッチドッグ起動（CamillaDSP死活監視）
                _watchdog_log = open('/tmp/cdsp_watchdog.log', 'w')
                _bi._watchdog_proc = _sp.Popen(
                    ['python3', f'{_HOME}/camilladsp_test/cdsp_watchdog.py'],
                    stdout=_watchdog_log, stderr=_watchdog_log
                )
                _time.sleep(1.0)
                print(f"🎛️  DSP v{_dsp_choice} started")
                dsp_mode_active = True
                # ★ CamillaDSP/wobbleはここで一度だけ起動する常駐プロセス。
                #   プログラム終了時にのみ確実に停止させる（途中のcleanup_processes()では止めない）
                atexit.register(shutdown_dsp)

        # ★★★ 起動時マイク設定（--no-voice 未指定時のみ） ★★★
        if args.voice:
            select_voice_recognition_interactive()

        # ★★★ マイク設定確定後にUSBマイク検出 ★★★
        if VOICE_RECOGNITION_AVAILABLE and VOICE_RECOGNITION_ENABLED:
            if args.mic_device is not None:
                USB_MIC_DEVICE_ID = args.mic_device
                print(f"🎤 Manually specified mic device: {USB_MIC_DEVICE_ID}")
            else:
                detect_usb_microphone()

        cli_filters = {}
        if args.tempo:
            cli_filters['tempo'] = (args.tempo, args.tempo_tol)
        if args.composer:
            cli_filters['composer'] = args.composer
        if args.performer:
            cli_filters['performer'] = args.performer
        if args.conductor:
            cli_filters['conductor'] = args.conductor
        if args.genre:
            cli_filters['genre'] = args.genre
        if args.mood:
            cli_filters['mood'] = args.mood

        if cli_filters:
            print(f"🔍 Searching with CLI filters: {cli_filters}")
            playlist = get_tracks_by_filters(cli_filters, limit=500)
            if playlist:
                print(f"🎵 Playing {len(playlist)} matching track(s)")
                play_music_with_mode_switching(playlist)
            else:
                print("⚠ No tracks matched the specified filters")
        else:
            print("🎵 Starting Qji Music Player")
            interactive_mode()

    except KeyboardInterrupt:
        print("\n👋 Exiting")
    except Exception as e:
        print(f"⚠ An error occurred: {e}")
        import traceback
        traceback.print_exc()
