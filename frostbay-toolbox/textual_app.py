#!/usr/bin/env python3
"""Frostbay terminal controller built with Textual.

Orange-themed TUI exposing every control from the tray app:
scan / connect / disconnect / refresh / OFF / smart presets /
auto-restart toggle / auto-temp toggle / manual fan-pump presets /
set pump, plus a live dashboard (sparklines + gauges) and host CPU/GPU.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    ProgressBar,
    Sparkline,
    Static,
    Switch,
)

from frostbay import __version__
from frostbay.ble import FrostbayBLE, FROSTBAY_NAME_PART
from frostbay.history import History
from frostbay.host import get_host_stats, format_cpu, format_gpu
from frostbay.protocol import FrostbayState, Mode, SMART_CURVES

AUTO_RESTART_COOLDOWN_SEC = 1.0

# Grace window after any active-mode command (user click or auto-restart) during
# which auto-restart stays silent, so the pump can spin up undisturbed.
STARTUP_GRACE_SEC = 10.0
THERMAL_ON_C = 50.0
THERMAL_OFF_C = 45.0

MANUAL_PRESETS: tuple[tuple[int, int], ...] = (
    (20, 60), (20, 70), (20, 80),
    (30, 60), (30, 70), (30, 80),
    (40, 70), (40, 80), (40, 90),
)

# Orange palette
ORANGE = "#ff8c1a"
ORANGE_BRIGHT = "#ffb347"
ORANGE_DIM = "#8a4a08"
BG = "#140f0a"
PANEL = "#1d150d"


class _LogToBuffer(logging.Handler):
    """Route library log records (frostbay/bleak) into the app's log buffer.

    ``emit`` only appends to a thread-safe deque; the periodic UI tick renders
    it, so this is safe to call from the BLE asyncio loop thread.
    """

    def __init__(self, app: "FrostbayTextualApp") -> None:
        super().__init__()
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._app.logbuf.append(f"{record.name}: {record.getMessage()}")
        except Exception:
            pass


class VersionFooter(Footer):
    """Footer that appends the application version after the key bindings."""

    def compose(self):
        yield from super().compose()
        yield Static(f"v{__version__}", id="app-version")


class FrostbayTextualApp(App):
    CSS = f"""
    Screen {{
        background: {BG};
    }}
    #main {{
        layout: horizontal;
        height: 1fr;
    }}
    #controls {{
        width: 38;
        border: round {ORANGE};
        background: {PANEL};
        padding: 1 2;
    }}
    #dashboard {{
        width: 1fr;
        border: round {ORANGE};
        background: {PANEL};
        padding: 1 2;
    }}
    .section-title {{
        color: {ORANGE_BRIGHT};
        text-style: bold;
        margin: 1 0 0 0;
    }}
    .status-line {{
        color: {ORANGE_BRIGHT};
        text-style: bold;
        margin-bottom: 1;
    }}
    .metric-label {{
        color: {ORANGE};
        margin: 1 0 0 0;
    }}
    Button {{
        width: 100%;
        margin: 0 0 1 0;
        background: #2a1c0e;
        color: #ffd9ad;
        border: round {ORANGE_DIM};
    }}
    Button:hover {{
        background: {ORANGE};
        color: #1a1006;
        border: round {ORANGE_BRIGHT};
    }}
    Button:disabled {{
        color: #6b5a48;
        border: round #3a2c1c;
    }}
    .switch-row {{
        height: auto;
        margin: 0 0 1 0;
    }}
    .switch-row Switch {{
        width: auto;
        margin-right: 1;
    }}
    .switch-row Static {{
        color: #ffd9ad;
        padding-top: 1;
    }}
    .input-row {{
        height: auto;
        margin: 0 0 1 0;
    }}
    .input-row Input {{
        width: 1fr;
        margin-right: 1;
        background: #241809;
        color: #ffd9ad;
        border: round {ORANGE_DIM};
    }}
    .input-row Button {{
        width: auto;
        margin: 0;
    }}
    #status_panel {{
        height: auto;
        color: #ffe6cc;
    }}
    #host_panel {{
        height: auto;
        color: #ffe6cc;
    }}
    ProgressBar {{
        height: 1;
        margin: 0 0 1 0;
        color: {ORANGE};
        background: #2a1c0e;
    }}
    ProgressBar > .bar--bar {{
        color: {ORANGE_BRIGHT};
        background: {ORANGE};
    }}
    Sparkline {{
        height: 7;
        margin: 0 0 1 0;
        border: round {ORANGE_DIM};
        background: #1a120a;
    }}
    #app-version {{
        color: {ORANGE_BRIGHT};
        text-style: bold;
        padding: 0 2;
    }}
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("s", "scan", "Scan"),
    ]

    def __init__(self, address: Optional[str] = None) -> None:
        super().__init__()
        self.title = "Frostbay"
        self.sub_title = "BLE cooling controller"
        self.address = address
        self.ble = FrostbayBLE(address=address)
        self.history = History()
        self.state: Optional[FrostbayState] = None
        self.devices: list[str] = []
        self.status = "Ready"
        self.logbuf: deque[str] = deque(maxlen=50)
        self._ble_log_handler: Optional[logging.Handler] = None
        self.auto_restart_enabled = True
        self.thermal_enabled = False
        self._last_auto_restart_ts = 0.0
        # Monotonic timestamp of the last active-mode command (user or auto).
        # Drives the startup grace window in _maybe_auto_restart.
        self._last_user_cmd_ts = 0.0
        # Latched True once the pump has been observed running since the last
        # command; auto-restart only fires on a running->stopped transition.
        self._was_running = False
        self._bg_stop = threading.Event()
        self._bg_thread: Optional[threading.Thread] = None

        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

    # ---- async bridge ----
    def _run_async(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=25)

    def _log(self, message: str) -> None:
        self.logbuf.append(message)

    def _note_command(self) -> None:
        """Arm the startup grace window and clear the running latch.

        Called on every user-initiated cooling command so auto-restart stays out
        of the way while the pump spins up, and only restarts on a real
        running->stopped transition afterwards.
        """
        self._last_user_cmd_ts = time.monotonic()
        self._was_running = False

    def _set_ble_logging(self, enabled: bool) -> None:
        """Attach/detach a handler that surfaces frostbay/bleak errors in the log."""
        names = ("frostbay", "bleak", "bleak.backends.bluezdbus.client", "bleak.backends.bluezdbus.manager")
        if enabled and self._ble_log_handler is None:
            handler = _LogToBuffer(self)
            handler.setLevel(logging.WARNING)
            for name in names:
                lg = logging.getLogger(name)
                lg.addHandler(handler)
                lg.setLevel(logging.WARNING)
            self._ble_log_handler = handler
            self._log("BLE error logging enabled")
        elif not enabled and self._ble_log_handler is not None:
            for name in names:
                logging.getLogger(name).removeHandler(self._ble_log_handler)
            self._ble_log_handler = None
            self._log("BLE error logging disabled")

    # ---- BLE operations (blocking, run in worker/bg threads) ----
    def _refresh_state(self) -> None:
        if not self.ble.is_connected:
            return
        state = self._run_async(self.ble.read_state())
        self.state = state
        if state.is_running():
            self._was_running = True
        self.history.push(state)
        self.status = "Connected"

    def _do_scan(self) -> None:
        results = self._run_async(self.ble.scan(timeout=8.0))
        self.devices = [
            f"{'★' if r.is_frostbay else ' '} {r.name or '<unnamed>'}  {r.address}  RSSI={r.rssi}"
            for r in results
        ]
        self._log(f"Found {len(results)} devices")
        self.status = "Scan finished"

    def _do_find_connect(self) -> None:
        found = self._run_async(self.ble.find_and_connect(timeout=10.0))
        self.address = found.address
        self.devices = []
        self._refresh_state()
        self._log(f"Connected to {found.name} ({found.address})")
        self.status = "Connected"

    def _do_connect_addr(self, addr: str) -> None:
        if not addr:
            self._log("Empty address")
            self.status = "Empty address"
            return
        self._run_async(self.ble.connect(address=addr))
        self.address = addr
        self.devices = []
        self._refresh_state()
        self._log(f"Connected to {addr}")
        self.status = "Connected"

    def _do_disconnect(self) -> None:
        self._run_async(self.ble.disconnect())
        self.state = None
        self.status = "Disconnected"
        self._log("Disconnected")

    def _do_off(self) -> None:
        self._note_command()
        self.state = self._run_async(self.ble.set_off())
        self.history.push(self.state)
        self._log("Device OFF")
        self.status = "OFF"

    def _do_smart(self, preset: str) -> None:
        self._note_command()
        self.state = self._run_async(self.ble.set_smart(preset))
        self.history.push(self.state)
        self._log(f"Smart {preset}")
        self.status = f"Smart {preset}"

    def _do_fixed(self, fan: int, pump: int) -> None:
        self._note_command()
        self.state = self._run_async(self.ble.set_fixed(fan, pump))
        self.history.push(self.state)
        self._log(f"Fixed {fan}% / {pump}%")
        self.status = f"Fixed {fan}% / {pump}%"

    def _do_set_pump(self, value: int) -> None:
        self._note_command()
        self.state = self._run_async(self.ble.set_pump(value))
        self.history.push(self.state)
        self._log(f"Pump set to {value}%")
        self.status = f"Pump {value}%"

    # ---- auto-restart / thermal (ported from tray) ----
    @staticmethod
    def _smart_preset_for(state: FrostbayState) -> str:
        for preset, curve in SMART_CURVES.items():
            if state.smart_curve == curve:
                return preset
        return "silent"

    @staticmethod
    def _host_max_temp() -> Optional[float]:
        host = get_host_stats()
        temps = [t for t in (host.cpu_temp_c, host.gpu_temp_c) if t is not None]
        return max(temps) if temps else None

    def _maybe_auto_restart(self) -> None:
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

    # ---- workers (button actions off the UI thread) ----
    @work(thread=True, exclusive=True, group="ble")
    def _w(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:
            self.status = f"Error: {exc}"
            self._log(str(exc))
        self.call_from_thread(self._update_ui)

    # ---- background periodic loop ----
    def _bg_loop(self) -> None:
        while not self._bg_stop.is_set():
            try:
                if self.ble.is_connected:
                    self._refresh_state()
                    self._maybe_auto_restart()
                    self._thermal_tick()
            except Exception as exc:
                self.status = f"Tick error: {exc}"
            try:
                self.call_from_thread(self._update_ui)
            except Exception:
                break
            self._bg_stop.wait(1.0)

    # ---- UI ----
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with VerticalScroll(id="controls"):
                yield Static("CONNECTION", classes="section-title")
                yield Button("Scan for devices", id="scan")
                yield Button(f"Find & connect (*{FROSTBAY_NAME_PART}*)", id="find")
                with Horizontal(classes="input-row"):
                    yield Input(placeholder="BLE address", id="addr_input", value=self.address or "")
                    yield Button("Connect", id="connect_addr")
                yield Button("Disconnect", id="disconnect")
                yield Button("Refresh state", id="refresh")

                yield Static("COOLING", classes="section-title")
                yield Button("Turn OFF", id="off")
                yield Button("Smart: Silent", id="smart_silent")
                yield Button("Smart: Soft", id="smart_soft")
                yield Button("Smart: Strong", id="smart_strong")

                yield Static("AUTOMATION", classes="section-title")
                with Horizontal(classes="switch-row"):
                    yield Switch(value=True, id="auto_restart")
                    yield Static("Auto-restart on stop")
                with Horizontal(classes="switch-row"):
                    yield Switch(value=False, id="auto_temp")
                    yield Static(f"Auto temp >{THERMAL_ON_C:.0f}C / <{THERMAL_OFF_C:.0f}C")

                yield Static("MANUAL PRESETS", classes="section-title")
                for fan, pump in MANUAL_PRESETS:
                    yield Button(f"Fan/Pump {fan}-{pump}", id=f"preset_{fan}_{pump}")

                yield Static("SET PUMP", classes="section-title")
                with Horizontal(classes="input-row"):
                    yield Input(placeholder="Pump %", id="pump_input")
                    yield Button("Apply", id="apply_pump")

                yield Static("LOGGING", classes="section-title")
                with Horizontal(classes="switch-row"):
                    yield Switch(value=True, id="ble_log")
                    yield Static("Show BLE errors in log")

            with VerticalScroll(id="dashboard"):
                yield Static("", id="status_line", classes="status-line")
                yield Static("DEVICE STATUS", classes="section-title")
                yield Static("", id="status_panel")
                yield Static("HOST", classes="section-title")
                yield Static("", id="host_panel")
                yield Static("LIVE DASHBOARD", classes="section-title")
                yield Static("Fan %", classes="metric-label")
                yield ProgressBar(id="pb_fan", total=100, show_eta=False)
                yield Static("Pump %", classes="metric-label")
                yield ProgressBar(id="pb_pump", total=100, show_eta=False)
                yield Sparkline(name="Temp IN °C", id="spark_temp_in",
                               min_color=ORANGE_DIM, max_color=ORANGE_BRIGHT)
                yield Sparkline(name="Temp OUT °C", id="spark_temp_out",
                               min_color=ORANGE_DIM, max_color=ORANGE_BRIGHT)
                yield Sparkline(name="Flow mL/min", id="spark_flow",
                               min_color=ORANGE_DIM, max_color=ORANGE_BRIGHT)
                yield Sparkline(name="Fan %", id="spark_fan",
                               min_color=ORANGE_DIM, max_color=ORANGE_BRIGHT)
                yield Sparkline(name="Pump %", id="spark_pump",
                               min_color=ORANGE_DIM, max_color=ORANGE_BRIGHT)
                yield Static("LOG", classes="section-title")
                yield Static("", id="log_panel")
        yield VersionFooter()

    def on_mount(self) -> None:
        self._set_ble_logging(True)
        self._bg_thread = threading.Thread(target=self._bg_loop, daemon=True)
        self._bg_thread.start()
        self._update_ui()

    def _set_spark(self, wid: str, values: list[float]) -> None:
        try:
            self.query_one(f"#{wid}", Sparkline).data = values
        except Exception:
            pass

    def _update_ui(self) -> None:
        s = self.state
        if s is None:
            status_text = "[dim]No live telemetry[/dim]"
            fan_val = pump_val = 0
        else:
            running = "[green]RUNNING[/green]" if s.is_running() else "[red]STOPPED[/red]"
            status_text = "\n".join([
                f"Mode:      [bold]{s.mode.label}[/bold]",
                f"Running:   {running}",
                f"Fan:       {s.fan_percent}%",
                f"Pump:      {s.pump_percent}%",
                f"Flow:      {s.flow_ml_min:.1f} mL/min",
                f"Temp in:   {s.temp_in_c}°C",
                f"Temp out:  {s.temp_out_c}°C",
                f"Protocol:  0x{s.protocol_version:02X}",
            ])
            fan_val = s.fan_percent
            pump_val = s.pump_percent

        self.query_one("#status_line", Static).update(
            f"● status: {self.status}   device: {self.address or '—'}"
        )
        self.query_one("#status_panel", Static).update(status_text)

        host = get_host_stats()
        self.query_one("#host_panel", Static).update(
            f"CPU: [bold]{format_cpu(host)}[/bold]    GPU: [bold]{format_gpu(host)}[/bold]"
        )

        try:
            self.query_one("#pb_fan", ProgressBar).update(total=100, progress=fan_val)
            self.query_one("#pb_pump", ProgressBar).update(total=100, progress=pump_val)
        except Exception:
            pass

        self._set_spark("spark_temp_in", self.history.series("temp_in"))
        self._set_spark("spark_temp_out", self.history.series("temp_out"))
        self._set_spark("spark_flow", self.history.series("flow"))
        self._set_spark("spark_fan", self.history.series("fan"))
        self._set_spark("spark_pump", self.history.series("pump"))

        log_text = "\n".join(list(self.logbuf)[-10:]) if self.logbuf else "[dim]—[/dim]"
        self.query_one("#log_panel", Static).update(log_text)

        connected = self.ble.is_connected
        for bid in ("off", "smart_silent", "smart_soft", "smart_strong",
                   "refresh", "disconnect", "apply_pump"):
            try:
                self.query_one(f"#{bid}", Button).disabled = not connected
            except Exception:
                pass
        for fan, pump in MANUAL_PRESETS:
            try:
                self.query_one(f"#preset_{fan}_{pump}", Button).disabled = not connected
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "scan":
            self._w(self._do_scan)
        elif bid == "find":
            self._w(self._do_find_connect)
        elif bid == "connect_addr":
            addr = self.query_one("#addr_input", Input).value.strip()
            self._w(self._do_connect_addr, addr)
        elif bid == "disconnect":
            self._w(self._do_disconnect)
        elif bid == "refresh":
            self._w(self._refresh_state)
        elif bid == "off":
            self._w(self._do_off)
        elif bid == "smart_silent":
            self._w(self._do_smart, "silent")
        elif bid == "smart_soft":
            self._w(self._do_smart, "soft")
        elif bid == "smart_strong":
            self._w(self._do_smart, "strong")
        elif bid == "apply_pump":
            raw = self.query_one("#pump_input", Input).value.strip()
            try:
                self._w(self._do_set_pump, int(raw))
            except ValueError:
                self._log("Pump value must be an integer")
                self._update_ui()
        elif bid and bid.startswith("preset_"):
            _, fan, pump = bid.split("_")
            self._w(self._do_fixed, int(fan), int(pump))

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "auto_restart":
            self.auto_restart_enabled = event.value
            self._log(f"Auto-restart {'enabled' if event.value else 'disabled'}")
        elif event.switch.id == "auto_temp":
            self.thermal_enabled = event.value
            self._log(f"Auto temp {'enabled' if event.value else 'disabled'}")
        elif event.switch.id == "ble_log":
            self._set_ble_logging(event.value)

    def action_refresh(self) -> None:
        if self.ble.is_connected:
            self._w(self._refresh_state)

    def action_scan(self) -> None:
        self._w(self._do_scan)

    def on_unmount(self) -> None:
        self._bg_stop.set()
        try:
            self._run_async(self.ble.disconnect())
        except Exception:
            pass


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Frostbay Textual controller")
    parser.add_argument("--address", help="BLE address to connect to")
    args = parser.parse_args()
    FrostbayTextualApp(address=args.address).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
