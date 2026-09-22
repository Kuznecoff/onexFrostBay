#!/usr/bin/env bash
# Frostbay installer for CachyOS (Arch-based; also works on other pacman distros).
#
# Does everything a user would do manually:
#   1. installs system packages (bluez, python tooling)
#   2. creates the Python venv and installs dependencies
#   3. adds a "Frostbay" entry to the applications menu (opens the TUI in a terminal)
#   4. optionally enables autostart on login
#
# Usage:
#   ./install.sh            # install + menu entry, ask about autostart
#   ./install.sh --autostart   # also start Frostbay on login
#   ./install.sh --no-autostart
set -euo pipefail

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
AUTOSTART="ask"

for arg in "$@"; do
    case "$arg" in
        --autostart) AUTOSTART="yes" ;;
        --no-autostart) AUTOSTART="no" ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

if [ "$(id -u)" = "0" ]; then
    echo "Run as a normal user (the script uses sudo where needed)." >&2
    exit 1
fi

# 1. System packages.
echo "[frostbay] Installing system packages (sudo)..."
if command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --needed --noconfirm python python-pip bluez bluez-utils git
else
    echo "[frostbay] pacman not found - install python, pip, bluez, bluez-utils manually." >&2
fi

# Make sure the Bluetooth daemon is running.
sudo systemctl enable --now bluetooth >/dev/null 2>&1 || true

# 2. Python venv + dependencies (same logic as run.sh).
echo "[frostbay] Setting up Python virtual environment..."
python3 -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

# 3. Applications-menu entry.
echo "[frostbay] Adding applications-menu entry..."
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/frostbay.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Frostbay
Comment=Frostbay BLE cooling controller
Exec=$PROJECT_DIR/run.sh
Path=$PROJECT_DIR
Icon=$PROJECT_DIR/frostbay/icon.png
Terminal=true
Categories=System;HardwareSettings;TerminalEmulator;
EOF
update-desktop-database ~/.local/share/applications >/dev/null 2>&1 || true

# 4. Autostart on login (optional).
AUTOSTART_DIR="$HOME/.config/autostart"
case "$AUTOSTART" in
    ask)
        read -r -p "[frostbay] Start Frostbay automatically on login? [y/N] " reply
        case "$reply" in [yY]*) AUTOSTART="yes" ;; *) AUTOSTART="no" ;; esac
        ;;
esac
if [ "$AUTOSTART" = "yes" ]; then
    mkdir -p "$AUTOSTART_DIR"
    cp ~/.local/share/applications/frostbay.desktop "$AUTOSTART_DIR/frostbay.desktop"
    echo "[frostbay] Autostart enabled."
else
    echo "[frostbay] Autostart not enabled (re-run: ./install.sh --autostart)."
fi

echo ""
echo "[frostbay] Done! Start with:  ./run.sh"
echo "[frostbay] Or find 'Frostbay' in the applications menu (opens a terminal)."
