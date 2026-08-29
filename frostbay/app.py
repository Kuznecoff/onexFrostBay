"""Frostbay tray application.

A cross-platform (Windows / macOS / Fedora) system-tray app that:
  * connects to the Frostbay BLE device over FFE0/FFE1
  * shows connection status with a colored tray icon
    - green: connected
    - red:   disconnected / error
    - grey:  idle / scanning
  * reads current parameters (mode, fan, flow, pump, temperatures)
  * sends control commands: OFF, Smart Fan (silent/soft/strong), Fixed Fan, Pump speed

Uses:
  * bleak     for cross-platform BLE
  * pystray   for the system tray icon
  * Pillow    for rendering icons

Run with:
    python -m frostbay
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Optional

from .ble import FrostbayBLE, ScanResult, FROSTBAY_NAME_PART, is_frostbay_name
from .icons import IconState, render_icon
from .protocol import FrostbayState, Mode, SMART_CURVES

try:
    import pystray
    from PIL import Image as PILImage
    _HAS_PYSTRAY = True
except ImportError:  # pragma: no cover - optional for console-only mode
    pystray = None  # type: ignore
    PILImage = None  # type: ignore
    _HAS_PYSTRAY = False


logger = logging.getLogger("frostbay.app")


# --- helpers ----------------------------------------------------------------


def _icon_image(state: IconState) -> PILImage.Image:
    import io
    return PILImage.open(io.BytesIO(render_icon(state)))


def _format_state_text(s: Optional[FrostbayState]) -> str:
    if s is None:
        return "No state available."
    running = "yes" if s.is_running() else "no"
    return (
        f"Mode:      {s.mode.label}\n"
        f"Running:   {running}\n"
        f"Fan:       {s.fan_percent} %\n"
        f"Flow:      {s.flow_ml_min:.1f} mL/min\n"
        f"Pump:      {s.pump_percent} %\n"
        f"Temp in:   {s.temp_in_c} C\n"
        f"Temp out:  {s.temp_out_c} C\n"
        f"Protocol:  0x{s.protocol_version:02X}"
    )


# --- main application -------------------------------------------------------


class FrostbayTrayApp:
    def __init__(self, address: Optional[str] = None, auto_connect: bool = True) -> None:
        self._address = address
        self._auto_connect = auto_connect
        self._ble: Optional[FrostbayBLE] = None
        self._loop: asyncio.AbstractEventLoop
        self._thread: Optional[threading.Thread] = None
        self._icon: Optional[pystray.Icon] = None
        self._last_state: Optional[FrostbayState] = None
        self._scan_results: list[ScanResult] = []
        self._stop_event = asyncio.Event()

    # ---- asyncio loop runner ----
    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        self._ble = FrostbayBLE(address=self._address, on_state=self._on_state_cb)
        # Auto-discover and connect to the Frostbay device by name substring.
        if self._auto_connect:
            await self._auto_connect_by_name()
        # Keep the loop alive until stop is requested.
        try:
            await self._stop_event.wait()
        finally:
            await self._ble.disconnect()

    async def _auto_connect_by_name(self) -> None:
        """Find the device whose advertised name contains `ONEC1` and connect.

        Runs at startup. On failure the tray icon turns red/orange and the user
        can retry from the menu (Scan / Reconnect).
        """
        assert self._ble is not None
        self._update_icon(IconState.IDLE)
        try:
            found = await self._ble.find_and_connect(timeout=10.0)
            self._address = found.address
            logger.info("Auto-connected to %s (%s)", found.name, found.address)
            self._update_icon(IconState.CONNECTED)
        except Exception as exc:
            logger.error("Auto-connect failed: %s", exc)
            self._update_icon(IconState.ERROR)
        self._refresh_menu()

    def _on_state_cb(self, state: FrostbayState) -> None:
        self._last_state = state
        self._update_icon(IconState.CONNECTED)
        self._refresh_menu()

    # ---- icon / menu ----
    def _update_icon(self, state: IconState) -> None:
        if self._icon is None:
            return
        try:
            self._icon.icon = _icon_image(state)
            self._icon.title = f"Frostbay - {state.value}"
        except Exception as exc:
            logger.warning("Icon update failed: %s", exc)

    def _refresh_menu(self) -> None:
        if self._icon is None:
            return
        self._icon.menu = self._build_menu()
        try:
            self._icon.update_menu()
        except Exception:
            pass

    def _build_menu(self) -> pystray.Menu:
        connected = bool(self._ble and self._ble.is_connected)
        status = "Connected" if connected else ("Idle" if not self._scan_results else "Disconnected")
        info = _format_state_text(self._last_state if connected else None)

        items: list[pystray.MenuItem] = []
        items.append(pystray.MenuItem(f"Status: {status}", None, enabled=False))
        items.append(pystray.MenuItem(info, None, enabled=False))
        items.append(pystray.Menu.SEPARATOR)

        # Scan
        items.append(pystray.MenuItem("Scan for devices...", self._on_scan))
        # Connect / disconnect
        if connected:
            items.append(pystray.MenuItem("Disconnect", self._on_disconnect))
        else:
            label = f"Reconnect to {self._address}" if self._address else f"Find & connect (*{FROSTBAY_NAME_PART}*)"
            items.append(pystray.MenuItem(label, self._on_connect))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Refresh state", self._on_refresh))

        if connected:
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("Turn OFF", self._on_off))
            items.append(pystray.MenuItem("Smart: Silent", self._on_smart("silent")))
            items.append(pystray.MenuItem("Smart: Soft", self._on_smart("soft")))
            items.append(pystray.MenuItem("Smart: Strong", self._on_smart("strong")))
            items.append(pystray.MenuItem("Fixed Fan 50% / Pump 80%", self._on_fixed(50, 80)))
            items.append(pystray.MenuItem("Fixed Fan 100% / Pump 100%", self._on_fixed(100, 100)))
            items.append(pystray.MenuItem("Pump -> 80%", self._on_pump(80)))
            items.append(pystray.MenuItem("Pump -> 100%", self._on_pump(100)))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Exit", self._on_exit))
        return pystray.Menu(*items)

    # ---- menu callbacks (run in asyncio loop) ----
    def _schedule(self, coro):
        if self._loop is None or not self._loop.is_running():
            logger.warning("Event loop not running; cannot schedule action.")
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _do_scan(self) -> None:
        assert self._ble is not None
        self._update_icon(IconState.IDLE)
        try:
            results = await self._ble.scan(timeout=10.0)
            self._scan_results = results
            logger.info("Scan results: %s", [(r.name, r.address, r.is_frostbay) for r in results])
            # If a Frostbay device was found, show it in the status line.
            frost = [r for r in results if r.is_frostbay]
            if frost:
                logger.info("Frostbay devices: %s", [(r.name, r.address) for r in frost])
        except Exception as exc:
            logger.error("Scan failed: %s", exc)
            self._update_icon(IconState.ERROR)
        else:
            self._update_icon(IconState.DISCONNECTED)
        self._refresh_menu()

    async def _do_connect(self, address: Optional[str] = None) -> None:
        assert self._ble is not None
        self._update_icon(IconState.IDLE)
        try:
            if address:
                await self._ble.connect(address=address)
                self._address = address
            elif self._address:
                await self._ble.connect(address=self._address)
            else:
                # No known address: scan by name and connect to the first match.
                found = await self._ble.find_and_connect(timeout=10.0)
                self._address = found.address
            self._update_icon(IconState.CONNECTED)
        except Exception as exc:
            logger.error("Connect failed: %s", exc)
            self._update_icon(IconState.ERROR)
        self._refresh_menu()

    async def _do_disconnect(self) -> None:
        assert self._ble is not None
        await self._ble.disconnect()
        self._update_icon(IconState.DISCONNECTED)
        self._refresh_menu()

    async def _do_refresh(self) -> None:
        if self._ble is None or not self._ble.is_connected:
            return
        try:
            state = await self._ble.read_state()
            self._last_state = state
            self._update_icon(IconState.CONNECTED)
        except Exception as exc:
            logger.error("Refresh failed: %s", exc)
            self._update_icon(IconState.ERROR)
        self._refresh_menu()

    async def _do_off(self) -> None:
        await self._command(self._ble.set_off())

    async def _do_smart(self, preset: str) -> None:
        await self._command(self._ble.set_smart(preset))

    async def _do_fixed(self, fan: int, pump: int) -> None:
        await self._command(self._ble.set_fixed(fan, pump))

    async def _do_pump(self, pump: int) -> None:
        await self._command(self._ble.set_pump(pump))

    async def _command(self, coro) -> None:
        if self._ble is None or not self._ble.is_connected:
            logger.warning("Not connected; command ignored.")
            return
        try:
            await coro
            self._last_state = await self._ble.read_state()
            self._update_icon(IconState.CONNECTED)
        except Exception as exc:
            logger.error("Command failed: %s", exc)
            self._update_icon(IconState.ERROR)
        self._refresh_menu()

    # ---- pystray handlers ----
    def _on_scan(self, icon) -> None:
        self._schedule(self._do_scan())

    def _on_connect(self, icon, *args) -> None:
        # pystray may pass extra args for submenu items.
        self._schedule(self._do_connect(None))

    def _on_disconnect(self, icon) -> None:
        self._schedule(self._do_disconnect())

    def _on_refresh(self, icon) -> None:
        self._schedule(self._do_refresh())

    def _on_off(self, icon) -> None:
        self._schedule(self._do_off())

    def _on_smart(self, preset: str):
        def handler(icon) -> None:
            self._schedule(self._do_smart(preset))
        return handler

    def _on_fixed(self, fan: int, pump: int):
        def handler(icon) -> None:
            self._schedule(self._do_fixed(fan, pump))
        return handler

    def _on_pump(self, pump: int):
        def handler(icon) -> None:
            self._schedule(self._do_pump(pump))
        return handler

    def _on_exit(self, icon) -> None:
        async def _stop() -> None:
            if self._ble is not None:
                await self._ble.disconnect()
            self._stop_event.set()
        self._schedule(_stop())
        try:
            icon.stop()
        except Exception:
            pass

    # ---- public lifecycle ----
    def run(self) -> bool:
        """Start the app. Returns True if running in tray mode, False if declined."""
        if not _HAS_PYSTRAY:
            logger.warning("pystray/Pillow not installed; falling back to console mode.")
            return False
        # Start the asyncio loop in a background thread.
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # Create the tray icon on the main thread (required by pystray).
        self._icon = pystray.Icon(
            "frostbay",
            icon=_icon_image(IconState.DISCONNECTED),
            title="Frostbay - idle",
            menu=self._build_menu(),
        )
        logger.info("Starting Frostbay tray icon...")
        try:
            self._icon.run()
            return True
        except Exception as exc:
            logger.warning("Tray backend not available (%s); falling back to console mode.", exc)
            return False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Local import to avoid pulling console deps when tray is used.
    from .console import run_console

    address: Optional[str] = None
    auto_connect = True
    use_console = "--no-tray" in sys.argv
    if "--address" in sys.argv:
        idx = sys.argv.index("--address")
        if idx + 1 < len(sys.argv):
            address = sys.argv[idx + 1]
    if "--no-auto" in sys.argv:
        auto_connect = False
    if "--debug" in sys.argv:
        logging.getLogger().setLevel(logging.DEBUG)

    # Explicit console request.
    if use_console:
        return run_console(address=address)

    # Try tray; fall back to console if the tray backend is unavailable
    # (e.g. inside WSL without a StatusNotifierItem host).
    app = FrostbayTrayApp(address=address, auto_connect=auto_connect)
    if app.run():
        return 0
    logger.info("Switching to console controller. Use --no-tray to start here directly.")
    return run_console(address=address)


if __name__ == "__main__":
    raise SystemExit(main())
