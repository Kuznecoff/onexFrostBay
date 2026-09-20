import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "frostbay-toolbox" / "textual_app.py"
SPEC = importlib.util.spec_from_file_location("frostbay_textual_app", MODULE_PATH)
toolbox = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(toolbox)


class ToolboxConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_timeout_waits_for_cancellation_cleanup(self):
        app = self.make_app()
        cleaned_up = []

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned_up.append(True)

        with self.assertRaisesRegex(TimeoutError, "BLE operation timed out"):
            app._run_async(operation(), timeout=0.01)

        self.assertEqual(cleaned_up, [True])

    def make_app(self):
        app = toolbox.FrostbayTextualApp(address="C8:17:17:F5:C8:93")
        app._bg_loop = Mock()

        def cleanup():
            app._set_ble_logging(False)
            app._ble_loop.call_soon_threadsafe(app._ble_loop.stop)
            app._ble_loop_thread.join(timeout=2)
            app._ble_loop.close()

        self.addCleanup(cleanup)
        return app

    async def test_app_uses_legacy_bleak_client(self):
        app = self.make_app()
        self.assertFalse(hasattr(app.ble, "_prefer_transport"))
        async with app.run_test():
            self.assertEqual(len(app.query("#legacy_041")), 0)


if __name__ == "__main__":
    unittest.main()
