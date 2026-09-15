#!/usr/bin/env bash
# Install Frostbay Toolbox and enable autostart on login.
#
# Thin wrapper around install.sh --autostart so there is a single, obvious
# entry point for "install and start on login".
#
# Usage:
#   ./install-autostart.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/install.sh" --autostart
