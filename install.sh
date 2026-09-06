#!/usr/bin/env bash
# Frostbay installer for Fedora (also works on other RPM distros with dnf).
#
# Does everything a user would do manually:
#   1. installs system packages (bluez, python tooling, GNOME tray support)
#   2. creates the Python venv and installs dependencies
#   3. adds a "Frostbay" entry to the applications menu
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

# 1. System packages. gnome-shell-extension-appindicator only matters on GNOME
#    (KDE has a native tray); dnf ignores it if the package set disallows it.
echo "[frostbay] Installing system packages (sudo)..."
if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip bluez git \
        gnome-shell-extension-appindicator || \
    sudo dnf install -y python3 python3-pip bluez git
else
    echo "[frostbay] dnf not found - install python3, pip, bluez manually." >&2
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
Terminal=false
Categories=System;HardwareSettings;
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
echo "[frostbay] Or find 'Frostbay' in the applications menu."
echo "[frostbay] GNOME users: enable the 'AppIndicator and KStatusNotifierItem'"
echo "[frostbay] support extension (then re-login) if the tray icon is missing."
