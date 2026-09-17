"""Transport abstraction for Frostbay GATT access.

Two implementations are provided behind a small, uniform interface so the
higher-level :class:`frostbay.ble.FrostbayBLE` logic (chunking, delays,
locking, polling) does not care which stack is underneath:

* :class:`BleakTransport` -- wraps ``bleak`` (WinRT / CoreBluetooth / BlueZ).
  This is the default on Windows and macOS and the fallback on Linux.

* :class:`BluezDbusTransport` -- talks to BlueZ directly over D-Bus and
  *attaches* to an already connected + services-resolved device object,
  then performs direct ``ReadValue`` / ``WriteValue`` on the resolved
  ``FFE1`` characteristic. This is the model recommended by
  ``specification.md`` for Linux: treat BlueZ as the transport owner and
  avoid forcing a second user-space GATT client to run a fresh connect +
  full service discovery, which the Frostbay firmware can reject with an
  ATT ``Unlikely Error`` (0x0E) and drop the link.

The BlueZ transport is only instantiated on Linux and only when the
``dbus-fast`` library (a bleak dependency on Linux) is importable.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from abc import ABC, abstractmethod
from typing import Callable, Optional

from .protocol import UUID_FFE0, UUID_FFE1, UUID_FFE4

logger = logging.getLogger("frostbay.transport")

NotifyCallback = Callable[[str, bytearray], None]

# BlueZ D-Bus interface names (kept local so this module has no hard
# dependency on bleak's private defs module).
BLUEZ_SERVICE = "org.bluez"
DEVICE_INTERFACE = "org.bluez.Device1"
ADAPTER_INTERFACE = "org.bluez.Adapter1"
GATT_SERVICE_INTERFACE = "org.bluez.GattService1"
GATT_CHARACTERISTIC_INTERFACE = "org.bluez.GattCharacteristic1"
OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
BLUEZ_ERROR_IN_PROGRESS = "org.bluez.Error.InProgress"


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
# bleak transport (Windows / macOS / Linux fallback)
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

    async def _ready_bluez_device(self, address: str):
        from bleak.backends.device import BLEDevice

        transport = BluezDbusTransport()
        bus = transport._MessageBus(bus_type=transport._dbus.BusType.SYSTEM)
        transport._bus = bus
        try:
            await bus.connect()
            objects = await transport._managed_objects()
            path = await transport._find_device_path(address, objects)
            if path is None:
                return None
            props = objects[path][DEVICE_INTERFACE]
            transport._resolve_chars(path, objects)
            if not (
                props.get("Connected") and props.get("ServicesResolved")
                and transport.has_characteristic(UUID_FFE1)
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


# ---------------------------------------------------------------------------
# BlueZ direct D-Bus transport (Linux)
# ---------------------------------------------------------------------------

class BluezDbusTransport(Transport):
    """Attach to an already resolved BlueZ device and drive ``FFE1`` directly.

    Connect strategy (per ``specification.md`` "Linux BlueZ Execution Model"):

    1. Locate the ``org.bluez.Device1`` object for the target address.
    2. If it is already ``Connected`` but not ``ServicesResolved`` (a stale
       half-open link), issue ``Disconnect`` and wait for it to clear so we
       do not inherit a broken session.
    3. If it is not connected, issue ``Connect``.
    4. Wait for ``ServicesResolved`` to become true.
    5. Resolve the ``FFE1`` / ``FFE4`` characteristic object paths from the
       managed object tree and perform direct ``ReadValue`` / ``WriteValue``.

    When the device is already connected + resolved (e.g. the desktop
    environment owns the session), step 3 is skipped entirely: we attach
    without triggering a fresh connect + full ATT discovery, which is the
    sequence the Frostbay firmware can reject with ATT 0x0E.
    """

    def __init__(self) -> None:
        # Imported lazily so this module imports fine on non-Linux hosts.
        from dbus_fast.aio import MessageBus  # noqa: F401
        import dbus_fast  # noqa: F401

        self._MessageBus = MessageBus
        self._dbus = dbus_fast
        self._bus = None
        self._device_path: Optional[str] = None
        self._char_paths: dict[str, str] = {}
        self._char_props: dict[str, set[str]] = {}
        self._notify_callbacks: dict[str, NotifyCallback] = {}
        self._connected = False
        self._handler = None

    # -- low level D-Bus helpers -----------------------------------------
    def _msg(self, **kwargs):
        return self._dbus.Message(**kwargs)

    async def _call(self, message) -> "object":
        reply = await self._bus.call(message)
        return reply

    async def _get_prop(self, path: str, interface: str, name: str):
        reply = await self._call(
            self._msg(
                destination=BLUEZ_SERVICE,
                path=path,
                interface=PROPERTIES_INTERFACE,
                member="Get",
                signature="ss",
                body=[interface, name],
            )
        )
        if reply.message_type == self._dbus.MessageType.ERROR:
            raise RuntimeError(f"BlueZ {name}: {reply.error_name}: {reply.body}")
        value = reply.body[0]
        # dbus_fast wraps typed values in Variant for "v" signatures.
        return getattr(value, "value", value)

    async def _managed_objects(self) -> dict:
        reply = await self._call(
            self._msg(
                destination=BLUEZ_SERVICE,
                path="/",
                member="GetManagedObjects",
                interface=OBJECT_MANAGER_INTERFACE,
            )
        )
        unpack = self._dbus.unpack_variants
        out: dict = {}
        for path, interfaces in reply.body[0].items():
            out[str(path)] = unpack(interfaces)
        return out

    async def _default_adapter_path(self, objects: dict) -> Optional[str]:
        for path, props in objects.items():
            if ADAPTER_INTERFACE in props and props[ADAPTER_INTERFACE].get("Powered"):
                return path
        # Fall back to the first adapter we can see.
        for path, props in objects.items():
            if ADAPTER_INTERFACE in props:
                return path
        return None

    async def _find_device_path(self, address: str, objects: dict) -> Optional[str]:
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

    def _resolve_chars(self, device_path: str, objects: dict) -> None:
        self._char_paths = {}
        self._char_props = {}
        prefix = device_path + "/"
        for path, props in objects.items():
            if not path.startswith(prefix):
                continue
            char = props.get(GATT_CHARACTERISTIC_INTERFACE)
            if not char:
                continue
            uuid = str(char.get("UUID", "")).lower()
            if uuid in (UUID_FFE1.lower(), UUID_FFE4.lower()):
                self._char_paths[uuid] = path
                try:
                    self._char_props[uuid] = set(char.get("Flags", []) or [])
                except Exception:  # noqa: BLE001
                    self._char_props[uuid] = set()

    async def _wait_services_resolved(self, device_path: str, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            connected = await self._get_prop(device_path, DEVICE_INTERFACE, "Connected")
            if not connected:
                raise RuntimeError(f"BlueZ disconnected during GATT discovery on {device_path}")
            resolved = await self._get_prop(device_path, DEVICE_INTERFACE, "ServicesResolved")
            if resolved:
                objects = await self._managed_objects()
                self._resolve_chars(device_path, objects)
                if self.has_characteristic(UUID_FFE1):
                    return True
            await asyncio.sleep(0.1)
        return False

    # -- signal handling for notifications -------------------------------
    def _on_message(self, message) -> None:
        MessageType = self._dbus.MessageType
        if message.message_type != MessageType.SIGNAL:
            return
        if message.interface != PROPERTIES_INTERFACE or message.member != "PropertiesChanged":
            return
        try:
            interface_name, changed, _invalidated = message.body
        except (ValueError, TypeError):
            return
        if interface_name == DEVICE_INTERFACE and message.path == self._device_path:
            unpacked = self._dbus.unpack_variants(dict(changed))
            if (
                unpacked.get("Connected") is False
                or unpacked.get("ServicesResolved") is False
                or "Connected" in _invalidated
                or "ServicesResolved" in _invalidated
            ):
                self._connected = False
                self._char_paths.clear()
                self._char_props.clear()
                self._notify_callbacks.clear()
                logger.warning("BlueZ connection or GATT services lost on %s", self._device_path)
            return
        if interface_name != GATT_CHARACTERISTIC_INTERFACE:
            return
        unpacked = self._dbus.unpack_variants(dict(changed))
        if "Value" not in unpacked:
            return
        cb = self._notify_callbacks.get(str(message.path))
        if cb is not None:
            try:
                cb(str(message.path), bytearray(unpacked["Value"]))
            except Exception as exc:  # noqa: BLE001 - never break the bus loop
                logger.debug("notify callback error: %s", exc)

    async def _add_signal_match(self) -> None:
        rule = (
            "type='signal',"
            "interface='org.freedesktop.DBus.Properties',"
            "member='PropertiesChanged',"
            "path_namespace='/org/bluez'"
        )
        await self._call(
            self._msg(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[rule],
            )
        )

    # -- Transport interface ---------------------------------------------
    async def connect(self, address: str, timeout: float) -> None:
        BusType = self._dbus.BusType
        self._bus = self._MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True)
        await self._bus.connect()
        self._handler = self._on_message
        self._bus.add_message_handler(self._handler)
        await self._add_signal_match()

        objects = await self._managed_objects()
        device_path = await self._find_device_path(address, objects)
        if device_path is None:
            adapter = await self._default_adapter_path(objects)
            if adapter is None:
                raise RuntimeError("No powered BlueZ adapter found")
            device_path = f"{adapter}/dev_{address.upper().replace(':', '_')}"
        self._device_path = device_path

        dev = (objects.get(device_path, {}) or {}).get(DEVICE_INTERFACE, {})
        connected = bool(dev.get("Connected"))
        resolved = bool(dev.get("ServicesResolved"))

        # Clear a stale half-open link so we never inherit a broken session.
        if connected and not resolved:
            logger.info("BlueZ device half-open (connected, not resolved); disconnecting first")
            try:
                await self._call(
                    self._msg(
                        destination=BLUEZ_SERVICE,
                        path=device_path,
                        interface=DEVICE_INTERFACE,
                        member="Disconnect",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Disconnect during cleanup: %s", exc)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + min(5.0, timeout)
            while loop.time() < deadline:
                try:
                    still = await self._get_prop(device_path, DEVICE_INTERFACE, "Connected")
                except Exception:  # noqa: BLE001 - object gone => disconnected
                    break
                if not still:
                    break
                await asyncio.sleep(0.1)
            connected = False

        if not (connected and resolved):
            logger.info("Issuing BlueZ Connect to %s", device_path)
            reply = await self._call(
                self._msg(
                    destination=BLUEZ_SERVICE,
                    path=device_path,
                    interface=DEVICE_INTERFACE,
                    member="Connect",
                )
            )
            MessageType = self._dbus.MessageType
            if reply.message_type == MessageType.ERROR:
                # A racing "already connected" is fine; anything else is fatal.
                err = (reply.error_name or "").lower()
                if "already" not in err:
                    raise RuntimeError(
                        f"BlueZ Connect failed: {reply.error_name} "
                        f"{reply.body[0] if reply.body else ''}".strip()
                    )

        if not await self._wait_services_resolved(device_path, timeout):
            raise RuntimeError(
                "Timed out waiting for BlueZ ServicesResolved and FFE1 on "
                f"{device_path}. BlueZ did not expose a usable Frostbay GATT tree; "
                "check the adapter / device cache (see README.md)."
            )

        self._connected = True
        logger.info("Attached to BlueZ device %s (FFE1 resolved)", device_path)

    async def disconnect(self) -> None:
        self._connected = False
        if self._bus is None:
            return
        try:
            if self._device_path is not None:
                await self._call(
                    self._msg(
                        destination=BLUEZ_SERVICE,
                        path=self._device_path,
                        interface=DEVICE_INTERFACE,
                        member="Disconnect",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("BlueZ Disconnect error: %s", exc)
        finally:
            try:
                if self._handler is not None and hasattr(self._bus, "remove_message_handler"):
                    self._bus.remove_message_handler(self._handler)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._bus.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._bus = None
            self._char_paths = {}
            self._char_props = {}
            self._notify_callbacks = {}

    @property
    def is_connected(self) -> bool:
        return self._connected and self._bus is not None

    def has_characteristic(self, uuid: str) -> bool:
        return uuid.lower() in self._char_paths

    def characteristic_properties(self, uuid: str) -> set[str]:
        return set(self._char_props.get(uuid.lower(), set()))

    async def read(self, uuid: str) -> bytes:
        path = self._char_paths[uuid.lower()]
        while True:
            reply = await self._call(
                self._msg(
                    destination=BLUEZ_SERVICE,
                    path=path,
                    interface=GATT_CHARACTERISTIC_INTERFACE,
                    member="ReadValue",
                    signature="a{sv}",
                    body=[{}],
                )
            )
            if reply.error_name == BLUEZ_ERROR_IN_PROGRESS:
                await asyncio.sleep(0.01)
                continue
            if reply.message_type == self._dbus.MessageType.ERROR:
                raise RuntimeError(f"ReadValue failed: {reply.error_name}")
            return bytes(reply.body[0])

    async def write(self, uuid: str, data: bytes, response: bool) -> None:
        path = self._char_paths[uuid.lower()]
        Variant = self._dbus.Variant
        while True:
            reply = await self._call(
                self._msg(
                    destination=BLUEZ_SERVICE,
                    path=path,
                    interface=GATT_CHARACTERISTIC_INTERFACE,
                    member="WriteValue",
                    signature="aya{sv}",
                    body=[bytes(data), {"type": Variant("s", "request" if response else "command")}],
                )
            )
            if reply.error_name == BLUEZ_ERROR_IN_PROGRESS:
                await asyncio.sleep(0.01)
                continue
            if reply.message_type == self._dbus.MessageType.ERROR:
                raise RuntimeError(f"WriteValue failed: {reply.error_name}")
            return

    async def start_notify(self, uuid: str, callback: NotifyCallback) -> None:
        path = self._char_paths[uuid.lower()]
        self._notify_callbacks[path] = callback
        reply = await self._call(
            self._msg(
                destination=BLUEZ_SERVICE,
                path=path,
                interface=GATT_CHARACTERISTIC_INTERFACE,
                member="StartNotify",
            )
        )
        if reply.message_type == self._dbus.MessageType.ERROR:
            self._notify_callbacks.pop(path, None)
            raise RuntimeError(f"StartNotify failed: {reply.error_name}")

    async def stop_notify(self, uuid: str) -> None:
        path = self._char_paths.get(uuid.lower())
        if path is None:
            return
        self._notify_callbacks.pop(path, None)
        try:
            await self._call(
                self._msg(
                    destination=BLUEZ_SERVICE,
                    path=path,
                    interface=GATT_CHARACTERISTIC_INTERFACE,
                    member="StopNotify",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("StopNotify error: %s", exc)


def bluez_dbus_available() -> bool:
    """True when we are on Linux and ``dbus-fast`` can be imported."""
    if sys.platform != "linux":
        return False
    try:
        import dbus_fast  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def backend_order(prefer: Optional[str] = None) -> list[str]:
    """Ordered backend names to try for a connect.

    ``"bleak"`` / ``"bluez"`` force a single backend. In auto mode the
    direct BlueZ D-Bus transport is tried first on Linux (it can attach to an
    already-resolved device and avoid the fresh-connect ATT ``0x0E`` drop),
    with the bleak transport as a fallback; elsewhere bleak only.
    """
    if prefer == "bleak":
        return ["bleak"]
    if prefer == "bluez":
        return ["bluez"]
    if bluez_dbus_available():
        return ["bluez", "bleak"]
    return ["bleak"]


def create_transport(prefer: Optional[str] = None) -> Transport:
    """Build a transport by backend name (``"bleak"`` or ``"bluez"``)."""
    if prefer == "bleak":
        return BleakTransport()
    if prefer == "bluez":
        return BluezDbusTransport()
    raise ValueError(f"unknown transport backend: {prefer!r}")
