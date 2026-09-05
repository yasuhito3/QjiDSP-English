#!/bin/bash
# =============================================================
#  QjiDSP Installer v2 (English)
#  3D spatial audio extension for Qji, powered by CamillaDSP
#  (six spatial soundfield presets, v1-v6)
#  This installs the full Qji app + DSP files together into ~/qji/
# =============================================================

# --- If not already running in a terminal, relaunch in one ---
if [ -z "$TERM" ] || [ "$TERM" = "dumb" ]; then
    INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    for term in xfce4-terminal lxterminal mate-terminal xterm gnome-terminal konsole qterminal; do
        if command -v "$term" &>/dev/null; then
            case "$term" in
                xfce4-terminal) exec "$term" --disable-server -T "QjiDSP Installer" -e "bash $0" ;;
                lxterminal)     exec "$term" --title "QjiDSP Installer" -e "bash $0" ;;
                mate-terminal)  exec "$term" --title "QjiDSP Installer" -e "bash $0" ;;
                gnome-terminal) exec "$term" --title "QjiDSP Installer" -- bash "$0" ;;
                konsole)        exec "$term" --title "QjiDSP Installer" -e "bash $0" ;;
                qterminal)      exec "$term" -e "bash $0" ;;
                xterm)          exec "$term" -fa "Monospace" -fs 12 -title "QjiDSP Installer" -geometry 90x40 -e bash "$0" ;;
            esac
        fi
    done
fi

set -e
trap 'ec=$?; echo ""; echo "----------------------------------------"; echo "An error occurred (exit code: $ec)."; echo "Please check the log above."; echo "----------------------------------------"; read -rp "Press Enter to close..." _; exit 1' ERR

# ~/qji/ is the standard layout for the GitHub distribution
QJI_DIR="$HOME/qji"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$HOME/qjidsp_install_debug.log"
exec > >(tee "$LOG_FILE") 2>&1

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

banner() {
    clear
    echo -e "${CYAN}${BOLD}"
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║          🎛️  QjiDSP Installer             ║"
    echo "  ║  3D Spatial Audio for Qji (presets v1-v6) ║"
    echo "  ╚══════════════════════════════════════════╝"
    echo -e "${RESET}"
}

step() { echo -e "\n${CYAN}▶ $1${RESET}"; }
ok()   { echo -e "  ${GREEN}✓ $1${RESET}"; }
warn() { echo -e "  ${YELLOW}⚠  $1${RESET}"; }
err()  { echo -e "  ${RED}✗ $1${RESET}"; }

banner

# -------------------------------------------------------------
# Step 0: Confirm install destination
# -------------------------------------------------------------
step "Checking install destination"
if [ ! -d "$QJI_DIR" ]; then
    warn "$QJI_DIR does not exist yet. It will be created."
    mkdir -p "$QJI_DIR"
elif [ -f "$QJI_DIR/qji.py" ]; then
    ok "Existing Qji installation detected: $QJI_DIR"
else
    ok "Found $QJI_DIR (this will be a fresh install)"
fi

echo ""
echo -e "${BOLD}Install destination: ${QJI_DIR}${RESET}"
echo "This script will:"
echo "  1. Install required system packages"
echo "  2. Install/update Python libraries and yt-dlp"
echo "  3. Install CamillaDSP"
echo "  4. Install deno (used to stabilize YouTube Music playback)"
echo "  5. Set up the virtual sound card (snd-aloop)"
echo "  6. Place the full Qji app + DSP files (soundfields v1-v6) into ${QJI_DIR}/"
echo "  7. Update existing files to the DSP-enabled version (old versions are auto-backed up)"
echo ""
read -rp "Continue? [Y/n] " answer
case "$answer" in
    [nN]*) echo "Installation cancelled."; exit 0 ;;
esac

# -------------------------------------------------------------
# Step 1: Install required packages
# -------------------------------------------------------------
step "Installing required packages"

if command -v apt >/dev/null 2>&1; then
    sudo apt update
    sudo apt install -y ffmpeg alsa-utils python3-pip python3-dev wget curl unzip git
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y ffmpeg alsa-utils python3-pip python3-devel wget curl unzip git
else
    warn "Unsupported package manager."
    echo "   Please install ffmpeg, alsa-utils, python3-pip, python3-dev, wget, curl, unzip, and git manually."
fi
ok "Base packages confirmed"

# -------------------------------------------------------------
# Step 2: Install/update Python libraries and yt-dlp
# -------------------------------------------------------------
step "Installing/updating Python libraries and yt-dlp"

# Note: rather than "try with --break-system-packages, and on any
# failure silently retry without the flag", which hides the real
# error behind a generic PEP668 message on the fallback attempt,
# we detect once whether pip supports the flag and then run once,
# with errors left visible.
PIP_FLAGS=""
if pip install --help 2>/dev/null | grep -q -- "--break-system-packages"; then
    PIP_FLAGS="--break-system-packages"
fi

pip install $PIP_FLAGS soundfile scipy
pip install $PIP_FLAGS "git+https://github.com/HEnquist/pycamilladsp.git"
ok "soundfile / scipy / pycamilladsp installed"

pip install -U $PIP_FLAGS yt-dlp
ok "yt-dlp updated to the latest version: $(yt-dlp --version 2>/dev/null || echo '(will be verified later)')"

# -------------------------------------------------------------
# Step 3: Install CamillaDSP
# -------------------------------------------------------------
step "Installing CamillaDSP"

if command -v camilladsp >/dev/null 2>&1; then
    ok "CamillaDSP is already installed: $(camilladsp --version)"
else
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  CDSP_ASSET="camilladsp-linux-amd64.tar.gz" ;;
        aarch64) CDSP_ASSET="camilladsp-linux-aarch64.tar.gz" ;;
        armv7l)  CDSP_ASSET="camilladsp-linux-armv7.tar.gz" ;;
        *)
            err "Unsupported architecture: $ARCH"
            echo "   Please download manually from https://github.com/HEnquist/camilladsp/releases"
            exit 1
            ;;
    esac
    echo "  Architecture: $ARCH → $CDSP_ASSET"
    TMP_DIR=$(mktemp -d)
    cd "$TMP_DIR"
    wget "https://github.com/HEnquist/camilladsp/releases/latest/download/${CDSP_ASSET}"
    tar xzf "$CDSP_ASSET"
    sudo mv camilladsp /usr/local/bin/
    cd - > /dev/null
    rm -rf "$TMP_DIR"
    ok "CamillaDSP installed: $(camilladsp --version)"
fi

# -------------------------------------------------------------
# Step 4: Install deno (helps yt-dlp with YouTube Music)
# -------------------------------------------------------------
step "Installing deno (stabilizes YouTube Music playback)"

if command -v deno >/dev/null 2>&1; then
    ok "deno is already installed: $(deno --version | head -n1)"
else
    curl -fsSL https://deno.land/install.sh | sh
    ok "deno installed"
fi

# Add to PATH via ~/.bashrc if not already present
DENO_ENV_MARK="# added by QjiDSP installer (deno)"
if ! grep -qs "$DENO_ENV_MARK" "$HOME/.bashrc" 2>/dev/null; then
    {
        echo ""
        echo "$DENO_ENV_MARK"
        echo 'export DENO_INSTALL="$HOME/.deno"'
        echo 'export PATH="$DENO_INSTALL/bin:$PATH"'
    } >> "$HOME/.bashrc"
    ok "Added deno's PATH setting to ~/.bashrc (takes effect in new terminals)"
else
    ok "~/.bashrc already has deno's PATH setting"
fi
# Also make it available for the rest of this install run
export DENO_INSTALL="$HOME/.deno"
export PATH="$DENO_INSTALL/bin:$PATH"

if command -v deno >/dev/null 2>&1; then
    ok "deno check OK: $(deno --version | head -n1)"
else
    warn "deno command not found. Please try again after restarting your terminal."
fi

# -------------------------------------------------------------
# Step 5: Set up the virtual sound card (snd-aloop)
# -------------------------------------------------------------
step "Setting up the virtual sound card (snd-aloop)"

echo "snd-aloop" | sudo tee /etc/modules-load.d/snd-aloop.conf > /dev/null
sudo modprobe snd-aloop 2>/dev/null || true

if aplay -L 2>/dev/null | grep -qi loopback; then
    ok "Loopback device confirmed"
else
    warn "Loopback device not found. Please check again after rebooting."
fi

# -------------------------------------------------------------
# Step 6: Place files (all unified under ~/qji/)
# -------------------------------------------------------------
step "Placing the Qji app + QjiDSP files (under ${QJI_DIR}/)"

mkdir -p "$QJI_DIR/camilladsp_test"
for v in 1 2 3 4 5 6; do
    mkdir -p "$QJI_DIR/qjidsp_backup_v${v}"
done

# IR file, wobble scripts, watchdog
cp "$SCRIPT_DIR/Musikvereinsaal_48k_tail.wav" "$QJI_DIR/camilladsp_test/"
cp "$SCRIPT_DIR/wobble_v1.py" "$QJI_DIR/camilladsp_test/"
cp "$SCRIPT_DIR/wobble_v2.py" "$QJI_DIR/camilladsp_test/"
cp "$SCRIPT_DIR/wobble_v3_static.py" "$QJI_DIR/camilladsp_test/"
cp "$SCRIPT_DIR/wobble_v4.py" "$QJI_DIR/camilladsp_test/"
cp "$SCRIPT_DIR/wobble_v5.py" "$QJI_DIR/camilladsp_test/"
# qji.py looks for "wobble_v5_harmonics_hp.py" (preset v6, headphone
# harmonics mode), so the distributed file is placed under that name too.
cp "$SCRIPT_DIR/wobble_v5_harmonics.py" "$QJI_DIR/camilladsp_test/wobble_v5_harmonics_hp.py"
cp "$SCRIPT_DIR/cdsp_watchdog.py" "$QJI_DIR/camilladsp_test/cdsp_watchdog.py"
ok "IR / wobble / watchdog files placed"

# YAML files: convert the IR (wav) file path to the home-directory-independent
# {HOME} placeholder before placing it (qji.py expands {HOME} to the real
# home path at startup).
# Note: the v5/v6 YAML files had CRLF line endings mixed in, so normalize to LF too.
for v in 1 2 3 4 5 6; do
    sed -E 's#/home/[^/"]+/camilladsp_test#{HOME}/camilladsp_test#g' \
        "$SCRIPT_DIR/spatial_final_v${v}.yml" | tr -d '\r' \
        > "$QJI_DIR/qjidsp_backup_v${v}/spatial_final.yml"
done
ok "DSP config files (v1-v6) placed"

# --- Qji app modules (existing files are backed up with a timestamp before being overwritten) ---
backup_and_install() {
    local fname="$1"
    if [ ! -f "$SCRIPT_DIR/$fname" ]; then
        warn "$fname was not found in the bundled package. Skipping."
        return
    fi
    if [ -f "$QJI_DIR/$fname" ]; then
        local backup_name="${fname}.bak_before_dsp_$(date +%Y%m%d_%H%M%S)"
        cp "$QJI_DIR/$fname" "$QJI_DIR/$backup_name"
        ok "Backed up existing $fname as: $backup_name"
    fi
    cp "$SCRIPT_DIR/$fname" "$QJI_DIR/$fname"
    ok "$fname updated"
}

backup_and_install "qji.py"
backup_and_install "qji_qobuzdsp.py"
backup_and_install "qji_qobuz_browser.py"
backup_and_install "qji_soundcloud.py"
backup_and_install "qji_soundcloud_browser.py"
backup_and_install "qji_ytmusic.py"
backup_and_install "qji_ytmusic_browser.py"

# Stale bytecode cache can prevent changes from taking effect
if [ -d "$QJI_DIR/__pycache__" ]; then
    rm -rf "$QJI_DIR/__pycache__"
    ok "Removed stale Python cache (__pycache__)"
fi

# Safety net: fix up old-style import names if they're still present
if grep -q "^import qji_qobuz$" "$QJI_DIR/qji.py"; then
    sed -i "s/^import qji_qobuz\$/import qji_qobuzdsp as qji_qobuz/" "$QJI_DIR/qji.py"
    ok "Updated qji.py's import statement for qji_qobuzdsp"
fi
if grep -q "modules.get('qji_qobuz')" "$QJI_DIR/qji.py"; then
    sed -i "s/modules.get('qji_qobuz')/modules.get('qji_qobuzdsp')/g" "$QJI_DIR/qji.py"
    ok "Updated qji.py's sys.modules reference for qji_qobuzdsp"
fi

# qji.py builds its DSP-related paths (under camilladsp_test/ and
# qjidsp_backup_vN/) straight from os.path.expanduser("~"), so when
# everything is unified under ~/qji/ we need to insert "qji/" into
# those paths. This covers soundfields v1-v6, wobble scripts, and the watchdog.
python3 - "$QJI_DIR" << 'PYEOF'
import sys
qji_dir = sys.argv[1]
path = f"{qji_dir}/qji.py"
with open(path, "r") as f:
    content = f.read()

before_camilladsp = content.count("{_HOME}/camilladsp_test/")
before_backup = content.count("{_HOME}/qjidsp_backup_v")

content = content.replace("{_HOME}/camilladsp_test/", "{_HOME}/qji/camilladsp_test/")
content = content.replace("{_HOME}/qjidsp_backup_v", "{_HOME}/qji/qjidsp_backup_v")

# The {HOME} placeholder inside the YAML content itself (used for the IR/wav
# path) is expanded through a separate mechanism (.replace('{HOME}', _HOME)),
# so it needs "qji/" added separately. Missing this causes the Conv filter's
# wav file to fail with "No such file or directory".
old_yml_replace = "_yml_content.replace('{HOME}', _HOME)"
new_yml_replace = "_yml_content.replace('{HOME}', _HOME + '/qji')"
yml_replace_count = content.count(old_yml_replace)
content = content.replace(old_yml_replace, new_yml_replace)

with open(path, "w") as f:
    f.write(content)

print(f"  Unified qji.py's DSP paths under ~/qji/ "
      f"(camilladsp_test-style: {before_camilladsp} / qjidsp_backup_v-style: {before_backup} / "
      f"yml {{HOME}} expansion: {yml_replace_count})")
if yml_replace_count == 0:
    print("  ⚠ Could not find the yml {HOME} expansion line. qji.py's implementation may have changed.")
PYEOF

# cdsp_watchdog.py independently builds its own path from
# os.path.expanduser("~") too, and needs the same "qji/" fix, or it
# will lose track of the spatial_final.yml it's supposed to watch.
python3 - "$QJI_DIR" << 'PYEOF'
import sys
qji_dir = sys.argv[1]
path = f"{qji_dir}/camilladsp_test/cdsp_watchdog.py"
with open(path, "r") as f:
    content = f.read()
old = 'CDSP_YML = f"{HOME}/camilladsp_test/spatial_final.yml"'
new = 'CDSP_YML = f"{HOME}/qji/camilladsp_test/spatial_final.yml"'
if old in content:
    content = content.replace(old, new)
    with open(path, "w") as f:
        f.write(content)
    print("  Unified cdsp_watchdog.py's path under ~/qji/")
else:
    print("  cdsp_watchdog.py already uses the expected path; no change needed")
PYEOF

ok "File placement and path adjustments complete"

# --- Place VERSION / the update script ---
# update_qjidsp.sh uses this to check the currently installed
# version. Placing it inside ~/qji/ means the update check works
# even if the qjidsp_installer/ folder isn't kept around — just
# "cd ~/qji && bash update_qjidsp.sh".
if [ -f "$SCRIPT_DIR/VERSION" ]; then
    cp "$SCRIPT_DIR/VERSION" "$QJI_DIR/VERSION"
else
    echo "unknown" > "$QJI_DIR/VERSION"
fi
if [ -f "$SCRIPT_DIR/update_qjidsp.sh" ]; then
    cp "$SCRIPT_DIR/update_qjidsp.sh" "$QJI_DIR/update_qjidsp.sh"
    chmod +x "$QJI_DIR/update_qjidsp.sh"
fi
if [ -f "$SCRIPT_DIR/QjiDSP Update Checker.desktop" ]; then
    cp "$SCRIPT_DIR/QjiDSP Update Checker.desktop" "$QJI_DIR/QjiDSP Update Checker.desktop"
    chmod +x "$QJI_DIR/QjiDSP Update Checker.desktop"
    if command -v gio >/dev/null 2>&1; then
        gio set "$QJI_DIR/QjiDSP Update Checker.desktop" "metadata::trusted" true >/dev/null 2>&1 || true
    fi
fi
ok "Placed VERSION / the update script / the update-checker icon"

# -------------------------------------------------------------
# Step 7: Check the desktop icon
# -------------------------------------------------------------
step "Checking the desktop icon"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null)"
[ -z "$DESKTOP_DIR" ] && DESKTOP_DIR="$HOME/Desktop"

if [ -d "$DESKTOP_DIR" ] && ls "$DESKTOP_DIR"/*.desktop >/dev/null 2>&1; then
    ok "Your existing Qji desktop icon can be used as-is (launches: ${QJI_DIR}/qji.py)"
else
    warn "No desktop icon found. Please use the icon created by the main Qji installer."
fi

# -------------------------------------------------------------
# Step 8: Verification
# -------------------------------------------------------------
step "Verification"

for pyfile in qji.py qji_qobuzdsp.py qji_qobuz_browser.py qji_soundcloud.py qji_soundcloud_browser.py qji_ytmusic.py qji_ytmusic_browser.py; do
    if [ -f "$QJI_DIR/$pyfile" ]; then
        python3 -c "import ast; ast.parse(open('$QJI_DIR/$pyfile').read())" \
            && ok "$pyfile syntax OK" \
            || warn "$pyfile failed the syntax check"
    fi
done

for v in 1 2 3 4 5 6; do
    if camilladsp "$QJI_DIR/qjidsp_backup_v${v}/spatial_final.yml" --check >/dev/null 2>&1; then
        ok "spatial_final_v${v}.yml syntax OK"
    else
        warn "spatial_final_v${v}.yml failed the syntax check (this may just be a DAC name mismatch, which is fixed automatically the first time you select your DAC)"
    fi
done

python3 -c "import camilladsp; print('  pycamilladsp check OK')" 2>/dev/null || \
    warn "Failed to import pycamilladsp"

command -v yt-dlp >/dev/null 2>&1 && ok "yt-dlp check OK: $(yt-dlp --version)" || warn "yt-dlp not found"
command -v deno >/dev/null 2>&1 && ok "deno check OK: $(deno --version | head -n1)" || warn "deno not found (please restart your terminal and check again)"

# -------------------------------------------------------------
# Done
# -------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}============================================================${RESET}"
echo -e "${GREEN}${BOLD}  🎉 QjiDSP installation complete!${RESET}"
echo -e "${GREEN}${BOLD}============================================================${RESET}"
echo ""
echo "  How to launch:"
echo "    Use your existing Qji desktop icon as-is."
echo "    (Manual launch: cd ${QJI_DIR} && python3 qji.py)"
echo ""
echo "  After launching, select \"Loopback\" as the output device to"
echo "  choose from six 3D spatial audio modes (v1-v6):"
echo "    1) Rich hall (static)         2) Rich hall (dynamic)"
echo "    3) Natural timbre (static)    4) Natural timbre (dynamic, gentle panning)"
echo "    5) Harmonics mode             6) Harmonics mode (for headphones)"
echo ""
echo "  Your DAC (audio interface) can be auto-detected and selected"
echo "  right after choosing a DSP mode."
echo ""
echo "  deno's PATH has been added to ~/.bashrc."
echo "  Open a new terminal, or run 'source ~/.bashrc', for it to take effect."
echo ""
echo "  Checking for updates:"
echo "    cd ${QJI_DIR} && bash update_qjidsp.sh"
echo "    (or double-click ${QJI_DIR}/QjiDSP Update Checker.desktop)"
echo -e "${GREEN}${BOLD}============================================================${RESET}"
read -rp "Press Enter to close..." _
