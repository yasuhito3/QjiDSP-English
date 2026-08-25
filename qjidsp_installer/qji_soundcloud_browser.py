#!/usr/bin/env python3
"""
qji_soundcloud_browser.py  —  SoundCloud ブラウザUI  (ポート 8081)

機能:
  ・トラック / プレイリスト 検索
  ・プレイリスト詳細（トラック一覧）
  ・お気に入り（qji_soundcloud_favorites.json）
  ・ブラウザ上でトラック選択 → ターミナルで即再生
"""

import json, time, threading, subprocess, sys, re
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, unquote
from typing import Optional, List

PORT          = 8081
FAVS_PATH        = Path.home() / '.config' / 'qji_soundcloud_favorites.json'
PLAYLISTS_PATH   = Path.home() / '.config' / 'qji_soundcloud_playlists.json'
CONFIG_PATH   = Path.home() / '.config' / 'qji_soundcloud.json'
REQUEST_PATH  = Path('/tmp/qji_soundcloud_request.json')
SC_API_BASE   = 'https://api-v2.soundcloud.com'
SC_AGENT      = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                 '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

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
GAIN_PRESET_LABELS = {
    'classical': 'Classical', 'general': 'General',
    'jazz_pop':  'Jazz/Pop',  'loud':    'Loud',
}

_client_id_cache: Optional[str] = None
_server        = None
_server_thread = None
_CLOSE_SIGNAL  = threading.Event()


# ═══════════════════════════════════════════════════════════════════════════
# 設定・client_id
# ═══════════════════════════════════════════════════════════════════════════

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    except Exception:
        return {}


def _get_client_id() -> Optional[str]:
    global _client_id_cache
    if _client_id_cache:
        return _client_id_cache
    cfg = _load_config()
    cid = cfg.get('client_id', '')
    age = (time.time() - cfg.get('client_id_fetched_at', 0)) / 3600
    if cid and age < 24:
        _client_id_cache = cid
        return cid
    # 再抽出
    new_cid = _extract_client_id()
    if new_cid:
        cfg['client_id'] = new_cid
        cfg['client_id_fetched_at'] = time.time()
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        except Exception:
            pass
        _client_id_cache = new_cid
        return new_cid
    if cid:
        _client_id_cache = cid
        return cid
    return None


def _extract_client_id() -> Optional[str]:
    try:
        import requests
        r = requests.get('https://soundcloud.com',
                         headers={'User-Agent': SC_AGENT}, timeout=15)
        scripts = re.findall(
            r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"',
            r.text)
        for url in reversed(scripts[-8:]):
            try:
                sr = requests.get(url, headers={'User-Agent': SC_AGENT}, timeout=10)
                for pat in [
                    r'client_id:"([a-zA-Z0-9]{32})"',
                    r'client_id,"([a-zA-Z0-9]{32})"',
                    r'"client_id":"([a-zA-Z0-9]{32})"',
                    r'clientId:"([a-zA-Z0-9]{32})"',
                ]:
                    m = re.search(pat, sr.text)
                    if m:
                        return m.group(1)
            except Exception:
                continue
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════
# SoundCloud API ラッパー
# ═══════════════════════════════════════════════════════════════════════════

def _sc_get(endpoint: str, params: dict) -> Optional[dict]:
    cid = _get_client_id()
    if not cid:
        return None
    try:
        import requests
        p = {'client_id': cid, **params}
        r = requests.get(f'{SC_API_BASE}{endpoint}',
                         params=p, headers={'User-Agent': SC_AGENT}, timeout=12)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _normalize_art(url: Optional[str]) -> str:
    if not url:
        return ''
    return re.sub(r'-(large|t300x300|t67x67|small|badge|tiny|crop|t20x20)\.jpg',
                  '-t500x500.jpg', url)


def _dur_str(sec: int) -> str:
    if not sec:
        return ''
    return f'{int(sec) // 60}:{int(sec) % 60:02d}'


def _num_fmt(n: int) -> str:
    if n >= 1_000_000:
        return f'{n / 1_000_000:.1f}M'
    if n >= 1_000:
        return f'{n / 1_000:.1f}K'
    return str(n)


def search_tracks(query: str, limit: int = 24, offset: int = 0) -> dict:
    d = _sc_get('/search/tracks', {'q': query, 'limit': limit, 'offset': offset})
    if not d:
        return {'items': [], 'total': 0}
    items = []
    for t in d.get('collection', []):
        user = t.get('user') or {}
        items.append({
            'url':      t.get('permalink_url', ''),
            'id':       str(t.get('id', '')),
            'title':    t.get('title', ''),
            'artist':   user.get('username', ''),
            'art':      _normalize_art(t.get('artwork_url') or user.get('avatar_url')),
            'duration': int((t.get('duration') or 0) // 1000),
            'genre':    t.get('genre', ''),
            'likes':    t.get('likes_count', 0),
            'plays':    t.get('playback_count', 0),
        })
    return {'items': items, 'total': d.get('total_results', len(items))}


def search_playlists(query: str, limit: int = 16, offset: int = 0) -> dict:
    d = _sc_get('/search/playlists', {'q': query, 'limit': limit, 'offset': offset})
    if not d:
        return {'items': [], 'total': 0}
    items = []
    for p in d.get('collection', []):
        user = p.get('user') or {}
        items.append({
            'url':         p.get('permalink_url', ''),
            'id':          str(p.get('id', '')),
            'title':       p.get('title', ''),
            'artist':      user.get('username', ''),
            'art':         _normalize_art(p.get('artwork_url')),
            'track_count': p.get('track_count', 0),
            'duration':    int((p.get('duration') or 0) // 1000),
            'is_album':    p.get('is_album', False),
        })
    return {'items': items, 'total': d.get('total_results', len(items))}


def get_playlist_detail(playlist_url: str) -> Optional[dict]:
    """yt-dlp で プレイリスト詳細とトラックリストを取得する。"""
    import subprocess
    cmd = ['yt-dlp', '--flat-playlist', '--dump-json',
           '--no-warnings', '--quiet', playlist_url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        tracks = []
        for line in r.stdout.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                tracks.append({
                    'url':      item.get('url', '') or item.get('webpage_url', ''),
                    'id':       str(item.get('id', '')),
                    'title':    item.get('title', ''),
                    'artist':   item.get('uploader', item.get('channel', '')),
                    'art':      _normalize_art(item.get('thumbnail', '')),
                    'duration': int(item.get('duration') or 0),
                    'genre':    item.get('genre', ''),
                    'likes':    item.get('like_count', 0),
                })
            except (json.JSONDecodeError, KeyError):
                continue
        return {'tracks': tracks, 'url': playlist_url} if tracks else None
    except Exception:
        return None


def load_favorites() -> List[dict]:
    try:
        return json.loads(FAVS_PATH.read_text()) if FAVS_PATH.exists() else []
    except Exception:
        return []


def load_playlists() -> List[dict]:
    try:
        return json.loads(PLAYLISTS_PATH.read_text()) if PLAYLISTS_PATH.exists() else []
    except Exception:
        return []


def save_playlists(pls: List[dict]):
    from datetime import datetime
    PLAYLISTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAYLISTS_PATH.write_text(json.dumps(pls, ensure_ascii=False, indent=2))


def fetch_cover(url: str) -> bytes:
    if not url:
        return b''
    try:
        import requests
        r = requests.get(url, timeout=10, headers={'User-Agent': SC_AGENT})
        return r.content if r.status_code == 200 else b''
    except Exception:
        return b''


# ═══════════════════════════════════════════════════════════════════════════
# HTML/CSS
# ═══════════════════════════════════════════════════════════════════════════

def _esc(s: str) -> str:
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

def _js(s: str) -> str:
    return _esc(str(s)).replace("'", "&#39;")


CSS = """
:root{--bg:#080c10;--surf:#0f1318;--card:#141920;--acc:#1db954;--acc2:#1ed760;
--text:#e8edf2;--muted:#607080;--border:#1e2830;--hover:#1a2230;--gold:#f5a623;--red:#e05555}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
font-family:'Helvetica Neue','Hiragino Kaku Gothic ProN',sans-serif;min-height:100vh}
header{background:var(--surf);border-bottom:1px solid var(--border);
padding:11px 18px;display:flex;align-items:center;gap:10px;
position:sticky;top:0;z-index:200;flex-wrap:wrap}
.logo{font-size:1.05rem;font-weight:700;color:var(--acc2);white-space:nowrap}
.logo span{color:var(--muted);font-weight:400;font-size:.85rem}
.nav{display:flex;gap:5px;flex-wrap:wrap}
.nav a{padding:4px 11px;border-radius:16px;border:1px solid var(--border);
color:var(--muted);text-decoration:none;font-size:.76rem;white-space:nowrap;transition:all .15s}
.nav a:hover,.nav a.active{border-color:var(--acc);color:var(--acc2);background:rgba(29,185,84,.1)}
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
.stab.active,.stab:hover{border-color:var(--acc);color:var(--acc2);background:rgba(29,185,84,.1)}
.gw{padding:18px}
.gi{font-size:.76rem;color:var(--muted);margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:9px;
overflow:hidden;cursor:pointer;text-decoration:none;color:inherit;display:block;
transition:transform .15s,border-color .15s,box-shadow .15s}
.card:hover{transform:translateY(-3px);border-color:var(--acc);
box-shadow:0 5px 20px rgba(29,185,84,.15)}
.cov{width:100%;aspect-ratio:1;background:var(--surf);position:relative;overflow:hidden}
.cov img{width:100%;height:100%;object-fit:cover;display:block}
.ni{width:100%;height:100%;display:flex;align-items:center;justify-content:center;
font-size:2.5rem;color:var(--border)}
.pbadge{position:absolute;bottom:5px;right:5px;background:rgba(8,12,16,.85);
border:1px solid var(--border);border-radius:3px;padding:1px 5px;
font-size:.58rem;color:var(--acc2);font-weight:600}
.plist-badge{position:absolute;top:5px;left:5px;background:var(--acc);
border-radius:3px;padding:1px 6px;font-size:.58rem;color:#fff;font-weight:700}
.ci{padding:9px}
.ci .ttl{font-size:.8rem;font-weight:600;line-height:1.3;margin-bottom:2px;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ci .art{font-size:.7rem;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ci .meta{margin-top:5px;font-size:.62rem;color:var(--muted);display:flex;gap:8px;flex-wrap:wrap}
.ci .meta span{color:var(--acc2)}
.pgr{display:flex;gap:5px;justify-content:center;padding:20px 0;flex-wrap:wrap}
.pgr a,.pgr span{padding:5px 12px;border-radius:7px;border:1px solid var(--border);
text-decoration:none;color:var(--muted);font-size:.8rem}
.pgr a:hover{border-color:var(--acc);color:var(--acc2)}
.pgr span.cur{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:700}
.pgr span.dt{border:none}
/* プレイリスト詳細 */
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
text-decoration:none;color:inherit;margin-bottom:3px;transition:all .12s;
position:relative}
.tr:hover{background:var(--hover);border-color:var(--acc)}
.tr.selected{background:var(--hover)!important;border-color:var(--acc)!important;
box-shadow:0 0 0 2px rgba(29,185,84,.2)!important}
.tr .sel-cb{top:auto;bottom:8px;left:auto;right:12px;opacity:0}
.tr:hover .sel-cb,.tr .sel-cb:checked{opacity:1}
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
.fav-preset{font-size:.68rem;color:var(--acc2);margin-top:2px}
.fav-no{font-size:.7rem;color:var(--muted);font-family:monospace;flex-shrink:0}
/* empty state */
.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.empty .icon{font-size:3rem;margin-bottom:12px}
.empty p{font-size:.9rem}
/* toast */
#toast{position:fixed;bottom:80px;right:24px;background:var(--acc);color:#fff;
padding:10px 18px;border-radius:8px;font-size:.85rem;font-weight:600;
opacity:0;transition:opacity .3s;z-index:999;pointer-events:none}
#toast.show{opacity:1}
/* 選択チェックボックス */
.card{position:relative}
.sel-cb{position:absolute;top:6px;left:6px;width:17px;height:17px;
cursor:pointer;accent-color:var(--acc);z-index:10;
opacity:0;transition:opacity .15s}
.card:hover .sel-cb,.sel-cb:checked{opacity:1}
.card.selected{border-color:var(--acc)!important;
box-shadow:0 0 0 2px rgba(29,185,84,.4)!important}
.card.selected .sel-cb{opacity:1}
/* お気に入りのチェック・削除ボタン */
.fav-item .sel-cb{position:static;opacity:1;flex-shrink:0;margin-right:2px}
.fav-item.selected{border-color:var(--acc)!important;
box-shadow:0 0 0 2px rgba(29,185,84,.4)!important}
.fav-del{background:none;border:none;cursor:pointer;color:var(--red);
font-size:1.1rem;flex-shrink:0;padding:4px 6px;border-radius:4px;
opacity:.65;transition:opacity .15s}
.fav-del:hover{opacity:1;background:rgba(224,85,85,.12)}
.pl-item{cursor:pointer}
/* フローティングキューバー */
#queue-bar{position:fixed;bottom:0;left:0;right:0;
background:var(--surf);border-top:2px solid var(--acc);
padding:11px 20px;display:none;align-items:center;gap:12px;flex-wrap:wrap;
z-index:500;box-shadow:0 -4px 24px rgba(29,185,84,.2)}
#queue-bar.show{display:flex}
#queue-count{font-size:.88rem;font-weight:700;color:var(--acc2);flex:1;min-width:120px}
.qbtn{padding:7px 18px;border:none;border-radius:7px;
cursor:pointer;font-size:.8rem;font-weight:700;white-space:nowrap}
.qbtn-play{background:var(--acc);color:#fff}
.qbtn-play:hover{background:var(--acc2)}
.qbtn-up{background:var(--hover);border:1px solid var(--border);color:var(--text)}
.qbtn-up:hover{border-color:var(--acc);color:var(--acc2)}
.qbtn-clear{background:transparent;border:1px solid var(--border);color:var(--muted)}
.qbtn-clear:hover{border-color:var(--red);color:var(--red)}
"""

TOAST_JS = """
function toast(msg){
  var t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  setTimeout(function(){t.classList.remove('show')},2200);
}
function playTrack(url,title,artist,art,dur,genre){
  fetch('/play',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:url,title:title,artist:artist,cover_url:art,
                         duration:dur,genre:genre})})
  .then(r=>r.json()).then(d=>{
    if(d.ok) toast('▶ Playing: '+title.substring(0,30));
    else toast('⚠ '+d.error);
  }).catch(()=>toast('⚠ Connection error'));
}
function playFavorite(url,title,artist,art,dur,genre,audioJson){
  var audio={};
  try{ audio=JSON.parse(audioJson); }catch(e){}
  fetch('/play',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:url,title:title,artist:artist,cover_url:art,
                         duration:dur,genre:genre,audio_settings:audio})})
  .then(r=>r.json()).then(d=>{
    if(d.ok) toast('▶ Playing favourite: '+title.substring(0,30));
    else toast('⚠ '+d.error);
  }).catch(()=>toast('⚠ Connection error'));
}
function playPlaylist(tracksJson){
  var tracks=JSON.parse(tracksJson);
  fetch('/play',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({type:'playlist',tracks:tracks})})
  .then(r=>r.json()).then(d=>{
    if(d.ok) toast('▶ Playing playlist: '+tracks.length+' tracks');
    else toast('⚠ '+d.error);
  }).catch(()=>toast('⚠ Connection error'));
}
// ─── キュー管理 ───────────────────────────────────────────────
// localStorage からキューを復元（ページをまたいで保持）
var _queue=(function(){
  try{return JSON.parse(localStorage.getItem('sc_queue')||'[]');}
  catch(e){return [];}
})();
function _updateQueueBar(){
  // localStorage に保存（ページまたぎ対応）
  try{localStorage.setItem('sc_queue',JSON.stringify(_queue));}catch(e){}
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
  // .card / .fav-item / .tr すべてに対応
  return el.closest('.card')||el.closest('.fav-item')||el.closest('.tr');
}
function toggleSelect(event){
  event.stopPropagation();
  var cb=event.target;            // currentTarget ではなく target を使用（inline handler 対応）
  if(cb.type!=='checkbox') return; // 安全チェック
  var card=_cardOf(cb);
  var d=cb.dataset;
  if(cb.checked){
    if(!_queue.find(function(t){return t.url===d.url;})){
      _queue.push({url:d.url,title:d.title,artist:d.artist,
                   cover_url:d.art,duration:parseInt(d.dur)||0,genre:d.genre||''});
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
        _queue.push({url:d.url,title:d.title,artist:d.artist,
                     cover_url:d.art,duration:parseInt(d.dur)||0,genre:d.genre||''});
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
  try{localStorage.removeItem('sc_queue');}catch(e){}
  document.querySelectorAll('.sel-cb').forEach(function(cb){cb.checked=false;});
  document.querySelectorAll('.card.selected,.fav-item.selected,.tr.selected').forEach(function(c){c.classList.remove('selected');});
  _updateQueueBar();
}
// ─── お気に入り削除 ───────────────────────────────────────────
function deleteFav(event, url){
  event.stopPropagation();
  fetch('/api/delete_favorite',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({url:url})})
  .then(function(r){return r.json();})
  .then(function(d){
    if(d.ok){
      var btn=event.target;
      var item=btn.closest('.fav-item');
      if(item) item.remove();
      _queue=_queue.filter(function(t){return t.url!==url;});
      _updateQueueBar();
      toast('🗑 Deleted');
    } else {
      toast('⚠ Delete failed: '+(d.error||''));
    }
  }).catch(function(){toast('⚠ Connection error');});
}
// ─── お気に入り全曲再生 ───────────────────────────────────────
function playAllFavorites(){
  var el=document.getElementById('favs-data');
  if(!el){toast('⚠ No data');return;}
  try{
    var tracks=JSON.parse(el.textContent);
    if(!tracks.length){toast('⚠ No favourites');return;}
    playPlaylist(JSON.stringify(tracks));
  }catch(e){toast('⚠ Data load error');}
}
// ─── プレイリスト保存 ───────────────────────────────────────
function savePlaylistDialog(tracksJson, defaultName){
  var name=prompt('Enter playlist name:', defaultName||'');
  if(!name || !name.trim()){toast('⚠ Cancelled');return;}
  var tracks;
  try{tracks=JSON.parse(tracksJson);}catch(e){toast('⚠ Data error');return;}
  if(!tracks.length){toast('⚠ No tracks');return;}
  fetch('/api/save_playlist',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:name.trim(),tracks:tracks})})
  .then(function(r){return r.json();})
  .then(function(d){
    if(d.ok) toast('💾 "'+name.trim()+'" saved ('+tracks.length+' tracks)');
    else toast('⚠ Save failed: '+(d.error||''));
  }).catch(function(){toast('⚠ Connection error');});
}
function saveQueueAsPlaylist(){
  if(_queue.length===0){toast('⚠ No tracks selected');return;}
  savePlaylistDialog(JSON.stringify(_queue), '');
}
function deletePlaylist(event, no){
  event.stopPropagation();
  if(!confirm('Delete this playlist?')) return;
  fetch('/api/delete_playlist',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({no:no})})
  .then(function(r){return r.json();})
  .then(function(d){
    if(d.ok){
      var item=event.target.closest('.pl-item');
      if(item) item.remove();
      toast('🗑 Deleted');
    } else toast('⚠ Delete failed: '+(d.error||''));
  }).catch(function(){toast('⚠ Connection error');});
}
// セッション監視
setInterval(function(){
  fetch('/api/status').then(function(r){return r.json();}).then(function(d){
    if(d.close){window.close();}
  }).catch(function(){});
},1500);
/* ── ページロード時：localStorage のキューをチェックボックスに反映 ── */
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
          tab: str = 'tracks', nav_active: str = 'search') -> str:
    nav_items = [
        ('search', '🔍 Search', '/search'),
        ('favorites', '⭐ Favourites', '/favorites'),
        ('playlists', '📁 Playlists', '/my_playlists'),
    ]
    nav_html = ''.join(
        f'<a href="{href}" class="{"active" if k == nav_active else ""}">{label}</a>'
        for k, label, href in nav_items
    )
    tab_html = (
        '<div class="stabs">'
        f'<button class="stab {"active" if tab=="tracks" else ""}" '
        f'onclick="switchTab(\'tracks\')">🎵 Tracks</button>'
        f'<button class="stab {"active" if tab=="playlists" else ""}" '
        f'onclick="switchTab(\'playlists\')">📋 Playlists</button>'
        '</div>'
    ) if nav_active == 'search' else ''

    return f"""<!DOCTYPE html><html lang="ja"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>{CSS}</style></head><body>
<header>
  <div class="logo">🟠 SoundCloud <span>× Qji</span></div>
  <nav class="nav">{nav_html}</nav>
  {tab_html}
  <form class="sf" action="/search" method="get" onsubmit="sessionStorage.setItem('tab','tracks')">
    <input name="q" value="{_esc(search_val)}" placeholder="Artist / title / genre..."
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
  <button class="qbtn qbtn-up" onclick="saveQueueAsPlaylist()">💾 Save as playlist</button>
</div>
<script>
{TOAST_JS}
function switchTab(t){{
  sessionStorage.setItem('tab',t);
  var q=document.getElementById('sq').value;
  if(q) window.location='/search?q='+encodeURIComponent(q)+'&tab='+t;
}}
// タブ復元
(function(){{
  var saved=sessionStorage.getItem('tab');
  if(saved && window.location.search.indexOf('tab=')===-1 && saved!=='tracks'){{
    var q=document.getElementById('sq').value;
    if(q && window.location.pathname==='/search'){{
      window.location='/search?q='+encodeURIComponent(q)+'&tab='+saved;
    }}
  }}
}})();
</script>
</body></html>"""


# ─── ページ: 検索（トラック） ─────────────────────────────────────────────

def page_search_tracks(query: str, page: int = 1) -> str:
    PAGE_SIZE = 24
    offset    = (page - 1) * PAGE_SIZE
    result    = search_tracks(query, limit=PAGE_SIZE, offset=offset) if query else {'items': [], 'total': 0}
    items     = result['items']
    total     = result['total']

    if not query:
        body = '<div class="empty"><div class="icon">🎵</div><p>Enter a keyword to search</p></div>'
        return _page('SoundCloud × Qji', body, tab='tracks')

    if not items:
        body = f'<div class="empty"><div class="icon">🔍</div><p>No results for "{_esc(query)}"</p></div>'
        return _page(f'{query} — SoundCloud', body, search_val=query, tab='tracks')

    cards = []
    for t in items:
        art_html = (f'<img src="/cover?url={quote(t["art"])}" loading="lazy" alt="">'
                    if t['art'] else '<div class="ni">🎵</div>')
        dur_badge = (f'<div class="pbadge">{_dur_str(t["duration"])}</div>'
                     if t['duration'] else '')
        likes = f'<span>♥ {_num_fmt(t["likes"])}</span>' if t['likes'] else ''
        plays = f'<span>▶ {_num_fmt(t["plays"])}</span>' if t['plays'] else ''
        genre = f'<span>{_esc(t["genre"])}</span>' if t['genre'] else ''
        # data-* 属性にトラック情報を持たせ、チェックボックスで選択
        cb_html = (
            f'<input type="checkbox" class="sel-cb"'
            f' data-url="{_esc(t["url"])}"'
            f' data-title="{_esc(t["title"])}"'
            f' data-artist="{_esc(t["artist"])}"'
            f' data-art="{_esc(t["art"])}"'
            f' data-dur="{t["duration"]}"'
            f' data-genre="{_esc(t["genre"])}"'
            f' onclick="toggleSelect(event)">'
        )
        cards.append(
            f'<div class="card" onclick="playTrack(\'{_js(t["url"])}\',\'{_js(t["title"])}\','
            f'\'{_js(t["artist"])}\',\'{_js(t["art"])}\',{t["duration"]},\'{_js(t["genre"])}\')">'
            f'{cb_html}'
            f'<div class="cov">{art_html}{dur_badge}</div>'
            f'<div class="ci">'
            f'<div class="ttl">{_esc(t["title"])}</div>'
            f'<div class="art">{_esc(t["artist"])}</div>'
            f'<div class="meta">{likes}{plays}{genre}</div>'
            f'</div></div>'
        )

    pages_html = _paginator(query, page, total, PAGE_SIZE, 'tracks')

    # 全曲再生用 JSON
    all_tracks_json = json.dumps([
        {'url': t['url'], 'title': t['title'], 'artist': t['artist'],
         'cover_url': t['art'], 'duration': t['duration'], 'genre': t['genre']}
        for t in items
    ], ensure_ascii=False).replace("'", "&#39;").replace('"', '&quot;')

    toolbar = (
        f'<div style="display:flex;align-items:center;gap:8px;'
        f'margin-bottom:14px;flex-wrap:wrap">'
        f'<div class="gi" style="margin:0">🎵 Tracks — approx. {total:,} / showing '
        f'{offset+1}–{offset+len(items)}</div>'
        # 全選択/解除
        f'<button onclick="selectAllToggle()" '
        f'style="padding:4px 12px;background:var(--hover);border:1px solid var(--border);'
        f'border-radius:7px;color:var(--muted);cursor:pointer;font-size:.76rem;'
        f'white-space:nowrap">☑ Select all / Deselect</button>'
        # 全曲再生
        f'<button onclick="playPlaylist(\'{all_tracks_json}\')" '
        f'style="margin-left:auto;padding:5px 16px;background:var(--hover);'
        f'border:1px solid var(--border);border-radius:7px;color:var(--muted);'
        f'cursor:pointer;font-size:.8rem;font-weight:600;white-space:nowrap">'
        f'▶ Play all {len(items)}</button>'
        f'</div>'
    )
    body = f'<div class="gw">{toolbar}<div class="grid">{"".join(cards)}</div>{pages_html}</div>'
    return _page(f'{query} — SoundCloud', body, search_val=query, tab='tracks')


# ─── ページ: 検索（プレイリスト） ────────────────────────────────────────

def page_search_playlists(query: str, page: int = 1) -> str:
    PAGE_SIZE = 16
    offset    = (page - 1) * PAGE_SIZE
    result    = search_playlists(query, limit=PAGE_SIZE, offset=offset) if query else {'items': [], 'total': 0}
    items     = result['items']
    total     = result['total']

    if not query:
        body = '<div class="empty"><div class="icon">📋</div><p>Enter a keyword to search</p></div>'
        return _page('SoundCloud × Qji', body, tab='playlists')

    if not items:
        body = f'<div class="empty"><div class="icon">🔍</div><p>No playlists found for "{_esc(query)}"</p></div>'
        return _page(f'{query} — SoundCloud', body, search_val=query, tab='playlists')

    cards = []
    for p in items:
        art_html = (f'<img src="/cover?url={quote(p["art"])}" loading="lazy" alt="">'
                    if p['art'] else '<div class="ni">📋</div>')
        kind = 'Album' if p.get('is_album') else 'Playlist'
        tc   = f'<span>{p["track_count"]} tracks</span>' if p['track_count'] else ''
        dur  = f'<span>{_dur_str(p["duration"])}</span>' if p['duration'] else ''
        cards.append(
            f'<a class="card" href="/playlist?url={quote(p["url"])}">'
            f'<div class="cov">{art_html}'
            f'<div class="plist-badge">{kind}</div>'
            f'</div>'
            f'<div class="ci">'
            f'<div class="ttl">{_esc(p["title"])}</div>'
            f'<div class="art">{_esc(p["artist"])}</div>'
            f'<div class="meta">{tc}{dur}</div>'
            f'</div></a>'
        )

    pages_html = _paginator(query, page, total, PAGE_SIZE, 'playlists')
    info = f'<div class="gi">📋 Playlists — approx. {total:,} / showing {offset+1}–{offset+len(items)}</div>'
    body = f'<div class="gw">{info}<div class="grid">{"".join(cards)}</div>{pages_html}</div>'
    return _page(f'{query} — SoundCloud', body, search_val=query, tab='playlists')


# ─── ページ: プレイリスト詳細 ─────────────────────────────────────────────

def page_playlist(playlist_url: str) -> str:
    if not playlist_url:
        return _page('Error', '<div class="empty"><p>No URL specified</p></div>')

    loading_msg = ('<div class="empty" id="loading">'
                   '<div class="icon">⏳</div>'
                   '<p>Loading playlist...</p></div>')

    # yt-dlp で取得
    detail = get_playlist_detail(playlist_url)
    if not detail or not detail.get('tracks'):
        return _page('Playlist', '<div class="empty"><div class="icon">⚠</div>'
                     '<p>Could not fetch tracks. Please check the URL.</p></div>')

    tracks = detail['tracks']
    total_dur = sum(t.get('duration', 0) for t in tracks)
    playlist_name = playlist_url.rstrip('/').split('/')[-1].replace('-', ' ').title()

    # 先頭のアートワーク
    first_art = next((t['art'] for t in tracks if t.get('art')), '')
    art_html = (f'<img src="/cover?url={quote(first_art)}" alt="">'
                if first_art else '<div class="ni2">📋</div>')

    # 全トラックを JSON に変換してボタンへ埋め込む
    tracks_json_html = json.dumps([
        {'url': t['url'], 'title': t['title'], 'artist': t['artist'],
         'cover_url': t['art'], 'duration': t['duration'], 'genre': t['genre']}
        for t in tracks
    ], ensure_ascii=False).replace("'", "&#39;").replace('"', '&quot;')

    rows = []
    for i, t in enumerate(tracks, 1):
        dur_html  = f'<span class="tdur">{_dur_str(t["duration"])}</span>' if t['duration'] else ''
        art_thumb = (f'<img src="/cover?url={quote(t["art"])}" '
                     f'style="width:36px;height:36px;border-radius:4px;object-fit:cover;'
                     f'border:1px solid var(--border);flex-shrink:0" loading="lazy" alt="">'
                     if t.get('art') else '')
        cb_html = (
            f'<input type="checkbox" class="sel-cb"'
            f' data-url="{_esc(t["url"])}"'
            f' data-title="{_esc(t["title"])}"'
            f' data-artist="{_esc(t["artist"])}"'
            f' data-art="{_esc(t.get("art",""))}"'
            f' data-dur="{t.get("duration",0)}"'
            f' data-genre="{_esc(t.get("genre",""))}"'
            f' onclick="toggleSelect(event)">'
        )
        rows.append(
            f'<div class="tr" onclick="playTrack(\'{_js(t["url"])}\',\'{_js(t["title"])}\','
            f'\'{_js(t["artist"])}\',\'{_js(t["art"])}\',{t["duration"]},\'{_js(t["genre"])}\')">'
            f'{cb_html}'
            f'<span class="tno">{i}</span>'
            f'<span class="pic">▶</span>'
            f'{art_thumb}'
            f'<span class="ttl2">{_esc(t["title"])}</span>'
            f'<span class="tar">{_esc(t["artist"])}</span>'
            f'{dur_html}</div>'
        )

    # 選択ツールバー（トラック一覧の上）
    pl_toolbar = (
        f'<div style="display:flex;align-items:center;gap:8px;'
        f'margin-bottom:10px;flex-wrap:wrap;padding:6px 0">'
        f'<span style="font-size:.76rem;color:var(--muted)">'
        f'🎵 {len(tracks)} tracks</span>'
        f'<button onclick="selectAllToggle()" '
        f'style="padding:3px 11px;background:var(--hover);border:1px solid var(--border);'
        f'border-radius:7px;color:var(--muted);cursor:pointer;font-size:.74rem;white-space:nowrap">'
        f'☑ Select all / Deselect</button>'
        f'</div>'
    )

    body = f"""<div class="pp">
<a class="pm" href="javascript:history.back()" style="display:inline-flex;align-items:center;
gap:4px;padding:5px 12px;background:var(--hover);border:1px solid var(--border);
border-radius:7px;color:var(--muted);text-decoration:none;font-size:.78rem;margin-bottom:14px">
← Back</a>
<div class="ph">
  {art_html}
  <div class="pm">
    <h2>{_esc(playlist_name)}</h2>
    <div class="par">SoundCloud Playlist</div>
    <div class="pstat">{len(tracks)} tracks  ·  {_dur_str(total_dur)}</div>
    <button class="ptag" onclick="playPlaylist('{tracks_json_html}')">
      ▶ Play all
    </button>
    <button class="ptag" style="background:var(--hover);border:1px solid var(--border);"
      onclick="savePlaylistDialog('{tracks_json_html}', '{playlist_name}')">
      💾 Save playlist
    </button>
    <a class="pback" href="{_esc(playlist_url)}" target="_blank" rel="noopener">
      🔗 Open on SoundCloud
    </a>
  </div>
</div>
{pl_toolbar}
{"".join(rows)}
</div>"""
    return _page(f'{playlist_name} — SoundCloud', body)


# ─── ページ: お気に入り ────────────────────────────────────────────────────

def page_favorites() -> str:
    favs = load_favorites()
    if not favs:
        body = ('<div class="empty"><div class="icon">⭐</div>'
                '<p>No favourites yet.<br>Press [s] during playback to add tracks.</p></div>')
        return _page('Favourites — SoundCloud', body, nav_active='favorites')

    rows = []
    for f in favs:
        art_html = (f'<img class="fav-art" src="/cover?url={quote(f.get("cover_url",""))}" '
                    f'loading="lazy" alt="">'
                    if f.get('cover_url') else '<div class="fav-ni">🎵</div>')
        dur   = _dur_str(f.get('duration', 0))
        audio = f.get('audio_settings') or {}
        # 音場プリセット
        fp_lbl  = FILTER_PRESET_LABELS.get(audio.get('filter_preset', ''), '')
        # ゲインプリセット（gain_preset → dB）
        gp      = audio.get('gain_preset', '')
        gp_db   = GAIN_PRESETS_DB.get(gp)
        gp_lbl  = (f'🔊{gp_db:+.1f}dB' if gp_db is not None else '')
        # 出力音量
        vol     = audio.get('volume')
        vol_lbl = (f'Vol:{vol}dB' if vol is not None else '')
        parts   = [x for x in [fp_lbl, gp_lbl, vol_lbl] if x]
        preset_display = '  '.join(parts)
        # audio_settingsをJSON文字列としてJSに渡す
        audio_json = json.dumps(audio, ensure_ascii=False)\
                         .replace('\\', '\\\\')\
                         .replace("'", "&#39;")\
                         .replace('"', '&quot;')
        # チェックボックス（キュー追加用）
        cb_html = (
            f'<input type="checkbox" class="sel-cb"'
            f' data-url="{_esc(f["url"])}"'
            f' data-title="{_esc(f["title"])}"'
            f' data-artist="{_esc(f["artist"])}"'
            f' data-art="{_esc(f.get("cover_url",""))}"'
            f' data-dur="{f.get("duration",0)}"'
            f' data-genre="{_esc(f.get("genre",""))}"'
            f' onclick="toggleSelect(event)">'
        )
        # 削除ボタン（ゴミ箱）
        del_btn = (
            f'<button class="fav-del" title="Remove"'
            f' onclick="deleteFav(event,\'{_js(f["url"])}\')">🗑</button>'
        )
        rows.append(
            f'<div class="fav-item" onclick="playFavorite(\'{_js(f["url"])}\','
            f'\'{_js(f["title"])}\',\'{_js(f["artist"])}\','
            f'\'{_js(f.get("cover_url",""))}\',{f.get("duration",0)},'
            f'\'{_js(f.get("genre",""))}\',\'{audio_json}\')">'
            f'{cb_html}'
            f'{art_html}'
            f'<div class="fav-info">'
            f'<div class="fav-title">{_esc(f["title"])}</div>'
            f'<div class="fav-artist">{_esc(f["artist"])}  {dur}</div>'
            f'<div class="fav-preset">{preset_display}</div>'
            f'</div>'
            f'<span class="fav-no">#{f.get("no",0):03d}</span>'
            f'{del_btn}'
            f'</div>'
        )

    # 全曲再生用 JSON（<script type="application/json"> タグに埋め込む）
    favs_json_str = json.dumps([
        {'url': f['url'], 'title': f['title'], 'artist': f['artist'],
         'cover_url': f.get('cover_url', ''), 'duration': f.get('duration', 0),
         'genre': f.get('genre', '')}
        for f in favs
    ], ensure_ascii=False)

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
        f'<button onclick="savePlaylistDialog(JSON.stringify(JSON.parse(document.getElementById(\'favs-data\').textContent)), \'Favourites\')" '
        f'style="padding:5px 14px;background:var(--hover);'
        f'border:1px solid var(--border);border-radius:7px;color:var(--muted);'
        f'cursor:pointer;font-size:.8rem;white-space:nowrap">'
        f'💾 Save as playlist</button>'
        f'<button onclick="saveQueueAsPlaylist()" '
        f'style="padding:5px 14px;background:var(--hover);'
        f'border:1px solid var(--border);border-radius:7px;color:var(--muted);'
        f'cursor:pointer;font-size:.8rem;white-space:nowrap">'
        f'📝 Save selected</button>'
        f'</div>'
    )
    favs_data_tag = f'<script id="favs-data" type="application/json">{favs_json_str}</script>'

    body = f'<div class="gw">{toolbar}{"".join(rows)}</div>{favs_data_tag}'
    return _page('Favourites — SoundCloud', body, nav_active='favorites')


# ─── ページ: 保存済みプレイリスト ────────────────────────────────────────────

def page_my_playlists() -> str:
    from datetime import datetime
    pls = load_playlists()
    if not pls:
        body = ('<div class="empty"><div class="icon">📁</div>'
                '<p>No saved playlists yet.<br>'
                'Use "💾 Save playlist" on a playlist detail page, or<br>'
                '"💾 Save as playlist" on the favourites page.</p></div>')
        return _page('My Playlists — SoundCloud', body, nav_active='playlists')

    rows = []
    for p in pls:
        tracks = p.get('tracks', [])
        first_art = next((t.get('cover_url', t.get('art', '')) for t in tracks if t.get('cover_url') or t.get('art')), '')
        art_html = (f'<img class="fav-art" src="/cover?url={quote(first_art)}" loading="lazy" alt="">'
                    if first_art else '<div class="fav-ni">📁</div>')
        saved = p.get('saved_at', '')[:10]
        # tracks JSON for play button
        tracks_json = json.dumps([
            {'url': t.get('url',''), 'title': t.get('title',''), 'artist': t.get('artist',''),
             'cover_url': t.get('cover_url', t.get('art','')), 'duration': t.get('duration',0), 'genre': t.get('genre','')}
            for t in tracks
        ], ensure_ascii=False).replace("'", "&#39;").replace('"', '&quot;')
        no = p.get('no', 0)
        del_btn = (
            f'<button class="fav-del" title="Remove"'
            f' onclick="deletePlaylist(event,{no})">🗑</button>'
        )
        rows.append(
            f'<div class="fav-item pl-item" onclick="playPlaylist(\'{tracks_json}\')">'
            f'{art_html}'
            f'<div class="fav-info">'
            f'<div class="fav-title">{_esc(p["name"])}</div>'
            f'<div class="fav-artist">{len(tracks)} tracks  {saved}</div>'
            f'</div>'
            f'<span class="fav-no">#{no:03d}</span>'
            f'{del_btn}'
            f'</div>'
        )

    toolbar = (
        f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px;flex-wrap:wrap">'
        f'<div class="gi" style="margin:0">📁 My Playlists — {len(pls)}</div>'
        f'</div>'
    )
    body = f'<div class="gw">{toolbar}{"".join(rows)}</div>'
    return _page('My Playlists — SoundCloud', body, nav_active='playlists')


# ─── ページネーター ────────────────────────────────────────────────────────

def _paginator(query: str, page: int, total: int,
               page_size: int, tab: str) -> str:
    total_pages = max(1, (total + page_size - 1) // page_size)
    if total_pages <= 1:
        return ''

    def link(p: int, label: str) -> str:
        return (f'<a href="/search?q={quote(query)}&tab={tab}&page={p}">{label}</a>')

    parts = []
    if page > 1:
        parts.append(link(1, '«'))
        parts.append(link(page - 1, '‹'))

    for p in range(max(1, page - 2), min(total_pages + 1, page + 3)):
        if p == page:
            parts.append(f'<span class="cur">{p}</span>')
        else:
            parts.append(link(p, str(p)))

    if page < total_pages:
        parts.append(link(page + 1, '›'))
        parts.append(link(total_pages, '»'))

    return f'<div class="pgr">{"".join(parts)}</div>'


# ═══════════════════════════════════════════════════════════════════════════
# HTTP ハンドラ
# ═══════════════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # アクセスログを抑制

    def _send(self, html: str, code: int = 200):
        data = html.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(data))
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
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _img(self, data: bytes):
        if not data:
            self.send_response(204); self.end_headers(); return
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', len(data))
        self.send_header('Cache-Control', 'max-age=3600')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        pars = urlparse(self.path)
        qs   = parse_qs(pars.query)
        def q(k, d=''): return unquote(qs.get(k, [''])[0]) or d
        def qi(k, d=1):
            try: return int(qs.get(k, [''])[0])
            except: return d
        path = pars.path

        if path in ('/', '/search'):
            query = q('q')
            page  = qi('page', 1)
            tab   = q('tab', 'tracks')
            if tab == 'playlists':
                self._send(page_search_playlists(query, page))
            else:
                self._send(page_search_tracks(query, page))

        elif path == '/playlist':
            self._send(page_playlist(q('url')))

        elif path == '/favorites':
            self._send(page_favorites())

        elif path == '/my_playlists':
            self._send(page_my_playlists())

        elif path == '/cover':
            self._img(fetch_cover(q('url')))

        elif path == '/api/status':
            closed = _CLOSE_SIGNAL.is_set()
            self._json({'active': not closed, 'close': closed})

        else:
            self._send('<p>Not Found</p>', code=404)

    def do_POST(self):
        pars = urlparse(self.path)
        path = pars.path
        try:
            length = int(self.headers.get('Content-Length', 0))
            body   = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            body = {}

        if path == '/play':
            req_type = body.get('type', 'track')

            if req_type == 'playlist':
                tracks = body.get('tracks', [])
                if not tracks:
                    self._json({'ok': False, 'error': 'tracks are empty'}, 400)
                    return
                req = {
                    'type':         'playlist',
                    'tracks':       tracks,
                    'start_from':   body.get('start_from', 1),
                    'requested_at': time.time(),
                }
                try:
                    REQUEST_PATH.write_text(json.dumps(req, ensure_ascii=False))
                    print(f'\n  📋 Playlist play request: {len(tracks)} tracks')
                    self._json({'ok': True})
                except Exception as e:
                    self._json({'ok': False, 'error': str(e)}, 500)

            else:
                url = body.get('url', '')
                if not url:
                    self._json({'ok': False, 'error': 'URL required'}, 400)
                    return
                req = {
                    'type':           'track',
                    'url':            url,
                    'title':          body.get('title', ''),
                    'artist':         body.get('artist', ''),
                    'cover_url':      body.get('cover_url', ''),
                    'duration':       body.get('duration', 0),
                    'genre':          body.get('genre', ''),
                    'audio_settings': body.get('audio_settings', {}),  # gain_preset/volume含む
                    'requested_at':   time.time(),
                }
                try:
                    REQUEST_PATH.write_text(json.dumps(req, ensure_ascii=False))
                    title = req['title'] or url
                    print(f'\n  📱 Browser play: {req["artist"]} — {title}')
                    self._json({'ok': True})
                except Exception as e:
                    self._json({'ok': False, 'error': str(e)}, 500)

        elif path == '/api/delete_favorite':
            url = body.get('url', '')
            if not url:
                self._json({'ok': False, 'error': 'URL required'}, 400)
                return
            try:
                favs     = load_favorites()
                new_favs = [f for f in favs if f.get('url') != url]
                if len(new_favs) == len(favs):
                    self._json({'ok': False, 'error': 'not found'}, 404)
                    return
                FAVS_PATH.write_text(
                    json.dumps(new_favs, ensure_ascii=False, indent=2))
                print(f'\n  🗑 Favourite removed: {url[:60]}')
                self._json({'ok': True})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 500)

        elif path == '/api/save_playlist':
            name   = body.get('name', '').strip()
            tracks = body.get('tracks', [])
            if not name:
                self._json({'ok': False, 'error': 'playlist name required'}, 400)
                return
            if not tracks:
                self._json({'ok': False, 'error': 'tracks are empty'}, 400)
                return
            try:
                from datetime import datetime
                pls = load_playlists()
                nos = [p.get('no', 0) for p in pls if isinstance(p.get('no'), int)]
                next_no = (max(nos) + 1) if nos else 1
                existing_idx = next((i for i, p in enumerate(pls) if p.get('name') == name), None)
                if existing_idx is not None:
                    next_no = pls[existing_idx].get('no', next_no)
                entry = {
                    'no':       next_no,
                    'name':     name,
                    'tracks':   tracks,
                    'count':    len(tracks),
                    'saved_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                }
                if existing_idx is not None:
                    pls[existing_idx] = entry
                    action = 'overwrite'
                else:
                    pls.append(entry)
                    action = 'new'
                save_playlists(pls)
                print(f'\n  💾 Playlist {action} saved: "{name}" ({len(tracks)} tracks)')
                self._json({'ok': True, 'no': next_no})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 500)

        elif path == '/api/delete_playlist':
            no = body.get('no')
            if no is None:
                self._json({'ok': False, 'error': 'no required'}, 400)
                return
            try:
                pls = load_playlists()
                target = next((p for p in pls if p.get('no') == no), None)
                if not target:
                    self._json({'ok': False, 'error': 'not found'}, 404)
                    return
                pls = [p for p in pls if p.get('no') != no]
                save_playlists(pls)
                print(f'\n  🗑 Playlist deleted: "{target["name"]}"')
                self._json({'ok': True})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 500)

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
        _server = HTTPServer(('0.0.0.0', port), Handler)
        _server_thread = threading.Thread(
            target=_server.serve_forever, daemon=True)
        _server_thread.start()
        print(f'  🌐 SoundCloud browser UI: http://localhost:{port}')
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
        print(f'  ⚠ Browser UI launch failed: {e}')
        return False


def stop_browser_server():
    global _server
    if _server:
        _server.shutdown()
        _server = None


def send_close_signal():
    """qji_soundcloud.py の b キー押下時に呼ぶ。ブラウザタブを閉じる。"""
    _CLOSE_SIGNAL.set()
    def _reset():
        time.sleep(3); _CLOSE_SIGNAL.clear()
    threading.Thread(target=_reset, daemon=True).start()


def check_browser_request() -> Optional[dict]:
    if not REQUEST_PATH.exists():
        return None
    try:
        req = json.loads(REQUEST_PATH.read_text())
        if time.time() - req.get('requested_at', 0) > 10:
            REQUEST_PATH.unlink(missing_ok=True)
            return None
        REQUEST_PATH.unlink(missing_ok=True)
        return req
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# スタンドアロン起動
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import signal
    print(f'🎵 Qji × SoundCloud Browser UI  port:{PORT}')
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
