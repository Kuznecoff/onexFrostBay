import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from dbus_fast import Message, Variant

from frostbay.ble import FrostbayBLE
from frostbay.protocol import UUID_FFE0, UUID_FFE1
from frostbay.transports import (
    BleakTransport,
    BluezDbusTransport,
    DEVICE_INTERFACE,
    GATT_CHARACTERISTIC_INTERFACE,
    PROPERTIES_INTERFACE,
    backend_order,
)


ADDRESS = "C8:17:17:F5:C8:93"
DEVICE_PATH = "/org/bluez/hci0/dev_C8_17_17_F5_C8_93"
CHAR_PATH = DEVICE_PATH + "/service003f/char0040"


class BluezTransportTests(unittest.IsolatedAsyncioTestCase):
    def make_transport(self):
        transport = BluezDbusTransport()
        bus = SimpleNamespace(connect=AsyncMock(), add_message_handler=Mock())
        transport._MessageBus = Mock(return_value=bus)
        transport._add_signal_match = AsyncMock()
        transport._get_prop = AsyncMock(return_value=True)
        transport._managed_objects = AsyncMock(return_value={
            DEVICE_PATH: {DEVICE_INTERFACE: {
                "Address": ADDRESS, "Connected": True, "ServicesResolved": True,
            }},
            CHAR_PATH: {GATT_CHARACTERISTIC_INTERFACE: {
                "UUID": UUID_FFE1, "Flags": ["read", "write"],
            }},
        })
        transport._call = AsyncMock()
        transport._connected = False
        transport._char_paths = {}
        transport._char_props = {}
        return transport

    async def test_attach_resolves_ffe1_without_reconnecting(self):
        transport = self.make_transport()

        await transport.connect(ADDRESS, timeout=1.0)

        self.assertTrue(transport.is_connected)
        self.assertTrue(transport.has_characteristic(UUID_FFE1))
        self.assertEqual(transport._char_paths[UUID_FFE1], CHAR_PATH)
        self.assertEqual(transport.characteristic_properties(UUID_FFE1), {"read", "write"})
        transport._call.assert_not_awaited()

    async def test_waits_for_characteristic_after_services_resolved(self):
        transport = self.make_transport()
        complete = transport._managed_objects.return_value
        partial = {DEVICE_PATH: complete[DEVICE_PATH]}
        transport._managed_objects.side_effect = [partial, partial, complete]

        await transport.connect(ADDRESS, timeout=1.0)

        self.assertTrue(transport.has_characteristic(UUID_FFE1))
        self.assertEqual(transport._managed_objects.await_count, 3)

    async def test_services_resolved_without_ffe1_is_not_success(self):
        transport = self.make_transport()
        del transport._managed_objects.return_value[CHAR_PATH]

        with self.assertRaisesRegex(RuntimeError, "ServicesResolved and FFE1"):
            await transport.connect(ADDRESS, timeout=0.01)

        self.assertFalse(transport.is_connected)

    async def test_prefers_adapter_with_ffe1(self):
        transport = self.make_transport()
        objects = transport._managed_objects.return_value
        other_device = DEVICE_PATH.replace("hci0", "hci1")
        other_char = CHAR_PATH.replace("hci0", "hci1")
        objects[other_device] = objects[DEVICE_PATH]
        objects[other_char] = objects.pop(CHAR_PATH)

        await transport.connect(ADDRESS, timeout=1.0)

        self.assertEqual(transport._device_path, other_device)
        self.assertEqual(transport._char_paths[UUID_FFE1], other_char)

    async def test_disconnect_during_discovery_is_reported(self):
        transport = self.make_transport()
        transport._get_prop.return_value = False

        with self.assertRaisesRegex(RuntimeError, "disconnected during GATT discovery"):
            await transport.connect(ADDRESS, timeout=1.0)

        self.assertFalse(transport.is_connected)

    async def test_device_signals_invalidate_connection_and_characteristics(self):
        for name in ("Connected", "ServicesResolved"):
            with self.subTest(property=name):
                transport = self.make_transport()
                await transport.connect(ADDRESS, timeout=1.0)
                message = Message.new_signal(
                    DEVICE_PATH, PROPERTIES_INTERFACE, "PropertiesChanged", "sa{sv}as",
                    [DEVICE_INTERFACE, {name: Variant("b", False)}, []],
                )

                transport._on_message(message)

                self.assertFalse(transport.is_connected)
                self.assertFalse(transport.has_characteristic(UUID_FFE1))

    async def test_property_error_is_not_treated_as_true(self):
        transport = BluezDbusTransport()
        request = Message(path=DEVICE_PATH, member="Get", serial=1)
        transport._call = AsyncMock(return_value=Message.new_error(
            request, "org.bluez.Error.Failed", "Service discovery failed",
        ))

        with self.assertRaisesRegex(RuntimeError, "org.bluez.Error.Failed"):
            await transport._get_prop(DEVICE_PATH, DEVICE_INTERFACE, "ServicesResolved")


class LegacyTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_uses_bleak_with_ffe0_discovery_and_ffe1_io(self):
        characteristic = SimpleNamespace(uuid=UUID_FFE1, properties=["read", "write"])
        client = SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(), is_connected=True,
            services=[SimpleNamespace(characteristics=[characteristic])],
            read_gatt_char=AsyncMock(return_value=b"state"), write_gatt_char=AsyncMock(),
        )
        with (
            patch("frostbay.transports.bluez_dbus_available", return_value=True),
            patch("bleak.BleakClient", return_value=client) as factory,
        ):
            self.assertEqual(backend_order("bleak"), ["bleak"])
            transport = BleakTransport()
            await transport.connect(ADDRESS, timeout=10.0)

        factory.assert_called_once_with(ADDRESS, services=[UUID_FFE0], timeout=10.0)
        self.assertTrue(transport.is_connected)
        self.assertEqual(await transport.read(UUID_FFE1), b"state")
        await transport.write(UUID_FFE1, b"command", response=True)
        client.read_gatt_char.assert_awaited_once_with(characteristic)
        client.write_gatt_char.assert_awaited_once_with(characteristic, b"command", response=True)
        await transport.disconnect()
        client.disconnect.assert_awaited_once()


class FrostbayConnectionTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self):
        client = FrostbayBLE(ADDRESS, prefer_transport="bluez")
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

        with patch("frostbay.ble.create_transport", return_value=transport):
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
            patch("frostbay.ble.create_transport", side_effect=[failed_transport, working_transport]),
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
            patch("frostbay.ble.create_transport", side_effect=[failed_transport, working_transport]),
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

        with patch("frostbay.ble.create_transport", return_value=transport):
            await client.connect(attempts=1)

        self.assertTrue(client.is_connected)
        transport.disconnect.assert_not_awaited()
        client.start_polling.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()