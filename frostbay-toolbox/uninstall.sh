#!/usr/bin/env bash
# Uninstall Frostbay Toolbox: launcher, applications-menu entry and autostart.
#
# By default the shared Python virtual environment is left untouched (the main
# tray app uses the same venv). Pass --purge to remove it as well.
#
# Usage:
#   ./uninstall.sh            # remove launcher + menu entry + autostart
#   ./uninstall.sh --purge    # also delete the virtual environment
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/frostbay-toolbox"
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$DESKTOP_DIR/frostbay-toolbox.desktop"
AUTOSTART_FILE="$HOME/.config/autostart/frostbay-toolbox.desktop"
VENV_DIR="$ROOT_DIR/.venv"

PURGE="no"
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE="yes" ;;
        *) echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

echo "[frostbay-toolbox] Disabling autostart..."
rm -f "$AUTOSTART_FILE"

echo "[frostbay-toolbox] Removing applications-menu entry..."
rm -f "$DESKTOP_FILE"
update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

echo "[frostbay-toolbox] Removing launcher..."
rm -f "$LAUNCHER"

if [ "$PURGE" = "yes" ]; then
    echo "[frostbay-toolbox] Removing virtual environment: $VENV_DIR"
    rm -rf "$VENV_DIR"
else
    echo "[frostbay-toolbox] Kept virtual environment ($VENV_DIR)."
    echo "[frostbay-toolbox] It is shared with the tray app; use --purge to delete it."
fi

echo "[frostbay-toolbox] Uninstalled."
