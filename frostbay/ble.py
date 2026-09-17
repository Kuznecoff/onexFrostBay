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
    from bleak import BleakScanner
except ImportError as exc:  # pragma: no cover - import guard
    raise RuntimeError(
        "bleak is required: pip install bleak"
    ) from exc

from .transports import Transport, backend_order, create_transport

from .protocol import (
    FrostbayState,
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

    def __init__(
        self,
        address: Optional[str] = None,
        on_state: Optional[Callable[[FrostbayState], None]] = None,
        prefer_transport: Optional[str] = None,
    ) -> None:
        self._address = address
        self._prefer_transport = prefer_transport
        self._transport: Optional[Transport] = None
        self._on_state = on_state
        self._lock = asyncio.Lock()
        self._connected = False
        # Whether FFE1 writes must use write-WITH-response. Detected from the
        # characteristic's advertised properties at connect time; some stacks
        # (macOS CoreBluetooth) silently drop WRITE_COMMAND to a char that only
        # declares WRITE, which made commands appear to do nothing.
        self._write_with_response = True
        self._notify_started = False
        # Strong references to in-flight notification-triggered refresh tasks,
        # so they are not garbage-collected before completion.
        self._notify_tasks: set[asyncio.Task] = set()
        self._poll_task: Optional[asyncio.Task] = None
        self._poll_interval = 2.0
        self._poll_stop: Optional[asyncio.Event] = None

    # --- status ---------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._connected and self._transport is not None and self._transport.is_connected

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
    async def connect(
        self,
        address: Optional[str] = None,
        timeout: float = 30.0,
        attempts: int = 3,
    ) -> None:
        addr = address or self._address
        if not addr:
            raise ValueError("No device address provided")
        self._address = addr

        last_exc: Optional[Exception] = None
        connected_ok = False
        for backend in backend_order(self._prefer_transport):
            for attempt in range(1, max(1, attempts) + 1):
                transport: Optional[Transport] = None
                try:
                    logger.info(
                        "Connecting to %s via %s (attempt %d/%d) ...",
                        addr, backend, attempt, attempts,
                    )
                    # On Linux the "bluez" backend attaches to an already
                    # connected + resolved device and drives FFE1 via direct
                    # D-Bus ReadValue/WriteValue, avoiding the fresh connect +
                    # full ATT discovery the Frostbay firmware can reject with
                    # an Unlikely Error (0x0E). "bleak" is the classic client.
                    transport = create_transport(backend)
                    await transport.connect(addr, timeout)
                    self._transport = transport
                    self._resolve_chars()
                    self._connected = True
                    await self.refresh_state()
                    if not self.is_connected:
                        raise RuntimeError("Device disconnected during initial state read")
                    try:
                        await self._start_notifications()
                    except Exception as exc:
                        logger.warning("Notification subscription failed: %s", exc)
                    if not self.is_connected:
                        raise RuntimeError("Device disconnected during notification setup")
                    last_exc = None
                    connected_ok = True
                    logger.info("Connected to Frostbay at %s (via %s)", addr, backend)
                    break
                except asyncio.CancelledError:
                    if transport is not None:
                        try:
                            await transport.disconnect()
                        except Exception as exc:
                            logger.debug("Cancelled connection cleanup failed: %s", exc)
                    self._transport = None
                    self._connected = False
                    self._notify_started = False
                    raise
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "Connect via %s attempt %d/%d failed: %s",
                        backend, attempt, attempts, exc,
                    )
                    # Tear down any half-open link so the stack does not keep a
                    # stale connection that blocks the next Connect call.
                    if transport is not None:
                        try:
                            await transport.disconnect()
                        except Exception:
                            pass
                    self._transport = None
                    self._connected = False
                    self._notify_started = False
                    if attempt < attempts:
                        await asyncio.sleep(0.6)
            if connected_ok:
                break

        if not connected_ok and last_exc is not None:
            raise last_exc

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
        if self._transport is None:
            self._connected = False
            return
        try:
            await self._stop_notifications_safe()
        except Exception:
            pass
        try:
            await self._transport.disconnect()
        except Exception as exc:
            logger.warning("Disconnect error: %s", exc)
        self._connected = False
        self._transport = None
        logger.info("Disconnected")

    def _resolve_chars(self) -> None:
        if self._transport is None:
            raise RuntimeError("Client not connected")
        if not self._transport.has_characteristic(UUID_FFE1):
            raise RuntimeError(f"Characteristic {UUID_FFE1} not found on the device")
        # Pick a write mode the characteristic actually advertises. If it only
        # supports WRITE (with response), sending WRITE_COMMAND is dropped by
        # CoreBluetooth and commands never reach the device.
        props = self._transport.characteristic_properties(UUID_FFE1)
        self._write_with_response = "write" in props and "write-without-response" not in props
        logger.info(
            "FFE1 properties=%s -> write with response=%s",
            sorted(props) if props else "?",
            self._write_with_response,
        )
        logger.debug(
            "Resolved FFE1=%s FFE4=%s",
            self._transport.has_characteristic(UUID_FFE1),
            self._transport.has_characteristic(UUID_FFE4),
        )

    # --- notifications --------------------------------------------------
    async def _start_notifications(self) -> None:
        if self._transport is None or not self._transport.has_characteristic(UUID_FFE4):
            return

        async def _safe_refresh() -> None:
            try:
                await self.refresh_state()
            except Exception as exc:
                logger.debug("Notification-triggered refresh failed: %s", exc)

        def _handler(char_uuid: str, data: bytearray) -> None:
            logger.debug("Notification from %s: %d bytes", char_uuid, len(data))
            # Notifications from FFE4 may indicate a state change; refresh FFE1.
            # Run in the background (the transport does not await handlers); keep
            # a reference so the task is not garbage-collected mid-flight and
            # its exceptions are always consumed by _safe_refresh.
            task = asyncio.create_task(_safe_refresh())
            self._notify_tasks.add(task)
            task.add_done_callback(self._notify_tasks.discard)

        try:
            await self._transport.start_notify(UUID_FFE4, _handler)
            self._notify_started = True
        except Exception as exc:
            logger.debug("Could not start notify on FFE4: %s", exc)
            self._notify_started = False

    async def _stop_notifications_safe(self) -> None:
        if self._notify_started and self._transport is not None:
            try:
                await self._transport.stop_notify(UUID_FFE4)
            except Exception:
                pass
        self._notify_started = False

    # --- core read / write ---------------------------------------------
    async def _read_state_locked(self) -> FrostbayState:
        """Read FFE1. Caller must hold ``self._lock``."""
        if self._transport is None or not self._transport.has_characteristic(UUID_FFE1):
            raise RuntimeError("Not connected or FFE1 not resolved")
        data = await self._transport.read(UUID_FFE1)
        logger.debug("Read FFE1: %d bytes: %s", len(data), data.hex())
        state = FrostbayState.from_raw(bytes(data))
        if self._on_state:
            try:
                self._on_state(state)
            except Exception as exc:
                logger.warning("on_state callback error: %s", exc)
        return state

    async def read_state(self) -> FrostbayState:
        # Serialize all GATT traffic: CoreBluetooth cannot handle two
        # concurrent reads of the same characteristic (TimeoutError /
        # KeyError from PeripheralDelegate). The poll loop, notification
        # handler and UI refresh all funnel through here.
        async with self._lock:
            return await self._read_state_locked()

    async def _write_state_locked(self, state: bytearray) -> None:
        """Write FFE1 in chunks. Caller must hold ``self._lock``."""
        if self._transport is None or not self._transport.has_characteristic(UUID_FFE1):
            raise RuntimeError("Not connected or FFE1 not resolved")
        chunks = encode_write_chunks(state)
        for idx, chunk in enumerate(chunks):
            logger.info(
                "Write chunk %d (%d bytes, response=%s): %s",
                idx + 1, len(chunk), self._write_with_response, chunk.hex(),
            )
            try:
                await self._transport.write(UUID_FFE1, chunk, self._write_with_response)
            except Exception as exc:
                # Fallback to the other write mode in case property detection
                # was wrong; re-raise if both fail.
                logger.warning(
                    "Write with response=%s failed (%s); retrying with the other mode",
                    self._write_with_response, exc,
                )
                await self._transport.write(UUID_FFE1, chunk, not self._write_with_response)
                # Stick with whichever mode actually worked for remaining chunks.
                self._write_with_response = not self._write_with_response
            if idx < len(chunks) - 1:
                await asyncio.sleep(WRITE_DELAY_SEC)
        await asyncio.sleep(READBACK_DELAY_SEC)

    async def write_state(self, state: bytearray) -> None:
        async with self._lock:
            await self._write_state_locked(state)

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
                if self.is_connected and self._transport is not None and self._transport.has_characteristic(UUID_FFE1):
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
            current = await self._read_state_locked()
            await self._write_state_locked(build_off(bytes(current.raw)))
            return await self._read_state_locked()

    async def set_smart(self, preset: str, pump_percent: Optional[int] = None) -> FrostbayState:
        async with self._lock:
            current = await self._read_state_locked()
            await self._write_state_locked(build_smart(bytes(current.raw), preset, pump_percent))
            return await self._read_state_locked()

    async def set_fixed(self, fan_percent: int, pump_percent: int) -> FrostbayState:
        async with self._lock:
            current = await self._read_state_locked()
            await self._write_state_locked(build_fixed(bytes(current.raw), fan_percent, pump_percent))
            return await self._read_state_locked()

    async def set_pump(self, pump_percent: int) -> FrostbayState:
        async with self._lock:
            current = await self._read_state_locked()
            await self._write_state_locked(build_set_pump(bytes(current.raw), pump_percent))
            return await self._read_state_locked()
