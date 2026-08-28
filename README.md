If you are using QjiDSP for the first time, please check this first → [QjiDSP Quick Start Guide](QjiDSP_Quick_Start_English.md)

# Qji

A hi-fi music playback system for Linux. In addition to local file playback, it integrates streaming from Qobuz, SoundCloud, and YouTube Music, 3D spatial audio extension via CamillaDSP (soundfields v1-v6), genre-adaptive EQ (Sonia Intelligence), and automatic distortion mitigation (Auto De-Clip).

> Developed and refined for daily use in a personal hi-fi setup (Mark Levinson amplifier, Amanero Combo384 USB DAC, etc.).

> ⚠️ **Prerequisite**: This repository (the QjiDSP extension) assumes **the [base Qji app](https://github.com/yasuhito3/Qji-Network-Audio-Player) is already installed**.
> If you haven't installed it yet, please set that up first, following its own install instructions, before running this repository's installer.

---



## Features

- **Local playback**: browse by album art, folder playback, search by composer / performer / conductor / genre / mood
- **Streaming**: Qobuz (Hi-Res), SoundCloud, YouTube Music (`yt-dlp` + `ytmusicapi`)
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
git clone https://github.com/<your-username>/<this-repo-name>.git
cd <this-repo-name>/qjidsp_installer
```

> 📦 The installer itself, DSP config files, IR file, etc. are all bundled inside the `qjidsp_installer/` **folder**.
> If you used GitHub's "Download ZIP" option, extract it, go into the resulting folder (something like `<this-repo-name>-main/`),
> and then into `qjidsp_installer/` before continuing to the next step.



### 2. Run the installer

From inside the `qjidsp_installer/` folder, run either of the following:

```bash
bash install_qjidsp.sh
```

Or double-click `QjiDSP Installer.desktop` in your desktop environment.

The installer will:

- Install required packages (ffmpeg, alsa-utils, git, etc.)
- Install/update Python libraries (soundfile, scipy, pycamilladsp) and `yt-dlp`
- Download CamillaDSP
- Install `deno` (used to stabilize YouTube Music playback)
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



## Optional setup



### YouTube Music (library access)

Set up browser authentication only if you want access to your liked songs and library.

```bash
python3 -c "from ytmusicapi import YTMusic; YTMusic.setup(filepath='~/.config/qji_ytmusic_auth.json')"
```

Playback itself works via `yt-dlp` search even without this.

### Voice control (Vosk)

Enabled automatically once a Japanese speech recognition model is placed at:

```
~/vosk-model-ja-0.22
```

(This is currently Japanese-only. If not present, voice control is simply disabled — everything else works normally.)
Get a model from [alphacep/vosk-api](https://alphacephei.com/vosk/models) or similar.

### Qji Peak Monitor (stereo VU meter)

⚠️ **Not included in this installer (**`install_qjidsp.sh`**).** It's a separate, optional repository:
👉 **[Qji Peak Monitor](https://github.com/yasuhito3/Qji-peak-monitor)**

A standalone real-time stereo peak/VU meter for Qji's final output stage, with a display-delay
feature to compensate for downstream buffering (handy when listening through QjiDSP). See that
repository's README for installation and usage instructions.

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

---



## Directory layout (after installation)

```
~/qji/
├── qji.py                      # main app
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