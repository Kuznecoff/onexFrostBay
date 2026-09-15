#!/usr/bin/env bash
# Frostbay Toolbox installer (terminal / Textual TUI).
#
# Sets up everything a terminal user needs:
#   1. system packages (python3, pip, bluez) via dnf or apt-get
#   2. the Python virtual environment + dependencies (incl. Textual)
#   3. a `frostbay-toolbox` launcher in ~/.local/bin
#   4. an applications-menu entry that opens the TUI in a terminal
#   5. optional autostart on login (--autostart)
#
# Usage:
#   ./install.sh                # install only
#   ./install.sh --autostart    # install + autostart on login
#   ./install.sh --no-autostart
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/frostbay-toolbox"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$DESKTOP_DIR/frostbay-toolbox.desktop"
AUTOSTART_DIR="$HOME/.config/autostart"
AUTOSTART_FILE="$AUTOSTART_DIR/frostbay-toolbox.desktop"

AUTOSTART="no"
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

# 1. System packages. bluez is required for BLE on Linux; python3 + pip for the
#    venv. Tolerates a missing optional package by retrying a minimal set.
echo "[frostbay-toolbox] Installing system packages (sudo)..."
if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip bluez git || \
    sudo dnf install -y python3 python3-pip bluez
elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv python3-pip bluez git || \
    sudo apt-get install -y python3 python3-venv python3-pip bluez
else
    echo "[frostbay-toolbox] No dnf/apt-get found - install python3, pip, bluez manually." >&2
fi

# Make sure the Bluetooth daemon is running.
sudo systemctl enable --now bluetooth >/dev/null 2>&1 || true

# 2. Python venv + dependencies (shared with the main project venv).
echo "[frostbay-toolbox] Setting up Python virtual environment..."
if [ ! -x "$VENV_DIR/bin/python" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -r "$ROOT_DIR/requirements.txt"

# 3. Launcher in ~/.local/bin so the TUI can be started by name.
echo "[frostbay-toolbox] Installing launcher: $LAUNCHER"
mkdir -p "$BIN_DIR"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$SCRIPT_DIR/run.sh" "\$@"
EOF
chmod +x "$LAUNCHER"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "[frostbay-toolbox] NOTE: $BIN_DIR is not on PATH. Add: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# 4. Applications-menu entry. Terminal=true makes the desktop session launch the
#    TUI inside the default terminal emulator.
echo "[frostbay-toolbox] Adding applications-menu entry..."
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Frostbay Toolbox
Comment=Frostbay BLE cooling controller (terminal TUI)
Exec=$SCRIPT_DIR/run.sh
Path=$SCRIPT_DIR
Icon=$ROOT_DIR/frostbay/icon.png
Terminal=true
Categories=System;HardwareSettings;TerminalEmulator;
EOF
update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

# 5. Autostart on login (optional).
if [ "$AUTOSTART" = "yes" ]; then
    mkdir -p "$AUTOSTART_DIR"
    cp "$DESKTOP_FILE" "$AUTOSTART_FILE"
    echo "[frostbay-toolbox] Autostart enabled."
else
    echo "[frostbay-toolbox] Autostart not enabled (use: ./install-autostart.sh)."
fi

echo ""
echo "[frostbay-toolbox] Done! Start with:  frostbay-toolbox   (or ./run.sh)"
