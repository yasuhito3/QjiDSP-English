# Qji User Manual

**〜 Bringing the sound of a concert hall into your room 〜**

---

## Table of Contents

1. [What Is Qji](#1-what-is-qji)
2. [System Requirements](#2-system-requirements)
3. [Setup](#3-setup)
4. [Starting Qji](#4-starting-qji)
5. [The Main Menu](#5-the-main-menu)
6. [Keys During Playback](#6-keys-during-playback)
7. [Sound Field Presets (Acoustic Filters)](#7-sound-field-presets-acoustic-filters)
8. [Gain Presets (Input-Stage Volume Adjustment)](#8-gain-presets-input-stage-volume-adjustment)
9. [Favorites](#9-favorites)
10. [Sonia Intelligence (SI) — AI Acoustic Feedback](#10-sonia-intelligence-si--ai-acoustic-feedback)
11. [Voice Commands](#11-voice-commands)
12. [Radio](#12-radio)
13. [Streaming Playback (Qobuz / SoundCloud / YouTube Music)](#13-streaming-playback-qobuz--soundcloud--youtube-music)
14. [AirPlay / DLNA Receiver](#14-airplay--dlna-receiver)
15. [Now Playing Mirror (Phone/Tablet Display)](#15-now-playing-mirror-phonetablet-display)
16. [Preset Management (Saving Settings)](#16-preset-management-saving-settings)
17. [Troubleshooting](#17-troubleshooting)
18. [Glossary](#18-glossary)

---

## 1. What Is Qji

**Qji (奏在)** is a high-fidelity music playback system that runs on Linux. It's more than a music player — it combines:

- A genuine audio pipeline built from **CamillaDSP, ffmpeg, and ALSA**
- Real-time acoustic processing modeled on the **Musikverein (Vienna)** hall acoustics
- A database that automatically analyzes each track's genre, mood, and tempo for selection
- A **Favorites** feature that lets you instantly recall a favorite track whenever you like
- **Sonia Intelligence (SI)**, which fine-tunes the sound just from how you describe it in natural Japanese
- Streaming playback via radio, Qobuz, SoundCloud, and YouTube Music
- Wireless playback via AirPlay and DLNA (UPnP) reception
- A "Now Playing Mirror" that shows the cover art and playback status in a phone's browser

All of this is woven into a single music playback system **designed around the experience of listening itself**.

The developer built it feature by feature, refining it through daily use in his own home audio setup. It's designed to prioritize "finding great music with great sound" over raw efficiency.

---

## 2. System Requirements

### Required

| Item | Details |
|---|---|
| OS | Linux (verified on Ubuntu / Xubuntu-based distributions) |
| Python | Python 3 (the `mutagen` package is required) |
| Audio | ffmpeg, ALSA (`aplay`) |
| Image display | `feh` (cover art display), ImageMagick (`convert`, for overlaying track info) |
| Music library | A pre-analyzed metadata database file (`~/music_mood_db.json`) |

This database is built by scanning your music folder with the bundled analysis script (e.g. the `music_analyzer2.py` family). Qji itself reads this JSON database to operate, so **you need to analyze your music library first**.

### Optional (enables additional features)

| Feature | Requirement |
|---|---|
| Voice-command control | `vosk`, `sounddevice` (with the Japanese speech model placed at `~/vosk-model-ja-0.22`) |
| Sonia Intelligence (SI) | The bundled `sonia_intelligence` module set |
| AirPlay reception | `shairport-sync`, `avahi-daemon`, an ALSA loopback device (`snd-aloop`) |
| DLNA/UPnP reception | `gmediarender`, an ALSA loopback device (`snd-aloop`) |
| Qobuz / SoundCloud / YouTube Music playback | The corresponding modules (`qji_qobuzdsp.py`, etc.), `yt-dlp` |
| Now Playing Mirror | A smartphone or similar browser on the same Wi-Fi network |

You don't need every feature set up from day one. Qji's audio engine is already fully enjoyable with nothing more than local library playback.

---

## 3. Setup

1. Install the required system packages (ffmpeg, ALSA tools, feh, ImageMagick, etc.) using your distribution's package manager.
2. Install the Python dependencies (`mutagen` is required; `vosk`/`sounddevice` are only needed for voice commands).
3. Place your music files in the music folder configured inside `qji.py` (e.g. `~/Music`, or wherever an external drive is mounted). Supported formats: `.wav / .flac / .wma / .aiff / .aif / .mp3 / .m4a / .aac / .ogg`.
4. Run the bundled analysis script to generate `~/music_mood_db.json`. This becomes Qji's track-selection database.
5. If you plan to use AirPlay / DLNA, load the `snd-aloop` kernel module:

   ```
   sudo modprobe snd-aloop
   ```

   To make this persistent across reboots, add `snd-aloop` to `/etc/modules`.

6. If you plan to use voice commands, unpack the Japanese Vosk model at `~/vosk-model-ja-0.22`.

---

## 4. Starting Qji

### 4.1 Double-click the desktop icon

A desktop icon is created automatically during installation. Just double-click it to start Qji (available from the very first launch).

### 4.2 Start from a terminal (alternative)

You can also start it from a terminal:

```
python3 qji.py
```

On startup, a splash screen (the "奏在" logo) appears for a few seconds, after which the database loads and the main menu is displayed.

To start with speech recognition disabled, launch with the `--no-voice` option.

---

## 5. The Main Menu

The main menu is organized into three broad groups: **Track Selection**, **Sound & Output Settings**, and **External Features**.

### Track Selection

| Key | Action |
|---|---|
| `0` | Random playback from the whole library (up to 500 tracks) |
| `1` | Select by tempo (BPM) |
| `2` | Select by composer |
| `3` | Select by performer |
| `4` | Select by conductor |
| `5` | Select by genre |
| `6` | Select by mood |
| `7` | 🔍 Keyword search (searches across title, composer, performer, etc.) |
| `8` | 🔷 Combine multiple criteria (e.g. composer + genre) |
| `9` | 📊 Show mood statistics for the whole library |
| `J` | 🖼️ Browse cover art visually to pick a track |
| `N` | 🆕 Browse recently added tracks by cover art |

### Sound & Output Settings

| Key | Action |
|---|---|
| `A` | Acoustic presets (simple EQ, e.g. "bring the vocal forward," "bring the soloist forward") |
| `G` | Gain presets (described below — input-stage volume adjustment) |
| `L` | Toggle loudness normalization |
| `T` | Toggle tinnitus-relief mode (gently tames the high end) |
| `W` | Toggle sound-field adjustment (Air Particle Layer / pink-noise air layer) |
| `V` | Toggle the Musikverein room-effect (hall reverberation) |
| `F` | Switch acoustic presets (11 genre-oriented filters, described below) |
| `E` | Switch echo mode (classical-oriented / jazz-vocal-oriented) |
| `Z` | Toggle gapless playback (active in folder-playback mode) |
| `U` | Toggle upsampling and set the target sample rate |

### External Features & Management

| Key | Action |
|---|---|
| `R` | 📻 Play an internet radio station |
| `P` | 💾 Preset management (save/restore the full set of current acoustic settings under a name) |
| `K` | ⭐ Manage favorites |
| `M` | 📱 Toggle the Now Playing Mirror (phone/browser display) |
| `QB` | 🎵 Qobuz streaming playback |
| `S` | 🟠 SoundCloud streaming playback |
| `Y` | 🔴 YouTube Music streaming playback |
| `AP` | 📡 AirPlay receiver (receive from an iPhone/Mac) |
| `DL` | 📻 UPnP/DLNA receiver (receive from BubbleUPnP, etc.) |
| `X` | 🔄 Re-detect the DSP output device (pick up a DAC connected after startup) |
| `Q` | Quit |

---

## 6. Keys During Playback

While a track is playing, you can control Qji in real time by pressing keys in the terminal (no Enter needed — each key takes effect the instant you press it).

| Key | Action |
|---|---|
| `r` | Restart the current track from the beginning |
| `f` | Switch to playing tracks in the current folder in sequence |
| `n` | Next track |
| `b` | Previous track |
| `i` | Redisplay the cover art |
| `o` | ⭐ Add the currently playing track to Favorites |
| `w` | Toggle sound-field adjustment (Air Particle Layer) |
| `c` | Change the filter preset (sound-field preset) on the spot |
| `g` | Cycle through gain presets |
| `+` / `=` | Increase output gain by 1 dB |
| `-` | Decrease output gain by 1 dB |
| `s` | Save all current acoustic settings as a profile (remembered per track or album) |
| `q` | Stop playback and return to the main menu |

**Additional keys available only when you selected Loopback and started in DSP mode:**

| Key | Action |
|---|---|
| `1` | v1 Rich Hall (Static) — full EQ chain, calm reverberation |
| `2` | v2 Rich Hall (Dynamic) — full EQ chain, moving air |
| `3` | v3 Taste of the Source (Static) — simple, emphasizes presence |
| `4` | v4 Taste of the Source (Dynamic) — simple, washoku-style panning |
| `5` | v5 Harmonics Mode — violin resonance / shakuhachi-style harmonics |
| `6` | v6 Harmonics Mode (for Headphones) — optimized for closed-/open-back headphones |

These let you switch the sound field on the spot, without interrupting playback. If you selected a DAC directly at startup (bypassing DSP for the raw source sound), these keys are not available.

**Additional keys available only when Sonia Intelligence is available:**

| Key | Action |
|---|---|
| `z` | Describe how you'd like the sound to change, in Japanese (e.g. "もっとピアノを前に") |
| `h` | Select a hall (acoustic space) |
| `p` | Select an SI profile |
| `x` | Select an SI acoustic preset by number |
| `a` | Manually register an acoustic preset for the current album |

**Additional keys available only when using a USB-connected DAC (USB-Noise-Guard):**

| Key | Action |
|---|---|
| `u` | Toggle the USB output digital-noise mitigation (autosuspend disable + realtime priority, together) ON/OFF |
| `k` | Toggle the autosuspend mitigation only ON/OFF (to isolate its effect on the noise floor during silence) |
| `j` | Toggle the realtime-priority mitigation only ON/OFF (to isolate its effect on imaging and timing precision) |
| `m` | Show the current ON/OFF status (does not toggle anything) |

With a USB-connected DAC, USB autosuspend (a power-saving feature that cuts power when idle) and timing jitter from CPU contention can sometimes show up as faint clicks or subtle changes in sound quality. QjiDSP automatically mitigates both as soon as it detects your DAC at startup (the USB-Noise-Guard feature). Use the keys above to toggle them on and off during playback and compare the effect for yourself, without interrupting the music.

Toggling the autosuspend mitigation with `k` requires write access to the USB DAC's power-management file. Run the bundled `install_usb_audio_optimize.sh` once with `sudo` (log out and back in afterward), and it will be applied automatically at startup, which also enables toggling with `k`. Playback works fine even without running it, but you may see a "permission denied"-type message in the startup diagnostics or when pressing `k`/`u`.

A guide to the available keys is always shown at the bottom of the screen during playback, so you never have to memorize this list.

---

## 7. Sound Field Presets (Acoustic Filters)

These are acoustic filter presets tailored to different genres and listening scenes, switchable with `F` (main menu) or `c` (during playback). 11 in total.

| Preset | Character |
|---|---|
| 🎻 Musikverein (Orchestra) | The default orchestral setting, modeled on the Vienna Musikverein's reverberation |
| 🎹 Piano | Tuned for solo piano and piano concertos |
| 🏠 Chamber | For chamber music, emphasizing a natural sense of closeness |
| 🎙 Vocal | For vocal music, bringing out the texture of the voice |
| 🎷 Jazz | For jazz and pop |
| 🌿 Calm | A gentle sound field, like a still water surface — for relaxed listening |
| 🌊 Deep | A sound field that sinks deeper — for focused late-night listening |
| 🌐 Spatial (3D) | Spatial audio processing for headphones |
| 📻 Radio (Standard) | Standard processing tuned for radio streams |
| ⚪ Bypass (No Processing) | No acoustic processing at all — a reference setting |
| 💿 Vinyl Emotion | Evokes the texture of an analog record |

If a track ever feels like it's wearing the wrong preset, switch it on the spot with `c`. The current preset name is also shown as a badge in the top-left of the cover-art display, so you'll notice right away if you forgot to switch it back.

---

## 8. Gain Presets (Input-Stage Volume Adjustment)

Set with `G` (main menu) or cycled through with `g` (during playback).

| Preset | Value | Use |
|---|---|---|
| Classical | 0 dB | When you don't want to compress delicate sounds like solo violin |
| General | -1.5 dB | A balanced, general-purpose setting |
| Jazz/Pop | -3.5 dB | Prevents distortion on higher-level recordings |
| Loud Material | -5 dB | For high-volume recordings and live sources |

There's also automatic switching to the Jazz/Pop setting based on genre, but manually selecting with `g` overrides that and keeps your chosen setting.

---

## 9. Favorites

Whenever a moment strikes you as worth keeping, **a single press of `o`** adds the current track to Favorites. Playback never pauses to wait for input.

When you add a track, its genre and mood information are automatically attached as **tags**, which you can later organize freely from the `K` menu.

### Favorites Management Menu (`K`)

| Number | Action |
|---|---|
| 1 | List all favorites (shows tags, notes, and whether the file still exists) |
| 2 | Filter by tag (choose by number, or type a name — case-insensitive) |
| 3 | Shuffle-play all favorites |
| 4 | Specify a tag and play only tracks with that tag |
| 5 | Pick a single track from the list to play |
| 6 | Edit the tags/notes of an existing favorite |
| 7 | Remove a favorite |
| 8 | Find and clean up favorites whose file has moved or gone missing |

### A Typical Workflow

1. While listening, press `o` on a track that moved you (genre/mood tags are added automatically)
2. Later, when you have time, go to `K` → `6` and add your own tags or notes — "Schubert," "Lieder," "miniature," or something personal like "sang this from a score years ago"
3. From then on, `K` → `4` and selecting that tag instantly pulls up every track in that same vein

Even as your collection of favorites grows, tag filtering and numbered selection keep it easy to navigate.

---

## 10. Sonia Intelligence (SI) — AI Acoustic Feedback

Sonia Intelligence (SI) turns natural-language impressions — in Japanese — into acoustic adjustments. It's only active when the `sonia_intelligence` module has loaded successfully.

| Key | Function |
|---|---|
| `z` | Freely describe what you want, in Japanese — e.g. "もっとピアノを前に" (bring the piano forward), "ホールの奥行きをもっと" (more depth to the hall), "低音が重すぎる" (the bass is too heavy), "弦の艶をかなり強調" (really bring out the shimmer of the strings) |
| `h` | Select a hall (acoustic space) |
| `p` | Select and apply a saved SI profile |
| `x` | Select an SI acoustic preset by number |
| `a` | Manually link an acoustic preset to the current album |

Whatever you type with `z` is translated directly into acoustic parameters and shown on screen along with a brief explanation. The point is that when something feels subtly "off," you can nudge it with words instead of having to think in dB values.

---

## 11. Voice Commands

Using Vosk's offline speech recognition, you can control Qji just by speaking to the microphone (when `VOICE_RECOGNITION_AVAILABLE` and enabled).

| Example phrase | Action |
|---|---|
| "フォルダ" / "フォルダー" (folder) | Switch to sequential folder playback |
| "テンポ" (tempo) | Switch to tempo mode |
| "作曲家" (composer) | Switch to composer mode |
| "演奏" / "オーケストラ" (performer/orchestra) | Switch to performer mode |
| "指揮" (conductor) | Switch to conductor mode |
| "ジャンル" (genre) | Switch to genre mode |
| "ムード" (mood) | Switch to mood mode |
| "次" (next) | Next track |
| "前" (previous) | Previous track |
| "もう一度" / "最初から" / "リプレイ" (again/from the start/replay) | Restart the track |
| "終了" / "ストップ" / "停止" (end/stop) | Stop playback |
| "画像" / "ジャケット" (image/cover) | Redisplay the cover art |

Recognition runs fully offline with no internet connection required, which also means your privacy is preserved.

---

## 12. Radio

Pressing `R` brings up a list of pre-registered internet radio stations (each with a flag icon). Just pick a number to start playback — the same acoustic filter chain used for local playback is applied.

After a station finishes, you stay in the radio menu so you can keep trying others. Press `0` to return to the main menu.

---

## 13. Streaming Playback (Qobuz / SoundCloud / YouTube Music)

| Key | Service |
|---|---|
| `QB` | Qobuz (hi-res streaming) |
| `S` | SoundCloud |
| `Y` | YouTube Music |

All three **carry over your current acoustic settings as-is** — gain preset, tinnitus relief, the Musikverein room effect, Air Particle Layer, echo mode, output device, and so on. You get the same "Qji sound" for streamed material as you do for local files.

---

## 14. AirPlay / DLNA Receiver

### AirPlay (`AP`)

Send audio to Qji from an iPhone or Mac via AirPlay, and it plays through Qji's full acoustic filter chain. Requires `shairport-sync`, `avahi-daemon`, and an ALSA loopback device.

### DLNA/UPnP (`DL`)

Select Qji as the renderer from a DLNA controller such as BubbleUPnP. Requires `gmediarender` and an ALSA loopback device.

In either receiving mode, these keys are available during playback:

| Key | Action |
|---|---|
| `c` | Change the filter |
| `h` | Toggle the Musikverein room effect |
| `a` | Toggle the Air Particle Layer |
| `x` | Select an SI preset |
| `+` / `-` | Adjust volume |
| `q` | Stop |

If a required component is missing, the screen will walk you through how to install it.

---

## 15. Now Playing Mirror (Phone/Tablet Display)

Press `M` to start the mirror server, and from any smartphone or tablet on the same Wi-Fi network, just open `http://(your PC's IP address):(port number)/` in a browser to see the currently playing cover art, track info, and sound-field preset in real time.

The same sound-field information (filter name + gain preset name) is also overlaid as a badge on the cover art shown on the PC itself, so you'll immediately notice if a setting drifted out of place mid-playback.

---

## 16. Preset Management (Saving Settings)

From `P`, you can save the **entire set of acoustic settings** — volume, output device, acoustic preset, gain preset, loudness normalization, tinnitus relief, the Musikverein room effect, gapless playback, upsampling, filter preset, and more — together, under a name.

| Number | Action |
|---|---|
| 1 | Save the current settings as a preset |
| 2 | Load and apply a preset (by number or name) |
| 3 | List all presets |
| 4 | Delete a preset |
| 5 | Show the current settings |

Creating presets for different listening scenes ("solo late-night listening," "loud party mode," etc.) means you never have to redo the fine-tuning from scratch.

---

## 17. Troubleshooting

| Symptom | Check |
|---|---|
| Startup says "database file not found" | Run the analysis script (e.g. `music_analyzer2.py`) on your library first to generate `~/music_mood_db.json` |
| Voice commands don't respond | Check that `vosk`/`sounddevice` are installed, that the model is unpacked at `~/vosk-model-ja-0.22`, and that your USB microphone is recognized |
| Can't use AirPlay/DLNA | Check that `snd-aloop` is loaded (`sudo modprobe snd-aloop`) and that `shairport-sync`/`gmediarender` are installed |
| The Sonia Intelligence keys (`z`/`h`/`p`/`x`/`a`) don't appear | The `sonia_intelligence` module may have failed to load — check the startup messages |
| Track info isn't overlaid on the cover art | Check that ImageMagick (the `convert` command) is installed |
| The output DAC isn't recognized | Try `X` from the main menu (re-detect the DSP output device) |
| Pressing `k` shows something like "could not turn the autosuspend mitigation OFF (no write permission)" | Run the bundled `install_usb_audio_optimize.sh` once: `sudo bash install_usb_audio_optimize.sh` (no further login is needed; if it still doesn't take effect, try unplugging and reconnecting your DAC) |
| At startup, the "USB output digital-noise mitigation check" says "not detected as a USB device" | This is expected right after startup, since Loopback (a virtual device) is selected at that point. Once you proceed to select a DSP sound field and then your DAC, the check result for your actual USB DAC will be shown |

---

## 18. Glossary

| Term | Explanation |
|---|---|
| Qji (奏在, "Sōzai") | The project's Japanese name, carrying the meaning "the music is present, right here" |
| Musikverein | The world-renowned concert hall in Vienna; Qji models its reverberation characteristics with an ffmpeg filter |
| Air Particle Layer | A faint layer of pink noise overlaid on the mix, evoking the sense of "air" you feel in a real concert hall |
| Filter preset (sound-field preset) | A set of acoustic filter chains tuned to a genre or listening scene (11 in total) |
| Gain preset | An input-stage volume-adjustment preset (prevents distortion) |
| Sonia Intelligence (SI) | An AI-assisted system that adjusts the sound based on natural-language impressions typed in Japanese |
| Now Playing Mirror | A mirrored display of playback status, viewable from a phone's browser |
| USB-Noise-Guard | A mechanism that mitigates digital noise specific to USB-connected DACs (clicks from USB autosuspend, jitter from CPU contention). Toggle and check it during playback with the `u`/`k`/`j`/`m` keys |
| USB autosuspend | An OS power-saving feature that lets a USB device drop into a low-power state when idle; on an audio DAC, this can cause clicks from the power cycling |

---

*This manual was written by Qji's developer. If you have questions or suggestions, feel free to reach out via the GitHub repository.*
