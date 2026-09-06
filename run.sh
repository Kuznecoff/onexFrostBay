#!/usr/bin/env bash
# Frostbay one-command launcher (Linux / macOS).
#
# Creates the virtual environment and installs dependencies on first run,
# then starts the app. Every later run just starts the app.
#
# Usage:
#   ./run.sh                 # tray app with auto-discovery (ONEC1)
#   ./run.sh --address MAC   # connect to a known device
#   ./run.sh --no-tray       # console mode
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
PY="$VENV/bin/python"

# First run: create venv and install Python dependencies.
if [ ! -x "$PY" ]; then
    echo "[frostbay] First run: creating virtual environment..."
    python3 -m venv "$VENV"
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r requirements.txt
    echo "[frostbay] Dependencies installed."
fi

# Linux: warn early if the Bluetooth stack is missing (bleak needs BlueZ).
if [ "$(uname)" = "Linux" ]; then
    if ! command -v bluetoothctl >/dev/null 2>&1; then
        echo "[frostbay] WARNING: bluez not found (bluetoothctl missing)." >&2
        echo "[frostbay]          Run ./install.sh or: sudo dnf install bluez" >&2
    fi
fi

exec "$PY" -m frostbay "$@"
