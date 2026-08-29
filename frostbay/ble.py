"""Cross-platform BLE client for the Frostbay device.

Uses `bleak` which wraps:
  * Windows: WinRT Bluetooth
  * macOS: CoreBluetooth
  * Linux: BlueZ D-Bus

The client exposes a small asyncio-friendly API used by the tray app:
  * connect / disconnect
  * read current state (FFE1)
  * write a patched state (3-chunk transport)
  * high-level helpers: off, smart, fixed, set_pump
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Optional

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.exc import BleakError
except ImportError as exc:  # pragma: no cover - import guard
    raise RuntimeError(
        "bleak is required: pip install bleak"
    ) from exc

from .protocol import (
    FrostbayState,
    UUID_FFE0,
    UUID_FFE1,
    UUID_FFE4,
    READBACK_DELAY_SEC,
    WRITE_DELAY_SEC,
    build_off,
    build_smart,
    build_fixed,
    build_set_pump,
    encode_write_chunks,
)

logger = logging.getLogger("frostbay.ble")

# Substring that identifies a Frostbay device in advertised BLE names.
# The device advertises names like "CoolingSystem_ONEC1", so we match on
# the case-insensitive substring "ONEC1".
FROSTBAY_NAME_PART = "ONEC1"


def is_frostbay_name(name: str) -> bool:
    """Return True if the advertised name looks like a Frostbay device."""
    return FROSTBAY_NAME_PART.lower() in (name or "").lower()


@dataclass
class ScanResult:
    name: str
    address: str
    rssi: int

    @property
    def is_frostbay(self) -> bool:
        return is_frostbay_name(self.name)


class FrostbayBLE:
    """High-level Frostbay BLE controller."""

    def __init__(self, address: Optional[str] = None, on_state: Optional[Callable[[FrostbayState], None]] = None) -> None:
        self._address = address
        self._client: Optional[BleakClient] = None
        self._ffe1: Optional[BleakGATTCharacteristic] = None
        self._ffe4: Optional[BleakGATTCharacteristic] = None
        self._on_state = on_state
        self._lock = asyncio.Lock()
        self._connected = False
        self._stop_notifications = None
        self._poll_task: Optional[asyncio.Task] = None
        self._poll_interval = 2.0
        self._poll_stop: Optional[asyncio.Event] = None

    # --- status ---------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None and self._client.is_connected

    @property
    def address(self) -> Optional[str]:
        return self._address

    # --- discovery ------------------------------------------------------
    async def scan(self, timeout: float = 10.0) -> list[ScanResult]:
        """Scan for BLE devices.

        Frostbay devices are identified by the substring `ONEC1` (case-insensitive)
        in the advertised name, e.g. ``CoolingSystem_ONEC1``. Frostbay matches are
        sorted first (strongest RSSI first), then other devices follow.
        """
        logger.info("Scanning for BLE devices (%.1fs)...", timeout)
        devices = await BleakScanner.discover(timeout=timeout)
        results = []
        for d in devices:
            name = d.name or ""
            results.append(ScanResult(name=name, address=d.address, rssi=getattr(d, "rssi", 0)))
        results.sort(key=lambda r: (0 if is_frostbay_name(r.name) else 1, -r.rssi))
        logger.info("Found %d devices (%d Frostbay)", len(results), sum(1 for r in results if r.is_frostbay))
        return results

    async def find_frostbay(self, timeout: float = 10.0) -> Optional[ScanResult]:
        """Scan and return the first Frostbay device found (by name substring).

        Returns ``None`` if no device advertising a name containing ``ONEC1`` was
        found during the scan window.
        """
        results = await self.scan(timeout=timeout)
        for r in results:
            if r.is_frostbay:
                logger.info("Frostbay device found: %s (%s, RSSI=%d)", r.name, r.address, r.rssi)
                return r
        logger.warning("No Frostbay device named *%s* found.", FROSTBAY_NAME_PART)
        return None

    async def find_and_connect(self, timeout: float = 10.0) -> ScanResult:
        """Scan for the Frostbay device by name and connect to it automatically.

        Raises ``RuntimeError`` if no device with a name containing ``ONEC1`` was
        found.
        """
        found = await self.find_frostbay(timeout=timeout)
        if found is None:
            raise RuntimeError(f"No Frostbay device named *{FROSTBAY_NAME_PART}* found")
        await self.connect(address=found.address)
        return found

    # --- connection -----------------------------------------------------
    async def connect(self, address: Optional[str] = None, timeout: float = 30.0) -> None:
        addr = address or self._address
        if not addr:
            raise ValueError("No device address provided")
        self._address = addr
        logger.info("Connecting to %s ...", addr)
        self._client = BleakClient(addr, timeout=timeout)
        await self._client.connect()
        await self._resolve_chars()
        self._connected = True
        logger.info("Connected to Frostbay at %s", addr)
        # Initial state read + notification subscription.
        try:
            await self.refresh_state()
        except Exception as exc:
            logger.warning("Initial state read failed: %s", exc)
        try:
            await self._start_notifications()
        except Exception as exc:
            logger.warning("Notification subscription failed: %s", exc)
        # Start background auto-polling for live parameter updates.
        try:
            await self.start_polling(interval=self._poll_interval)
        except Exception as exc:
            logger.warning("Auto-polling start failed: %s", exc)

    async def disconnect(self) -> None:
        try:
            await self.stop_polling()
        except Exception:
            pass
        if self._client is None:
            self._connected = False
            return
        try:
            await self._stop_notifications_safe()
        except Exception:
            pass
        try:
            await self._client.disconnect()
        except Exception as exc:
            logger.warning("Disconnect error: %s", exc)
        self._connected = False
        self._ffe1 = None
        self._ffe4 = None
        logger.info("Disconnected")

    async def _resolve_chars(self) -> None:
        if self._client is None:
            raise RuntimeError("Client not connected")
        await self._client.get_services()
        ffe1 = self._find_char(UUID_FFE1)
        if ffe1 is None:
            raise RuntimeError(f"Characteristic {UUID_FFE1} not found on the device")
        self._ffe1 = ffe1
        self._ffe4 = self._find_char(UUID_FFE4)
        logger.debug("Resolved FFE1=%s FFE4=%s", bool(self._ffe1), bool(self._ffe4))

    def _find_char(self, uuid: str) -> Optional[BleakGATTCharacteristic]:
        assert self._client is not None
        for service in self._client.services:
            for char in service.characteristics:
                if char.uuid.lower() == uuid.lower():
                    return char
        return None

    # --- notifications --------------------------------------------------
    async def _start_notifications(self) -> None:
        if self._ffe4 is None:
            return
        async def _handler(sender, data: bytearray) -> None:
            logger.debug("Notification from %s: %d bytes", sender, len(data))
            # Notifications from FFE4 may indicate a state change; refresh FFE1.
            await asyncio.create_task(self.refresh_state())
        try:
            await self._client.start_notify(self._ffe4, _handler)
            self._stop_notifications = lambda: self._client and self._client.stop_notify(self._ffe4)
        except Exception as exc:
            logger.debug("Could not start notify on FFE4: %s", exc)

    async def _stop_notifications_safe(self) -> None:
        if self._stop_notifications is not None:
            try:
                await self._stop_notifications()
            except Exception:
                pass
            self._stop_notifications = None

    # --- core read / write ---------------------------------------------
    async def read_state(self) -> FrostbayState:
        if self._ffe1 is None:
            raise RuntimeError("Not connected or FFE1 not resolved")
        data = await self._client.read_gatt_char(self._ffe1)
        logger.debug("Read FFE1: %d bytes: %s", len(data), data.hex())
        state = FrostbayState.from_raw(bytes(data))
        if self._on_state:
            try:
                self._on_state(state)
            except Exception as exc:
                logger.warning("on_state callback error: %s", exc)
        return state

    async def write_state(self, state: bytearray) -> None:
        if self._ffe1 is None:
            raise RuntimeError("Not connected or FFE1 not resolved")
        chunks = encode_write_chunks(state)
        for idx, chunk in enumerate(chunks):
            logger.debug("Write chunk %d: %s", idx + 1, chunk.hex())
            await self._client.write_gatt_char(self._ffe1, chunk, response=False)
            if idx < len(chunks) - 1:
                await asyncio.sleep(WRITE_DELAY_SEC)
        await asyncio.sleep(READBACK_DELAY_SEC)

    async def refresh_state(self) -> FrostbayState:
        return await self.read_state()

    # --- auto-polling ---------------------------------------------------
    async def start_polling(self, interval: float = 2.0) -> None:
        """Start a background loop that periodically reads the state.

        Each tick calls ``on_state`` (if provided) with the fresh
        :class:`FrostbayState`, so a UI can render live-updating parameters
        such as temperatures and flow rate without manual refresh.
        """
        await self.stop_polling()
        self._poll_interval = float(interval)
        self._poll_stop = asyncio.Event()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Auto-polling started (%.1fs)", self._poll_interval)

    async def stop_polling(self) -> None:
        if self._poll_stop is not None:
            self._poll_stop.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        self._poll_stop = None
        logger.debug("Auto-polling stopped")

    async def _poll_loop(self) -> None:
        assert self._poll_stop is not None
        while not self._poll_stop.is_set():
            try:
                if self.is_connected and self._ffe1 is not None:
                    await self.read_state()
            except Exception as exc:
                logger.debug("Poll tick error: %s", exc)
            try:
                await asyncio.wait_for(self._poll_stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    # --- high-level commands ------------------------------------------
    async def set_off(self) -> FrostbayState:
        async with self._lock:
            current = await self.read_state()
            await self.write_state(build_off(bytes(current.raw)))
            return await self.read_state()

    async def set_smart(self, preset: str, pump_percent: Optional[int] = None) -> FrostbayState:
        async with self._lock:
            current = await self.read_state()
            await self.write_state(build_smart(bytes(current.raw), preset, pump_percent))
            return await self.read_state()

    async def set_fixed(self, fan_percent: int, pump_percent: int) -> FrostbayState:
        async with self._lock:
            current = await self.read_state()
            await self.write_state(build_fixed(bytes(current.raw), fan_percent, pump_percent))
            return await self.read_state()

    async def set_pump(self, pump_percent: int) -> FrostbayState:
        async with self._lock:
            current = await self.read_state()
            await self.write_state(build_set_pump(bytes(current.raw), pump_percent))
            return await self.read_state()
