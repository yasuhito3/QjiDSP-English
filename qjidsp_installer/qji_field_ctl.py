# -*- coding: utf-8 -*-
"""
qji_field_ctl.py — sound-field preset control (module shared by qji.py and wobble)

  qji.py   : press [e] then another key to pick a sound-field preset (writer side)
  wobble_vN: reads the control file every 0.2 s and updates the wobble parameters on the fly (reader side)

Location: ~/camilladsp_test/ (the same place as the wobble scripts).
Note: with WOBBLE_LINK=False the wobble does not read the control file and [e] handles only instrument EQ and memory (v7/v8 use True).
qji.py adds this folder to sys.path
          and imports it from there.
Control file: /run/user/UID/qji_field_ctl.json  (changeable with the QJI_FIELD_CTL environment variable)
Per-track / per-album memory: ~/qji_field_prefs.json  (changeable with the QJI_FIELD_PREFS environment variable)
  At the start of each track on_track_start() is called, and settings are decided in the order: track memory > album memory > automatic rules (from the title, etc.) > default,
  then reflected into the SI EQ before the filter is built (no [r] reload needed).
"""
import json
import os
import select
import sys
import time

def _default_ctl_path():
    """Prefer a place other users cannot rewrite (/run/user/UID). qji and wobble end up with the same path regardless of environment variables."""
    d = f"/run/user/{os.getuid()}"
    if os.path.isdir(d) and os.access(d, os.W_OK):
        return os.path.join(d, 'qji_field_ctl.json')
    return f"/tmp/qji_field_ctl_{os.getuid()}.json"


CTL_PATH = os.environ.get('QJI_FIELD_CTL', _default_ctl_path())
PREFS_PATH = os.environ.get('QJI_FIELD_PREFS', os.path.expanduser('~/qji_field_prefs.json'))

# Default state: wobble and volume-link are ON. For tracks that should have them OFF, switch them off with [e]->[f]/[l] and store that with [e]->[m].
DEFAULT_FLUTTER = True
DEFAULT_LOUD = True
RULES_ENABLED = True    # automatic selection from the title etc. (only for tracks with no memory)

# ★ Whether the wobble side (reverb / wobble / volume-link) reads this control file.
#   False: [e] handles only instrument EQ, SI EQ and track/album memory (for the static wobble).
#   (v7/v8 use a wobble that reads the control file, so the current default is True)
#          [w][n][f][l] and the reverb / wobble / volume-link display are not shown.
#   True : combined with a reactive wobble that reads read_ctl() (v7/v8 use this).
WOBBLE_LINK = True

# -- Defaults the wobble side receives (= reset state) ---------------------------
DEFAULTS = {
    'seq': 0,
    'label': 'Normal',
    'wet_offset_db': 0.0,    # added to wet_gain (dB). + = wider sound field, - = closer
    'depth_scale': 1.0,      # multiplier for the wobble depth
    'period_scale': 1.0,     # multiplier for the wobble period (<1 = faster)
    'loud_link': False,      # volume-link (the louder it is, the wider the sound field)
    'loud_boost_db': 1.5,    # maximum added amount of volume-link (dB)
}

# -- Scenes (only one active at a time): spatial axis + (optionally) ffmpeg-side SI-EQ delta --
#  wet_offset_db is how much wider / closer to go. The values start modest, so adjust by ear.
# Pressing the same scene key repeatedly cycles weak -> medium -> strong -> off. The multiplier applies to both the wet amount and the EQ delta.
LEVELS = (1.0, 1.5, 2.0)          # w / n / o (wider / closer / vocal forward)
BEAUTY_LEVELS = (0.8, 1.1, 1.5)   # "beautifully" presets (modest, to avoid listening fatigue)
LEVEL_NAMES = ('Weak', 'Medium', 'Strong')
BEAUTY_KEYS = ['v', 'u', 'b', 'c', 't', 'h', 'd', 'p', 'q', 'j', 's',
               'a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7', 'a8']   # scenes that use BEAUTY_LEVELS

SCENES = {
    'w': {'label': 'Wider sound field',         'wet_offset_db': +2.0, 'eq': None},
    'n': {'label': 'Closer sound field',         'wet_offset_db': -2.0, 'eq': None},
    # -- "Bring forward" type: voices are mid-centred so they are less piercing (the violin "forward" preset was dropped) --
    'o': {'label': 'Vocals forward',   'wet_offset_db': -1.5,
          'eq': {'mid': +3.0, 'upper_mid': +2.5, 'presence': +1.0}},
    # -- "Make it sound beautiful" type: add a little reverb (wet), do not lift 2-5 kHz,
    #    but pull it down slightly to soften hardness, and tune the sheen of the overtones (air bands) and the body richness (mid bands) --
    #    The SI EQ uses fairly narrow bell shapes (centre frequency +/- width in Hz), so the adjustments are spread over neighbouring bands.
    'v': {'label': 'Violin, beautifully', 'wet_offset_db': +1.5,
          'eq': {'mid': +0.5, 'presence': -0.5, 'air_low': +2.0, 'air': +2.0}},
    # "Moist" type: pull down around 2 kHz (presence) to thicken the mids, round off the edges, then add a little 6-10 kHz sheen and low-end foundation.
    #  (For comparing against [v]. At the strong level it comes close to the sound that arose by accident when the SI EQ stacked up: mids +3.6 / presence -3.6 / air +3.0)
    'u': {'label': 'Violin, warm and moist', 'wet_offset_db': +1.5,
          'eq': {'sub_bass': +0.3, 'bass': +0.6, 'low_mid': +0.3, 'mid': +2.4, 'presence': -2.4,
                 'air_low': +2.0, 'air': +2.0}},
    'b': {'label': 'Oboe, beautifully',     'wet_offset_db': +1.5,
          'eq': {'low_mid': +1.0, 'mid': +1.5, 'upper_mid': +0.5,
                 'presence': -0.5, 'air_low': +1.5}},
    'c': {'label': 'Clarinet, beautifully', 'wet_offset_db': +1.5,
          'eq': {'low_mid': +1.5, 'mid': +1.5, 'presence': -0.5, 'air': +1.5}},
    'p': {'label': 'Piano, beautifully',       'wet_offset_db': +2.0,
          'eq': {'bass': +1.0, 'mid': +0.5, 'high_mid': +0.5,
                 'air_low': +1.5, 'air': +2.0}},
    # -- Additional classical presets --
    'd': {'label': 'Cello / low strings, beautifully', 'wet_offset_db': +1.5,
          'eq': {'bass': +1.0, 'low_mid': +0.5, 'mid': +1.0, 'presence': -0.5, 'air_low': +1.0}},
    't': {'label': 'Flute, beautifully',     'wet_offset_db': +1.5,
          'eq': {'low_mid': +0.5, 'upper_mid': +0.5, 'presence': -0.5,
                 'air': +2.0, 'air_high': +1.0}},
    'h': {'label': 'Horn / brass, soft and bright', 'wet_offset_db': +1.5,
          'eq': {'low_mid': +1.0, 'mid': +1.5, 'high_mid': -1.0, 'air_low': +1.0}},
    # -- Vocal: pressing [a] opens a second menu [1]-[8]. The "body" (fundamental thickness) band differs by voice range.
    #    Singing voices tend to have sibilance above 4 kHz, so do not lift it, and pull down slightly around 2 kHz --
    'a1': {'label': 'Soprano (vocal), beautifully', 'wet_offset_db': +1.5,
           'eq': {'mid': +1.0, 'upper_mid': +1.0, 'presence': -0.5, 'air_low': +1.5, 'air': +1.5}},
    'a2': {'label': 'Alto / mezzo (vocal), beautifully', 'wet_offset_db': +1.5,
           'eq': {'low_mid': +1.0, 'mid': +1.5, 'presence': -0.5, 'air_low': +1.0, 'air': +1.0}},
    'a3': {'label': 'Tenor (vocal), beautifully', 'wet_offset_db': +1.5,
           'eq': {'low_mid': +0.5, 'mid': +1.0, 'upper_mid': +1.0, 'presence': -0.5,
                  'air_low': +1.0, 'air': +1.0}},
    'a4': {'label': 'Baritone (vocal), beautifully', 'wet_offset_db': +1.5,
           'eq': {'bass': +0.5, 'low_mid': +1.0, 'mid': +1.0, 'upper_mid': +0.5,
                  'presence': -0.5, 'air_low': +1.0}},
    'a5': {'label': 'Bass (vocal), beautifully', 'wet_offset_db': +1.5,
           'eq': {'bass': +1.0, 'low_mid': +0.5, 'mid': +1.0, 'upper_mid': +0.5, 'air_low': +1.0}},
    'a6': {'label': 'Choir resonance, beautifully', 'wet_offset_db': +2.0,
           'eq': {'low_mid': +0.5, 'mid': +0.5, 'upper_mid': +0.5, 'presence': -0.5,
                  'air_low': +1.0, 'air': +1.5}},
    # -- Jazz / pop vocals: keep the reverb (wet) modest to preserve the closeness and warmth of the voice --
    'a7': {'label': 'Female vocal (jazz / pop), beautifully', 'wet_offset_db': +0.5,
           'eq': {'low_mid': +0.5, 'mid': +1.0, 'upper_mid': +0.5, 'presence': -0.5,
                  'air_low': +1.0, 'air': +1.0}},
    'a8': {'label': 'Male vocal (jazz / pop), beautifully', 'wet_offset_db': +0.5,
           'eq': {'bass': +0.5, 'low_mid': +0.5, 'mid': +1.0, 'upper_mid': +0.5,
                  'presence': -0.5, 'air_low': +1.0}},
    'q': {'label': 'String-quartet unity',   'wet_offset_db': +1.0,
          'eq': {'low_mid': +0.5, 'mid': +1.0, 'presence': -0.5, 'air_low': +1.5, 'air': +1.0}},
    # -- Jazz: modest reverb (wet). Bass: tidy the body resonance and add a little definition; sax: body and breathiness --
    'j': {'label': 'Jazz bass (pizzicato), beautifully', 'wet_offset_db': +0.5,
          'eq': {'sub_bass': +0.5, 'bass': +1.5, 'low_mid': -1.0, 'upper_mid': +1.0, 'presence': +0.5}},
    's': {'label': 'Saxophone resonance',   'wet_offset_db': +1.0,
          'eq': {'low_mid': +1.0, 'mid': +1.0, 'presence': -0.5, 'air_low': +1.0}},
}
for _k in BEAUTY_KEYS:
    SCENES[_k]['levels'] = BEAUTY_LEVELS

# ★ How reverb is added per sound field (v7 / v8). The EQ (instrument / vocal) is the same for both; only the strength of reverb (wet), wobble and volume-link differs.
#   inst_wet_scale: multiplier for the reverb that the "beautifully" instrument / vocal presets add (not applied to [w] wider / [n] closer / [o] vocal forward)
#   flutter_depth : depth multiplier when [f] wobble is ON / loud_boost_db: maximum added amount of [l] volume-link (dB)
PROFILES = {
    '7': {'name': 'Harmonic Hall mode', 'inst_wet_scale': 0.0, 'flutter_depth': 2.0,
          'flutter_period': 0.5, 'loud_boost_db': 1.5},   # not muffled and easy to listen to (EQ-centred, modest reverb)
    '8': {'name': 'Tangible Hall',     'inst_wet_scale': 1.0, 'flutter_depth': 4.0,
          'flutter_period': 0.5, 'loud_boost_db': 3.0},   # dense reverb with a strong sense of physical presence (slightly prone to muffling)
}
_profile_key = '7'


def _prof():
    return PROFILES[_profile_key]


def set_venue(venue):
    """Call this when the DSP sound field changes (outside v7/v8 the modest v7 settings are used). The control file is updated when it changes."""
    global _profile_key
    key = str(venue) if str(venue) in PROFILES else '7'
    if key != _profile_key:
        _profile_key = key
        try:
            publish()
        except Exception:
            pass

KEY_WAIT_SEC = 20.0     # seconds to wait for the next key after [e] (cancelled on timeout). The same applies to the second menus ([a] vocal / [m] memory)
EQ_BAND_CAP = 6.0      # cap per band (dB). Anything above tends to sound piercing, so it is clipped

VOICE_GUIDE = ("🎤 Vocal: [1]Soprano [2]Alto/Mezzo [3]Tenor [4]Baritone [5]Bass [6]Choir\n"
               "   Songs (jazz / pop): [7]Female [8]Male")


GUIDE_FULL = ("🎛 Sound field: [w]Wider [n]Closer [o]Vocal forward [f]Wobble [l]Volume-link [r]Reset to default\n"
         "   [z]Use / don't use SI EQ [m]Memory (track/album) [?]Current settings\n"
         "   Classical: [v]Violin [u]Violin (moist) [b]Oboe [c]Clarinet [t]Flute [h]Horn/brass\n"
         "              [d]Cello/low strings [p]Piano [q]String quartet [a]Vocal (->1-8)\n"
         "   Jazz: [j]Bass (pizzicato) [s]Sax     (press the same key repeatedly: weak -> medium -> strong -> off)")
GUIDE_EQ = GUIDE_FULL.replace("[w]Wider [n]Closer [o]Vocal forward [f]Wobble [l]Volume-link [r]Reset to default",
                              "[o]Vocal forward [r]Reset to default")
GUIDE = GUIDE_FULL if WOBBLE_LINK else GUIDE_EQ

# -- qji.py-side state (in process) ------------------------------------------
_state = {'scene': None, 'level': 1, 'flutter': DEFAULT_FLUTTER, 'loud': DEFAULT_LOUD,
          'source': 'default', 'reason': '', 'si_off': False}
_cur = {'path': None, 'title': '', 'album_key': None, 'album_name': '', 'saved': False}   # the track now playing
# The SI EQ confirmed in the [x] menu. Valid until the track changes (re-applied to SI on every reload of that track).
# Closing the [x] menu automatically reloads the track from the start, and the saved profile and SI memory were re-read at that point,
# cancelling [EQ off] and other adjustments. This "confirmed value for the duration of the track" prevents that.
_si_override = {'path': None, 'eq': None}
SOURCE_LABELS = {'default': 'Default', 'rule': 'Auto (from title etc.)', 'track': 'This track\'s memory',
                 'album': 'This album\'s memory', 'preset': 'Per-sound-field default', 'manual': 'Manual (unsaved)'}


def _write_atomic(data):
    tmp = CTL_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, CTL_PATH)


def _levels_for(key):
    sc = SCENES.get(key)
    return sc.get('levels', LEVELS) if sc else LEVELS


def _derive():
    d = dict(DEFAULTS)
    d['seq'] = int(time.time() * 1000) % 2_000_000_000
    labels = []
    sc = SCENES.get(_state['scene'])
    if sc:
        mult = _levels_for(_state['scene'])[_state['level'] - 1]
        if WOBBLE_LINK:
            _wo = sc['wet_offset_db'] * mult
            if _state['scene'] not in ('w', 'n', 'o') and _wo > 0:
                _wo *= _prof()['inst_wet_scale']
            d['wet_offset_db'] = round(_wo, 2)
        labels.append(f"{sc['label']} ({LEVEL_NAMES[_state['level'] - 1]})")
    if WOBBLE_LINK and _state['flutter']:
        d.update(depth_scale=_prof()['flutter_depth'], period_scale=_prof()['flutter_period'])
        labels.append('Wobble')
    if WOBBLE_LINK and _state['loud']:
        d['loud_link'] = True
        d['loud_boost_db'] = _prof()['loud_boost_db']
        labels.append('Volume-link')
    if _state.get('si_off'):
        labels.append('No SI EQ')
    d['label'] = ' + '.join(labels) if labels else 'Normal'
    return d


def publish():
    d = _derive()
    _write_atomic(d)
    return d


def _reset_state(source='default'):
    _state.update({'scene': None, 'level': 1, 'flutter': DEFAULT_FLUTTER,
                   'loud': DEFAULT_LOUD, 'source': source, 'reason': '', 'si_off': False})


def init():
    """Call at qji startup: do not carry over the previous state; start from the default state (wobble and volume-link ON)"""
    _reset_state()
    _cur.update(path=None, title='', album_key=None, album_name='', saved=False)
    _si_override.update(path=None, eq=None)
    try:
        publish()
    except Exception:
        pass


# -- wobble side: reading ------------------------------------------------------
_cache = {'mtime': None, 'data': dict(DEFAULTS)}


def _num(v, lo, hi, dflt):
    try:
        x = float(v)
    except Exception:
        return dflt
    if x != x:          # NaN
        return dflt
    return max(lo, min(hi, x))


def _sanitize(d):
    d['wet_offset_db'] = _num(d.get('wet_offset_db'), -6.0, 6.0, 0.0)
    d['depth_scale'] = _num(d.get('depth_scale'), 0.0, 8.0, 1.0)
    d['period_scale'] = _num(d.get('period_scale'), 0.25, 4.0, 1.0)
    d['loud_boost_db'] = _num(d.get('loud_boost_db'), 0.0, 6.0, 1.5)
    d['loud_link'] = bool(d.get('loud_link'))
    d['label'] = str(d.get('label', ''))[:80]
    return d


def read_ctl():
    """Read the control file. If it is missing or broken, return the previous value (the default at first)."""
    try:
        m = os.stat(CTL_PATH).st_mtime_ns
        if m != _cache['mtime']:
            with open(CTL_PATH, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            data = dict(DEFAULTS)
            for k in DEFAULTS:
                if k in raw:
                    data[k] = raw[k]
            data = _sanitize(data)
            _cache['mtime'], _cache['data'] = m, data
    except FileNotFoundError:
        _cache['mtime'], _cache['data'] = None, dict(DEFAULTS)
    except Exception:
        pass   # keep the previous value, e.g. when a write is in progress
    return _cache['data']


# -- Reflecting into SI (ffmpeg-side EQ) ----------------------------------------
# -- The SI EQ is never modified ---------------------------------------------
# (The old method rewrote SI's current_params.eq directly, so when SI reused the EQ dictionary internally
#  the sound-field preset amount piled up every time a track started. Now a combined view of
#  "SI EQ + sound-field preset EQ" is handed over only at the moment the filter is built. SI's own values and saved data are not changed.)
EQ_VIEW_CAP = 8.0      # final cap per band (dB). Even if an odd value arrives, extreme values are never passed to ffmpeg


class _EqView:
    """Read-only EQ view handed to _si_get_eq_filters() (has only .eq)"""
    def __init__(self, eq):
        self.eq = eq


def params_with_scene(p):
    base = {}
    if p is not None and not _state.get('si_off'):
        try:
            base = dict(getattr(p, 'eq', None) or {})
        except Exception:
            base = {}
    delta = _scene_eq_delta()
    if delta:
        for band, d in delta.items():
            base[band] = round(base.get(band, 0.0) + d, 2)
    for band in list(base):
        try:
            base[band] = max(-EQ_VIEW_CAP, min(EQ_VIEW_CAP, float(base[band])))
        except Exception:
            base.pop(band, None)
    return _EqView(base)


def _scene_eq_delta():
    sc = SCENES.get(_state['scene'])
    if not sc or not sc['eq']:
        return None
    mult = _levels_for(_state['scene'])[_state['level'] - 1]
    return {b: round(max(-EQ_BAND_CAP, min(EQ_BAND_CAP, d * mult)), 2)
            for b, d in sc['eq'].items()}


# -- Per-track / per-album memory (~/qji_field_prefs.json) -------------------------
# Kept in a file separate from music_mood_db.json: several processes read and write the whole DB,
# so writing to the same place risks overwrites and corruption. A small dedicated file is fast and safe.
def _empty_prefs():
    return {'version': 1, 'tracks': {}, 'albums': {}}


def _load_prefs():
    try:
        with open(PREFS_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return _empty_prefs()
        d.setdefault('version', 1)
        if not isinstance(d.get('tracks'), dict):
            d['tracks'] = {}
        if not isinstance(d.get('albums'), dict):
            d['albums'] = {}
        return d
    except FileNotFoundError:
        return _empty_prefs()
    except Exception:
        return None          # broken: treated as "no memory" when reading; when writing, it is set aside and rebuilt


def _save_prefs(d):
    try:
        os.makedirs(os.path.dirname(PREFS_PATH) or '.', exist_ok=True)
        tmp = PREFS_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PREFS_PATH)
        return True
    except Exception:
        return False


def _prefs_for_write():
    d = _load_prefs()
    if d is None:       # a broken file is set aside rather than deleted
        try:
            os.replace(PREFS_PATH, PREFS_PATH + time.strftime('.broken-%Y%m%d-%H%M%S'))
        except Exception:
            pass
        d = _empty_prefs()
    return d


def _current_record():
    return {'scene': _state['scene'], 'level': _state['level'],
            'flutter': bool(_state['flutter']), 'loud': bool(_state['loud']),
            'si_off': bool(_state.get('si_off')),
            'title': (_cur['title'] or '')[:60], 'saved': time.strftime('%Y-%m-%d')}


# -- Automatic rules from the title etc. (only for tracks with no memory) --------------
# Modest rules that apply only when the lead instrument is obvious from the title.
# Nothing is done for pieces such as symphonies with no single lead. Feel free to add or change. (regex, scene key, level 1-3)
# (The Japanese patterns are intentional: they let Japanese-language track titles match as well.)
RULES = [
    (r'violin\s+concerto|concerto\s+for\s+violin|violinkonzert|concerto\s+pour\s+violon'
     r'|ヴァイオリン協奏曲|バイオリン協奏曲|ヴァイオリン・協奏曲', 'v', 2),
    (r'violin\s+sonata|sonata\s+for\s+violin|ヴァイオリン・?ソナタ|バイオリン・?ソナタ', 'v', 2),
    (r'piano\s+concerto|concerto\s+for\s+piano|klavierkonzert|ピアノ協奏曲|ピアノ・協奏曲', 'p', 2),
    (r'piano\s+sonata|klaviersonate|ピアノ・?ソナタ', 'p', 2),
    (r'oboe\s+concerto|concerto\s+for\s+oboe|オーボエ協奏曲', 'b', 2),
    (r'clarinet\s+(concerto|quintet)|concerto\s+for\s+clarinet|クラリネット協奏曲|クラリネット五重奏', 'c', 2),
    (r'flute\s+concerto|concerto\s+for\s+flute|フルート協奏曲', 't', 2),
    (r'horn\s+concerto|concerto\s+for\s+horn|ホルン協奏曲', 'h', 2),
    (r'cello\s+(concerto|suite|sonata)|concerto\s+for\s+cello|チェロ協奏曲|チェロ組曲|無伴奏チェロ|チェロ・?ソナタ',
     'd', 2),
    (r'string\s+quartet|streichquartett|弦楽四重奏', 'q', 2),
]


# -- Per-sound-field default (only for tracks with neither memory nor a title rule) ---
# (DSP sound-field number, filter preset name, scene key, level 1-3)
#   Only when the sound field is V7 or V8 and the filter is musikverein (chosen with [c], auto genre detection or a saved profile all count),
#   "Violin, beautifully - weak" becomes the default. Nothing is applied for other sound fields.
CONTEXT_DEFAULTS = [
    ('7', 'musikverein', 'v', 1),
    ('8', 'musikverein', 'v', 1),
]


def _context_default(context):
    if not context or not context.get('dsp'):
        return None
    for venue, fp, scene, level in CONTEXT_DEFAULTS:
        if str(context.get('venue')) == venue and context.get('filter_preset') == fp and scene in SCENES:
            return scene, level, f"V{venue} x {fp}"
    return None


def _source_tag():
    src = _state['source']
    tag = SOURCE_LABELS.get(src, src)
    if src == 'rule' and _state['reason']:
        tag += f": \"{_state['reason']}\""
    elif src == 'preset' and _state['reason']:
        tag += f"({_state['reason']})"
    return tag


def _match_rule(track):
    import re
    hay = ' '.join(str(track.get(k, '') or '') for k in
                   ('title', 'album', 'composer', 'performer', 'genre', 'filename')).lower()
    for pat, scene, level in RULES:
        m = re.search(pat, hay, re.I)
        if m and scene in SCENES:
            return scene, level, m.group(0)
    return None


def set_si_eq_override(path, eq):
    """Call when the [x] menu is closed: remember the current SI EQ (empty after a reset) as the confirmed value for the duration of this track."""
    if not path:
        return
    _si_override['path'] = os.path.normpath(path)
    _si_override['eq'] = {b: round(float(v), 2) for b, v in (eq or {}).items()
                          if isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) > 0.001}


def eq_without_scene(eq):
    """For compatibility: sound-field presets are no longer added to the SI EQ, so the given EQ is returned as is (for [s] saving)."""
    return {b: v for b, v in (eq or {}).items() if abs(v) > 0.01}


def on_track_start(track, si=None, say=print, context=None):
    """Call at the start of a track (before the filter is built).
    Settings are decided in the order track memory > album memory > automatic rules > default, then reflected into the SI EQ and the wobble.
    On a reload of the same track ([r] / repeat), manual changes are kept and only the EQ is re-applied to the new filter."""
    path = (track or {}).get('path') or ''
    if not path:
        return
    if context and context.get('dsp'):
        set_venue(context.get('venue'))
    norm = os.path.normpath(path)
    same = (_cur['path'] == norm)
    album_dir = os.path.dirname(norm)
    if _si_override['path'] is not None:
        if _si_override['path'] == norm:
            _p = getattr(si, 'current_params', None) if si else None
            if _p is not None:
                try:
                    _p.eq = dict(_si_override['eq'] or {})      # assignment only (no accumulation): the same result however often it is called
                except Exception:
                    pass
        else:
            _si_override.update(path=None, eq=None)         # the track has changed
    _cur.update(path=norm, title=str(track.get('title', '') or os.path.basename(norm)),
                album_key=album_dir, album_name=os.path.basename(album_dir))
    if same and _state['source'] in ('default', 'preset'):
        # Reload of the same track: if things changed (e.g. the filter was switched with [c]), re-evaluate the per-sound-field default
        ctxd = _context_default(context)
        if ctxd:
            _state.update(scene=ctxd[0], level=ctxd[1], source='preset', reason=ctxd[2])
        else:
            _state.update(scene=None, level=1, source='default', reason='')
    if not same:
        prefs = _load_prefs() or _empty_prefs()
        rec, src, reason = prefs['tracks'].get(norm), 'track', ''
        if rec is None:
            rec, src = prefs['albums'].get(album_dir), 'album'
        if rec is None and RULES_ENABLED:
            hit = _match_rule(track)
            if hit:
                rec = {'scene': hit[0], 'level': hit[1]}
                src, reason = 'rule', hit[2]
        if rec is None:
            ctxd = _context_default(context)
            if ctxd:
                rec = {'scene': ctxd[0], 'level': ctxd[1]}
                src, reason = 'preset', ctxd[2]
        if rec is None:
            rec, src = {}, 'default'
        scene = rec.get('scene')
        lv = rec.get('level', 1)
        _state.update(
            scene=scene if scene in SCENES else None,
            level=lv if isinstance(lv, int) and 1 <= lv <= 3 else 1,
            flutter=bool(rec.get('flutter', DEFAULT_FLUTTER)),
            loud=bool(rec.get('loud', DEFAULT_LOUD)),
            si_off=bool(rec.get('si_off', False)),
            source=src, reason=reason)
    d = publish()
    say(f"   🎛 Sound field: {d['label']}  [{_source_tag()}]")


def consume_saved():
    """Return True if the previous operation stored a track/album memory, and clear the flag (qji uses this to return to the cover-art display)."""
    v = bool(_cur.get('saved'))
    _cur['saved'] = False
    return v


MEM_GUIDE = ("💾 Store the current settings: [1]This track  [2]Whole album\n"
             "   Clear a memory: [3]This track  [4]Whole album")


def _memory_menu(say, timeout):
    if not _cur['path']:
        say("   ⚠️ No track to store a memory for (only while a local track is playing)")
        return
    say(MEM_GUIDE)
    sub = _read_key(KEY_WAIT_SEC if timeout is None else timeout)
    if sub not in ('1', '2', '3', '4'):
        say("   (cancelled)" if sub is None else "   (no match)")
        return
    d = _prefs_for_write()
    if sub in ('1', '2'):
        table, key = ('tracks', _cur['path']) if sub == '1' else ('albums', _cur['album_key'])
        d[table][key] = _current_record()
        ok = _save_prefs(d)
        if ok:
            _state['source'] = 'track' if sub == '1' else 'album'
            _state['reason'] = ''
            what = 'this track' if sub == '1' else f"the album \"{_cur['album_name']}\""
            _cur['saved'] = True
            say(f"   💾 Stored for {what}: {_derive()['label']}")
        else:
            say("   ⚠️ Failed to save the memory")
    else:
        table, key = ('tracks', _cur['path']) if sub == '3' else ('albums', _cur['album_key'])
        if d[table].pop(key, None) is None:
            say("   (there is no such memory)")
        elif _save_prefs(d):
            _state['source'] = 'manual'
            say("   🗑 Memory cleared (from next time the automatic rules / default apply)")
        else:
            say("   ⚠️ Failed to update the memory")


# -- Key handling (two stages): [e] -> w/n/v/o/f/l/r ----------------------------------
BAND_JA = {
    'sub_bass': 'Sub-bass', 'bass': 'Bass', 'low_mid': 'Low mids', 'mid': 'Mids',
    'upper_mid': 'Upper mids', 'presence': 'Presence', 'high_mid': 'High mids',
    'air_low': 'Air (low)', 'air': 'Air', 'air_high': 'Air (high)',
}


def status_lines(si=None):
    """Return the current settings as a list of display lines ([e]->[?])"""
    d = _derive()
    sc = SCENES.get(_state['scene'])
    L = ["📋 Current sound-field settings"]
    if sc:
        lv = _state['level']
        mult = _levels_for(_state['scene'])[lv - 1]
        L.append(f"   Instrument/scene : {sc['label']}  [{LEVEL_NAMES[lv - 1]} x{mult:g}]")
        if WOBBLE_LINK:
            L.append(f"   Reverb (wet)     : {d['wet_offset_db']:+.1f} dB")
        eq = _scene_eq_delta()
        if eq:
            L.append("   Instrument EQ   : " + ' / '.join(
                f"{BAND_JA.get(b, b)} {v:+.1f}" for b, v in eq.items()) + " dB")
    else:
        L.append("   Instrument/scene : none (normal)")
    if WOBBLE_LINK:
        L.append("   Wobble           : " + ("ON (depth x%g / period x%g)" % (_prof()['flutter_depth'], _prof()['flutter_period'])
                                    if _state['flutter'] else "OFF"))
        L.append("   Volume-link      : " + (f"ON (max +{_prof()['loud_boost_db']:g} dB)" if _state['loud'] else "OFF"))
    if _state.get('si_off'):
        L.append("   SI EQ            : not used (instrument EQ only)  *restore with [e]->[z]")
    if _si_override['path'] is not None and _si_override['path'] == _cur.get('path'):
        L.append("   SI EQ            : holding the values adjusted in the [x] menu for this track (save with [s] to keep them)")
    p = getattr(si, 'current_params', None) if si else None
    eqall = {b: v for b, v in (getattr(p, 'eq', None) or {}).items() if abs(v) > 0.01}
    if eqall:
        L.append("   SI EQ total      : " + ' / '.join(
            f"{BAND_JA.get(b, b)} {v:+.1f}" for b, v in eqall.items()) + " dB")
        if any(abs(v) > EQ_VIEW_CAP for v in eqall.values()):
            L.append("   ⚠️ The SI EQ holds a large value (possibly abnormal). Use [x] -> clear all EQ, or restart Qji")
    src = _state['source']
    L.append(f"   Setting source   : {_source_tag()}")
    prefs = _load_prefs() or _empty_prefs()
    have = []
    if _cur['path'] and _cur['path'] in prefs['tracks']:
        have.append('this track')
    if _cur['album_key'] and _cur['album_key'] in prefs['albums']:
        have.append('album')
    L.append("   Memory           : " + (', '.join(have) + ' (stored)' if have else 'none'))
    if src == 'manual':
        L.append("   * Unsaved: on the next track the memory / automatic rules / default apply again. To keep it: [e]->[m]")
    L.append("   * Instrument EQ takes effect from the next track or an [r] reload" + (" / reverb and wobble apply to V7 / V8 only" if WOBBLE_LINK else ""))
    return L


def _read_key(timeout):
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.read(1).lower() if r else None


def handle_two_stage_key(si=None, say=print, timeout=None, key=None):
    """Call after [e] has been pressed. Waits for the next key and applies the sound-field preset.
    Returns True if the SI EQ was changed (it takes effect from the next track / [r] reload)."""
    if key is None:
        say(f"\n📋 Now: {_derive()['label']}\n" + GUIDE)
        key = _read_key(KEY_WAIT_SEC if timeout is None else timeout)
    if key is None:
        say("   (timed out: cancelled)")
        return False
    eq_changed = False
    _before = _scene_eq_delta()
    if key == 'a':                      # vocal: second menu
        say(VOICE_GUIDE)
        sub = _read_key(KEY_WAIT_SEC if timeout is None else timeout)
        if sub is None:
            say("   (timed out: cancelled)")
            return False
        key = 'a' + sub
        if key not in SCENES:
            say("   (no match)  " + VOICE_GUIDE)
            return False
    if key in ('?', '/'):
        for line in status_lines(si):
            say(line)
        return False
    if key == 'm':                      # memory menu (save / clear)
        _memory_menu(say, timeout)
        return False
    if not WOBBLE_LINK and key in ('w', 'n', 'f', 'l'):   # disabled when the wobble side is not used
        say("   (no match)  " + GUIDE)
        return False
    if key in SCENES:
        # same key repeatedly: weak -> medium -> strong -> off / a different scene starts at "weak"
        if _state['scene'] == key:
            if _state['level'] < len(_levels_for(key)):
                _state['level'] += 1
            else:
                _state['scene'], _state['level'] = None, 1
        else:
            _state['scene'], _state['level'] = key, 1
    elif key == 'f':
        _state['flutter'] = not _state['flutter']
    elif key == 'l':
        _state['loud'] = not _state['loud']
    elif key == 'z':                    # toggle between using / not using the SI EQ
        _state['si_off'] = not _state.get('si_off')
    elif key == 'r':
        _reset_state('manual')
    else:
        say("   (no match)  " + GUIDE)
        return False
    _state['source'], _state['reason'] = 'manual', ''
    eq_changed = (_scene_eq_delta() != _before) or key == 'z' or key == 'r'
    d = publish()
    if WOBBLE_LINK:
        say(f"   🎛 Sound-field preset: {d['label']}"
            f"   [wet {d['wet_offset_db']:+.1f}dB / wobble x{d['depth_scale']:g}"
            f" / volume-link {'ON' if d['loud_link'] else 'OFF'}]")
    else:
        say(f"   🎛 Sound-field preset: {d['label']}")
    if eq_changed:
        say("   ℹ️ The instrument EQ takes effect from the next track or an [r] reload")
    return eq_changed
