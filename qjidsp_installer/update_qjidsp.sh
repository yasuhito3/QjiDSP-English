#!/usr/bin/env bash
#
# QjiDSP — Update Checker
#
# Compares the VERSION file on GitHub against the version installed
# under ~/qji/, and if a newer version is available, downloads it
# and re-runs install_qjidsp.sh (after confirmation).
#
# Uses the same approach (VERSION file comparison → download zip →
# re-run the installer) as Qji Peak Monitor's update.sh.
#
set -euo pipefail

QJI_DIR="${HOME}/qji"

# ── Source repository for updates ────────────────────────
REPO_OWNER="yasuhito3"
REPO_NAME="QjiDSP-English"
REPO_BRANCH="main"
RAW_VERSION_URL="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${REPO_BRANCH}/VERSION"
ZIP_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/refs/heads/${REPO_BRANCH}.zip"

TMP_DIR=""
_cleanup() {
    if [ -n "${TMP_DIR}" ] && [ -d "${TMP_DIR}" ]; then
        rm -rf "${TMP_DIR}"
    fi
}
trap _cleanup EXIT

_pause() {
    read -r -p "Press Enter to close this window… " _dummy || true
}

echo "=============================================="
echo " QjiDSP — Update Checker"
echo "=============================================="
echo

# ── 1. Check the locally installed version ───────────────
LOCAL_VERSION="unknown"
if [ -f "${QJI_DIR}/VERSION" ]; then
    LOCAL_VERSION="$(tr -d '[:space:]' < "${QJI_DIR}/VERSION")"
fi
echo "Current version: ${LOCAL_VERSION}"

# ── 2. Check for curl / wget ──────────────────────────────
if command -v curl >/dev/null 2>&1; then
    _fetch() { curl -fsSL "$1"; }
elif command -v wget >/dev/null 2>&1; then
    _fetch() { wget -qO- "$1"; }
else
    echo "✗ Neither curl nor wget was found."
    echo "  Please install one, then try again: sudo apt install curl"
    _pause
    exit 1
fi

# ── 3. Fetch the latest version from GitHub ──────────────
echo "Checking the latest version on GitHub…"
REMOTE_VERSION="$(_fetch "${RAW_VERSION_URL}" 2>/dev/null | tr -d '[:space:]' || true)"
if [ -z "${REMOTE_VERSION}" ]; then
    echo "✗ Failed to fetch the latest version info. Please check your internet connection."
    _pause
    exit 1
fi
echo "Latest version on GitHub: ${REMOTE_VERSION}"
echo

# ── 4. Compare versions (simple semantic versioning) ──────
# Returns 0 if arg1 > arg2, 1 otherwise.
_version_gt() {
    [ "$1" = "$2" ] && return 1
    local IFS=.
    local -a ver1 ver2
    read -r -a ver1 <<< "$1"
    read -r -a ver2 <<< "$2"
    local len=${#ver1[@]}
    [ "${#ver2[@]}" -gt "${len}" ] && len=${#ver2[@]}
    local i a b
    for ((i = 0; i < len; i++)); do
        a="${ver1[i]:-0}"
        b="${ver2[i]:-0}"
        if ((10#${a} > 10#${b})); then return 0; fi
        if ((10#${a} < 10#${b})); then return 1; fi
    done
    return 1
}

if [ "${LOCAL_VERSION}" != "unknown" ] && ! _version_gt "${REMOTE_VERSION}" "${LOCAL_VERSION}"; then
    echo "✓ You already have the latest version. No update needed."
    _pause
    exit 0
fi

if [ "${LOCAL_VERSION}" = "unknown" ]; then
    echo "Couldn't determine the currently installed version (this may be a fresh install, or an older version)."
    read -r -p "Download and install the latest version (${REMOTE_VERSION})? [y/N]: " ANSWER
else
    echo "🆕 A newer version is available (current: ${LOCAL_VERSION} → latest: ${REMOTE_VERSION})."
    read -r -p "Update now? [y/N]: " ANSWER
fi
case "${ANSWER}" in
    [yY]|[yY][eE][sS]) ;;
    *) echo "Cancelled."; _pause; exit 0 ;;
esac
echo

# ── 5. Check for unzip ────────────────────────────────────
if ! command -v unzip >/dev/null 2>&1; then
    echo "✗ unzip was not found."
    echo "  Please install it, then try again: sudo apt install unzip"
    _pause
    exit 1
fi

# ── 6. Download and extract the latest version ────────────
echo "Downloading the latest version…"
TMP_DIR="$(mktemp -d)"
if ! _fetch "${ZIP_URL}" > "${TMP_DIR}/update.zip"; then
    echo "✗ Download failed. Please check your internet connection."
    _pause
    exit 1
fi

echo "Extracting…"
unzip -q "${TMP_DIR}/update.zip" -d "${TMP_DIR}"

EXTRACTED_DIR="$(find "${TMP_DIR}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
# Note: unlike Qji Peak Monitor (whose install.sh sits at the repo
# root), QjiDSP's installer lives inside the "qjidsp_installer/"
# subfolder.
INSTALLER_PATH="${EXTRACTED_DIR}/qjidsp_installer/install_qjidsp.sh"
if [ -z "${EXTRACTED_DIR}" ] || [ ! -f "${INSTALLER_PATH}" ]; then
    echo "✗ Could not find install_qjidsp.sh in the extracted files."
    _pause
    exit 1
fi
echo "  ✓ Extraction complete"
echo

# ── 7. Re-run install_qjidsp.sh ──────────────────────────
# install_qjidsp.sh already pauses for Enter on its own successful
# exit, so we don't add another pause here after calling it.
echo "Running the installer…"
echo "----------------------------------------------"
bash "${INSTALLER_PATH}"
echo "----------------------------------------------"
echo

echo "=============================================="
echo " Update complete! (${LOCAL_VERSION} → ${REMOTE_VERSION})"
echo "=============================================="
