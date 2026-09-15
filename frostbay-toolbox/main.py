#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import curses
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from frostbay.ble import FrostbayBLE, FROSTBAY_NAME_PART
from frostbay.history import History
from frostbay.host import get_host_stats, format_cpu, format_gpu
from frostbay.protocol import FrostbayState, Mode, SMART_CURVES

# Mirrors the tray app's auto-restart cooldown: minimum seconds between
# re-applying the current mode's settings when a stopped pump is detected.
AUTO_RESTART_COOLDOWN_SEC = 1.0

# Grace window after any active-mode command (user click or auto-restart) during
# which auto-restart stays silent, so the pump can spin up undisturbed.
STARTUP_GRACE_SEC = 10.0

# Thermal auto-control thresholds (host CPU/GPU temperature in degrees C).
# Above THERMAL_ON_C the pump is started (Smart Silent) when stopped; below
# THERMAL_OFF_C a running pump is turned OFF. The band between is a hysteresis
# zone where nothing is sent.
THERMAL_ON_C = 50.0
THERMAL_OFF_C = 45.0

# Manual fan/pump percentage pairs offered in the tray's "Manual settings"
# submenu. Each applies both parameters at once via set_fixed(fan, pump).
MANUAL_PRESETS: tuple[tuple[int, int], ...] = (
    (20, 60), (20, 70), (20, 80),
    (30, 60), (30, 70), (30, 80),
    (40, 70), (40, 80), (40, 90),
)


class TerminalFrostbayApp:
    def __init__(self, address: Optional[str] = None) -> None:
        self.address = address
        self.ble = FrostbayBLE(address=address)
        self.history = History()
        self.state: Optional[FrostbayState] = None
        self.devices: list[str] = []
        self.status = "Ready"
        self.log: deque[str] = deque(maxlen=12)
        self.cursor = 0
        self.menu_rects: dict[int, tuple[int, int, int, int]] = {}
        self.menu_scroll = 0
        self._menu_visible = 1

        # Auto-restart on stop (tray checkbox). When enabled and the device is
        # in an active mode (Smart/Fixed) but reports stopped, the current mode's
        # settings are re-applied to wake the pump back up.
        self.auto_restart_enabled = True
        self._last_auto_restart_ts = 0.0
        # Monotonic timestamp of the last active-mode command (user or auto).
        # Drives the startup grace window in _maybe_auto_restart.
        self._last_user_cmd_ts = 0.0
        # Latched True once the pump has been observed running since the last
        # command; auto-restart only fires on a running->stopped transition.
        self._was_running = False
        # Thermal auto-control (tray "Auto temp" checkbox). When enabled the app
        # watches host CPU/GPU temperature and starts/stops the pump accordingly.
        self.thermal_enabled = False

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

    def _run_async(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=25)

    def _log(self, message: str) -> None:
        self.log.append(message)

    def _note_command(self) -> None:
        """Arm the startup grace window and clear the running latch.

        Called on every user-initiated cooling command so auto-restart stays out
        of the way while the pump spins up, and only restarts on a real
        running->stopped transition afterwards.
        """
        self._last_user_cmd_ts = time.monotonic()
        self._was_running = False

    def _refresh_state(self) -> None:
        if not self.ble.is_connected:
            return
        try:
            state = self._run_async(self.ble.read_state())
            self.state = state
            if state.is_running():
                self._was_running = True
            self.history.push(state)
            self.status = "Connected"
        except Exception as exc:
            self.status = f"Read error: {exc}"
            self._log(f"State read failed: {exc}")

    def _action_scan(self) -> None:
        try:
            results = self._run_async(self.ble.scan(timeout=8.0))
            self.devices = [
                f"{('★ ' if r.is_frostbay else '  ')}{r.name or '<unnamed>'} {r.address} RSSI={r.rssi}"
                for r in results
            ]
            self._log(f"Found {len(results)} devices")
            self.status = "Scan finished"
        except Exception as exc:
            self._log(f"Scan failed: {exc}")
            self.status = f"Scan failed: {exc}"

    def _action_find_and_connect(self) -> None:
        try:
            found = self._run_async(self.ble.find_and_connect(timeout=10.0))
            self.address = found.address
            self.devices = []
            self.state = self.history.last() if self.history.last() is not None else None
            self._log(f"Connected to {found.name} ({found.address})")
            self.status = "Connected"
        except Exception as exc:
            self._log(f"Auto-connect failed: {exc}")
            self.status = f"Auto-connect failed: {exc}"

    def _action_connect_by_address(self, addr: str) -> None:
        if not addr:
            self._log("Empty address")
            return
        try:
            self._run_async(self.ble.connect(address=addr))
            self.address = addr
            self.devices = []
            self._log(f"Connected to {addr}")
            self.status = "Connected"
        except Exception as exc:
            self._log(f"Connect failed: {exc}")
            self.status = f"Connect failed: {exc}"

    def _action_disconnect(self) -> None:
        try:
            self._run_async(self.ble.disconnect())
            self.state = None
            self.status = "Disconnected"
            self._log("Disconnected")
        except Exception as exc:
            self._log(f"Disconnect failed: {exc}")

    def _action_refresh(self) -> None:
        if not self.ble.is_connected:
            self._log("Not connected")
            return
        try:
            self.state = self._run_async(self.ble.read_state())
            self.history.push(self.state)
            self._log("State refreshed")
            self.status = "State refreshed"
        except Exception as exc:
            self._log(f"Refresh failed: {exc}")
            self.status = f"Refresh failed: {exc}"

    def _action_off(self) -> None:
        self._note_command()
        if not self.ble.is_connected:
            self._log("Not connected")
            return
        try:
            self.state = self._run_async(self.ble.set_off())
            self.history.push(self.state)
            self._log("Device OFF")
            self.status = "OFF"
        except Exception as exc:
            self._log(f"OFF failed: {exc}")
            self.status = f"OFF failed: {exc}"

    def _action_smart(self, preset: str) -> None:
        self._note_command()
        if not self.ble.is_connected:
            self._log("Not connected")
            return
        try:
            self.state = self._run_async(self.ble.set_smart(preset))
            self.history.push(self.state)
            self._log(f"Smart {preset}")
            self.status = f"Smart {preset}"
        except Exception as exc:
            self._log(f"Smart {preset} failed: {exc}")
            self.status = f"Smart {preset} failed: {exc}"

    def _action_fixed(self, fan: int, pump: int) -> None:
        self._note_command()
        if not self.ble.is_connected:
            self._log("Not connected")
            return
        try:
            self.state = self._run_async(self.ble.set_fixed(fan, pump))
            self.history.push(self.state)
            self._log(f"Fixed {fan}% / {pump}%")
            self.status = f"Fixed {fan}% / {pump}%"
        except Exception as exc:
            self._log(f"Fixed mode failed: {exc}")
            self.status = f"Fixed mode failed: {exc}"

    def _action_set_pump(self, value: int) -> None:
        self._note_command()
        if not self.ble.is_connected:
            self._log("Not connected")
            return
        try:
            self.state = self._run_async(self.ble.set_pump(value))
            self.history.push(self.state)
            self._log(f"Pump set to {value}%")
            self.status = f"Pump {value}%"
        except Exception as exc:
            self._log(f"Pump change failed: {exc}")
            self.status = f"Pump change failed: {exc}"

    # --- auto-restart / thermal auto-control (ported from the tray app) ---
    @staticmethod
    def _smart_preset_for(state: FrostbayState) -> str:
        """Return the smart preset whose curve matches the device's current one."""
        for preset, curve in SMART_CURVES.items():
            if state.smart_curve == curve:
                return preset
        return "silent"

    @staticmethod
    def _host_max_temp() -> Optional[float]:
        """Max of host CPU/GPU temperature in C, or None when no sensor."""
        host = get_host_stats()
        temps = [t for t in (host.cpu_temp_c, host.gpu_temp_c) if t is not None]
        return max(temps) if temps else None

    def _toggle_auto_restart(self) -> None:
        self.auto_restart_enabled = not self.auto_restart_enabled
        self._log(f"Auto-restart {'enabled' if self.auto_restart_enabled else 'disabled'}")

    def _toggle_thermal(self) -> None:
        self.thermal_enabled = not self.thermal_enabled
        self._log(
            f"Auto temp {'enabled' if self.thermal_enabled else 'disabled'} "
            f"(start >{THERMAL_ON_C:.0f}C, stop <{THERMAL_OFF_C:.0f}C)"
        )

    def _maybe_auto_restart(self) -> None:
        """Re-apply the current mode's settings if the pump stopped in an active mode.

        Skipped while thermal auto-control is on (it does its own temperature-gated
        start-on-stop handling) and while the cooldown has not elapsed.
        """
        if not self.auto_restart_enabled or self.thermal_enabled:
            return
        if not self.ble.is_connected:
            return
        state = self.state
        if state is None or state.mode == Mode.OFF or state.is_running():
            return
        if not self._was_running:
            return
        now = time.monotonic()
        if now - self._last_user_cmd_ts < STARTUP_GRACE_SEC:
            return
        if now - self._last_auto_restart_ts < AUTO_RESTART_COOLDOWN_SEC:
            return
        try:
            if state.mode == Mode.SMART:
                self.state = self._run_async(self.ble.set_smart(self._smart_preset_for(state)))
            else:
                self.state = self._run_async(self.ble.set_fixed(state.fan_percent, state.pump_percent))
            self.history.push(self.state)
            self._log(f"Auto-restart: re-applied {state.mode.label}")
            self.status = "Auto-restarted"
        except Exception as exc:
            self._log(f"Auto-restart failed: {exc}")
        finally:
            self._last_auto_restart_ts = time.monotonic()
            self._last_user_cmd_ts = time.monotonic()
            self._was_running = False

    def _thermal_tick(self) -> None:
        """One thermal check: start Smart Silent above THERMAL_ON_C, OFF below THERMAL_OFF_C."""
        if not self.thermal_enabled or not self.ble.is_connected:
            return
        temp = self._host_max_temp()
        if temp is None:
            return
        try:
            if temp > THERMAL_ON_C:
                state = self._run_async(self.ble.read_state())
                self.state = state
                self.history.push(state)
                if not state.is_running():
                    self.state = self._run_async(self.ble.set_smart("silent"))
                    self.history.push(self.state)
                    self._log(f"Thermal: {temp:.1f}C > {THERMAL_ON_C:.0f}C -> Smart Silent")
                    self.status = "Thermal ON"
            elif temp < THERMAL_OFF_C:
                state = self.state if self.state is not None else self._run_async(self.ble.read_state())
                if state.is_running():
                    self.state = self._run_async(self.ble.set_off())
                    self.history.push(self.state)
                    self._log(f"Thermal: {temp:.1f}C < {THERMAL_OFF_C:.0f}C -> OFF")
                    self.status = "Thermal OFF"
        except Exception as exc:
            self._log(f"Thermal control failed: {exc}")

    @staticmethod
    def _sparkline(values, width: int = 24, lo: Optional[float] = None, hi: Optional[float] = None) -> str:
        bars = "▁▂▃▄▅▆▇█"
        if not values:
            return " " * width
        vals = values[-width:]
        if lo is None:
            lo = min(vals)
        if hi is None:
            hi = max(vals)
        if hi <= lo:
            hi = lo + 1.0
        span = hi - lo
        out = []
        for v in vals:
            frac = max(0.0, min(1.0, (v - lo) / span))
            idx = min(len(bars) - 1, int(frac * len(bars)))
            out.append(bars[idx])
        pad = width - len(out)
        if pad > 0:
            out = [" "] * pad + out
        return "".join(out[-width:])

    def _render_status_columns(self, stdscr) -> None:
        h, w = stdscr.getmaxyx()
        if h < 20:
            return

        state = self.state
        x = 42
        if state is None:
            stdscr.addstr(4, x, "No live telemetry")
        else:
            rows = [
                ("Mode", state.mode.label),
                ("Running", "YES" if state.is_running() else "NO"),
                ("Fan", f"{state.fan_percent}%"),
                ("Pump", f"{state.pump_percent}%"),
                ("Flow", f"{state.flow_ml_min:.1f} mL/min"),
                ("Temp in", f"{state.temp_in_c}C"),
                ("Temp out", f"{state.temp_out_c}C"),
                ("Protocol", f"0x{state.protocol_version:02X}"),
            ]
            y = 4
            stdscr.addstr(y - 1, x, "Status", curses.A_BOLD)
            for label, value in rows:
                if y >= h - 2:
                    break
                stdscr.addstr(y, x, f"{label:<12}{value}")
                y += 1

        host = get_host_stats()
        hy = 13
        if hy + 2 < h - 1:
            stdscr.addstr(hy, x, "Host", curses.A_BOLD)
            stdscr.addstr(hy + 1, x, f"{'CPU':<12}{format_cpu(host)}")
            stdscr.addstr(hy + 2, x, f"{'GPU':<12}{format_gpu(host)}")

    def _render_dashboard(self, stdscr) -> None:
        h, w = stdscr.getmaxyx()
        x = 42
        y = 18
        stdscr.addstr(y - 1, x, "Live dashboard", curses.A_BOLD)

        metrics = [
            ("Temp IN", self.history.series("temp_in"), 0, 60),
            ("Temp OUT", self.history.series("temp_out"), 0, 60),
            ("Flow", self.history.series("flow"), 0, 250),
            ("Fan", self.history.series("fan"), 0, 100),
            ("Pump", self.history.series("pump"), 0, 100),
        ]

        for idx, (label, values, lo, hi) in enumerate(metrics):
            row_y = y + idx * 3
            if row_y + 2 >= h:
                break
            spark = self._sparkline(values, width=24, lo=lo, hi=hi)
            stdscr.addstr(row_y, x, f"{label:<9}{spark}")
            stdscr.addstr(row_y + 1, x, " " * 9 + ("-" * 24))

    def _prompt(self, stdscr, label: str) -> str:
        curses.echo()
        curses.curs_set(1)
        max_y, max_x = stdscr.getmaxyx()
        y = max_y - 3
        x = 2
        stdscr.addstr(y, x, label)
        stdscr.clrtoeol()
        stdscr.refresh()
        try:
            text = stdscr.getstr(y, x + len(label) + 1, 32).decode("utf-8", "replace").strip()
        finally:
            curses.noecho()
            curses.curs_set(0)
        return text

    def _get_menu_items(self) -> list[tuple[str, str]]:
        """Return the menu as (label, action_id) pairs.

        Mirrors every actionable control from the tray menu: scan / connect /
        disconnect / refresh / OFF / smart presets / auto-restart toggle /
        auto-temp toggle / manual fan-pump presets / exit.
        """
        items: list[tuple[str, str]] = [
            ("Scan for devices", "scan"),
            (f"Find & connect (*{FROSTBAY_NAME_PART}*)", "find_connect"),
            ("Connect by address", "connect_addr"),
            ("Disconnect", "disconnect"),
            ("Refresh state", "refresh"),
            ("Turn OFF", "off"),
            ("Smart: Silent", "smart:silent"),
            ("Smart: Soft", "smart:soft"),
            ("Smart: Strong", "smart:strong"),
            (f"Auto-restart on stop [{'x' if self.auto_restart_enabled else ' '}]", "toggle_auto_restart"),
            (f"Auto temp >{THERMAL_ON_C:.0f}C / <{THERMAL_OFF_C:.0f}C [{'x' if self.thermal_enabled else ' '}]", "toggle_thermal"),
            ("Fixed Fan/Pump", "fixed"),
            ("Set Pump %", "set_pump"),
        ]
        cur = self.state
        for fan, pump in MANUAL_PRESETS:
            check = " ✓" if cur is not None and cur.fan_percent == fan and cur.pump_percent == pump else ""
            items.append((f"Manual Fan/Pump: {fan}-{pump}{check}", f"preset:{fan}:{pump}"))
        items.append(("Exit", "exit"))
        return items

    def _invoke_action(self, index: int, stdscr=None) -> None:
        items = self._get_menu_items()
        if index < 0 or index >= len(items):
            return
        _, action = items[index]
        if action == "scan":
            self._action_scan()
        elif action == "find_connect":
            self._action_find_and_connect()
        elif action == "connect_addr":
            addr = self._prompt(stdscr, "Address: ") if stdscr is not None else ""
            self._action_connect_by_address(addr)
        elif action == "disconnect":
            self._action_disconnect()
        elif action == "refresh":
            self._action_refresh()
        elif action == "off":
            self._action_off()
        elif action.startswith("smart:"):
            self._action_smart(action.split(":", 1)[1])
        elif action == "toggle_auto_restart":
            self._toggle_auto_restart()
        elif action == "toggle_thermal":
            self._toggle_thermal()
        elif action == "fixed":
            if stdscr is not None:
                fan = self._prompt(stdscr, "Fan %: ")
                pump = self._prompt(stdscr, "Pump %: ")
                try:
                    self._action_fixed(int(fan), int(pump))
                except ValueError:
                    self._log("Fan/Pump must be integers")
        elif action == "set_pump":
            if stdscr is not None:
                value = self._prompt(stdscr, "Pump %: ")
                try:
                    self._action_set_pump(int(value))
                except ValueError:
                    self._log("Pump value must be an integer")
        elif action.startswith("preset:"):
            _, fan, pump = action.split(":")
            self._action_fixed(int(fan), int(pump))
        elif action == "exit":
            raise KeyboardInterrupt

    def _render_menu(self, stdscr) -> None:
        h, w = stdscr.getmaxyx()
        items = self._get_menu_items()
        menu_x = 2
        menu_y = 4
        # Leave a gap above the log block (log starts at h-6).
        max_rows = max(1, (h - 8) - menu_y)
        self._menu_visible = max_rows

        if self.cursor < self.menu_scroll:
            self.menu_scroll = self.cursor
        elif self.cursor >= self.menu_scroll + max_rows:
            self.menu_scroll = self.cursor - max_rows + 1
        self.menu_scroll = max(0, min(self.menu_scroll, max(0, len(items) - max_rows)))

        self.menu_rects = {}
        last = min(len(items), self.menu_scroll + max_rows)
        for idx in range(self.menu_scroll, last):
            y = menu_y + (idx - self.menu_scroll)
            if y >= h - 2:
                break
            label = items[idx][0]
            attr = curses.A_REVERSE if idx == self.cursor else curses.A_NORMAL
            stdscr.addstr(y, menu_x, label[: max(0, w - menu_x - 2)], attr)
            self.menu_rects[idx] = (y, menu_x, y, menu_x + len(label))

        if len(items) > max_rows:
            iy = menu_y + (last - self.menu_scroll)
            if iy < h - 2:
                up = "^ " if self.menu_scroll > 0 else "  "
                down = " v" if last < len(items) else ""
                indicator = f"{up}[{self.menu_scroll + 1}-{last}/{len(items)}]{down}"
                stdscr.addstr(iy, menu_x, indicator[: max(0, w - menu_x - 2)], curses.A_DIM)

    def _render_devices(self, stdscr) -> None:
        if not self.devices:
            return
        h, w = stdscr.getmaxyx()
        x = 42
        box_y = 18
        stdscr.addstr(box_y - 1, x, "Devices", curses.A_BOLD)
        idx = 0
        for line in self.devices:
            yy = box_y + idx
            if yy >= h - 1:
                break
            stdscr.addstr(yy, x, line[: max(0, w - x - 2)])
            idx += 1

    def _render_log(self, stdscr) -> None:
        h, w = stdscr.getmaxyx()
        y = h - 6
        stdscr.addstr(y, 2, "Log:")
        for idx, line in enumerate(list(self.log)[-5:]):
            yy = y + 1 + idx
            if yy >= h - 1:
                break
            stdscr.addstr(yy, 2, line[: max(0, w - 4)])

    def _draw(self, stdscr) -> None:
        stdscr.erase()
        stdscr.addstr(0, 2, "Frostbay Terminal TUI", curses.A_BOLD)
        stdscr.addstr(0, 30, f"status: {self.status}")
        stdscr.addstr(
            2, 2,
            "Keys: arrows/j-k Enter, PgUp/PgDn/Home/End scroll, q quit; mouse click",
        )
        self._render_menu(stdscr)
        self._render_status_columns(stdscr)
        if self.devices:
            self._render_devices(stdscr)
        else:
            self._render_dashboard(stdscr)
        self._render_log(stdscr)
        stdscr.refresh()

    def _handle_mouse(self, stdscr, event) -> None:
        if not getattr(event, "bstate", 0) & curses.BUTTON1_CLICKED:
            return
        y = event.y
        x = event.x
        for idx, rect in self.menu_rects.items():
            y0, x0, y1, x1 = rect
            if y0 <= y <= y1 and x0 <= x <= x1:
                self.cursor = idx
                self._invoke_action(idx, stdscr)
                return

    def run(self) -> int:
        curses.wrapper(self._main)
        return 0

    def _main(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        last_tick = 0.0

        try:
            while True:
                now = time.monotonic()
                if now - last_tick >= 1.0:
                    if self.ble.is_connected:
                        try:
                            self._refresh_state()
                        except Exception:
                            pass
                        try:
                            self._maybe_auto_restart()
                        except Exception:
                            pass
                        try:
                            self._thermal_tick()
                        except Exception:
                            pass
                    last_tick = now

                self._draw(stdscr)
                ch = stdscr.getch()

                if ch == -1:
                    time.sleep(0.05)
                    continue
                if ch in (ord('q'), ord('Q')):
                    break
                if ch in (curses.KEY_UP, ord('k')):
                    self.cursor = max(0, self.cursor - 1)
                elif ch in (curses.KEY_DOWN, ord('j')):
                    self.cursor = min(len(self._get_menu_items()) - 1, self.cursor + 1)
                elif ch == curses.KEY_NPAGE:
                    self.cursor = min(len(self._get_menu_items()) - 1, self.cursor + max(1, self._menu_visible))
                elif ch == curses.KEY_PPAGE:
                    self.cursor = max(0, self.cursor - max(1, self._menu_visible))
                elif ch == curses.KEY_HOME:
                    self.cursor = 0
                elif ch == curses.KEY_END:
                    self.cursor = len(self._get_menu_items()) - 1
                elif ch in (10, 13, curses.KEY_ENTER, ord(' ')):
                    self._invoke_action(self.cursor, stdscr)
                elif ch == curses.KEY_MOUSE:
                    _, x, y, _, bstate = curses.getmouse()
                    if bstate & curses.BUTTON1_CLICKED:
                        event = type('MouseEvent', (), {'y': y, 'x': x, 'bstate': bstate})()
                        self._handle_mouse(stdscr, event)

        except KeyboardInterrupt:
            pass
        finally:
            try:
                self._run_async(self.ble.disconnect())
            except Exception:
                pass
            curses.curs_set(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frostbay terminal-only controller")
    parser.add_argument("--address", help="BLE address to connect to")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = TerminalFrostbayApp(address=args.address)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
