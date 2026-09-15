#!/usr/bin/env bash
# Disable Frostbay Toolbox autostart on login.
#
# Removes only the autostart entry; the app, launcher and menu entry stay
# installed. Re-enable later with ./install-autostart.sh.
#
# Usage:
#   ./disable-autostart.sh
set -euo pipefail

AUTOSTART_DIR="$HOME/.config/autostart"
AUTOSTART_FILE="$AUTOSTART_DIR/frostbay-toolbox.desktop"

if [ -f "$AUTOSTART_FILE" ]; then
    rm -f "$AUTOSTART_FILE"
    echo "[frostbay-toolbox] Autostart disabled ($AUTOSTART_FILE removed)."
else
    echo "[frostbay-toolbox] Autostart was not enabled; nothing to do."
fi
