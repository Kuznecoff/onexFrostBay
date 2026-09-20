"""Transport abstraction for Frostbay GATT access.

A single implementation is provided behind a small, uniform interface so the
higher-level :class:`frostbay.ble.FrostbayBLE` logic (chunking, delays,
locking, polling) does not care which stack is underneath:

* :class:`BleakTransport` -- wraps ``bleak`` (WinRT / CoreBluetooth / BlueZ).
  This is the legacy 0.4.1 protocol path: a fresh ``BleakClient`` connect
  scoped to the ``FFE0`` primary service. On Linux it prefers an already
  connected + services-resolved BlueZ device exposing ``FFE1``, preserving
  its adapter and avoiding an implicit scan.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from abc import ABC, abstractmethod
from typing import Callable, Optional

from .protocol import UUID_FFE0, UUID_FFE1

logger = logging.getLogger("frostbay.transport")

NotifyCallback = Callable[[str, bytearray], None]

# BlueZ D-Bus interface names (kept local so this module has no hard
# dependency on bleak's private defs module).
BLUEZ_SERVICE = "org.bluez"
DEVICE_INTERFACE = "org.bluez.Device1"
GATT_CHARACTERISTIC_INTERFACE = "org.bluez.GattCharacteristic1"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"


class Transport(ABC):
    """Uniform GATT transport interface used by :class:`FrostbayBLE`."""

    @abstractmethod
    async def connect(self, address: str, timeout: float) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def has_characteristic(self, uuid: str) -> bool: ...

    @abstractmethod
    def characteristic_properties(self, uuid: str) -> set[str]: ...

    @abstractmethod
    async def read(self, uuid: str) -> bytes: ...

    @abstractmethod
    async def write(self, uuid: str, data: bytes, response: bool) -> None: ...

    @abstractmethod
    async def start_notify(self, uuid: str, callback: NotifyCallback) -> None: ...

    @abstractmethod
    async def stop_notify(self, uuid: str) -> None: ...


# ---------------------------------------------------------------------------
# bleak transport (legacy 0.4.1 protocol path)
# ---------------------------------------------------------------------------

class BleakTransport(Transport):
    """Transport backed by ``bleak``.

    Mirrors the historical behaviour of :class:`FrostbayBLE`: a fresh
    ``BleakClient`` connect scoped to the ``FFE0`` primary service, with
    the characteristic objects resolved from the discovered service tree.
    On Linux, prefer an already resolved BlueZ device path to avoid an
    implicit scan and preserve the adapter that exposes FFE1.
    """

    def __init__(self) -> None:
        from bleak import BleakClient  # local import: keeps module import cheap

        self._client_factory = BleakClient
        self._client = None
        self._chars: dict[str, object] = {}
        self._connected = False

    # -- BlueZ ready-device lookup (Linux) -------------------------------
    @staticmethod
    async def _managed_objects(bus) -> dict:
        import dbus_fast

        reply = await bus.call(
            dbus_fast.Message(
                destination=BLUEZ_SERVICE,
                path="/",
                member="GetManagedObjects",
                interface=OBJECT_MANAGER_INTERFACE,
            )
        )
        unpack = dbus_fast.unpack_variants
        out: dict = {}
        for path, interfaces in reply.body[0].items():
            out[str(path)] = unpack(interfaces)
        return out

    @staticmethod
    def _find_device_path(address: str, objects: dict) -> Optional[str]:
        want = address.upper()
        candidates = []
        for path, props in objects.items():
            dev = props.get(DEVICE_INTERFACE)
            if dev and str(dev.get("Address", "")).upper() == want:
                candidates.append((path, dev))
        if not candidates:
            return None
        candidates.sort(
            key=lambda candidate: (
                bool(candidate[1].get("Connected"))
                and bool(candidate[1].get("ServicesResolved"))
                and any(
                    path.startswith(candidate[0] + "/")
                    and str(interfaces.get(GATT_CHARACTERISTIC_INTERFACE, {}).get("UUID", "")).lower()
                    == UUID_FFE1.lower()
                    for path, interfaces in objects.items()
                ),
                bool(candidate[1].get("Connected")),
                bool(candidate[1].get("ServicesResolved")),
            ),
            reverse=True,
        )
        return candidates[0][0]

    @staticmethod
    def _exposes_ffe1(device_path: str, objects: dict) -> bool:
        prefix = device_path + "/"
        for path, props in objects.items():
            if not path.startswith(prefix):
                continue
            char = props.get(GATT_CHARACTERISTIC_INTERFACE)
            if char and str(char.get("UUID", "")).lower() == UUID_FFE1.lower():
                return True
        return False

    async def _ready_bluez_device(self, address: str):
        """Return a ``BLEDevice`` for an already connected + resolved device.

        Avoids an implicit scan and preserves the adapter that exposes
        ``FFE1``. Returns ``None`` when no ready session exists.
        """
        import dbus_fast
        from bleak.backends.device import BLEDevice
        from dbus_fast.aio import MessageBus

        bus = MessageBus(bus_type=dbus_fast.BusType.SYSTEM)
        try:
            await bus.connect()
            objects = await self._managed_objects(bus)
            path = self._find_device_path(address, objects)
            if path is None:
                return None
            props = objects[path][DEVICE_INTERFACE]
            if not (
                props.get("Connected")
                and props.get("ServicesResolved")
                and self._exposes_ffe1(path, objects)
            ):
                return None
            logger.info("Bleak using resolved BlueZ device %s", path)
            return BLEDevice(address, props.get("Name"), {"path": path, "props": props})
        finally:
            bus.disconnect()

    async def connect(self, address: str, timeout: float) -> None:
        device = address
        if sys.platform == "linux":
            try:
                device = await asyncio.wait_for(
                    self._ready_bluez_device(address), timeout=min(5.0, timeout),
                ) or address
            except Exception as exc:
                logger.debug("Ready BlueZ device lookup failed; using Bleak scan: %s", exc)
        self._client = self._client_factory(device, services=[UUID_FFE0], timeout=timeout)
        await self._client.connect()
        self._chars = {}
        if not self._client.is_connected:
            raise RuntimeError("Bleak disconnected during GATT discovery")
        for service in self._client.services:
            for char in service.characteristics:
                self._chars[char.uuid.lower()] = char
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self._chars = {}
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as exc:  # noqa: BLE001 - best effort teardown
                logger.debug("BleakTransport disconnect error: %s", exc)

    @property
    def is_connected(self) -> bool:
        return (
            self._connected
            and self._client is not None
            and getattr(self._client, "is_connected", False)
        )

    def has_characteristic(self, uuid: str) -> bool:
        return uuid.lower() in self._chars

    def characteristic_properties(self, uuid: str) -> set[str]:
        char = self._chars.get(uuid.lower())
        if char is None:
            return set()
        try:
            return set(char.properties or [])
        except Exception:  # noqa: BLE001
            return set()

    async def read(self, uuid: str) -> bytes:
        char = self._chars[uuid.lower()]
        data = await self._client.read_gatt_char(char)
        return bytes(data)

    async def write(self, uuid: str, data: bytes, response: bool) -> None:
        char = self._chars[uuid.lower()]
        await self._client.write_gatt_char(char, data, response=response)

    async def start_notify(self, uuid: str, callback: NotifyCallback) -> None:
        char = self._chars[uuid.lower()]

        def _bridge(sender, data: bytearray) -> None:
            callback(uuid, bytearray(data))

        await self._client.start_notify(char, _bridge)

    async def stop_notify(self, uuid: str) -> None:
        char = self._chars.get(uuid.lower())
        if char is not None:
            await self._client.stop_notify(char)
