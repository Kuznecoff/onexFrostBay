#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "[frostbay-toolbox] virtual environment not found: $VENV_DIR"
  echo "Create it with: python3 -m venv .venv"
  exit 1
fi

exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/textual_app.py" "$@"
