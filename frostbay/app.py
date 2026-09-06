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
import time
from typing import Optional

from .ble import FrostbayBLE, ScanResult, FROSTBAY_NAME_PART, is_frostbay_name
from .history import History
from .host import get_host_stats, format_cpu, format_gpu
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

# When the pump is in FIXED (manual) mode and reports it has stopped, auto-restart
# re-applies the fixed settings to wake it back up. This works regardless of the
# set pump value -- any stop at any setting triggers a restart attempt.

# Minimum seconds between auto-restart attempts. The polling callback fires every
# ~2s, so this cooldown stops us from hammering the device on every tick while a
# stopped pump is reported repeatedly.
AUTO_RESTART_COOLDOWN_SEC = 10.0


# --- helpers ----------------------------------------------------------------


def _icon_image(state: IconState) -> PILImage.Image:
    import io
    return PILImage.open(io.BytesIO(render_icon(state)))


def _spark(values, lo=None, hi=None, width=20) -> str:
    bars = "▁▂▃▄▅▆▇█"
    if not values:
        return " " * width
    vals = list(values)[-width:]
    if lo is None:
        lo = min(vals)
    if hi is None:
        hi = max(vals)
    if hi <= lo:
        hi = lo + 1.0
    out = []
    for v in vals:
        frac = max(0.0, min(1.0, (v - lo) / (hi - lo)))
        out.append(bars[min(len(bars) - 1, int(frac * len(bars)))])
    pad = width - len(out)
    if pad > 0:
        out = [" "] * pad + out
    return "".join(out[-width:])


# Native menus on Windows and Linux (GTK/AppIndicator) do not wrap item text, so
# long status lines make the menu overflow the screen there. macOS renders tray
# menu titles fine on one line, so it keeps the compact single-line layout.
_IS_WINDOWS = sys.platform == "win32"
_NEEDS_SHORT_MENU_LINES = _IS_WINDOWS or sys.platform.startswith("linux")


def _format_state_lines(s: Optional[FrostbayState], history: Optional[History] = None) -> list[str]:
    """Status text (no sparklines) for the tray menu, as one or more short lines.

    Windows/Linux native menus cannot wrap item text and strip embedded newlines,
    so a single long line makes the whole context menu wider than the screen
    there. On those platforms we split the state into several compact menu lines;
    macOS keeps the original single-line layout.
    """
    if s is None:
        return ["No state available."]
    running = "Running" if s.is_running() else "Stopped"
    if not _NEEDS_SHORT_MENU_LINES:
        temp_in = f"In {s.temp_in_c}°" if s.temp_in_c else ""
        temp_out = f"Out {s.temp_out_c}°" if s.temp_out_c else ""
        parts = [
            s.mode.label,
            running,
            f"Fan {s.fan_percent}%",
            f"{s.flow_ml_min:.0f}",
            f"Pump {s.pump_percent}%",
        ]
        if temp_in:
            parts.append(temp_in)
        if temp_out:
            parts.append(temp_out)
        return ["  ·  ".join(parts)]
    # Windows: keep every line short enough to fit the menu on screen.
    lines = [f"{s.mode.label} - {running}"]
    lines.append(f"Fan {s.fan_percent}%  Pump {s.pump_percent}%  Flow {s.flow_ml_min:.0f}")
    temps = []
    if s.temp_in_c:
        temps.append(f"In {s.temp_in_c}°")
    if s.temp_out_c:
        temps.append(f"Out {s.temp_out_c}°")
    if temps:
        lines.append("  ".join(temps))
    return lines


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
        self._history = History()
        self._scan_results: list[ScanResult] = []
        self._stop_event = asyncio.Event()
        # Monotonic timestamp of the last auto-restart attempt (see _maybe_auto_restart).
        # Start at 0 so the first detected stop always triggers an immediate restart.
        self._last_auto_restart_ts = 0.0
        # Auto-restart is opt-in via a menu checkbox (see _on_toggle_auto_restart).
        self._auto_restart_enabled = True

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
        self._history.push(state)
        self._update_icon(IconState.CONNECTED)
        self._refresh_menu()
        # Auto-restart: only when the user has enabled it via the menu checkbox, we
        # are in FIXED (manual) mode and the device reports it has stopped. Re-apply
        # the fixed settings to wake the pump back up. Runs inside the event loop
        # (polling callback), so schedule directly; cooldown prevents spamming every
        # poll tick. Works regardless of the chosen fan/pump values.
        if self._auto_restart_enabled and self._should_auto_restart(state):
            self._maybe_auto_restart()

    @staticmethod
    def _should_auto_restart(state: FrostbayState) -> bool:
        """True when the pump is in FIXED (manual) mode and currently stopped.

        Works regardless of the set fan/pump values -- any stop at any setting
        triggers a restart attempt (when enabled via the menu checkbox).
        """
        return state.mode == Mode.FIXED and not state.is_running()

    def _on_toggle_auto_restart(self, icon) -> None:
        """Toggle the auto-restart checkbox in the tray menu."""
        self._auto_restart_enabled = not self._auto_restart_enabled
        logger.info("Auto-restart %s", "enabled" if self._auto_restart_enabled else "disabled")
        self._refresh_menu()

    def _maybe_auto_restart(self) -> None:
        """Re-apply fixed settings if the cooldown has elapsed since last attempt."""
        now = time.monotonic()
        if now - self._last_auto_restart_ts < AUTO_RESTART_COOLDOWN_SEC:
            return
        self._last_auto_restart_ts = now
        state = self._last_state
        if state is None:
            return
        logger.info(
            "Pump stopped in manual mode; re-applying fixed settings to wake it.",
        )
        # Schedule inside the loop; _do_fixed sends set_fixed(fan, pump).
        asyncio.create_task(self._do_fixed(state.fan_percent, state.pump_percent))

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
        info_lines = _format_state_lines(self._last_state if connected else None, self._history if connected else None)

        items: list[pystray.MenuItem] = []
        items.append(pystray.MenuItem(f"Status: {status}", None, enabled=False))
        # One menu item per status line: on Windows each line is kept short so the
        # native menu never grows wider than the screen.
        for line in info_lines:
            items.append(pystray.MenuItem(line, None, enabled=False))

        # Host CPU / GPU telemetry as their own lines so they render reliably in the
        # tray menu (a single multiline item can be truncated by some native menus).
        host = get_host_stats()
        cpu_line = f"CPU:       {format_cpu(host)}"
        gpu_line = f"GPU:       {format_gpu(host)}"
        items.append(pystray.MenuItem(cpu_line, None, enabled=False))
        items.append(pystray.MenuItem(gpu_line, None, enabled=False))

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
            items.append(pystray.MenuItem("Live dashboard...", self._on_dashboard))

        if connected:
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("Turn OFF", self._on_off))
            items.append(pystray.MenuItem("Smart: Silent", self._on_smart("silent")))
            items.append(pystray.MenuItem("Smart: Soft", self._on_smart("soft")))
            items.append(pystray.MenuItem("Smart: Strong", self._on_smart("strong")))

            # Manual fixed settings via a nested dropdown menu. Selecting a fan or
            # pump value applies both parameters at once through _on_fixed(x, y):
            # the chosen value is paired with the current value of the other param.
            items.append(pystray.Menu.SEPARATOR)
            # Auto-restart checkbox: when checked, the app re-applies fixed settings
            # whenever a stopped pump is detected in manual mode. Toggled via handler.
            items.append(pystray.MenuItem(
                "Auto-restart on stop",
                self._on_toggle_auto_restart,
                checked=lambda _: self._auto_restart_enabled,
            ))

            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem(
                "Manual settings",
                pystray.Menu(
                    pystray.MenuItem("Fan speed", self._build_speed_menu("fan")),
                    pystray.MenuItem("Pump speed", self._build_speed_menu("pump")),
                ),
            ))

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

    async def _do_dashboard(self) -> None:
        if self._ble is None or not self._ble.is_connected:
            logger.warning("Not connected; dashboard unavailable.")
            return
        from .dashboard import run_dashboard
        try:
            await self._ble.start_polling(interval=1.0)
            await run_dashboard(self._history, poll_interval=1.0, use_curses=True)
        except Exception as exc:
            logger.error("Dashboard failed: %s", exc)
        finally:
            try:
                await self._ble.stop_polling()
            except Exception:
                pass

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

    def _on_dashboard(self, icon) -> None:
        self._schedule(self._do_dashboard())

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

    # ---- manual fixed settings (nested dropdown menu) ----
    @staticmethod
    def _speed_choices() -> list[int]:
        """Percentage steps offered in the fan/pump speed submenus."""
        return list(range(0, 110, 10))

    def _build_speed_menu(self, kind: str) -> pystray.Menu:
        """Build a submenu of percentage options for "fan" or "pump".

        Each option applies both parameters at once via ``_on_fixed(x, y)``: the
        selected value is paired with the current value of the *other* parameter
        (read from the last known state), so only one thing changes.
        """
        if kind not in ("fan", "pump"):
            raise ValueError(f"unknown speed kind: {kind!r}")

        current = self._last_state
        other_value = current.fan_percent if current else 0
        pump_value = current.pump_percent if current else 0

        def make_handler(value: int):
            def handler(icon) -> None:
                fan = value if kind == "fan" else other_value
                pump = value if kind == "pump" else pump_value
                self._schedule(self._do_fixed(fan, pump))
            return handler

        # Show a checkmark (✓) next to the option matching the current value.
        def is_current(value: int) -> bool:
            return current is not None and getattr(current, f"{kind}_percent") == value

        items: list[pystray.MenuItem] = []
        for value in self._speed_choices():
            label = f"{value}%" if not is_current(value) else f"{value}% ✓"
            items.append(pystray.MenuItem(label, make_handler(value)))
        return pystray.Menu(*items)

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
