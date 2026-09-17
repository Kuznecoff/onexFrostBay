import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from textual.widgets import Switch


MODULE_PATH = Path(__file__).resolve().parents[1] / "frostbay-toolbox" / "textual_app.py"
SPEC = importlib.util.spec_from_file_location("frostbay_textual_app", MODULE_PATH)
toolbox = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(toolbox)


class ToolboxConnectionModeTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, legacy_041=False):
        app = toolbox.FrostbayTextualApp(address="C8:17:17:F5:C8:93", legacy_041=legacy_041)
        app._bg_loop = Mock()

        def cleanup():
            app._set_ble_logging(False)
            app._ble_loop.call_soon_threadsafe(app._ble_loop.stop)
            app._ble_loop_thread.join(timeout=2)
            app._ble_loop.close()

        self.addCleanup(cleanup)
        return app

    async def test_default_mode_uses_auto_transport(self):
        app = self.make_app()
        self.assertIsNone(app.ble._prefer_transport)
        async with app.run_test():
            self.assertFalse(app.query_one("#legacy_041", Switch).value)

    async def test_legacy_startup_uses_bleak(self):
        app = self.make_app(legacy_041=True)
        async with app.run_test():
            self.assertTrue(app.query_one("#legacy_041", Switch).value)
            self.assertEqual(app.ble._prefer_transport, "bleak")

    async def test_switch_disconnects_and_replaces_client_in_both_directions(self):
        app = self.make_app()
        async with app.run_test() as pilot:
            for enabled, backend in ((True, "bleak"), (False, None)):
                previous_client = app.ble
                previous_history = app.history
                app._was_running = True
                app.state = Mock()
                with patch.object(previous_client, "disconnect") as disconnect:
                    app.query_one("#legacy_041", Switch).value = enabled
                    await pilot.pause()
                    await app.workers.wait_for_complete()

                disconnect.assert_awaited_once()
                self.assertIsNot(app.ble, previous_client)
                self.assertEqual(app.ble._prefer_transport, backend)
                self.assertEqual(app.ble.address, app.address)
                self.assertEqual(app.legacy_041, enabled)
                self.assertIsNone(app.state)
                self.assertIsNot(app.history, previous_history)
                self.assertFalse(app._was_running)
                self.assertFalse(app.query_one("#legacy_041", Switch).disabled)

    async def test_legacy_launch_flag(self):
        with (
            patch("sys.argv", ["textual_app.py", "--legacy-041", "--address", "test-address"]),
            patch.object(toolbox, "FrostbayTextualApp") as app_class,
        ):
            self.assertEqual(toolbox.main(), 0)

        app_class.assert_called_once_with(address="test-address", legacy_041=True)
        app_class.return_value.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()