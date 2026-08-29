"""Frostbay BLE tray application package."""

from .protocol import (
    FrostbayState,
    Mode,
    SMART_CURVES,
    build_off,
    build_smart,
    build_fixed,
    build_set_pump,
)
from .ble import FrostbayBLE, ScanResult, FROSTBAY_NAME_PART, is_frostbay_name
from .icons import IconState, render_icon
from .app import FrostbayTrayApp, main
from .console import FrostbayConsole, run_console

__all__ = [
    "FrostbayState",
    "Mode",
    "SMART_CURVES",
    "build_off",
    "build_smart",
    "build_fixed",
    "build_set_pump",
    "FrostbayBLE",
    "ScanResult",
    "FROSTBAY_NAME_PART",
    "is_frostbay_name",
    "IconState",
    "render_icon",
    "FrostbayTrayApp",
    "main",
    "FrostbayConsole",
    "run_console",
]
