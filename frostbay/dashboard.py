"""Live Frostbay dashboard.

A compact real-time view of the device parameters with rolling sparkline
graphs. Two backends are provided:

* ``curses`` (Linux/macOS)  - full-screen live dashboard.
* text fallback (WSL etc.)  - refreshed multi-line block with ascii sparklines.

The dashboard is driven by a :class:`frostbay.history.History` ring buffer that
is fed by the BLE auto-poll loop.
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
from typing import Optional

from .history import History, Sample
from .host import get_host_stats, format_cpu, format_gpu
from .protocol import FrostbayState


# --- sparkline helpers -------------------------------------------------------

# 8 levels of vertical fill, bottom -> top.
_BARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 40, lo: Optional[float] = None, hi: Optional[float] = None) -> str:
    """Render a unicode sparkline for a series of values."""
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
        frac = (v - lo) / span
        frac = max(0.0, min(1.0, frac))
        idx = min(len(_BARS) - 1, int(frac * len(_BARS)))
        out.append(_BARS[idx])
    # pad to width
    pad = width - len(out)
    if pad > 0:
        out = [" " * pad] + out  # type: ignore
    return "".join(out[-width:])


def _box(label: str, value: str, width: int = 44) -> str:
    return f"┌─ {label} {'─' * max(1, width - len(label) - 4)}┐\n│ {value} │\n└{'─' * width}┘"


# --- text dashboard (no curses) ----------------------------------------------

def render_text(h: History, width: int = 72) -> str:
    s = h.last()
    lines = []
    lines.append("=" * width)
    lines.append("  ❄ Frostbay live dashboard".center(width))
    lines.append("=" * width)
    if s is None:
        lines.append("  No samples yet — waiting for the device…")
        return "\n".join(lines)

    status = "● RUNNING" if s.running else "○ stopped"
    lines.append(f"  Status: {status}    Fan: {s.fan_percent:5.0f}%    Pump: {s.pump_percent:5.0f}%")
    cpu = format_cpu(get_host_stats())
    gpu = format_gpu(get_host_stats())
    lines.append(f"  CPU       {cpu}")
    lines.append(f"  GPU       {gpu}")
    lines.append("")
    temp_lo, temp_hi = _auto_range(h.temp_in, h.temp_out, margin=2)
    flow_lo, flow_hi = _auto_range(h.flow, margin=10)
    fan_lo, fan_hi = 0, 100
    lines.append(f"  Temp in   {s.temp_in_c:5.1f} C   {sparkline(h.series('temp_in'), width - 30, temp_lo, temp_hi)}")
    lines.append(f"  Temp out  {s.temp_out_c:5.1f} C   {sparkline(h.series('temp_out'), width - 30, temp_lo, temp_hi)}")
    lines.append(f"  Flow      {s.flow_ml_min:5.1f} mL/min  {sparkline(h.series('flow'), width - 36, flow_lo, flow_hi)}")
    lines.append(f"  Fan %     {s.fan_percent:5.0f}   {sparkline(h.series('fan'), width - 28, fan_lo, fan_hi)}")
    lines.append(f"  Pump %    {s.pump_percent:5.0f}   {sparkline(h.series('pump'), width - 28, 0, 100)}")
    lines.append("=" * width)
    return "\n".join(lines)


def _auto_range(*series, margin: float = 5.0):
    vals = []
    for s in series:
        if hasattr(s, "__iter__"):
            vals.extend(list(s))
    if not vals:
        return 0.0, 1.0
    lo = min(vals) - margin
    hi = max(vals) + margin
    if hi <= lo:
        hi = lo + 1.0
    return float(lo), float(hi)


# --- curses dashboard (Linux/macOS) -----------------------------------------

def _run_curses(h: History, stop_event: asyncio.Event, poll_interval: float) -> None:
    import curses

    def _main(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        while not stop_event.is_set():
            try:
                stdscr.clear()
                h_screen, w_screen = stdscr.getmaxyx()
                title = "❄ Frostbay live dashboard"
                stdscr.addstr(0, max(0, (w_screen - len(title)) // 2), title)
                s = h.last()
                if s is None:
                    stdscr.addstr(2, 2, "No samples yet — waiting for the device…")
                    stdscr.refresh()
                    _sleep(stop_event, poll_interval)
                    continue
                status = "● RUNNING" if s.running else "○ stopped"
                stdscr.addstr(2, 2, f"Status: {status}")
                stdscr.addstr(2, max(2, w_screen - 24), time.strftime("%H:%M:%S"))
                rows = [
                    ("Temp in  ", f"{s.temp_in_c:5.1f} C", h.series('temp_in'), _auto_range(h.temp_in, h.temp_out, margin=2)),
                    ("Temp out ", f"{s.temp_out_c:5.1f} C", h.series('temp_out'), _auto_range(h.temp_in, h.temp_out, margin=2)),
                    ("Flow     ", f"{s.flow_ml_min:5.1f} mL/min", h.series('flow'), _auto_range(h.flow, margin=10)),
                    ("Fan %    ", f"{s.fan_percent:5.0f}", h.series('fan'), (0, 100)),
                    ("Pump %   ", f"{s.pump_percent:5.0f}", h.series('pump'), (0, 100)),
                ]
                row = 4
                for label, val, series, rng in rows:
                    lo, hi = rng
                    stdscr.addstr(row, 2, f"{label} {val}")
                    sp = sparkline(series, max(10, w_screen - 28), lo, hi)
                    stdscr.addstr(row, 26, sp)
                    row += 2
                # Host CPU/GPU telemetry (no sparkline — single live value each).
                cpu = format_cpu(get_host_stats())
                gpu = format_gpu(get_host_stats())
                stdscr.addstr(row, 2, f"CPU       {cpu}")
                row += 1
                stdscr.addstr(row, 2, f"GPU       {gpu}")
                row += 1
                stdscr.addstr(min(h_screen - 2, row + 2), 2, f"1) refresh  2) OFF  3) smart 4) fixed 0) exit  (poll {poll_interval:.1f}s)")
                stdscr.refresh()
                _sleep(stop_event, poll_interval)
            except curses.error:
                _sleep(stop_event, 0.2)
            except Exception:
                _sleep(stop_event, 0.5)

    def _sleep(stop: asyncio.Event, t: float) -> None:
        # cooperative sleep so stop is responsive
        end = time.time() + t
        while time.time() < end and not stop.is_set():
            time.sleep(0.05)

    try:
        curses.wrapper(_main)
    except Exception:
        # fall back to text loop if curses fails (e.g. no tty)
        _run_text_loop(h, stop_event, poll_interval)


def _run_text_loop(h: History, stop_event: asyncio.Event, poll_interval: float) -> None:
    while not stop_event.is_set():
        sys.stdout.write("\x1b[2J\x1b[H")  # ANSI clear
        sys.stdout.write(render_text(h))
        sys.stdout.write("\n")
        sys.stdout.flush()
        _end = time.time() + poll_interval
        while time.time() < _end and not stop_event.is_set():
            time.sleep(0.1)


# --- public API --------------------------------------------------------------

async def run_dashboard(h: History, poll_interval: float = 1.0, use_curses: bool = True) -> None:
    """Run the live dashboard until cancelled.

    Runs the blocking curses/text render loop in a worker thread so the
    asyncio event loop keeps handling BLE I/O.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _thread_target():
        if use_curses:
            try:
                import curses  # noqa: F401
                _run_curses(h, stop, poll_interval)
            except ImportError:
                _run_text_loop(h, stop, poll_interval)
        else:
            _run_text_loop(h, stop, poll_interval)

    await asyncio.to_thread(_thread_target)


def render_once(h: History) -> str:
    """Single text render of the dashboard (for non-interactive use)."""
    return render_text(h)
