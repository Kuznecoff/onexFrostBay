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

from rich.segment import Segment
from rich.style import Style

from textual import work
from textual.app import App, ComposeResult, RenderResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.renderables._blend_colors import blend_colors
from textual.renderables.sparkline import Sparkline as SparklineRenderable
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    ProgressBar,
    Select,
    Sparkline,
    Static,
    Switch,
)

from frostbay import __version__
from frostbay.ble import FrostbayBLE, FROSTBAY_NAME_PART
from frostbay.config import load_config, save_config
from frostbay.history import History
from frostbay.host import get_host_stats, format_cpu, format_gpu
from frostbay.protocol import FrostbayState, Mode, SMART_CURVES

AUTO_RESTART_COOLDOWN_SEC = 1.0

# Grace window after any active-mode command (user click or auto-restart) during
# which auto-restart stays silent, so the pump can spin up undisturbed.
STARTUP_GRACE_SEC = 10.0
THERMAL_ON_C = 50.0
THERMAL_OFF_C = 45.0
# Thermal resend policy while a stage is active:
#  "1s"/"2s"  - fire-and-forget: re-issue the ON command every N seconds
#               regardless of the reported running state.
#  "on_stop"  - legacy: only resend when the device reports it stopped.
THERMAL_RESEND_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Every 1s", "1s"),
    ("Every 2s", "2s"),
    ("On stop (wait)", "on_stop"),
)
THERMAL_RESEND_INTERVALS: dict[str, float] = {"1s": 1.0, "2s": 2.0}
THERMAL_RESEND_DEFAULT = "2s"

MANUAL_PRESETS: tuple[tuple[int, int], ...] = (
    (20, 60), (30, 70), (50, 80), (100, 100),
)

# Extra fixed presets available only in thermal auto-on mode list.
THERMAL_EXTRA_PRESETS: tuple[tuple[int, int], ...] = (
    (20, 40), (20, 50),
)

SMART_PRESETS: tuple[str, ...] = ("silent", "soft", "strong")

THERMAL_MODE_OPTIONS: dict[str, tuple[str, str]] = {
    **{f"smart_{p}": ("SMART", f"Smart: {p.capitalize()}") for p in SMART_PRESETS},
    **{f"fixed_{f}_{p}": ("FIXED", f"Fan/Pump {f}-{p}")
       for f, p in MANUAL_PRESETS + THERMAL_EXTRA_PRESETS},
}

# (title, History series key, value format, fixed scale low/high or None) per sparkline.
SPARK_METRICS: tuple[tuple[str, str, str, Optional[tuple[float, float]]], ...] = (
    ("Temp IN °C", "temp_in", "{:.0f}°C", (25.0, 50.0)),
    ("Temp OUT °C", "temp_out", "{:.0f}°C", (25.0, 50.0)),
    ("Flow mL/min", "flow", "{:.1f} mL/min", None),
    ("Fan %", "fan", "{:.0f}%", None),
    ("Pump %", "pump", "{:.0f}%", None),
)


class _FixedScaleSparklineRenderable(SparklineRenderable):
    """Sparkline renderable with a fixed value scale instead of the data's min/max."""

    def __init__(self, data, *, fixed_min: float, fixed_max: float, **kwargs) -> None:
        super().__init__(data, **kwargs)
        self.fixed_min = fixed_min
        self.fixed_max = fixed_max

    def __rich_console__(self, console, options):
        width = self.width or options.max_width
        height = self.height or 1

        if len(self.data) == 0:
            for _ in range(height - 1):
                yield Segment.line()
            yield Segment("▁" * width, self.min_color)
            return
        if len(self.data) == 1:
            for i in range(height):
                yield Segment("█" * width, self.max_color)
                if i < height - 1:
                    yield Segment.line()
            return

        bar_line_segments = len(self.BARS)
        bar_segments = bar_line_segments * height - 1

        minimum, maximum = self.fixed_min, self.fixed_max
        extent = maximum - minimum or 1

        summary_function = self.summary_function
        min_color, max_color = self.min_color.color, self.max_color.color

        buckets = tuple(self._buckets(list(self.data), num_buckets=width))

        for i in reversed(range(height)):
            current_bar_part_low = i * bar_line_segments
            current_bar_part_high = (i + 1) * bar_line_segments

            bucket_index = 0.0
            bars_rendered = 0
            step = len(buckets) / width
            while bars_rendered < width:
                partition = buckets[int(bucket_index)]
                partition_summary = summary_function(partition)
                height_ratio = min(1.0, max(0.0, (partition_summary - minimum) / extent))
                bar_index = int(height_ratio * bar_segments)

                if bar_index < current_bar_part_low:
                    bar = " "
                    with_color = False
                elif bar_index >= current_bar_part_high:
                    bar = "█"
                    with_color = True
                else:
                    bar = self.BARS[bar_index % bar_line_segments]
                    with_color = True

                if with_color:
                    bar_color = blend_colors(min_color, max_color, height_ratio)
                    style = Style.from_color(bar_color)
                else:
                    style = None

                bars_rendered += 1
                bucket_index += step
                yield Segment(bar, style)

            if i > 0:
                yield Segment.line()


class FixedScaleSparkline(Sparkline):
    """Sparkline widget whose vertical scale is pinned to fixed_min..fixed_max."""

    def __init__(self, *args, fixed_min: float = 0.0, fixed_max: float = 100.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fixed_min = fixed_min
        self.fixed_max = fixed_max

    def render(self) -> RenderResult:
        data = self.data or []
        _, base = self.background_colors
        min_color = base + (
            self.get_component_styles("sparkline--min-color").color
            if self.min_color is None
            else self.min_color
        )
        max_color = base + (
            self.get_component_styles("sparkline--max-color").color
            if self.max_color is None
            else self.max_color
        )
        return _FixedScaleSparklineRenderable(
            data,
            width=self.size.width,
            height=self.size.height,
            min_color=min_color.rich_color,
            max_color=max_color.rich_color,
            summary_function=self.summary_function,
            fixed_min=self.fixed_min,
            fixed_max=self.fixed_max,
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
    #settings_panel {{
        display: none;
        width: 1fr;
        border: round {ORANGE};
        background: {PANEL};
        padding: 1 2;
    }}
    #controls, #dashboard, #settings_panel {{
        scrollbar-color: #6e3c0c;
        scrollbar-size-vertical: 1;
    }}
    #controls > * {{
        margin-right: 1;
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
    Button.toggle-on {{
        background: {ORANGE};
        color: #1a1006;
        border: round {ORANGE_BRIGHT};
    }}
    Button.toggle-off {{
        color: #9a8a76;
        border: round {ORANGE};
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
    .input-row Static {{
        width: auto;
    }}
    .input-row .field-label {{
        width: 10;
        color: #ffd9ad;
        padding-top: 1;
    }}
    .input-row Select {{
        width: 1fr;
        background: #241809;
        color: #ffd9ad;
        border: round {ORANGE_DIM};
    }}
    #thermal_stages_box {{
        height: auto;
    }}
    .stage-panel {{
        border: round {ORANGE};
        background: #1a120a;
        padding: 0 1;
        margin: 0 0 1 0;
        height: auto;
    }}
    .stage-header {{
        height: 1;
    }}
    .stage-title {{
        color: {ORANGE_BRIGHT};
        text-style: bold;
    }}
    Button.stage-del {{
        dock: right;
        width: 5;
        min-width: 5;
        height: 1;
        min-height: 1;
        margin: 0;
        border: none;
        background: #3a1408;
        color: #ff9a7a;
    }}
    Button.stage-del:hover {{
        background: #b3341a;
        color: #fff0e6;
    }}
    Button.stage-del:disabled {{
        color: #6b5a48;
        background: #241809;
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
        self.config = load_config()
        if not address:
            address = (self.config.get("deviceUUID") or "").strip() or None
        self.address = address
        self.ble = FrostbayBLE(address=address)
        self._ble_operation_lock = threading.Lock()
        self.history = History()
        self.state: Optional[FrostbayState] = None
        self.devices: list[str] = []
        self.status = "Ready"
        self.logbuf: deque[str] = deque(maxlen=50)
        self._ble_log_handler: Optional[logging.Handler] = None
        self.auto_restart_enabled = bool(self.config.get("auto_restart", True))
        self.thermal_enabled = bool(self.config.get("auto_temp", False))
        self.thermal_off_c = float(self.config.get("thermal_off_c", THERMAL_OFF_C))
        if not (25.0 <= self.thermal_off_c <= 75.0):
            self.thermal_off_c = THERMAL_OFF_C
        self.thermal_stages = self._load_thermal_stages()
        self._thermal_active_mode: Optional[str] = None
        self._last_thermal_send_ts = 0.0
        resend = str(self.config.get("thermal_resend", THERMAL_RESEND_DEFAULT))
        self.thermal_resend = resend if resend in ("1s", "2s", "on_stop") else THERMAL_RESEND_DEFAULT
        self.ble_log_enabled = bool(self.config.get("ble_log", True))
        self.auto_connect_enabled = bool(self.config.get("auto_connect", False))
        self._last_auto_restart_ts = 0.0
        # Monotonic timestamp of the last active-mode command (user or auto).
        # Drives the startup grace window in _maybe_auto_restart.
        self._last_user_cmd_ts = 0.0
        # Latched True once the pump has been observed running since the last
        # command; auto-restart only fires on a running->stopped transition.
        self._was_running = False
        self._bg_stop = threading.Event()
        self._bg_thread: Optional[threading.Thread] = None

        self._ble_loop = asyncio.new_event_loop()
        self._ble_loop_thread = threading.Thread(target=self._ble_loop.run_forever, daemon=True)
        self._ble_loop_thread.start()

    def _valid_stage(self, on_c: float, mode: str) -> bool:
        return (mode in THERMAL_MODE_OPTIONS
                and 30.0 <= on_c <= 80.0
                and on_c - self.thermal_off_c >= 3.0)

    def _load_thermal_stages(self) -> list[dict]:
        raw = self.config.get("thermal_stages")
        stages: list[dict] = []
        if isinstance(raw, list):
            for item in raw[:5]:
                if not isinstance(item, dict):
                    continue
                try:
                    on_c = float(item["on_c"])
                except (KeyError, TypeError, ValueError):
                    continue
                mode = str(item.get("mode", ""))
                if self._valid_stage(on_c, mode):
                    stages.append({"on_c": on_c, "mode": mode})
        if not stages:
            try:
                on0 = float(self.config.get("thermal_on_c", THERMAL_ON_C))
            except (TypeError, ValueError):
                on0 = THERMAL_ON_C
            mode0 = str(self.config.get("thermal_mode", "smart_silent"))
            if not self._valid_stage(on0, mode0):
                on0, mode0 = THERMAL_ON_C, "smart_silent"
            stages = [{"on_c": on0, "mode": mode0}]
        return stages

    def _save_thermal_stages(self) -> None:
        self.config["thermal_stages"] = self.thermal_stages
        try:
            save_config(self.config)
        except OSError as exc:
            self._log(f"Config save failed: {exc}")

    def _thermal_button_label(self) -> str:
        min_on = min(s["on_c"] for s in self.thermal_stages)
        return f"Auto temp >{min_on:g}C / <{self.thermal_off_c:g}C"

    def _update_thermal_button(self) -> None:
        try:
            self.query_one("#auto_temp_toggle", Button).label = self._thermal_button_label()
        except Exception:
            pass

    # ---- async bridge ----
    def _run_async(self, coro, timeout: float = 25.0):
        async def run_with_timeout():
            try:
                return await asyncio.wait_for(coro, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"BLE operation timed out after {timeout:g}s") from exc

        return asyncio.run_coroutine_threadsafe(run_with_timeout(), self._ble_loop).result()

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

    def _save_device_uuid(self, addr: str) -> None:
        self.config["deviceUUID"] = addr
        try:
            save_config(self.config)
        except OSError as exc:
            self._log(f"Config save failed: {exc}")

    def _do_find_connect(self) -> None:
        found = self._run_async(self.ble.find_and_connect(timeout=10.0), timeout=240.0)
        self.address = found.address
        self._save_device_uuid(found.address)
        self.devices = []
        self._refresh_state()
        self._log(f"Connected to {found.name} ({found.address})")
        self.status = "Connected"

    def _do_auto_connect(self) -> None:
        addr = (self.address or "").strip()
        if addr:
            try:
                self._log(f"Auto-connect: trying remembered device {addr}")
                self._run_async(self.ble.connect(address=addr), timeout=240.0)
                self.devices = []
                self._refresh_state()
                self._log(f"Auto-connected to {addr}")
                self.status = "Connected"
                return
            except Exception as exc:
                self._log(f"Auto-connect to {addr} failed: {exc}; searching by name")
        self._do_find_connect()

    def _do_connect_addr(self, addr: str) -> None:
        if not addr:
            self._log("Empty address")
            self.status = "Empty address"
            return
        self._run_async(self.ble.connect(address=addr), timeout=240.0)
        self.address = addr
        self._save_device_uuid(addr)
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

    def _apply_thermal_mode(self, mode: str):
        if mode.startswith("smart_"):
            return self.ble.set_smart(mode.split("_", 1)[1])
        _, fan, pump = mode.split("_")
        return self.ble.set_fixed(int(fan), int(pump))

    def _thermal_stage_for_temp(self, temp: float) -> Optional[dict]:
        best: Optional[dict] = None
        for st in self.thermal_stages:
            if st["on_c"] - self.thermal_off_c < 3.0:
                continue
            if temp >= st["on_c"] and (best is None or st["on_c"] >= best["on_c"]):
                best = st
        return best

    def _thermal_tick(self) -> None:
        if not self.thermal_enabled or not self.ble.is_connected:
            return
        temp = self._host_max_temp()
        if temp is None:
            return
        try:
            if temp < self.thermal_off_c:
                state = self.state if self.state is not None else self._run_async(self.ble.read_state())
                if state.is_running():
                    self.state = self._run_async(self.ble.set_off())
                    self.history.push(self.state)
                    self._thermal_active_mode = None
                    self._log(f"Thermal: {temp:.1f}C < {self.thermal_off_c:g}C -> OFF")
                    self.status = "Thermal OFF"
                return
            stage = self._thermal_stage_for_temp(temp)
            if stage is None:
                return
            desired = stage["mode"]
            now = time.monotonic()
            if self.thermal_resend == "on_stop":
                state = self._run_async(self.ble.read_state())
                self.state = state
                self.history.push(state)
                resend = desired != self._thermal_active_mode or not state.is_running()
            else:
                interval = THERMAL_RESEND_INTERVALS[self.thermal_resend]
                resend = (desired != self._thermal_active_mode
                         or now - self._last_thermal_send_ts >= interval)
            if resend:
                self.state = self._run_async(self._apply_thermal_mode(desired))
                self.history.push(self.state)
                self._thermal_active_mode = desired
                self._last_thermal_send_ts = now
                self._log(f"Thermal: {temp:.1f}C -> {THERMAL_MODE_OPTIONS[desired][1]}")
                self.status = "Thermal ON"
        except Exception as exc:
            self._log(f"Thermal control failed: {exc}")

    # ---- workers (button actions off the UI thread) ----
    @work(thread=True, exclusive=True, group="ble")
    def _w(self, fn, *args) -> None:
        try:
            with self._ble_operation_lock:
                fn(*args)
        except Exception as exc:
            self.status = f"Error: {exc}"
            self._log(str(exc))
        self.call_from_thread(self._update_ui)

    # ---- background periodic loop ----
    def _bg_loop(self) -> None:
        while not self._bg_stop.is_set():
            try:
                with self._ble_operation_lock:
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
                yield Button(f"Find & connect (*{FROSTBAY_NAME_PART}*)", id="find")
                yield Button("Connect", id="connect_addr")
                yield Button("Disconnect", id="disconnect")
                yield Button("Refresh state", id="refresh")

                yield Static("COOLING", classes="section-title")
                yield Button("Turn OFF", id="off")
                yield Button("Smart: Silent", id="smart_silent")
                yield Button("Smart: Soft", id="smart_soft")
                yield Button("Smart: Strong", id="smart_strong")

                yield Static("AUTOMATION", classes="section-title")
                yield Button(
                    self._thermal_button_label(),
                    id="auto_temp_toggle",
                    classes="toggle-on" if self.thermal_enabled else "toggle-off",
                )

                yield Static("MANUAL PRESETS", classes="section-title")
                for fan, pump in MANUAL_PRESETS:
                    yield Button(f"Fan/Pump {fan}-{pump}", id=f"preset_{fan}_{pump}")

                yield Static("SET PUMP", classes="section-title")
                with Horizontal(classes="input-row"):
                    yield Input(placeholder="Pump %", id="pump_input")
                    yield Button("Apply", id="apply_pump")

                yield Button("Settings", id="open_settings")

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
                for title, key, _fmt, scale in SPARK_METRICS:
                    yield Static("", classes="metric-label", id=f"lbl_{key}")
                    spark_cls = FixedScaleSparkline if scale else Sparkline
                    extra = ({"fixed_min": scale[0], "fixed_max": scale[1]} if scale else {})
                    yield spark_cls(name=title, id=f"spark_{key}",
                                   min_color=ORANGE_DIM, max_color=ORANGE_BRIGHT, **extra)
                yield Static("LOG", classes="section-title")
                yield Static("", id="log_panel")

            with VerticalScroll(id="settings_panel"):
                yield Static("SETTINGS", classes="section-title")
                yield Button("Back to dashboard", id="close_settings")
                with Horizontal(classes="switch-row"):
                    yield Switch(value=self.auto_connect_enabled, id="auto_connect")
                    yield Static("Auto-connect on startup")
                with Horizontal(classes="switch-row"):
                    yield Switch(value=self.auto_restart_enabled, id="auto_restart")
                    yield Static("Auto-restart on stop")
                with Horizontal(classes="switch-row"):
                    yield Switch(value=self.ble_log_enabled, id="ble_log")
                    yield Static("Show BLE errors in log")
                yield Static("DEVICE UUID", classes="section-title")
                with Horizontal(classes="input-row"):
                    yield Input(placeholder="BLE address", id="addr_input", value=self.address or "")
                yield Static("AUTO TEMP", classes="section-title")
                with Horizontal(classes="input-row"):
                    yield Static("OFF °C", classes="field-label")
                    yield Input(value=f"{self.thermal_off_c:g}", placeholder="25-75", id="thermal_off_input")
                with Horizontal(classes="input-row"):
                    yield Static("RESEND", classes="field-label")
                    yield Select(
                        list(THERMAL_RESEND_OPTIONS),
                        value=self.thermal_resend,
                        id="thermal_resend_select",
                    )
                with Vertical(id="thermal_stages_box"):
                    pass
                yield Button("ADD MODE BY TEMP STEP", id="add_thermal_stage")
        yield VersionFooter()

    def _make_stage_panel(self, idx: int, stage: dict) -> Vertical:
        del_btn = Button("[X]", id=f"del_stage_{idx}", classes="stage-del")
        del_btn.disabled = idx == 0
        return Vertical(
            Horizontal(
                Static(f"STEP {idx + 1}" + (" (required)" if idx == 0 else ""),
                      classes="stage-title"),
                del_btn,
                classes="stage-header",
            ),
            Horizontal(
                Static("ON °C", classes="field-label"),
                Input(value=f"{stage['on_c']:g}", placeholder="30-80, >= OFF+3", id=f"stage_on_{idx}"),
                classes="input-row",
            ),
            Horizontal(
                Static("MODE", classes="field-label"),
                Select(
                    [(label, val) for val, (_grp, label) in THERMAL_MODE_OPTIONS.items()],
                    value=stage["mode"],
                    id=f"stage_mode_{idx}",
                ),
                classes="input-row",
            ),
            classes="stage-panel",
        )

    def _rebuild_stages(self) -> None:
        box = self.query_one("#thermal_stages_box", Vertical)
        box.remove_children()
        for i, st in enumerate(self.thermal_stages):
            box.mount(self._make_stage_panel(i, st))
        self.query_one("#add_thermal_stage", Button).display = len(self.thermal_stages) < 5

    def on_mount(self) -> None:
        self._set_ble_logging(self.ble_log_enabled)
        self._rebuild_stages()
        self._bg_thread = threading.Thread(target=self._bg_loop, daemon=True)
        self._bg_thread.start()
        self._update_ui()
        if self.auto_connect_enabled and not self.ble.is_connected:
            self._w(self._do_auto_connect)

    def _show_settings(self, show: bool) -> None:
        self.query_one("#dashboard", VerticalScroll).display = not show
        self.query_one("#settings_panel", VerticalScroll).display = show

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

        try:
            addr_input = self.query_one("#addr_input", Input)
            if not addr_input.has_focus and addr_input.value != (self.address or ""):
                addr_input.value = self.address or ""
        except Exception:
            pass

        host = get_host_stats()
        self.query_one("#host_panel", Static).update(
            f"CPU: [bold]{format_cpu(host)}[/bold]    GPU: [bold]{format_gpu(host)}[/bold]"
        )

        try:
            self.query_one("#pb_fan", ProgressBar).update(total=100, progress=fan_val)
            self.query_one("#pb_pump", ProgressBar).update(total=100, progress=pump_val)
        except Exception:
            pass

        for title, key, fmt, scale in SPARK_METRICS:
            values = self.history.series(key)
            self._set_spark(f"spark_{key}", values)
            scale_txt = f"  scale {fmt.format(scale[0])}–{fmt.format(scale[1])}" if scale else ""
            if values:
                text = (f"[bold]{title}[/bold]  now {fmt.format(values[-1])}  "
                       f"min {fmt.format(min(values))} / max {fmt.format(max(values))}"
                       f"{scale_txt}  [dim]last {len(values)} s[/dim]")
            else:
                text = f"[bold]{title}[/bold]  [dim]no data yet[/dim]{scale_txt}"
            try:
                self.query_one(f"#lbl_{key}", Static).update(text)
            except Exception:
                pass

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
        if bid == "find":
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
        elif bid == "open_settings":
            self._show_settings(True)
        elif bid == "close_settings":
            self._show_settings(False)
        elif bid == "auto_temp_toggle":
            self.thermal_enabled = not self.thermal_enabled
            self.config["auto_temp"] = self.thermal_enabled
            self._log(f"Auto temp {'enabled' if self.thermal_enabled else 'disabled'}")
            try:
                save_config(self.config)
            except OSError as exc:
                self._log(f"Config save failed: {exc}")
            btn = self.query_one("#auto_temp_toggle", Button)
            btn.remove_class("toggle-on")
            btn.remove_class("toggle-off")
            btn.add_class("toggle-on" if self.thermal_enabled else "toggle-off")
        elif bid == "add_thermal_stage":
            if len(self.thermal_stages) >= 5:
                return
            last_on = max(s["on_c"] for s in self.thermal_stages)
            new_on = min(80.0, last_on + 5.0)
            if new_on <= last_on:
                self._log("No room for another temp step (max 80C)")
                return
            self.thermal_stages.append({"on_c": new_on, "mode": "smart_strong"})
            self._save_thermal_stages()
            self._rebuild_stages()
            self._update_thermal_button()
            self._log(f"Added thermal step at {new_on:g}C")
        elif bid and bid.startswith("del_stage_"):
            try:
                idx = int(bid.rsplit("_", 1)[1])
            except (ValueError, IndexError):
                return
            if idx <= 0 or idx >= len(self.thermal_stages):
                return
            removed = self.thermal_stages.pop(idx)
            self._save_thermal_stages()
            self._rebuild_stages()
            self._update_thermal_button()
            self._log(f"Removed thermal step {idx + 1} ({removed['on_c']:g}C)")
        elif bid and bid.startswith("preset_"):
            _, fan, pump = bid.split("_")
            self._w(self._do_fixed, int(fan), int(pump))

    def on_input_changed(self, event: Input.Changed) -> None:
        iid = event.input.id or ""
        if iid == "thermal_off_input":
            try:
                off_v = float(event.input.value.strip())
            except ValueError:
                return
            if not (25.0 <= off_v <= 75.0):
                return
            if off_v == self.thermal_off_c:
                return
            self.thermal_off_c = off_v
            self.config["thermal_off_c"] = off_v
            try:
                save_config(self.config)
            except OSError as exc:
                self._log(f"Config save failed: {exc}")
            self._update_thermal_button()
            self._log(f"Auto temp OFF <{off_v:g}C")
        elif iid.startswith("stage_on_"):
            try:
                idx = int(iid.rsplit("_", 1)[1])
                on_v = float(event.input.value.strip())
            except (ValueError, IndexError):
                return
            if idx >= len(self.thermal_stages):
                return
            if not self._valid_stage(on_v, self.thermal_stages[idx]["mode"]):
                return
            if on_v == self.thermal_stages[idx]["on_c"]:
                return
            self.thermal_stages[idx]["on_c"] = on_v
            self._save_thermal_stages()
            self._update_thermal_button()
            self._log(f"Thermal step {idx + 1}: ON >{on_v:g}C")

    def on_select_changed(self, event: Select.Changed) -> None:
        sid = event.select.id or ""
        if sid == "thermal_resend_select":
            if event.value is None or event.value is Select.BLANK:
                return
            mode = str(event.value)
            if mode not in ("1s", "2s", "on_stop") or mode == self.thermal_resend:
                return
            self.thermal_resend = mode
            self.config["thermal_resend"] = mode
            try:
                save_config(self.config)
            except OSError as exc:
                self._log(f"Config save failed: {exc}")
            label = next(lbl for lbl, val in THERMAL_RESEND_OPTIONS if val == mode)
            self._log(f"Thermal resend: {label}")
            return
        if not sid.startswith("stage_mode_") or event.value is None:
            return
        try:
            idx = int(sid.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            return
        mode = str(event.value)
        if idx >= len(self.thermal_stages) or mode not in THERMAL_MODE_OPTIONS:
            return
        if mode == self.thermal_stages[idx]["mode"]:
            return
        self.thermal_stages[idx]["mode"] = mode
        self._save_thermal_stages()
        self._log(f"Thermal step {idx + 1}: mode {THERMAL_MODE_OPTIONS[mode][1]}")

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "auto_restart":
            self.auto_restart_enabled = event.value
            self.config["auto_restart"] = event.value
            self._log(f"Auto-restart {'enabled' if event.value else 'disabled'}")
        elif event.switch.id == "ble_log":
            self.ble_log_enabled = event.value
            self.config["ble_log"] = event.value
            self._set_ble_logging(event.value)
        elif event.switch.id == "auto_connect":
            self.auto_connect_enabled = event.value
            self.config["auto_connect"] = event.value
            self._log(f"Auto-connect {'enabled' if event.value else 'disabled'}")
        else:
            return
        try:
            save_config(self.config)
        except OSError as exc:
            self._log(f"Config save failed: {exc}")

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
