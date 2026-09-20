"""JSON configuration for Frostbay tools.

Stored at ``~/.config/frostbay/config.json`` (override with the
``FROSTBAY_CONFIG`` env var). Holds the preferred device address and the
automation switch states so they survive restarts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "deviceUUID": "C8:17:17:F5:C8:63",
    "auto_restart": True,
    "ble_log": True,
    "auto_temp": False,
    "auto_connect": False,
    "thermal_on_c": 50.0,
    "thermal_off_c": 45.0,
    "thermal_mode": "smart_silent",
    "thermal_stages": None,
}


def config_path() -> Path:
    override = os.environ.get("FROSTBAY_CONFIG")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "frostbay" / "config.json"


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(config_path().read_text())
    except (OSError, json.JSONDecodeError):
        return cfg
    if isinstance(raw, dict):
        for key in DEFAULTS:
            if key in raw:
                cfg[key] = raw[key]
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(path)
