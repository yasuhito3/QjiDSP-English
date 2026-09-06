# The Complete Beginner's Guide
### ―― From Installing Linux to Hearing Sound Through QjiDSP ――

---

## Introduction

This guide is written for people who have almost no experience with computers or Linux.
It will walk you through **installing Xubuntu (a version of Linux) and getting actual sound
playing through QjiDSP (a music player)**.

The next steps after that (building playlists, using the various sound presets, and so on)
are outside the scope of this guide. The goal here is simply **"get sound coming out."**

Who this is for: anyone who knows how to turn a computer on, but has never used Linux or a
terminal (command-line screen) before.

---

## What you'll need

- A computer (see Chapter 1 below)
- An internet connection (for downloading files)
- One USB flash drive (8GB or larger — everything on it will be erased, so make sure it's empty)
- (Optional but strongly recommended) A USB DAC (external D/A converter) plus headphones or speakers
- A second device (phone/tablet) to keep this guide open on while you work, since your PC's
  screen will be taken up by the Linux installer

---

## Chapter 1 — Preparing a PC

1. **We recommend using a dedicated machine.** If you install over an existing Windows or Mac,
   all the data currently on that computer will be erased. The safest option is a used PC or an
   old laptop you're no longer using for anything else.
2. **Minimum guidelines**
   - CPU: anything from the last 10 years (Intel or AMD, either is fine)
   - Memory: 4GB or more (8GB+ is more comfortable)
   - Storage: at least 20GB free (an SSD is recommended)
   - At least one free USB port (if you plan to connect a DAC over USB)
3. Turn the PC on and check whether it can **boot from USB in the BIOS/UEFI boot order**. If
   you're not sure how, don't worry — you can figure it out while actually doing it in Chapter 4.

---

## Chapter 2 — Downloading Xubuntu

1. On another PC or phone's browser, go to the official Xubuntu site.
   - `https://xubuntu.org/download/`
2. Choose a version labeled **"LTS"** (for example, "24.04 LTS"). LTS means Long Term Support,
   and is the safer choice for beginners.
3. Download the **64-bit ISO file** (a file ending in `.iso`, roughly 3GB).
4. Once the download finishes, note where the file was saved (usually your "Downloads" folder).

---

## Chapter 3 — Creating a Bootable USB Drive

Now you'll write the ISO file you downloaded onto the USB drive, turning it into a "bootable"
USB drive.

1. Download and install **"balenaEtcher"** (free) on the computer you're currently working on
   (Windows or Mac).
   - `https://etcher.balena.io/`
2. Plug the USB flash drive into your computer (its contents will be erased, so back anything up first if needed).
3. Open balenaEtcher and:
   - Click "Flash from file" → select the Xubuntu ISO file you downloaded
   - Click "Select target" → select the USB drive you inserted
   - Click "Flash!"
4. Wait for the write to finish (a few minutes to around ten minutes). Once done, remove the USB drive.

---

## Chapter 4 — Installing Xubuntu

1. Insert the USB drive you just created into the PC you set aside in Chapter 1.
2. Turn the PC on, and immediately after power-on, repeatedly press **F2, F10, F12, Delete, or
   Esc** (which one depends on the manufacturer) to open the "Boot Device Menu."
3. Select the USB drive to boot from it; the Xubuntu menu screen will appear.
   - "Try Xubuntu" — try it first before installing
   - "Install Xubuntu" — install right away
   Either option is fine.
4. Follow the on-screen instructions. The main choices you'll be asked to make:
   - Language: your preferred language
   - Keyboard layout: matching your keyboard
   - "Updates and other software" → **check both boxes** (this makes it more likely the drivers
     you'll need later for audio software get installed automatically)
   - Installation type: **"Erase disk and install Xubuntu"** (if you want to dedicate this whole
     PC to Xubuntu)
     - If you want to keep another OS alongside it, choose "Something else (manual)" instead —
       but this isn't recommended for beginners.
   - Time zone: your local time zone
   - Set a username and password (**write the password down somewhere so you don't forget it**)
5. When installation finishes, you'll be prompted to restart. Remove the USB drive and restart.
6. Once the desktop appears, Xubuntu is installed.

---

## Chapter 5 — Initial Setup After Installation

Once you see the desktop, do the following:

1. **Update the system**
   - Open the applications menu (top left) → "Settings" → "Software Updater," apply any
     available updates, and restart.
2. **Open a terminal**
   - Press `Ctrl` + `Alt` + `T` on your keyboard. A black screen (the terminal) will open. From
     here on, you'll type commands into this screen.
   - Type each command on its own line and press `Enter` to run it.
3. **Update the system and install some essential tools** (type these into the terminal, one
   line at a time)
   ```bash
   sudo apt update
   sudo apt upgrade -y
   sudo apt install -y python3 python3-pip python3-venv alsa-utils
   ```
   - If asked for a password after `sudo`, enter the password you set in Chapter 4 (nothing will
     appear on screen as you type it, but it is being entered).

---

## Chapter 6 — Setting Up a Web Browser

Xubuntu comes with a browser called "Firefox" already installed. You'll find its icon on the
desktop or in the applications menu.

- If Firefox is fine for you, no extra steps are needed.
- If you'd prefer Google Chrome, open Firefox, go to `https://www.google.com/chrome/`, download
  the Linux version (a `.deb` file), and double-click it to install.

From here on, you'll use this browser to download files from GitHub (no terminal needed for this part).

---

## Chapter 7 — Downloading the Qji Software from GitHub

Open each of the three repository pages in your browser and download each one as a ZIP file.

1. Visit each of the following three pages in your browser, one at a time:
   - Qji player: `https://github.com/yasuhito3/Qji-Network-Audio-Player`
   - QjiDSP: `https://github.com/yasuhito3/QjiDSP-English`
   - Peak Monitor: `https://github.com/yasuhito3/Qji-peak-monitor`
2. On each page, click the green **"Code"** button near the top right, then click
   **"Download ZIP"** at the bottom of the menu that appears.
3. Doing this for all three will leave the following three ZIP files in your "Downloads" folder:
   - `Qji-Network-Audio-Player-main.zip`
   - `QjiDSP-English-main.zip`
   - `Qji-peak-monitor-main.zip`
4. Open your file manager, go to the "Downloads" folder, and **right-click each ZIP file →
   "Extract Here"**. This will produce three folders:
   - `Qji-Network-Audio-Player-main`
   - `QjiDSP-English-main`
   - `Qji-peak-monitor-main`

---

## Chapter 8 — Running Each Installer

Each of the three folders contains a `.desktop` file you can double-click to run. The basic
steps are the same for all three.

**Basic steps (same for all three)**
- Double-click the `.desktop` file in question.
- The first time, you may see a warning like "Untrusted application launcher."
  1. If a dialog appears with a button like "Launch" or "Trust and Launch," click it.
  2. If no such button appears, or the warning won't go away, **right-click the file →
     "Properties" → "Permissions" tab**, check "Allow this file to run as a program," then
     double-click it again.
- Then just follow whatever instructions appear on screen.

**1. Installing the Qji player**
   - Open the `Qji-Network-Audio-Player-main` folder → then the `qji_installer` folder inside it.
   - Double-click **"Install.desktop"** and proceed using the basic steps above.

**2. Installing QjiDSP**
   - Open the `QjiDSP-English-main` folder → then the `qjidsp_installer` folder inside it.
   - Double-click **"QjiDSP Installer.desktop"** and proceed the same way.
   - During this install, everything else needed to produce sound — CamillaDSP, ffmpeg, and so
     on — is installed automatically (this can take a little while).

**3. Installing the Peak Monitor**
   - Open the `Qji-peak-monitor-main` folder directly (there's no subfolder here).
   - Double-click **"INSTALL.desktop"** and proceed the same way.

If something fails to start, or you get an error, note down the error message (see "If You Get
Stuck" at the end of this guide).

---

## Chapter 9 — Preparing and Connecting Your DAC

1. If you have a USB DAC, plug it into the PC's USB port now.
2. In the terminal, check that Linux recognizes it:
   ```bash
   aplay -l
   ```
   If your DAC's name (for example, "Amanero") appears in the list, it's been recognized successfully.
3. Connect headphones to the DAC, or speakers via an amplifier.
4. If you're not using a DAC and just want sound from the PC's built-in headphone jack, you can
   skip this chapter.

---

## Chapter 10 — Building Your Music Library

Xubuntu sometimes comes with a folder already named "Music," but depending on your language
settings this folder can end up with a translated name instead, which Qji's library-building
script may not recognize correctly. To be safe, we recommend **creating a new folder named
"Music" in plain English letters.**

1. Open the file manager and go to your home folder (the house icon).
2. **Right-click** an empty area and choose **"Create Folder"** (or "New Folder").
3. Name the folder **`Music`**, in plain English letters — make sure it's exactly this and not
   a translated version.
4. Copy your music files (mp3, FLAC, etc.) into this new `Music` folder — via a USB drive, an
   external hard drive, or drag-and-drop.
5. Confirm that at least one track is in the `Music` folder. It's fine to start with just one
   track to test playback, and add more later.
6. Double-click the **"🗄 1-Build Music Library"** icon on the desktop. This will scan the
   `Music` folder and build it into a library. Wait for it to finish.

---

## Chapter 11 — Launching Qji and Hearing Sound

1. Once the Chapter 8 install finishes, a **"Qji奏在"** icon will appear on your desktop.
2. Double-click that icon to launch the Qji player.
3. A terminal window will open showing **"Please select your output sound card"** (or similar).
   Enter the number for the DAC you identified in Chapter 9.
4. Next you'll be asked to configure **speech recognition (microphone)**, with a choice of "1"
   or "2" — pick either one.
5. This brings up the main menu screen for playback.
6. **Enter "0"** to start playing tracks from your `Music` folder in random order.
7. If sound comes out, you're done! If it doesn't:
   - Re-check that your DAC is recognized using `aplay -l` from Chapter 9
   - Check that the volume isn't muted, via the speaker icon in the top-right of the screen
   - Double-check your headphone/speaker connection
   Then try again.

---

## If You Get Stuck

- If a command produces an error, save the exact error message (a screenshot works well) — it
  will be useful if you need to look something up or ask for help later.
- Each repository's README.md contains more detailed, up-to-date installation instructions. If
  this guide and the README disagree, follow the README.
- Everything beyond this point (building playlists, using the various sound presets, adjusting
  the soundstage with DSP, and so on) will be covered separately, as a next step.

Great work — reaching this point means you now have sound playing.
