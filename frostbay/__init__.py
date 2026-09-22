"""Frostbay BLE core package (used by the Textual terminal Toolbox)."""

__version__ = "0.7.0"

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
from .history import History, Sample

__all__ = [
    "__version__",
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
    "History",
    "Sample",
]
