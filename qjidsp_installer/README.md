# Qji

A hi-fi music playback system for Linux. In addition to local file playback, it integrates streaming from Qobuz, SoundCloud, and YouTube Music, 3D spatial audio extension via CamillaDSP (soundfields v1-v6), genre-adaptive EQ (Sonia Intelligence), and automatic distortion mitigation (Auto De-Clip).

> Developed and refined for daily use in a personal hi-fi setup (Mark Levinson amplifier, Amanero Combo384 USB DAC, etc.).

> ⚠️ **Prerequisite**: This repository (the QjiDSP extension) assumes **the [base Qji app](https://github.com/yasuhito3/Qji-Network-Audio-Player) is already installed**.
> If you haven't installed it yet, please set that up first, following its own install instructions, before running this repository's installer.

---

## Features

- **Local playback**: browse by album art, folder playback, search by composer / performer / conductor / genre / mood
- **Streaming**: Qobuz (Hi-Res), SoundCloud (fetched directly through the SoundCloud API), YouTube Music (`yt-dlp` + `ytmusicapi`)
- **3D spatial audio extension (QjiDSP)**: a DSP pipeline through a virtual sound card (ALSA Loopback) powered by CamillaDSP, with six soundfield presets:
  1. Rich hall (static) — full EQ chain, calm reverb
  2. Rich hall (dynamic) — full EQ chain, gentle airflow modulation
  3. Natural timbre (static) — simple, emphasizes presence and realism
  4. Natural timbre (dynamic) — simple, with subtle panning
  5. Harmonics mode — violin resonance and shakuhachi-style harmonic emphasis
  6. Harmonics mode (for headphones) — crossfeed processing optimized for headphone listening
- **Sonia Intelligence (SI)**: automatically adjusts EQ based on the genre currently playing
- **Auto De-Clip**: detects distortion in real time via ffmpeg's astats/ametadata filters and auto-adjusts gain
- **Voice control (optional)**: offline Japanese speech recognition via Vosk
- **AirPlay / UPnP receiver**: via shairport-sync / gmediarender
- **Named presets**: save and recall volume, audio, and gain presets by name

---

## Requirements

- Linux (tested on Ubuntu / Linux Mint)
- Python 3.10+
- ALSA (uses the `snd-aloop` kernel module)
- ffmpeg
- (Recommended) a USB DAC or similar audio interface. A DAC that supports 48kHz is recommended when using QjiDSP.

---

## Installation

### 0. Prerequisite: install the base Qji app (if not already installed)

This repository does not work on its own. If you haven't installed the base app yet, do that first.

```bash
git clone https://github.com/yasuhito3/Qji-Network-Audio-Player.git
cd Qji-Network-Audio-Player
# follow the base Qji app's own install instructions
```

### 1. Get this repository (the QjiDSP extension)

```bash
git clone https://github.com/yasuhito3/QjiDSP-English.git
cd QjiDSP-English/qjidsp_installer
```

> 📦 The installer itself, DSP config files, IR file, etc. are all bundled inside the **`qjidsp_installer/` folder**.
> If you used GitHub's "Download ZIP" option, extract it, go into the resulting folder (something like `QjiDSP-English-main/`),
> and then into `qjidsp_installer/` before continuing to the next step.

### 2. Run the installer

From inside the `qjidsp_installer/` folder, run either of the following:

```bash
bash install_qjidsp.sh
```

Or double-click `QjiDSP Installer.desktop` in your desktop environment.

The installer will:

- Install required packages (ffmpeg, alsa-utils, git, etc.)
- Install/update Python libraries (soundfile, scipy, pycamilladsp) and `yt-dlp[default]` (this includes `yt-dlp-ejs`, which YouTube playback needs; if an older `yt-dlp` is taking priority on your PATH, the installer detects it and updates it system-wide with `sudo`)
- Download CamillaDSP
- Install `deno` (used to solve YouTube's signature challenges; **version 2.3 or later is required**)
- Enable the virtual sound card (`snd-aloop`)
- Place the full app + DSP config files (soundfields v1-v6) + IR (reverb) file into `~/qji/`

If you already have `~/qji/` installed, any file that gets overwritten is automatically backed up with a timestamp first.

### 3. Launch

Double-click the Qji icon on your desktop to launch it.

To launch manually:

```bash
cd ~/qji && python3 qji.py
```

Selecting "Loopback" as the output device lets you choose from six soundfields (v1-v6). Selecting any other device gives you direct output, bypassing the DSP.

---

## Updating

Once installed, you don't need to re-download the zip or `git clone` again each time. Just like the main Qji launcher icon, the installer creates a **"QjiDSP Update Checker"** icon directly on your Desktop.

- **From the desktop icon**: double-click the "QjiDSP Update Checker" icon
- **From the terminal**: `cd ~/qji && bash update_qjidsp.sh`

This checks this repository's `VERSION` file, and if a newer version is available, downloads it (after confirmation) and re-runs `install_qjidsp.sh` with the latest files. Any existing files that get overwritten are automatically backed up with a timestamp, per the installer's usual behavior. If you're already on the latest version, it just says so and exits.

This is the same approach used by Qji Peak Monitor's update checker.

---

## Optional setup

### YouTube Music (library access)

Set up browser authentication only if you want access to your liked songs and library.

```bash
mkdir -p ~/.config
python3 -c "import os, ytmusicapi; ytmusicapi.setup(filepath=os.path.expanduser('~/.config/qji_ytmusic_auth.json'))"
```

You will be asked to paste the request headers copied from your browser (Chrome / Firefox) while logged in to music.youtube.com. Paste them, then press `Ctrl-D` to confirm.

Playback itself works via `yt-dlp` search even without this.

> 🔒 **About security**
> - The auth file contains your logged-in cookies, so when loading it Qji checks its owner and permissions, and automatically tightens them to `600` (readable only by you) if needed.
> - The YouTube Music browser UI (port 8082) accepts connections **only from this PC itself (127.0.0.1)**. It cannot be opened from a smartphone or other device on your network.
> - Only YouTube / YouTube Music URLs can be played. Cover art is likewise fetched only from YouTube / Google image hosts.
> - If a package such as `ytmusicapi` is missing, Qji shows the command it would run and asks for confirmation before installing it (the default is "no").

### SoundCloud

No login or authentication setup is needed. The `client_id` used for search and playback is fetched from the SoundCloud website automatically and refreshed when it expires (it is stored in `~/.config/qji_soundcloud.json`, with file permissions `600`).

Both tracks and playlists (`/sets/…`) are fetched directly through the SoundCloud API. `yt-dlp` is used only as a fallback when the API cannot return the result.

> 🔒 **About security**
> - The SoundCloud browser UI (port 8081) accepts connections **only from this PC itself (127.0.0.1)**. It cannot be opened from a smartphone or other device on your network. It also rejects requests triggered from other websites (CSRF and DNS rebinding).
> - Only SoundCloud (`soundcloud.com`) URLs can be played. Cover art is likewise fetched only from SoundCloud's image hosts.
> - Stream URLs returned by SoundCloud are used only if they are `https` on SoundCloud's delivery hosts. ffmpeg is started with `-protocol_whitelist`, so it cannot be made to read local things such as `file:`.
> - Terminal control sequences are stripped from track titles and other external strings before they are displayed.
> - The play-request hand-off file and the temporary cover-art file are kept not in the shared `/tmp` but in a folder of your own (`$XDG_RUNTIME_DIR/qji`, or `/tmp/qji-<UID>` if that is not set), with permissions `600`.
> - If you point Qji at a cookie file for `yt-dlp`, it checks the file's owner and permissions when loading it, and automatically tightens them to `600` if needed.
> - If `requests` is missing, Qji shows the command it would run and asks for confirmation before installing it (the default is "no").

> ℹ️ **About playback**
> - In DSP mode (when you choose "Loopback" as the output device), ffmpeg converts the audio to 48000Hz before handing it to CamillaDSP. With direct output, the audio stays at SoundCloud's native 44.1kHz.
> - The Peak Monitor display also works with SoundCloud playback.
> - Tracks that SoundCloud protects with **DRM** cannot be played with ffmpeg. Qji skips them automatically (a different upload of the same piece may play fine).

### Voice control (Vosk)

Enabled automatically once a Japanese speech recognition model is placed at:

```
~/vosk-model-ja-0.22
```

(This is currently Japanese-only. If not present, voice control is simply disabled — everything else works normally.)
Get a model from [alphacep/vosk-api](https://alphacephei.com/vosk/models) or similar.

### Qji Peak Monitor (stereo VU meter)

⚠️ **Not included in this installer (`install_qjidsp.sh`).** It is published as a separate, optional repository:
👉 **[Qji Peak Monitor](https://github.com/yasuhito3/Qji-peak-monitor)**

A standalone stereo peak (VU) meter that visualizes Qji's final output stage in real time.
It also has a "display delay" feature that compensates for the downstream buffer offset you get when listening through QjiDSP.
See that repository's README for installation and usage.

---

## Command-line options

```
python3 qji.py --tempo 120 --tempo-tol 10       # play by tempo (BPM)
python3 qji.py --composer "Mozart"              # play by composer
python3 qji.py --genre Classical --mood Calm    # combined search
python3 qji.py --device hw:2,0                  # specify output device
python3 qji.py --no-voice                       # launch with voice recognition disabled
```

---

## Troubleshooting

**If you get no sound in DSP mode**, check the following:

```bash
cat /tmp/camilladsp.log /tmp/wobble.log /tmp/cdsp_watchdog.log
```

If you see an error like `Invalid filter ... No such file or directory`, the IR (reverb) file's path was not expanded correctly. Check that the file exists under `~/qji/camilladsp_test/`.

To test just the Loopback → DAC path on its own:

```bash
speaker-test -D hw:CARD=Loopback,DEV=0 -c 2 -r 48000 -F S32_LE
```

**If YouTube Music won't play** (a `403` error, tracks ending immediately, etc.)

Because of changes on YouTube's side, playback stops working if `yt-dlp` is out of date. First, update `yt-dlp`:

```bash
pip install -U "yt-dlp[default]" --break-system-packages
```

The `[default]` part is important (it also installs `yt-dlp-ejs`, which is needed to solve YouTube's signature challenges).

If it still doesn't work after updating, **an older `yt-dlp` may be taking priority** (for example, a copy installed earlier with `sudo` or via `apt`). Check with:

```bash
which -a yt-dlp      # lists every yt-dlp found (the first one is used)
yt-dlp --version     # the version actually being used
```

If the first one listed is old, update it system-wide:

```bash
sudo pip3 install --upgrade "yt-dlp[default]" --break-system-packages
```

If an old copy installed via `apt` is still there, remove it with `sudo apt remove yt-dlp` and then run the command above.

Also make sure `deno` is **version 2.3 or later**:

```bash
deno --version       # if it's older: deno upgrade
```

Re-running the installer (`install_qjidsp.sh`) performs these updates and checks automatically.

---

**If SoundCloud won't play**

When playback starts, the screen shows clues about what is and isn't playable.

- `📡 Available: …` ── the list of delivery formats that can be played for that track, plus how many were excluded because they are encrypted (DRM). If one format fails (e.g. `HTTP 404`), the next format is tried automatically.
- `🔒 This track is DRM-protected and cannot be played — skipping` ── SoundCloud protects this track with DRM. ffmpeg cannot play it, so it is skipped by design. Updating `yt-dlp` does not change this.
- `Not using this format — the delivery host is not allowed …` ── SoundCloud may have started using a new delivery host. Please open an Issue and include the host name shown.
- `HTTP 401` / `403` keeps appearing ── the saved `client_id` may have expired. It is normally refreshed automatically, but if that doesn't help, delete the saved `client_id` with the following command and try playing again.

```bash
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path.home()/'.config'/'qji_soundcloud.json'
c = json.loads(p.read_text()); c.pop('client_id', None); c.pop('client_id_fetched_at', None)
p.write_text(json.dumps(c, ensure_ascii=False, indent=2))
EOF
```

SoundCloud playback does not depend on `yt-dlp`, so an out-of-date `yt-dlp` normally has no effect on it (`yt-dlp` is used only as a fallback).

---



## Directory layout (after installation)

```
~/qji/
├── qji.py                      # main app
├── VERSION                     # installed version (read by update_qjidsp.sh)
├── update_qjidsp.sh            # update checker script (target of the Desktop icon)
├── qji_qobuzdsp.py             # Qobuz module
├── qji_qobuz_browser.py        # Qobuz browser UI
├── qji_soundcloud.py           # SoundCloud module
├── qji_soundcloud_browser.py   # SoundCloud browser UI
├── qji_ytmusic.py              # YouTube Music module
├── qji_ytmusic_browser.py      # YouTube Music browser UI
├── camilladsp_test/
│   ├── spatial_final.yml       # currently active DSP config (generated at startup)
│   ├── wobble_v1-v5.py         # wobble LFO scripts
│   ├── wobble_v5_harmonics_hp.py
│   ├── cdsp_watchdog.py        # CamillaDSP health monitor
│   └── Musikvereinsaal_48k_tail.wav  # reverb IR file
└── qjidsp_backup_v1-v6/
    └── spatial_final.yml       # soundfield presets v1-v6 (templates)
```

---

## Acknowledgments & libraries used

- [CamillaDSP](https://github.com/HEnquist/camilladsp) / [pycamilladsp](https://github.com/HEnquist/pycamilladsp)
- [ffmpeg](https://ffmpeg.org/)
- [mutagen](https://github.com/quodlibet/mutagen)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) / [ytmusicapi](https://github.com/sigma67/ytmusicapi)
- [Vosk](https://alphacephei.com/vosk/)
- [deno](https://deno.com/)
- [shairport-sync](https://github.com/mikebrady/shairport-sync)

For the provenance/license of the reverb IR file (`Musikvereinsaal_48k_tail.wav`) and the licenses of dependencies
(e.g. `mutagen` is GPLv2+; its source is not bundled here and is installed separately via pip), see [NOTICE.md](./NOTICE.md).

## License

See [LICENSE](./LICENSE).
