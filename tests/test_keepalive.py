"""Keep-alive regression tests.

Live devices stop the pump a few seconds after the last FFE1 *write* while a
BLE central stays connected (firmware watchdog for a dead controller app), so
the client must keep re-issuing the last active state blob.
"""

import asyncio
import unittest
from unittest.mock import patch

from frostbay.ble import FrostbayBLE
from frostbay.protocol import (
    CHUNK_PREFIX_3,
    FrostbayState,
    Mode,
    STATE_LEN,
    UUID_FFE1,
)
from frostbay.transports import Transport


ADDRESS = "C8:17:17:F5:C8:93"


def state_bytes(mode: Mode, fan: int = 50, pump: int = 80, flow: float = 0.0,
                activity: int = 0x00) -> bytearray:
    raw = bytearray(STATE_LEN)
    raw[2] = 0x02
    raw[4] = mode.value
    raw[5] = fan
    flow_word = int(flow * 10)
    raw[6] = (flow_word >> 8) & 0xFF
    raw[7] = flow_word & 0xFF
    raw[8] = pump
    raw[12] = activity
    return raw


class FakeTransport(Transport):
    """In-memory stand-in for the GATT link: records whole write batches."""

    def __init__(self, state: bytearray) -> None:
        self.state = bytearray(state)
        self._connected = True
        self.batches = 0
        self.chunks: list[bytes] = []

    async def connect(self, address: str, timeout: float) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def has_characteristic(self, uuid: str) -> bool:
        return uuid == UUID_FFE1

    def characteristic_properties(self, uuid: str) -> set[str]:
        return {"write"}

    async def read(self, uuid: str) -> bytes:
        return bytes(self.state)

    async def write(self, uuid: str, data: bytes, response: bool) -> None:
        self.chunks.append(bytes(data))
        if data[0] == CHUNK_PREFIX_3:
            self.batches += 1

    async def start_notify(self, uuid: str, callback) -> None:
        pass

    async def stop_notify(self, uuid: str) -> None:
        pass


class KeepaliveTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, mode: Mode = Mode.OFF) -> tuple[FrostbayBLE, FakeTransport]:
        client = FrostbayBLE(address=ADDRESS)
        transport = FakeTransport(state_bytes(mode))
        client._transport = transport
        client._connected = True
        client._write_with_response = True
        self.addCleanup(self._cleanup, client)
        return client, transport

    @staticmethod
    async def _cleanup(client: FrostbayBLE) -> None:
        await client.stop_keepalive()
        await client.stop_polling()

    async def _run_keepalive(self, client: FrostbayBLE, seconds: float) -> None:
        await client.start_keepalive(interval=0.1)
        await asyncio.sleep(seconds)
        await client.stop_keepalive()

    async def test_active_mode_is_re_issued(self):
        client, transport = self.make_client()
        with patch("frostbay.ble.WRITE_DELAY_SEC", 0.0), \
                patch("frostbay.ble.READBACK_DELAY_SEC", 0.0), \
                patch("frostbay.ble.KEEPALIVE_TICK_SEC", 0.05):
            await client.set_fixed(50, 80)
            self.assertEqual(transport.batches, 1)
            await self._run_keepalive(client, 0.8)
        self.assertGreaterEqual(transport.batches, 4,
                                "keep-alive must re-write the active mode repeatedly")
        adopted = client._last_active_state
        self.assertIsNotNone(adopted)
        self.assertEqual(adopted[4], Mode.FIXED.value)

    async def test_off_state_is_not_kept_alive(self):
        client, transport = self.make_client()
        with patch("frostbay.ble.WRITE_DELAY_SEC", 0.0), \
                patch("frostbay.ble.READBACK_DELAY_SEC", 0.0), \
                patch("frostbay.ble.KEEPALIVE_TICK_SEC", 0.05):
            await client.set_fixed(50, 80)
            await client.set_off()
            self.assertIsNone(client._last_active_state)
            before = transport.batches
            await self._run_keepalive(client, 0.6)
        self.assertEqual(transport.batches, before,
                        "an OFF device must not be re-written")

    async def test_disabled_keepalive_stays_silent(self):
        client, transport = self.make_client()
        client.keepalive_enabled = False
        with patch("frostbay.ble.WRITE_DELAY_SEC", 0.0), \
                patch("frostbay.ble.READBACK_DELAY_SEC", 0.0), \
                patch("frostbay.ble.KEEPALIVE_TICK_SEC", 0.05):
            await client.set_fixed(50, 80)
            before = transport.batches
            await self._run_keepalive(client, 0.6)
        self.assertEqual(transport.batches, before)

    async def test_connect_adopts_a_mode_the_device_already_runs(self):
        # A pump left running by a previous session (or a host crash) has no
        # keep-alive armed; connecting must take its mode over.
        client, _ = self.make_client(Mode.FIXED)
        state = await client.read_state()
        client._adopt_active_state(state)
        adopted = client._last_active_state
        self.assertIsNotNone(adopted)
        self.assertEqual(adopted[4], Mode.FIXED.value)
        self.assertEqual(client._last_write_ts, 0.0)

    async def test_connect_does_not_adopt_off(self):
        client, _ = self.make_client(Mode.OFF)
        state = await client.read_state()
        client._adopt_active_state(state)
        self.assertIsNone(client._last_active_state)


class RunningDetectionTests(unittest.TestCase):
    """state[12] stays 0 on live firmware; the flow word is the fallback."""

    def test_running_via_flow_when_activity_byte_is_zero(self):
        state = FrostbayState.from_raw(bytes(state_bytes(Mode.FIXED, flow=43.2)))
        self.assertFalse(state.activity_running)
        self.assertTrue(state.is_running())

    def test_stopped_when_no_activity_and_no_flow(self):
        state = FrostbayState.from_raw(bytes(state_bytes(Mode.FIXED)))
        self.assertFalse(state.is_running())

    def test_running_via_activity_byte(self):
        state = FrostbayState.from_raw(bytes(state_bytes(Mode.SMART, activity=0x01)))
        self.assertTrue(state.is_running())


if __name__ == "__main__":
    unittest.main()
