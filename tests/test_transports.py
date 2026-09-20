import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from frostbay.ble import FrostbayBLE
from frostbay.protocol import UUID_FFE0, UUID_FFE1
from frostbay.transports import (
    BleakTransport,
    DEVICE_INTERFACE,
    GATT_CHARACTERISTIC_INTERFACE,
)


ADDRESS = "C8:17:17:F5:C8:93"
DEVICE_PATH = "/org/bluez/hci0/dev_C8_17_17_F5_C8_93"
CHAR_PATH = DEVICE_PATH + "/service003f/char0040"


def base_objects():
    return {
        DEVICE_PATH: {DEVICE_INTERFACE: {
            "Address": ADDRESS, "Connected": True, "ServicesResolved": True,
        }},
        CHAR_PATH: {GATT_CHARACTERISTIC_INTERFACE: {
            "UUID": UUID_FFE1, "Flags": ["read", "write"],
        }},
    }


def make_bus(objects):
    return SimpleNamespace(
        connect=AsyncMock(),
        disconnect=Mock(),
        call=AsyncMock(return_value=SimpleNamespace(body=[objects])),
    )


class LegacyTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_linux_passes_ready_device_to_bleak_without_scanning(self):
        device = SimpleNamespace(address=ADDRESS)
        client = SimpleNamespace(connect=AsyncMock(), is_connected=True, services=[])
        with (
            patch("frostbay.transports.sys.platform", "linux"),
            patch("bleak.BleakClient", return_value=client) as factory,
            patch.object(BleakTransport, "_ready_bluez_device", return_value=device) as lookup,
        ):
            await BleakTransport().connect(ADDRESS, timeout=10.0)

        lookup.assert_awaited_once_with(ADDRESS)
        factory.assert_called_once_with(device, services=[UUID_FFE0], timeout=10.0)
        client.connect.assert_awaited_once_with()

    async def test_linux_falls_back_to_address_without_ready_session(self):
        for lookup_result in (None, RuntimeError("D-Bus unavailable")):
            with self.subTest(result=lookup_result):
                client = SimpleNamespace(connect=AsyncMock(), is_connected=True, services=[])
                with (
                    patch("frostbay.transports.sys.platform", "linux"),
                    patch("bleak.BleakClient", return_value=client) as factory,
                    patch.object(BleakTransport, "_ready_bluez_device", side_effect=[lookup_result]),
                ):
                    await BleakTransport().connect(ADDRESS, timeout=10.0)

                factory.assert_called_once_with(ADDRESS, services=[UUID_FFE0], timeout=10.0)

    async def test_ready_lookup_prefers_resolved_adapter_and_only_closes_bus(self):
        objects = base_objects()
        other_device = DEVICE_PATH.replace("hci0", "hci1")
        other_char = CHAR_PATH.replace("hci0", "hci1")
        objects[other_device] = objects[DEVICE_PATH]
        objects[other_char] = objects.pop(CHAR_PATH)
        bus = make_bus(objects)
        with patch("dbus_fast.aio.MessageBus", return_value=bus):
            device = await BleakTransport()._ready_bluez_device(ADDRESS)

        self.assertEqual(device.details["path"], other_device)
        bus.disconnect.assert_called_once()
        self.assertEqual(bus.call.await_count, 1)

    async def test_ready_lookup_rejects_incomplete_sessions(self):
        for missing in ("device", "Connected", "ServicesResolved", "FFE1"):
            with self.subTest(missing=missing):
                objects = base_objects()
                if missing == "device":
                    objects.clear()
                elif missing == "FFE1":
                    del objects[CHAR_PATH]
                else:
                    objects[DEVICE_PATH][DEVICE_INTERFACE][missing] = False
                bus = make_bus(objects)
                with patch("dbus_fast.aio.MessageBus", return_value=bus):
                    self.assertIsNone(await BleakTransport()._ready_bluez_device(ADDRESS))

                bus.disconnect.assert_called_once()
                self.assertEqual(bus.call.await_count, 1)

    async def test_disconnected_client_is_not_queried_for_services(self):
        client = SimpleNamespace(connect=AsyncMock(), is_connected=False)
        with (
            patch("frostbay.transports.sys.platform", "win32"),
            patch("bleak.BleakClient", return_value=client),
        ):
            transport = BleakTransport()
            with self.assertRaisesRegex(RuntimeError, "disconnected during GATT discovery"):
                await transport.connect(ADDRESS, timeout=10.0)

        self.assertFalse(transport.is_connected)

    async def test_legacy_uses_bleak_with_ffe0_discovery_and_ffe1_io(self):
        characteristic = SimpleNamespace(uuid=UUID_FFE1, properties=["read", "write"])
        client = SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(), is_connected=True,
            services=[SimpleNamespace(characteristics=[characteristic])],
            read_gatt_char=AsyncMock(return_value=b"state"), write_gatt_char=AsyncMock(),
        )
        with (
            patch("frostbay.transports.sys.platform", "win32"),
            patch.object(BleakTransport, "_ready_bluez_device") as lookup,
            patch("bleak.BleakClient", return_value=client) as factory,
        ):
            transport = BleakTransport()
            await transport.connect(ADDRESS, timeout=10.0)

        factory.assert_called_once_with(ADDRESS, services=[UUID_FFE0], timeout=10.0)
        lookup.assert_not_awaited()
        self.assertTrue(transport.is_connected)
        self.assertEqual(await transport.read(UUID_FFE1), b"state")
        await transport.write(UUID_FFE1, b"command", response=True)
        client.read_gatt_char.assert_awaited_once_with(characteristic)
        client.write_gatt_char.assert_awaited_once_with(characteristic, b"command", response=True)
        await transport.disconnect()
        client.disconnect.assert_awaited_once()


class FrostbayConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_connection_cleans_up_without_retry(self):
        for stage in ("connect", "read"):
            with self.subTest(stage=stage):
                client = self.make_client()
                transport = self.make_transport()
                operation = transport.connect if stage == "connect" else client.refresh_state
                operation.side_effect = asyncio.CancelledError()
                with patch("frostbay.ble.BleakTransport", return_value=transport) as factory:
                    with self.assertRaises(asyncio.CancelledError):
                        await client.connect()

                factory.assert_called_once()
                transport.disconnect.assert_awaited_once()
                self.assertFalse(client.is_connected)
                self.assertIsNone(client._transport)
                client.start_polling.assert_not_awaited()

    def make_client(self):
        client = FrostbayBLE(ADDRESS)
        client.refresh_state = AsyncMock()
        client._start_notifications = AsyncMock()
        client.start_polling = AsyncMock()
        return client

    def make_transport(self):
        return SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(), is_connected=True,
            has_characteristic=Mock(return_value=True),
            characteristic_properties=Mock(return_value={"read", "write"}),
        )

    async def test_failed_initial_read_does_not_report_connected(self):
        client = self.make_client()
        transport = self.make_transport()
        client.refresh_state.side_effect = RuntimeError("GATT Unlikely Error")

        with patch("frostbay.ble.BleakTransport", return_value=transport):
            with self.assertRaisesRegex(RuntimeError, "GATT Unlikely Error"):
                await client.connect(attempts=1)

        self.assertFalse(client.is_connected)
        transport.disconnect.assert_awaited_once()
        client._start_notifications.assert_not_awaited()
        client.start_polling.assert_not_awaited()

    async def test_initial_read_failure_is_retried(self):
        client = self.make_client()
        failed_transport = self.make_transport()
        working_transport = self.make_transport()
        client.refresh_state.side_effect = [RuntimeError("GATT Unlikely Error"), None]

        with (
            patch("frostbay.ble.BleakTransport", side_effect=[failed_transport, working_transport]),
            patch("frostbay.ble.asyncio.sleep", new_callable=AsyncMock),
        ):
            await client.connect(attempts=2)

        self.assertTrue(client.is_connected)
        self.assertIs(client._transport, working_transport)
        failed_transport.disconnect.assert_awaited_once()
        working_transport.disconnect.assert_not_awaited()
        client._start_notifications.assert_awaited_once()
        client.start_polling.assert_awaited_once()

    async def test_disconnect_during_notification_setup_is_retried(self):
        client = self.make_client()
        failed_transport = self.make_transport()
        working_transport = self.make_transport()

        async def start_notifications():
            if client._transport is failed_transport:
                failed_transport.is_connected = False
                raise RuntimeError("BlueZ connection lost")

        client._start_notifications.side_effect = start_notifications
        with (
            patch("frostbay.ble.BleakTransport", side_effect=[failed_transport, working_transport]),
            patch("frostbay.ble.asyncio.sleep", new_callable=AsyncMock),
        ):
            await client.connect(attempts=2)

        self.assertTrue(client.is_connected)
        self.assertIs(client._transport, working_transport)
        failed_transport.disconnect.assert_awaited_once()
        self.assertEqual(client._start_notifications.await_count, 2)
        client.start_polling.assert_awaited_once()

    async def test_notification_error_with_live_connection_is_optional(self):
        client = self.make_client()
        transport = self.make_transport()
        client._start_notifications.side_effect = RuntimeError("Notify unsupported")

        with patch("frostbay.ble.BleakTransport", return_value=transport):
            await client.connect(attempts=1)

        self.assertTrue(client.is_connected)
        transport.disconnect.assert_not_awaited()
        client.start_polling.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
