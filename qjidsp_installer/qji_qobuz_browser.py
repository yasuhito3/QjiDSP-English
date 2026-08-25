#!/usr/bin/env python3
"""
qji_qobuz_browser.py  —  Qobuz ライブラリブラウザUI  (ポート 8080)

機能:
  ・Qobuz お気に入りライブラリ（全件・ページネーション）
  ・ローカルお気に入り（qji_qobuz_favorites.json）
  ・プレイリスト
  ・アルバム検索
  ・ブラウザ上でトラック選択 → ターミナルで即再生
"""

import json, time, threading, subprocess, sys
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, unquote
from typing import Optional

PORT           = 8080
CONFIG_PATH    = Path.home() / '.config' / 'qji_qobuz_direct.json'
FAVORITES_PATH = Path.home() / '.config' / 'qji_qobuz_favorites.json'
REQUEST_PATH   = Path('/tmp/qji_qobuz_request.json')
PLAYLIST_STATUS_PATH = Path('/tmp/qji_playlist_status.json')
BASE           = 'https://www.qobuz.com/api.json/0.2/'
PAGE_SIZE      = 60

FILTER_PRESET_LABELS = {
    'musikverein': '🎻 Musikverein', 'piano': '🎹 Piano',
    'chamber': '🏠 Chamber', 'vocal': '🎙 Vocal',
    'jazz': '🎷 Jazz', 'radio': '📻 Radio',
}

_session = None

def _load_config():
    try:
        return json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    except Exception:
        return {}

def _get_session():
    global _session
    if _session:
        return _session, _load_config()
    try:
        import requests
        cfg = _load_config()
        _session = requests.Session()
        _session.headers.update({'X-App-Id': cfg['app_id'],
                                  'X-User-Auth-Token': cfg['auth_token'],
                                  'User-Agent': 'Mozilla/5.0'})
        return _session, cfg
    except Exception:
        return None, {}

def _api(endpoint, params={}):
    s, cfg = _get_session()
    if not s: return None
    p = {'app_id': cfg.get('app_id',''), 'user_auth_token': cfg.get('auth_token',''), **params}
    try:
        r = s.get(BASE + endpoint, params=p, timeout=20)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def fetch_library_albums(offset=0, limit=PAGE_SIZE):
    d = _api('favorite/getUserFavorites', {'type': 'albums', 'limit': limit, 'offset': offset})
    if not d: return {'items': [], 'total': 0}
    albums = d.get('albums', {})
    return {'items': albums.get('items', []), 'total': albums.get('total', 0)}

def fetch_playlists(offset=0, limit=PAGE_SIZE):
    d = _api('playlist/getUserPlaylists', {'limit': limit, 'offset': offset})
    if not d: return {'items': [], 'total': 0}
    pl = d.get('playlists', {})
    return {'items': pl.get('items', []), 'total': pl.get('total', 0)}

def fetch_playlist_tracks(playlist_id):
    # extra=tracks を付けないとトラック一覧が返らないAPIバージョンがある
    d = _api('playlist/get', {
        'playlist_id': playlist_id,
        'extra':       'tracks',
        'limit':       500,
        'offset':      0,
    })
    if not d:
        print(f'  [playlist] API returned None for playlist_id={playlist_id}')
        return []

    # レスポンス構造のパターンを順に試す
    # パターン1: d['tracks']['items']  （標準）
    tracks_obj = d.get('tracks')
    if isinstance(tracks_obj, dict):
        items = tracks_obj.get('items', [])
        if items:
            print(f'  [playlist] {len(items)} tracks fetched (tracks.items)')
            return items

    # パターン2: d['playlist']['tracks']['items']  （ラッパーあり）
    pl_obj = d.get('playlist') or {}
    tracks_obj2 = pl_obj.get('tracks')
    if isinstance(tracks_obj2, dict):
        items = tracks_obj2.get('items', [])
        if items:
            print(f'  [playlist] {len(items)} tracks fetched (playlist.tracks.items)')
            return items

    # パターン3: d['items']  （フラット）
    if isinstance(d.get('items'), list):
        items = d['items']
        if items:
            print(f'  [playlist] {len(items)} tracks fetched (items)')
            return items

    # デバッグ用: トップレベルのキーをターミナルに出力
    print(f'  [playlist] Could not get tracks. Response keys: {list(d.keys())}')
    if 'tracks' in d:
        print(f'  [playlist] tracks type: {type(d["tracks"])}, value: {str(d["tracks"])[:200]}')
    return []

def fetch_playlist_detail(playlist_id):
    """プレイリスト名とトラック一覧を返す（マイ保存の詳細ページ用）。
    {'name': str, 'tracks': list} を返す。"""
    d = _api('playlist/get', {
        'playlist_id': playlist_id,
        'extra':       'tracks',
        'limit':       500,
        'offset':      0,
    })
    if not d:
        return {'name': '', 'tracks': []}
    # プレイリスト名の抽出（複数パターン対応）
    name = (d.get('name') or
            (d.get('playlist') or {}).get('name') or '')
    # トラックの抽出
    tracks_obj = d.get('tracks')
    if isinstance(tracks_obj, dict):
        items = tracks_obj.get('items', [])
        if items:
            return {'name': name, 'tracks': items}
    pl_obj = d.get('playlist') or {}
    name = name or pl_obj.get('name', '')
    tracks_obj2 = pl_obj.get('tracks')
    if isinstance(tracks_obj2, dict):
        items = tracks_obj2.get('items', [])
        if items:
            return {'name': name, 'tracks': items}
    items = d.get('items', [])
    return {'name': name, 'tracks': items if items else []}


def fetch_album_detail(album_id):
    return _api('album/get', {'album_id': album_id})

def search_albums(query, offset=0, limit=PAGE_SIZE):
    d = _api('catalog/search', {'query': query, 'type': 'albums', 'limit': limit, 'offset': offset})
    if not d: return {'items': [], 'total': 0}
    albums = d.get('albums', {})
    return {'items': albums.get('items', []), 'total': albums.get('total', 0)}

def fetch_cover(url):
    if not url: return b''
    try:
        import requests
        r = requests.get(url, timeout=10)
        return r.content if r.status_code == 200 else b''
    except Exception:
        return b''

def load_local_favorites():
    try:
        return json.loads(FAVORITES_PATH.read_text()) if FAVORITES_PATH.exists() else []
    except Exception:
        return []

def _esc(s):
    return str(s).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

def _js(s):
    """JS の シングルクォート文字列内で安全に使える文字列に変換する。
    アポストロフィ・改行・バックスラッシュを含むタイトルでの onclick 破損を防ぐ。"""
    s = str(s)
    s = s.replace('\\', '\\\\')   # バックスラッシュを先にエスケープ
    s = s.replace('\n', ' ').replace('\r', '').replace('\t', ' ')  # 改行除去
    return _esc(s).replace("'", "\\'")   # ← &#39; → \\' に変更

def _dur(sec):
    if not sec: return ''
    return f'{int(sec)//60}:{int(sec)%60:02d}'

CSS = """
:root{--bg:#090910;--surf:#12121a;--card:#191923;--acc:#7b68ee;--acc2:#a99fff;
--text:#e8e8f0;--muted:#6868a0;--border:#252535;--hover:#1e1e2e;--gold:#c8a84b;--red:#e05555}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
font-family:'Helvetica Neue','Hiragino Kaku Gothic ProN',sans-serif;min-height:100vh}
header{background:var(--surf);border-bottom:1px solid var(--border);
padding:11px 18px;display:flex;align-items:center;gap:10px;
position:sticky;top:0;z-index:200;flex-wrap:wrap}
.logo{font-size:1.05rem;font-weight:700;color:var(--acc2);white-space:nowrap}
.nav{display:flex;gap:5px;flex-wrap:wrap}
.nav a{padding:4px 11px;border-radius:16px;border:1px solid var(--border);
color:var(--muted);text-decoration:none;font-size:.76rem;white-space:nowrap;transition:all .15s}
.nav a:hover,.nav a.active{border-color:var(--acc);color:var(--acc2);background:rgba(123,104,238,.1)}
.sf{display:flex;gap:5px;margin-left:auto}
.sf input{background:var(--card);border:1px solid var(--border);border-radius:7px;
padding:4px 11px;color:var(--text);font-size:.8rem;width:200px;outline:none}
.sf input:focus{border-color:var(--acc)}
.sf button{background:var(--acc);color:#fff;border:none;border-radius:7px;
padding:4px 13px;cursor:pointer;font-size:.8rem;font-weight:600}
.gw{padding:18px}
.gi{font-size:.76rem;color:var(--muted);margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:9px;
overflow:hidden;cursor:pointer;text-decoration:none;color:inherit;display:block;
transition:transform .15s,border-color .15s,box-shadow .15s}
.card:hover{transform:translateY(-3px);border-color:var(--acc);
box-shadow:0 5px 18px rgba(123,104,238,.2)}
.cov{width:100%;aspect-ratio:1;background:var(--surf);position:relative;overflow:hidden}
.cov img{width:100%;height:100%;object-fit:cover;display:block}
.ni{width:100%;height:100%;display:flex;align-items:center;justify-content:center;
font-size:2.2rem;color:var(--border)}
.bdg{position:absolute;top:5px;right:5px;background:rgba(9,9,16,.85);
border:1px solid var(--border);border-radius:3px;padding:1px 5px;
font-size:.58rem;color:var(--gold);font-weight:700}
.ci{padding:9px}
.ci .ttl{font-size:.8rem;font-weight:600;line-height:1.3;margin-bottom:2px;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ci .art{font-size:.7rem;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ci .pst{margin-top:4px;font-size:.62rem;color:var(--acc2)}
.pgr{display:flex;gap:5px;justify-content:center;padding:20px 0;flex-wrap:wrap}
.pgr a,.pgr span{padding:5px 12px;border-radius:7px;border:1px solid var(--border);
text-decoration:none;color:var(--muted);font-size:.8rem}
.pgr a:hover{border-color:var(--acc);color:var(--acc2)}
.pgr span.cur{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:700}
.pgr span.dt{border:none}
.ap{max-width:840px;margin:0 auto;padding:22px 18px}
.ah{display:flex;gap:20px;margin-bottom:22px;align-items:flex-start}
.ah img{width:145px;height:145px;object-fit:cover;border-radius:7px;
border:1px solid var(--border);flex-shrink:0}
.am h2{font-size:1.25rem;font-weight:700;margin-bottom:4px;line-height:1.3}
.am .ar{color:var(--muted);font-size:.88rem;margin-bottom:9px}
.ab{display:inline-block;background:var(--hover);border:1px solid var(--border);
border-radius:5px;padding:2px 9px;font-size:.7rem;color:var(--acc2);margin-bottom:7px}
.pa{display:inline-flex;align-items:center;gap:5px;margin-top:7px;padding:7px 16px;
background:var(--acc);color:#fff;border:none;border-radius:7px;
font-size:.83rem;font-weight:600;cursor:pointer;text-decoration:none}
.pa:hover{opacity:.85}
.bb{display:inline-flex;align-items:center;gap:4px;margin-bottom:14px;
padding:5px 12px;background:var(--hover);border:1px solid var(--border);
border-radius:7px;color:var(--muted);text-decoration:none;font-size:.78rem}
.bb:hover{border-color:var(--acc);color:var(--acc2)}
.nt{background:rgba(123,104,238,.07);border:1px solid rgba(123,104,238,.3);
border-radius:7px;padding:9px 14px;margin-bottom:14px;font-size:.8rem;
color:var(--acc2);line-height:1.5}
.tr{display:flex;align-items:center;gap:11px;padding:9px 13px;
border-radius:7px;cursor:pointer;border:1px solid transparent;
text-decoration:none;color:inherit;margin-bottom:3px;transition:all .12s}
.tr:hover{background:var(--hover);border-color:var(--acc)}
.tno{width:25px;text-align:right;font-size:.76rem;color:var(--muted);font-family:monospace;flex-shrink:0}
.pic{font-size:.72rem;flex-shrink:0;opacity:0;color:var(--acc2)}
.tr:hover .pic{opacity:1}
.ttl{flex:1;font-size:.86rem;font-weight:500}
.tar{font-size:.73rem;color:var(--muted);flex-shrink:0;max-width:140px;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tdr{font-size:.73rem;color:var(--muted);font-family:monospace;flex-shrink:0}
.pl{max-width:680px;margin:0 auto;padding:22px 18px}
.pi{display:flex;align-items:center;gap:13px;padding:13px 15px;
background:var(--card);border:1px solid var(--border);border-radius:9px;
cursor:pointer;text-decoration:none;color:inherit;margin-bottom:7px;transition:all .15s}
.pi:hover{border-color:var(--acc);transform:translateY(-2px)}
.pic2{font-size:1.8rem;flex-shrink:0}
.pn{font-weight:600;margin-bottom:2px}.pm{font-size:.76rem;color:var(--muted)}
#toast{position:fixed;bottom:18px;right:18px;background:var(--acc);color:#fff;
padding:9px 16px;border-radius:9px;font-size:.85rem;font-weight:600;
opacity:0;transform:translateY(7px);transition:all .3s;z-index:999;pointer-events:none}
#toast.show{opacity:1;transform:translateY(0)}
#toast.err{background:var(--red)}
.del-btn{position:absolute;top:5px;right:5px;background:var(--red);color:#fff;
border:none;border-radius:50%;width:24px;height:24px;font-size:.75rem;cursor:pointer;
display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .2s;z-index:2}
.card:hover .del-btn{opacity:1}
.cov{position:relative}
.del-btn-album{background:var(--red);color:#fff;border:none;border-radius:7px;
padding:7px 16px;font-size:.84rem;cursor:pointer;margin-top:10px;display:inline-block}
/* ── プレイリストモード ──────────────────────────── */
.pl-add{position:absolute;top:5px;left:5px;background:rgba(123,104,238,.92);color:#fff;
border:none;border-radius:50%;width:28px;height:28px;font-size:1.1rem;cursor:pointer;
display:none;align-items:center;justify-content:center;z-index:3;font-weight:700;
box-shadow:0 2px 8px rgba(0,0,0,.5);transition:transform .15s}
.pl-add:hover{transform:scale(1.15)}
body.playlist-mode .pl-add{display:flex}
body.playlist-mode .card.in-queue{border-color:#4ecdc4 !important;
box-shadow:0 0 0 2px rgba(78,205,196,.35)}
body.playlist-mode .card.in-queue .pl-add{background:rgba(78,205,196,.92);content:'✓'}
.pl-btn{padding:5px 12px;border-radius:16px;border:1px solid var(--border);
color:var(--muted);font-size:.76rem;cursor:pointer;background:transparent;
white-space:nowrap;transition:all .15s;font-family:inherit}
.pl-btn:hover,.pl-btn.active{border-color:#4ecdc4;color:#4ecdc4;background:rgba(78,205,196,.1)}
.pl-btn.active{font-weight:700}
#pl-bar{position:fixed;bottom:0;left:0;right:0;background:rgba(18,18,26,.97);
border-top:2px solid #4ecdc4;padding:10px 18px;z-index:500;
display:none;align-items:center;gap:12px;backdrop-filter:blur(8px)}
body.playlist-mode #pl-bar{display:flex}
.pl-thumbs{display:flex;gap:5px;flex:1;overflow-x:auto;padding-bottom:2px;align-items:center}
.pl-thumb-wrap{position:relative;flex-shrink:0}
.pl-thumbs img{width:40px;height:40px;border-radius:5px;object-fit:cover;
border:1px solid var(--border);display:block}
.pl-thumb-del{position:absolute;top:-5px;right:-5px;background:var(--red);color:#fff;
border:none;border-radius:50%;width:16px;height:16px;font-size:.6rem;cursor:pointer;
display:flex;align-items:center;justify-content:center;padding:0}
.pl-count{color:#4ecdc4;font-size:.82rem;font-weight:700;white-space:nowrap;min-width:60px}
.pl-play-btn{background:#4ecdc4;color:#090910;border:none;border-radius:8px;
padding:8px 18px;font-size:.86rem;font-weight:700;cursor:pointer;white-space:nowrap;flex-shrink:0}
.pl-play-btn:hover{opacity:.85}
.pl-play-btn:disabled{opacity:.4;cursor:not-allowed}
.pl-clear-btn{background:transparent;border:1px solid var(--border);color:var(--muted);
border-radius:8px;padding:8px 12px;font-size:.82rem;cursor:pointer;flex-shrink:0}
.pl-clear-btn:hover{border-color:var(--red);color:var(--red)}
"""

JS = """
/* ── トースト ── */
function toast(msg,err,ms){ms=ms||2800;var t=document.getElementById('toast');
t.textContent=msg;t.className='show'+(err?' err':'');setTimeout(function(){t.className=''},ms);}

/* ── 通常再生 ── */
function playTrack(albumId,trackId,startNo,title){
  var u='/play?album='+albumId+'&track='+trackId+'&start='+startNo;
  fetch(u).then(function(r){return r.json();}).then(function(d){
    if(d.ok)toast('▶ '+title+' — playing');
    else toast('❌ '+d.error,true);
  }).catch(function(){toast('❌ Connection error',true);});
}
function playAll(albumId,title){playTrack(albumId,'',1,title);}

/* ── プレイリスト全曲再生 ── */
function playAllPlaylistTracks(){
  var tracks = (typeof _PLAYLIST_TRACK_DATA !== 'undefined') ? _PLAYLIST_TRACK_DATA : [];
  if(!tracks||tracks.length===0){ toast('No tracks found',true); return; }
  var pid   = (typeof _PLAYLIST_ID   !== 'undefined') ? _PLAYLIST_ID   : '';
  var pname = (typeof _PLAYLIST_NAME !== 'undefined') ? _PLAYLIST_NAME : '';
  fetch('/api/playlist-tracks-play',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({track_list:tracks, playlist_id:pid, playlist_name:pname})
  }).then(function(r){return r.json();}).then(function(d){
    if(d.ok) toast('▶ Playing playlist ('+tracks.length+' tracks)');
    else toast('❌ '+(d.error||'Error'),true);
  }).catch(function(){toast('❌ Connection error',true);});
}

/* ── マイ保存削除 ── */
function deleteLocal(albumId,title,btn){
  if(!confirm('Remove "'+title+'" from saved?')) return;
  fetch('/api/delete-local?album_id='+albumId).then(function(r){return r.json();}).then(function(d){
    if(d.ok){
      toast('🗑 Deleted');
      var card = btn ? btn.closest('.card') : null;
      if(card){ card.style.opacity='0.3'; setTimeout(function(){card.remove();},400); }
      else { setTimeout(function(){window.location.href='/local';},800); }
    } else { toast('❌ '+(d.error||'Delete failed'),true); }
  }).catch(function(){toast('❌ Connection error',true);});
}

/* ── プレイリスト ── */
// localStorage からキューを復元（ページをまたいで保持）
var _plQueue = (function(){
  try { return JSON.parse(localStorage.getItem('qji_queue') || '[]'); }
  catch(e) { return []; }
})();
var _plMode  = false;

function togglePlaylistMode(){
  _plMode = !_plMode;
  document.body.classList.toggle('playlist-mode', _plMode);
  var btn = document.getElementById('pl-toggle');
  if(btn){ btn.classList.toggle('active', _plMode);
    btn.textContent = _plMode ? '✓ Queue ON' : '🎵 Create queue'; }
  renderPlBar();
  if(!_plMode){ toast('Queue mode off'); }
  else { toast('🎵 Click ＋ on an album to add to queue'); }
}

function addToQueue(albumId, title, artist, imgUrl){
  // 既に追加済みなら削除（トグル）
  var idx = _plQueue.findIndex(function(a){return a.id===albumId;});
  if(idx>=0){ _plQueue.splice(idx,1); }
  else { _plQueue.push({id:albumId, title:title, artist:artist, img:imgUrl}); }
  // localStorage に保存（ページまたぎ対応）
  try { localStorage.setItem('qji_queue', JSON.stringify(_plQueue)); } catch(e){}
  // カードのスタイル更新
  var card = document.querySelector('.card[data-aid="'+albumId+'"]');
  if(card){ card.classList.toggle('in-queue', idx<0); }
  var btn  = document.querySelector('.pl-add[data-aid="'+albumId+'"]');
  if(btn){ btn.textContent = (idx<0) ? '✓' : '+'; }
  renderPlBar();
  toast((idx<0) ? '＋ Added: '+title : '－ Removed: '+title);
}

function removeFromQueue(idx){
  var item = _plQueue[idx];
  _plQueue.splice(idx,1);
  // localStorage に保存（ページまたぎ対応）
  try { localStorage.setItem('qji_queue', JSON.stringify(_plQueue)); } catch(e){}
  if(item){
    var card = document.querySelector('.card[data-aid="'+item.id+'"]');
    if(card) card.classList.remove('in-queue');
    var btn  = document.querySelector('.pl-add[data-aid="'+item.id+'"]');
    if(btn)  btn.textContent = '+';
  }
  renderPlBar();
}

function clearQueue(){
  _plQueue = [];
  try { localStorage.removeItem('qji_queue'); } catch(e){}
  document.querySelectorAll('.card.in-queue').forEach(function(c){c.classList.remove('in-queue');});
  document.querySelectorAll('.pl-add').forEach(function(b){b.textContent='+';});
  renderPlBar();
}

function renderPlBar(){
  var bar = document.getElementById('pl-bar');
  if(!bar) return;
  var n = _plQueue.length;
  document.getElementById('pl-count').textContent = n;
  var pb = document.getElementById('pl-play-btn');
  if(pb) pb.disabled = n===0;
  var thumbs = document.getElementById('pl-thumbs');
  if(!thumbs) return;
  thumbs.innerHTML = '';
  _plQueue.forEach(function(a, i){
    var wrap = document.createElement('div');
    wrap.className = 'pl-thumb-wrap';
    wrap.title = a.title;
    if(a.img){
      var img = document.createElement('img');
      img.src = '/cover?url='+encodeURIComponent(a.img);
      img.alt = a.title;
      wrap.appendChild(img);
    } else {
      var ph = document.createElement('div');
      ph.style.cssText='width:40px;height:40px;border-radius:5px;background:var(--surf);display:flex;align-items:center;justify-content:center;font-size:1.2rem;';
      ph.textContent='🎵';
      wrap.appendChild(ph);
    }
    var del = document.createElement('button');
    del.className='pl-thumb-del'; del.textContent='✕';
    (function(ii){del.onclick=function(){removeFromQueue(ii);};})(i);
    wrap.appendChild(del);
    thumbs.appendChild(wrap);
  });
}

function playPlaylist(){
  if(_plQueue.length===0){ toast('Queue is empty',true); return; }
  var ids     = _plQueue.map(function(a){return a.id;});
  var titles  = _plQueue.map(function(a){return a.title;});
  var artists = _plQueue.map(function(a){return a.artist;});
  var imgs    = _plQueue.map(function(a){return a.img;});
  fetch('/api/queue-play',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({album_ids:ids, titles:titles, artists:artists, imgs:imgs})
  }).then(function(r){return r.json();}).then(function(d){
    if(d.ok){
      toast('▶ Playlist started ('+ids.length+' albums)');
      _showPlayingBar(_plQueue.slice());
      _plQueue=[];
      try { localStorage.removeItem('qji_queue'); } catch(e){}
      document.querySelectorAll('.card.in-queue').forEach(function(c){c.classList.remove('in-queue');});
      document.querySelectorAll('.pl-add').forEach(function(b){b.textContent='+';});
      // キューモードを終了（ボタンのみリセット）
      _plMode=false;
      document.body.classList.remove('playlist-mode');
      var btn=document.getElementById('pl-toggle');
      if(btn){btn.classList.remove('active');btn.textContent='🎵 Create queue';}
    } else { toast('❌ '+(d.error||'Error'),true); }
  }).catch(function(){toast('❌ Connection error',true);});
}

/* ── 再生中キューバー（静的表示・ポーリングなし） ── */
function _showPlayingBar(queue){
  var bar = document.getElementById('pl-bar');
  if(!bar) return;

  // サムネイル列を生成
  var thumbsHtml = '';
  queue.forEach(function(item, i){
    var src = item.img ? '/cover?url='+encodeURIComponent(item.img) : '';
    var imgTag = src
      ? '<img src="'+src+'" style="width:40px;height:40px;border-radius:5px;object-fit:cover;border:1px solid var(--border);" title="'+_esc2(item.title)+'">'
      : '<div style="width:40px;height:40px;border-radius:5px;background:var(--surf);display:flex;align-items:center;justify-content:center;font-size:1.1rem;" title="'+_esc2(item.title)+'">🎵</div>';
    thumbsHtml += '<div style="flex-shrink:0;">'+imgTag+'</div>';
  });

  bar.innerHTML =
    '<div style="color:#4ecdc4;font-size:.78rem;font-weight:700;white-space:nowrap;flex-shrink:0;">▶ Now playing</div>'
    +'<div style="color:var(--muted);font-size:.74rem;white-space:nowrap;flex-shrink:0;">'+queue.length+'</div>'
    +'<div style="display:flex;gap:5px;flex:1;overflow-x:auto;align-items:center;padding:2px 0;">'+thumbsHtml+'</div>';
  bar.style.cssText = 'display:flex;align-items:center;gap:10px;padding:8px 18px;'
    +'position:fixed;bottom:0;left:0;right:0;background:rgba(18,18,26,.97);'
    +'border-top:2px solid #4ecdc4;z-index:500;backdrop-filter:blur(8px);';
}

function _esc2(s){ return (s||'').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }

/* ── ページロード時：localStorage からキューを復元してカードに反映 ── */
(function(){
  if(_plQueue.length === 0) return;
  // キューに含まれるアルバムのカードにスタイルを適用
  _plQueue.forEach(function(a){
    var card = document.querySelector('.card[data-aid="'+a.id+'"]');
    if(card){ card.classList.add('in-queue'); }
    var btn = document.querySelector('.pl-add[data-aid="'+a.id+'"]');
    if(btn){ btn.textContent = '✓'; }
  });
  // キューが空でなければ自動的にキューモードをオン
  _plMode = true;
  document.body.classList.add('playlist-mode');
  var btn = document.getElementById('pl-toggle');
  if(btn){ btn.classList.add('active'); btn.textContent = '✓ Queue ON'; }
  renderPlBar();
})();

/* ── 再生終了検知 ── */
(function(){
  function poll(){
    fetch('/api/status').then(function(r){return r.json();}).then(function(d){
      if(d.close){
        try { window.open('','_self',''); window.close(); } catch(e){}
        setTimeout(function(){
          document.body.innerHTML=
            '<div style="display:flex;flex-direction:column;align-items:center;'
            +'justify-content:center;height:100vh;background:#090910;color:#7b68ee;'
            +'font-size:1.1rem;font-family:sans-serif;gap:18px;">'
            +'<div style="font-size:2.5rem;">🎵</div>'
            +'<div>Qobuz playback ended</div>'
            +'<div style="font-size:.82rem;color:#6868a0;">You can close this window</div>'
            +'</div>';
        }, 300);
      } else { setTimeout(poll, 1000); }
    }).catch(function(){ setTimeout(poll, 2000); });
  }
  setTimeout(poll, 1500);
})();
"""

def _head(title):
    nav = ''.join(f'<a href="{h}" class="{("active" if k in title.lower() or (h=="/" and title in ["Library","Qji × Qobuz"]) else "")}">{l}</a>'
                  for h,l,k in [('/','Library','Library'),('/local','Saved','Saved'),
                                  ('/playlists','Playlists','Playlists'),('/search','Search','Search')])
    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} — Qji</title><style>{CSS}</style></head><body>
<div id="toast"></div><script>{JS}</script>
<header><div class="logo">🎵 Qji × Qobuz</div>
<nav class="nav">{nav}</nav>
<button id="pl-toggle" class="pl-btn" onclick="togglePlaylistMode()">🎵 Create queue</button>
<form class="sf" action="/search" method="get">
<input name="q" placeholder="Search..." autocomplete="off">
<button type="submit">GO</button></form></header>
<div id="pl-bar">
  <div class="pl-count" id="pl-count">0</div>
  <div class="pl-thumbs" id="pl-thumbs"></div>
  <button class="pl-clear-btn" onclick="clearQueue()">🗑 Clear</button>
  <button id="pl-play-btn" class="pl-play-btn" onclick="playPlaylist()" disabled>▶ Play</button>
</div>"""

def _grid(items, total, page, base_url, prefix=''):
    cards = ''
    for a in items:
        aid = str(a.get('id',''))
        ttl = a.get('title','(Unknown)')
        art = (a.get('artist') or {}).get('name','')
        img = ((a.get('image') or {}).get('large') or (a.get('image') or {}).get('small') or '')
        pst = a.get('_preset','')
        no  = a.get('_no','')
        lp  = a.get('_local', False)
        it  = f'<img src="/cover?url={quote(img)}" alt="" loading="lazy">' if img else '<div class="ni">🎵</div>'
        bdg = f'<div class="bdg">#{no}</div>' if no else ''
        href = f'/album?id={aid}{"&local=1" if lp else ""}'
        # マイ保存カードには削除ボタンを追加
        delbtn = (f'<button class="del-btn" title="Remove" '
                  f'onclick="event.preventDefault();event.stopPropagation();'
                  f'deleteLocal(\'{aid}\',\'{_js(ttl)}\',this)">✕</button>') if lp else ''
        # プレイリスト追加ボタン（プレイリストモード時のみ表示）
        pladd = (f'<button class="pl-add" data-aid="{aid}" title="Add to queue" '
                 f'onclick="event.preventDefault();event.stopPropagation();'
                 f'addToQueue(\'{aid}\',\'{_js(ttl)}\',\'{_js(art)}\',\'{_js(img)}\')">+</button>')
        cards += (f'<a class="card" href="{href}" data-aid="{aid}">'
                  f'<div class="cov">{it}{bdg}{delbtn}{pladd}</div>'
                  f'<div class="ci"><div class="ttl">{_esc(ttl)}</div>'
                  f'<div class="art">{_esc(art)}</div>'
                  f'{"<div class=pst>"+pst+"</div>" if pst else ""}</div></a>')
    tp = max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
    def pg(p,l=None):
        l=l or str(p)
        if p==page: return f'<span class="cur">{l}</span>'
        return f'<a href="{base_url}&page={p}">{l}</a>'
    pts=[]
    if tp>1:
        pts.append(pg(1))
        if page>3: pts.append('<span class="dt">…</span>')
        for p in range(max(2,page-1),min(tp,page+2)+1): pts.append(pg(p))
        if page<tp-2: pts.append('<span class="dt">…</span>')
        if tp>1: pts.append(pg(tp))
    pgr=f'<div class="pgr">{"".join(pts)}</div>' if pts else ''
    return (f'<div class="gw"><div class="gi">{prefix} {total} / page {page} of {tp}</div>'
            f'<div class="grid">{cards}</div>{pgr}</div>')

def page_library(page=1):
    data = fetch_library_albums((page-1)*PAGE_SIZE, PAGE_SIZE)
    body = _grid(data['items'], data['total'], page, '/library?', 'Qobuz Favourites ')
    return _head('Library') + body + '</body></html>'

def page_local(page=1):
    favs  = load_local_favorites()
    total = len(favs)
    items = []
    for f in favs[(page-1)*PAGE_SIZE:(page-1)*PAGE_SIZE+PAGE_SIZE]:
        audio = f.get('audio_settings', {})
        items.append({'id': f.get('album_id',''), 'title': f.get('title',''),
                      'artist': {'name': f.get('artist','')},
                      'image':  {'large': f.get('cover_url',''), 'small': f.get('cover_url','')},
                      '_preset': FILTER_PRESET_LABELS.get(audio.get('filter_preset',''),''),
                      '_no': f.get('no',''), '_local': True})
    body = _grid(items, total, page, '/local?', 'Saved ')
    return _head('Saved') + body + '</body></html>'

def page_playlists(page=1):
    data  = fetch_playlists((page - 1) * PAGE_SIZE, PAGE_SIZE)
    total = data['total']
    tp    = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    rows = ''
    for pl in data['items']:
        pid  = str(pl.get('id', ''))
        name = pl.get('name', '')
        cnt  = pl.get('tracks_count', 0)
        add_btn = (
            f'<button class="pl-q-add" id="plq-btn-{pid}" title="Add to PL queue" '
            f'onclick="event.preventDefault();event.stopPropagation();'
            f'addPLQ(\'{pid}\',\'{_js(name)}\')">+</button>'
        )
        rows += (
            f'<a class="pi" href="/playlist?id={pid}">'
            f'<div class="pic2">🎵</div>'
            f'<div style="flex:1"><div class="pn">{_esc(name)}</div>'
            f'<div class="pm">{cnt} tracks</div></div>'
            f'{add_btn}</a>'
        )
    if not rows:
        rows = '<p style="color:var(--muted);padding:30px">No playlists</p>'

    # ページネーション
    def pg(p, l=None):
        l = l or str(p)
        if p == page: return f'<span class="cur">{l}</span>'
        return f'<a href="/playlists?page={p}">{l}</a>'
    pts = []
    if tp > 1:
        pts.append(pg(1))
        if page > 3: pts.append('<span class="dt">…</span>')
        for p in range(max(2, page - 1), min(tp, page + 2) + 1): pts.append(pg(p))
        if page < tp - 2: pts.append('<span class="dt">…</span>')
        pts.append(pg(tp))
    pgr = f'<div class="pgr">{"".join(pts)}</div>' if pts else ''
    gi  = f'<div class="gi">Playlists {total} / page {page} of {tp}</div>'

    # PLキューモード専用 CSS（インライン）
    plq_css = (
        '<style>'
        '.pl-q-add{display:none;align-items:center;justify-content:center;'
        'flex-shrink:0;width:30px;height:30px;border-radius:50%;border:none;'
        'background:rgba(123,104,238,.85);color:#fff;font-size:1.1rem;'
        'cursor:pointer;transition:transform .15s;}'
        '.pl-q-add:hover{transform:scale(1.15);}'
        '.pl-q-add.inq{background:#4ecdc4;color:#090910;font-size:.9rem;}'
        'body.plq-mode .pl-q-add{display:flex;}'
        '#plq-bar{display:none;position:fixed;bottom:0;left:0;right:0;'
        'background:rgba(18,18,26,.97);border-top:2px solid #7b68ee;'
        'padding:10px 18px;z-index:510;align-items:center;gap:12px;'
        'backdrop-filter:blur(8px);}'
        'body.plq-mode #plq-bar{display:flex;}'
        '</style>'
    )

    # PLキュー固定バー（画面下部）
    plq_bar = (
        '<div id="plq-bar">'
        '<div style="color:#7b68ee;font-size:.78rem;font-weight:700;flex-shrink:0;">🎵 Playlist queue</div>'
        '<div id="plq-cnt" style="color:var(--muted);font-size:.74rem;'
        'flex-shrink:0;min-width:30px;">0</div>'
        '<div id="plq-names" style="flex:1;font-size:.72rem;color:var(--muted);'
        'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></div>'
        '<button id="plq-play-btn" onclick="playPLQ()" disabled '
        'style="background:#7b68ee;color:#fff;border:none;border-radius:8px;'
        'padding:6px 16px;font-size:.82rem;font-weight:700;cursor:pointer;'
        'white-space:nowrap;flex-shrink:0;">▶ Play</button>'
        '<button onclick="clearPLQ()" '
        'style="background:transparent;border:1px solid var(--border);color:var(--muted);'
        'border-radius:8px;padding:6px 12px;font-size:.78rem;cursor:pointer;flex-shrink:0;">'
        'Clear</button>'
        '</div>'
    )

    # PLキューモード切替ボタン（リスト上部）
    plq_toggle = (
        '<div style="display:flex;justify-content:flex-end;padding:4px 0 10px;">'
        '<button id="plq-toggle-btn" class="pl-btn" onclick="togglePLQ()">'
        '🎵 Create PL queue</button>'
        '</div>'
    )

    # PLキュー専用 JS（このページのみ）
    plq_js = """<script>
var _PLQ=[];
function togglePLQ(){
  var on=document.body.classList.toggle('plq-mode');
  var btn=document.getElementById('plq-toggle-btn');
  if(btn){btn.classList.toggle('active',on);
    btn.textContent=on?'✓ PL Queue ON':'🎵 Create PL queue';}
  toast(on?'🎵 Click ＋ to add PL to queue':'PL queue mode off');
  _renderPLQ();
}
function addPLQ(pid,name){
  var idx=_PLQ.findIndex(function(p){return p.playlist_id===pid;});
  var btn=document.getElementById('plq-btn-'+pid);
  if(idx>=0){
    _PLQ.splice(idx,1);
    if(btn){btn.textContent='+';btn.classList.remove('inq');}
    toast('－ Removed: '+name);
  }else{
    _PLQ.push({playlist_id:pid,name:name});
    if(btn){btn.textContent='✓';btn.classList.add('inq');}
    toast('＋ Added to PL queue: '+name);
  }
  _renderPLQ();
}
function clearPLQ(){
  _PLQ=[];
  document.querySelectorAll('.pl-q-add').forEach(function(b){
    b.textContent='+';b.classList.remove('inq');
  });
  _renderPLQ();
  toast('PL queue cleared');
}
function _renderPLQ(){
  var cnt=document.getElementById('plq-cnt');
  var nm=document.getElementById('plq-names');
  var pb=document.getElementById('plq-play-btn');
  if(cnt)cnt.textContent=_PLQ.length;
  if(nm)nm.textContent=_PLQ.map(function(p){return p.name;}).join(' → ');
  if(pb)pb.disabled=(_PLQ.length===0);
}
function playPLQ(){
  if(_PLQ.length===0){toast('Queue is empty',true);return;}
  fetch('/api/playlist-queue-play',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({playlists:_PLQ})
  }).then(function(r){return r.json();}).then(function(d){
    if(d.ok){
      toast('▶ PL queue started ('+_PLQ.length+') — [q] for next PL');
      _PLQ=[];
      document.querySelectorAll('.pl-q-add').forEach(function(b){
        b.textContent='+';b.classList.remove('inq');
      });
      document.body.classList.remove('plq-mode');
      var btn=document.getElementById('plq-toggle-btn');
      if(btn){btn.classList.remove('active');btn.textContent='🎵 Create PL queue';}
      _renderPLQ();
    }else{toast('❌ '+(d.error||'Error'),true);}
  }).catch(function(){toast('❌ Connection error',true);});
}
</script>"""

    return (
        _head('Playlists') + plq_css
        + f'<div class="pl">{plq_toggle}{gi}{rows}{pgr}</div>'
        + plq_bar + plq_js + '</body></html>'
    )

def page_playlist_detail(pid):
    tracks = fetch_playlist_tracks(pid)
    # プレイリスト名を別途取得（fetch_playlist_tracks はトラックのみ返す）
    pl_detail = fetch_playlist_detail(pid)
    pl_name   = pl_detail['name']
    track_data = []
    rows = ''
    for i,t in enumerate(tracks,1):
        tid = str(t.get('id',''))
        aid = str((t.get('album') or {}).get('id',''))
        ttl = t.get('title','')
        art = (t.get('performer') or {}).get('name','') or (t.get('album') or {}).get('artist',{}).get('name','')
        dur = _dur(t.get('duration',0))
        alb_ttl = (t.get('album') or {}).get('title','')
        cover = ((t.get('album') or {}).get('image') or {}).get('large','') or \
                ((t.get('album') or {}).get('image') or {}).get('small','')\
                or ''
        track_data.append({
            'track_id':  tid,
            'album_id':  aid,
            'title':     ttl,
            'artist':    art,
            'album':     alb_ttl,
            'cover_url': cover,
        })
        rows += (f'<a class="tr" onclick="playTrack(\'{aid}\',\'{tid}\',{i},\'{_js(ttl)}\');return false" href="#">'
                 f'<span class="tno">{i}</span><span class="pic">▶</span>'
                 f'<span class="ttl">{_esc(ttl)}</span><span class="tar">{_esc(art)}</span>'
                 f'<span class="tdr">{dur}</span></a>')
    # トラックデータをscriptタグで安全に埋め込む（onclick属性だとクォートが衝突する）
    track_data_json = json.dumps(track_data, ensure_ascii=False)
    track_data_script = (
        f'<script>'
        f'var _PLAYLIST_TRACK_DATA = {track_data_json};'
        f'var _PLAYLIST_ID   = {json.dumps(pid)};'
        f'var _PLAYLIST_NAME = {json.dumps(pl_name)};'
        f'</script>'
    )
    play_all_btn = (
        f'<a class="pa" href="#" onclick="playAllPlaylistTracks();return false">'
        f'▶ Play all</a><br>'
    ) if tracks else ''
    no_tracks_msg = (
        '<p style="color:var(--muted);padding:20px;background:rgba(255,100,100,.08);'
        'border:1px solid rgba(255,100,100,.3);border-radius:8px;margin-top:12px;">'
        '⚠ Could not fetch tracks.<br>'
        f'<a href="/api/debug-playlist?id={_esc(pid)}" target="_blank" '
        'style="color:#7b68ee;font-size:.85rem;">→ View debug info</a>'
        '</p>'
    ) if not tracks else ''
    body = (f'<div class="ap"><a class="bb" href="/playlists">← Playlists</a>'
            f'{track_data_script}'
            f'<div class="nt">Click a track to play / "Play all" to play the full playlist</div>'
            f'{play_all_btn}{no_tracks_msg}{rows}</div>')
    return _head('Playlists') + body + '</body></html>'

def page_search(query, page=1):
    if not query:
        return _head('Search') + '<div class="ap"><p style="color:var(--muted);padding:30px;text-align:center">Enter a keyword to search</p></div></body></html>'
    data = search_albums(query, (page-1)*PAGE_SIZE, PAGE_SIZE)
    body = _grid(data['items'], data['total'], page, f'/search?q={quote(query)}&', f'"{_esc(query)}" ')
    return _head(f'Search: {query}') + body + '</body></html>'

def _page_playlist_local(album_id_key, local=False):
    """'pl:XXXX' 形式のマイ保存プレイリストエントリを表示する。"""
    pid     = album_id_key[3:]   # 'pl:' を除いた Qobuz プレイリスト ID
    detail  = fetch_playlist_detail(pid)
    tracks  = detail['tracks']
    pl_name = detail['name']

    # 音響バッジ（マイ保存のみ）
    abadge = ''
    if local:
        favs = load_local_favorites()
        fav  = next((f for f in favs if f.get('album_id') == album_id_key), {})
        audio_saved = fav.get('audio_settings', {})
        pst  = FILTER_PRESET_LABELS.get(audio_saved.get('filter_preset', ''), '')
        if pst:
            abadge = (f'<div class="ab">{pst} / Gain:{audio_saved.get("gain_preset","")} '
                      f'/ Vol:{audio_saved.get("volume",12)}dB</div><br>')

    # トラック行
    track_data = []
    rows = ''
    for i, t in enumerate(tracks, 1):
        tid     = str(t.get('id', ''))
        aid     = str((t.get('album') or {}).get('id', ''))
        ttl     = t.get('title', '')
        art     = ((t.get('performer') or {}).get('name', '')
                   or (t.get('album') or {}).get('artist', {}).get('name', ''))
        dur     = _dur(t.get('duration', 0))
        alb_ttl = (t.get('album') or {}).get('title', '')
        cover   = (((t.get('album') or {}).get('image') or {}).get('large', '')
                   or ((t.get('album') or {}).get('image') or {}).get('small', '') or '')
        track_data.append({'track_id': tid, 'album_id': aid, 'title': ttl,
                           'artist': art, 'album': alb_ttl, 'cover_url': cover})
        rows += (f'<a class="tr" onclick="playTrack(\'{aid}\',\'{tid}\',{i},\'{_js(ttl)}\');return false" href="#">'
                 f'<span class="tno">{i}</span><span class="pic">▶</span>'
                 f'<span class="ttl">{_esc(ttl)}</span>'
                 f'<span class="tar">{_esc(art)}</span>'
                 f'<span class="tdr">{dur}</span></a>')

    td_json  = json.dumps(track_data, ensure_ascii=False)
    pl_vars  = (f'<script>'
                f'var _PLAYLIST_TRACK_DATA={td_json};'
                f'var _PLAYLIST_ID={json.dumps(pid)};'
                f'var _PLAYLIST_NAME={json.dumps(pl_name)};'
                f'</script>')
    play_btn = (f'<a class="pa" href="#" onclick="playAllPlaylistTracks();return false">'
                f'▶ Play all</a><br>') if tracks else ''
    del_btn  = (f'<button class="del-btn-album" '
                f'onclick="deleteLocal(\'{_js(album_id_key)}\',\'{_js(pl_name)}\',null)">'
                f'🗑 Remove from saved</button>') if local else ''
    no_msg   = ('<p style="color:var(--muted);padding:20px">⚠ Could not fetch tracks</p>'
                ) if not tracks else ''
    back = '/local' if local else '/playlists'
    blab = '← Saved' if local else '← Playlists'

    body = (f'<div class="ap"><a class="bb" href="{back}">{blab}</a>'
            f'{pl_vars}'
            f'<div class="ah">'
            f'<div class="ni" style="width:80px;height:80px;font-size:2.5rem;'
            f'display:flex;align-items:center;justify-content:center;'
            f'background:var(--card);border-radius:12px;flex-shrink:0;">🎵</div>'
            f'<div class="am"><h2>{_esc(pl_name or "Playlist")}</h2>'
            f'<div class="ar">Qobuz Playlist · {len(tracks)} tracks</div>'
            f'{abadge}{play_btn}{del_btn}</div></div>'
            f'<div class="nt">Click a track to start playback in the terminal</div>'
            f'{no_msg}{rows}</div>')
    return _head(pl_name or 'Playlist') + body + '</body></html>'

def page_album(album_id, local=False):
    # マイ保存プレイリストエントリ（'pl:XXXX' 合成キー）
    if album_id.startswith('pl:'):
        return _page_playlist_local(album_id, local)
    album = fetch_album_detail(album_id)
    if not album:
        return _head('Error') + '<div class="ap"><p>Could not fetch album info</p></div></body></html>'
    ttl  = album.get('title','')
    art  = (album.get('artist') or {}).get('name','')
    img  = ((album.get('image') or {}).get('large') or '')
    trks = (album.get('tracks') or {}).get('items', [])

    # 音響バッジ（マイ保存のみ）
    abadge = ''
    audio_saved = {}
    if local:
        favs = load_local_favorites()
        fav  = next((f for f in favs if f.get('album_id') == album_id), {})
        audio_saved = fav.get('audio_settings', {})
        pst  = FILTER_PRESET_LABELS.get(audio_saved.get('filter_preset',''),'')
        if pst:
            abadge = f'<div class="ab">{pst} / Gain:{audio_saved.get("gain_preset","")} / Vol:{audio_saved.get("volume",12)}dB</div><br>'

    ih = f'<img src="/cover?url={quote(img)}" alt="{_esc(ttl)}">' if img else ''
    rows = ''
    for t in trks:
        tid = str(t.get('id',''))
        tno = int(t.get('track_number') or 0)
        tttl= t.get('title','')
        tart= (t.get('performer') or {}).get('name','') or art
        dur = _dur(t.get('duration',0))
        rows += (f'<a class="tr" onclick="playTrack(\'{album_id}\',\'{tid}\',{tno},\'{_js(tttl)}\');return false" href="#">'
                 f'<span class="tno">{tno}</span><span class="pic">▶</span>'
                 f'<span class="ttl">{_esc(tttl)}</span><span class="tar">{_esc(tart)}</span>'
                 f'<span class="tdr">{dur}</span></a>')

    back  = '/local' if local else '/'
    blab  = '← Saved' if local else '← Library'
    del_btn = (f'<button class="del-btn-album" '
               f'onclick="deleteLocal(\'{album_id}\',\'{_js(ttl)}\',null)">🗑 Remove from saved</button>'
               ) if local else ''
    body  = (f'<div class="ap"><a class="bb" href="{back}">{blab}</a>'
             f'<div class="ah">{ih}<div class="am"><h2>{_esc(ttl)}</h2>'
             f'<div class="ar">{_esc(art)}</div>{abadge}'
             f'<a class="pa" href="#" onclick="playAll(\'{album_id}\',\'{_js(ttl)}\');return false">▶ Play from track 1</a>'
             f'{del_btn}'
             f'</div></div>'
             f'<div class="nt">Click a track to start playback in the terminal</div>'
             f'{rows}</div>')
    return _head(ttl) + body + '</body></html>'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _send(self, body, ctype='text/html; charset=utf-8', code=200):
        b = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', len(b))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        try:
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass  # ブラウザが接続を切った場合 — 正常な動作

    def _json(self, data, code=200):
        b = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(b))
        self.end_headers()
        try:
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _img(self, data):
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
            pass  # 画像送信中にブラウザが移動した場合 — 正常な動作

    def do_POST(self):
        pars = urlparse(self.path)
        path = pars.path
        try:
            length = int(self.headers.get('Content-Length', 0))
            body   = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            body = {}

        if path == '/api/queue-play':
            album_ids = body.get('album_ids', [])
            titles    = body.get('titles', [])
            artists   = body.get('artists', [])
            imgs      = body.get('imgs', [])
            if not album_ids:
                self._json({'ok': False, 'error': 'album_ids is empty'}, 400)
                return
            req = {
                'type':         'playlist',
                'album_ids':    album_ids,
                'titles':       titles,
                'artists':      artists,
                'imgs':         imgs,
                'requested_at': time.time(),
            }
            try:
                REQUEST_PATH.write_text(json.dumps(req, ensure_ascii=False))
                print(f'\n  📋 Playlist play: {len(album_ids)} albums')
                for i, (aid, ttl) in enumerate(zip(album_ids, titles or ['']*len(album_ids)), 1):
                    print(f'       [{i}] {ttl or aid}')
                self._json({'ok': True})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 500)
        elif path == '/api/playlist-tracks-play':
            track_list = body.get('track_list', [])
            if not track_list:
                self._json({'ok': False, 'error': 'track_list is empty'}, 400)
                return
            req = {
                'type':         'playlist_tracks',
                'track_list':   track_list,
                'requested_at': time.time(),
            }
            try:
                REQUEST_PATH.write_text(json.dumps(req, ensure_ascii=False))
                print(f'\n  🎵 Playlist track-by-track play: {len(track_list)} tracks')
                for i, t in enumerate(track_list[:5], 1):
                    print(f'       [{i}] {t.get("title","") or t.get("track_id","")}')
                if len(track_list) > 5:
                    print(f'       ... and {len(track_list)-5} more')
                self._json({'ok': True})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 500)
        elif path == '/api/playlist-queue-play':
            playlists = body.get('playlists', [])
            if not playlists:
                self._json({'ok': False, 'error': 'playlists is empty'}, 400)
                return
            req = {
                'type':         'playlist_queue',
                'playlists':    playlists,   # [{playlist_id, name}, ...]
                'requested_at': time.time(),
            }
            try:
                REQUEST_PATH.write_text(json.dumps(req, ensure_ascii=False))
                print(f'\n  🎵 PL queue play: {len(playlists)} playlists')
                for i, pl in enumerate(playlists, 1):
                    print(f'       [{i}] {pl.get("name", pl.get("playlist_id","?"))}')
                self._json({'ok': True})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 500)
        else:
            self._send('<p>Not Found</p>', code=404)

    def do_GET(self):
        pars = urlparse(self.path)
        qs   = parse_qs(pars.query)
        def q(k, d=''): return unquote(qs.get(k,[''])[0]) or d
        def qi(k, d=1):
            try: return int(qs.get(k,[''])[0])
            except: return d
        path = pars.path

        if path in ('/', '/index.html', '/library'):
            self._send(page_library(qi('page')))
        elif path == '/local':
            self._send(page_local(qi('page')))
        elif path == '/playlists':
            self._send(page_playlists(qi('page')))
        elif path == '/playlist':
            self._send(page_playlist_detail(q('id')))
        elif path == '/search':
            self._send(page_search(q('q'), qi('page')))
        elif path == '/album':
            self._send(page_album(q('id'), local=bool(q('local'))))
        elif path == '/cover':
            self._img(fetch_cover(q('url')))
        elif path == '/play':
            album_id   = q('album')
            track_id   = q('track')
            start_from = qi('start', 1)
            favs  = load_local_favorites()
            fav   = next((f for f in favs if f.get('album_id') == album_id), {})
            audio = fav.get('audio_settings', {})
            req = {'album_id': album_id, 'track_id': track_id,
                   'start_from': start_from, 'source_url': fav.get('source_url',''),
                   'audio_settings': audio, 'requested_at': time.time()}
            try:
                REQUEST_PATH.write_text(json.dumps(req, ensure_ascii=False))
                print(f'\n  📱 Browser play: album={album_id} track #{start_from}')
                self._json({'ok': True})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 500)
        elif path == '/api/status':
            # JS ポーリング用: セッション状態を返す
            import json as _json
            closed = _CLOSE_SIGNAL.is_set()
            self._json({'active': not closed, 'close': closed})

        elif path == '/api/playlist-status':
            # プレイリスト再生状態をファイル経由で取得（モジュール共有より確実）
            try:
                if PLAYLIST_STATUS_PATH.exists():
                    st = json.loads(PLAYLIST_STATUS_PATH.read_text())
                else:
                    st = {'active': False}
            except Exception:
                st = {'active': False}
            self._json(st)
        elif path == '/api/debug-playlist':
            # プレイリストAPIレスポンス生データを返す（デバッグ用）
            pid = q('id')
            raw = _api('playlist/get', {
                'playlist_id': pid, 'extra': 'tracks', 'limit': 10, 'offset': 0,
            })
            self._json({'playlist_id': pid, 'response': raw})
        elif path == '/api/delete-local':
            # マイ保存からアルバムを削除
            album_id = q('album_id')
            if not album_id:
                self._json({'ok': False, 'error': 'album_id required'}, 400)
                return
            try:
                favs = load_local_favorites()
                before = len(favs)
                favs = [f for f in favs if f.get('album_id') != album_id]
                if len(favs) < before:
                    FAVORITES_PATH.write_text(json.dumps(favs, ensure_ascii=False, indent=2))
                    self._json({'ok': True, 'deleted': before - len(favs)})
                else:
                    self._json({'ok': False, 'error': 'album not found'})
            except Exception as e:
                self._json({'ok': False, 'error': str(e)}, 500)
        else:
            self._send('<p>Not Found</p>', code=404)


_server        = None
_server_thread = None
_CLOSE_SIGNAL  = __import__('threading').Event()  # b キー押下で set()
_PLAYLIST_STATUS: dict = {          # qji_qobuz.py から直接書き込まれる
    'active': False, 'total': 0, 'current': 0,
    'title': '', 'artist': '', 'img': '', 'queue': [],
}

def start_browser_server(port=PORT, open_browser=True):
    global _server, _server_thread
    if _server: return True
    try:
        _server = HTTPServer(('0.0.0.0', port), Handler)
        _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
        _server_thread.start()
        print(f'  🌐 Browser UI: http://localhost:{port}')
        if open_browser:
            time.sleep(0.3)
            try:
                subprocess.Popen(['xdg-open', f'http://localhost:{port}'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception: pass
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
    """qji_qobuz.py の b キー押下時に呼ぶ。ブラウザタブを閉じる。"""
    _CLOSE_SIGNAL.set()
    # 少し待ってからリセット（次回起動時のために）
    def _reset():
        import time as _t; _t.sleep(3); _CLOSE_SIGNAL.clear()
    __import__('threading').Thread(target=_reset, daemon=True).start()


def check_browser_request():
    if not REQUEST_PATH.exists(): return None
    try:
        req = json.loads(REQUEST_PATH.read_text())
        if time.time() - req.get('requested_at', 0) > 10:
            REQUEST_PATH.unlink(missing_ok=True); return None
        REQUEST_PATH.unlink(missing_ok=True)
        return req
    except Exception:
        return None

if __name__ == '__main__':
    import signal
    print(f'🎵 Qji × Qobuz Browser UI  port:{PORT}')
    start_browser_server(PORT, open_browser=True)
    def _sig(s, f):
        print('\nExiting'); stop_browser_server(); sys.exit(0)
    signal.signal(signal.SIGINT, _sig)
    print('  Ctrl+C to exit')
    while True:
        req = check_browser_request()
        if req: print(f'  📱 Request: {req}')
        time.sleep(0.5)
