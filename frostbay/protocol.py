"""Frostbay BLE protocol implementation.

Parses and builds the 64-byte FFE1 state blob described in specification.md.
All byte offsets and command recipes follow the verified protocol.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


# Service / characteristic UUIDs (short 16-bit form expanded to full 128-bit)
UUID_FFE0 = "0000ffe0-0000-1000-8000-00805f9b34fb"
UUID_FFE1 = "0000ffe1-0000-1000-8000-00805f9b34fb"
UUID_FFE4 = "0000ffe4-0000-1000-8000-00805f9b34fb"

STATE_LEN = 64
CHUNK_SIZE = 20
WRITE_DELAY_SEC = 0.020
READBACK_DELAY_SEC = 0.300

# Transport control bytes that prefix each chunk written to FFE1.
CHUNK_PREFIX_1 = 0x1C
CHUNK_PREFIX_2 = 0x2C
CHUNK_PREFIX_3 = 0x3C


class Mode(Enum):
    OFF = 0x00
    SMART = 0xFE
    FIXED = 0xFF

    @property
    def label(self) -> str:
        return {
            Mode.OFF: "OFF",
            Mode.SMART: "Smart Fan",
            Mode.FIXED: "Fixed Fan",
        }[self]

    @classmethod
    def from_byte(cls, value: int) -> "Mode":
        if value == 0xFE:
            return Mode.SMART
        if value == 0xFF:
            return Mode.FIXED
        return Mode.OFF


# Known-good smart preset curve payloads (state[23..40]).
SMART_CURVES: dict[str, bytes] = {
    "silent": bytes.fromhex("1E18201C2124222C233024342538263C2846"),
    "soft": bytes.fromhex("1E222026212E2236233A243E254226462850"),
    "strong": bytes.fromhex("1E2C20302138224023442448254C26502864"),
}

# Known-good outbound state[6..7] recipe bytes per smart preset.
SMART_SELECTORS: dict[str, tuple[int, int]] = {
    "silent": (0x06, 0x00),
    "soft": (0x06, 0xF0),
    "strong": (0x08, 0x10),
}

SMART_BASELINE_FAN = 0x32
SMART_BASELINE_PUMP = 0x64


@dataclass
class FrostbayState:
    """Structured view of the 64-byte FFE1 state blob."""

    raw: bytearray = field(default_factory=lambda: bytearray(STATE_LEN))

    # --- parsed fields ---
    protocol_version: int = 0
    mode: Mode = Mode.OFF
    fan_percent: int = 0
    flow_ml_min: float = 0.0
    pump_percent: int = 0
    activity_running: bool = False
    temp_in_c: int = 0
    temp_out_c: int = 0
    smart_curve: bytes = b""

    @classmethod
    def from_raw(cls, data: bytes) -> "FrostbayState":
        if len(data) < STATE_LEN:
            data = data + bytearray(STATE_LEN - len(data))
        raw = bytearray(data[:STATE_LEN])
        s = cls(raw=raw)
        s.protocol_version = raw[2]
        s.mode = Mode.from_byte(raw[4])
        s.fan_percent = raw[5]
        flow_word = (raw[6] << 8) | raw[7]
        s.flow_ml_min = flow_word / 10.0
        s.pump_percent = raw[8]
        s.activity_running = raw[12] != 0
        s.temp_in_c = raw[13]
        s.temp_out_c = raw[14]
        s.smart_curve = bytes(raw[23:41])
        return s

    def is_running(self) -> bool:
        """Best runtime ON/OFF indicator per spec: state[12] != 0."""
        return self.activity_running

    def summary(self) -> str:
        lines = [
            f"Mode:          {self.mode.label}",
            f"Running:       {'yes' if self.is_running() else 'no'}",
            f"Fan:           {self.fan_percent} %",
            f"Flow:          {self.flow_ml_min:.1f} mL/min",
            f"Pump:          {self.pump_percent} %",
            f"Temp in:       {self.temp_in_c} C",
            f"Temp out:      {self.temp_out_c} C",
            f"Protocol ver:  0x{self.protocol_version:02X}",
        ]
        return "\n".join(lines)


# --- command builders -------------------------------------------------------

def _patch_state(current: bytes, patches: dict[int, int], curve: bytes | None = None) -> bytearray:
    """Return a copy of current state with selected byte offsets patched."""
    state = bytearray(STATE_LEN)
    # Preserve original bytes where available.
    for i in range(min(len(current), STATE_LEN)):
        state[i] = current[i]
    for offset, value in patches.items():
        state[offset] = value
    if curve is not None:
        # Smart curve occupies state[23..40] inclusive = 18 bytes.
        state[23:41] = curve[:18]
    return state


def build_off(current: bytes) -> bytearray:
    return _patch_state(current, {4: Mode.OFF.value})


def build_smart(current: bytes, preset: str, pump_percent: int | None = None) -> bytearray:
    if preset not in SMART_CURVES:
        raise ValueError(f"unknown smart preset: {preset}")
    patches: dict[int, int] = {
        4: Mode.SMART.value,
        5: SMART_BASELINE_FAN,
    }
    sel = SMART_SELECTORS[preset]
    patches[6] = sel[0]
    patches[7] = sel[1]
    if pump_percent is not None:
        patches[8] = _clamp_pump(pump_percent)
    else:
        patches[8] = SMART_BASELINE_PUMP
    return _patch_state(current, patches, curve=SMART_CURVES[preset])


def build_fixed(current: bytes, fan_percent: int, pump_percent: int) -> bytearray:
    patches = {
        4: Mode.FIXED.value,
        5: _clamp_percent(fan_percent),
        8: _clamp_pump(pump_percent),
    }
    return _patch_state(current, patches)


def build_set_pump(current: bytes, pump_percent: int) -> bytearray:
    return _patch_state(current, {8: _clamp_pump(pump_percent)})


# --- transport encoding -----------------------------------------------------

def encode_write_chunks(state: bytearray) -> list[bytes]:
    """Encode a 64-byte state blob into the three FFE1 transport chunks.

    Layout (per spec):
        write_payload[0]     = 0x02
        write_payload[1..57] = state[2..59]   (58-byte payload)
        chunk_1 = [0x1C] + write_payload[0..18]
        chunk_2 = [0x2C] + write_payload[19..37]
        chunk_3 = [0x3C] + write_payload[38..56] + zero pad to 20 bytes
    """
    payload = bytearray(58)
    payload[0] = 0x02
    payload[1:58] = state[2:59]

    chunk1 = bytes(bytearray([CHUNK_PREFIX_1]) + payload[0:19])
    chunk2 = bytes(bytearray([CHUNK_PREFIX_2]) + payload[19:38])
    chunk3_core = bytearray([CHUNK_PREFIX_3]) + payload[38:57]
    # zero-pad chunk3 to CHUNK_SIZE bytes total
    if len(chunk3_core) < CHUNK_SIZE:
        chunk3_core += bytearray(CHUNK_SIZE - len(chunk3_core))
    return [chunk1, chunk2, bytes(chunk3_core)]


# --- helpers ----------------------------------------------------------------

def _clamp_percent(value: int, lo: int = 0, hi: int = 100) -> int:
    return max(lo, min(hi, int(value)))


def _clamp_pump(value: int) -> int:
    """Pump is supported in 50..100; clamp into that range."""
    return _clamp_percent(value, 50, 100)


def _clamp_percent_byte(value: int) -> int:
    return _clamp_percent(value, 0, 100)


def chunk_write_delays() -> list[float]:
    """Inter-chunk delays in seconds: delay before chunk 2 and chunk 3."""
    return [WRITE_DELAY_SEC, WRITE_DELAY_SEC]


def readback_delay() -> float:
    return READBACK_DELAY_SEC
