"""Interactive console / TUI controller for Frostbay.

Used when a system tray is not available (e.g. inside WSL or a headless box),
or when the user starts the app with ``--no-tray``.

Runs a simple async REPL on top of :class:`frostbay.ble.FrostbayBLE` so the same
control surface as the tray app is available from the keyboard:

    1) Scan for devices
    2) Find & connect (*ONEC1*)
    3) Connect by address
    4) Disconnect
    5) Refresh state
    6) Live dashboard (auto-refresh with graphs)
    7) Turn OFF
    8) Smart: silent / 9) soft / 10) strong
   11) Fixed fan (fan% pump%)
   12) Set pump %
    0) Exit
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .ble import FrostbayBLE, FROSTBAY_NAME_PART, is_frostbay_name
from .history import History
from .protocol import FrostbayState

logger = logging.getLogger("frostbay.console")


def _banner() -> str:
    return (
        "\n=== Frostbay console controller ===\n"
        f"Auto-discovery name substring: *{FROSTBAY_NAME_PART}* (case-insensitive)\n"
    )


def _menu(connected: bool) -> str:
    lines = [
        "1) Scan for devices",
        f"2) Find & connect (*{FROSTBAY_NAME_PART}*)",
        "3) Connect by address",
        "4) Disconnect",
        "5) Refresh state",
        "6) Live dashboard (auto-refresh)",
        "7) Turn OFF",
        "8) Smart: silent",
        "9) Smart: soft",
        "10) Smart: strong",
        "11) Fixed fan (fan% pump%)",
        "12) Set pump %",
        "0) Exit",
    ]
    return "\n".join(lines)


async def _ainput(prompt: str) -> str:
    """Non-blocking input via a worker thread."""
    return await asyncio.to_thread(input, prompt)


def _spark(values: list[float], lo: float | None = None, hi: float | None = None, width: int = 20) -> str:
    """Tiny inline sparkline for the console state block."""
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
        out.append(bars[min(len(bars) - 1, int(frac * len(bars)))])
    pad = width - len(out)
    if pad > 0:
        out = [" "] * pad + out
    return "".join(out[-width:])


def _print_state(s: Optional[FrostbayState], history: Optional[History] = None) -> None:
    if s is None:
        print("  No state available.")
        return
    running = "yes" if s.is_running() else "no"
    print("\n  ┌─────────────────────────────────────────────┐")
    print(f"  │ Status   {running:<6}   Mode  {s.mode.label:<11} proto 0x{s.protocol_version:02X} │")
    print(f"  │ Fan  {s.fan_percent:>3}%   Pump {s.pump_percent:>3}%   Flow {s.flow_ml_min:>6.1f} mL/min │")
    print(f"  │ Temp in  {s.temp_in_c:>3}C    Temp out {s.temp_out_c:>3}C              │")
    if history is not None and history.last() is not None:
        t0, t1 = float(s.temp_in_c), float(s.temp_out_c)
        sp_in = _spark(history.series('temp_in'))
        sp_out = _spark(history.series('temp_out'))
        sp_flow = _spark(history.series('flow'))
        sp_fan = _spark(history.series('fan'), 0, 100)
        sp_pump = _spark(history.series('pump'), 0, 100)
        print("  │                                             │")
        print(f"  │ Temp in  {sp_in}  Temp out {sp_out} │")
        print(f"  │ Flow    {sp_flow}                │")
        print(f"  │ Fan %   {sp_fan}  Pump %  {sp_pump} │")
    print("  └─────────────────────────────────────────────┘")


class FrostbayConsole:
    def __init__(self, address: Optional[str] = None) -> None:
        self._ble = FrostbayBLE(address=address)
        self._address = address
        self._history = History()
        # Feed every read state into the history ring buffer.
        self._ble._on_state = self._on_state

    def _on_state(self, state: FrostbayState) -> None:
        self._history.push(state)

    async def run(self) -> None:
        print(_banner())
        running = True
        while running:
            connected = self._ble.is_connected
            print(f"\n[Status: {'connected' if connected else 'disconnected'}]")
            print(_menu(connected))
            choice = (await _ainput("Choice> ")).strip()
            try:
                running = await self._dispatch(choice)
            except Exception as exc:
                print(f"  Error: {exc}")
                logger.error("Console action failed: %s", exc, exc_info=True)

    async def _dispatch(self, choice: str) -> bool:
        if choice in ("0", "q", "quit", "exit"):
            await self._ble.disconnect()
            print("Bye.")
            return False
        if choice == "1":
            await self._scan()
        elif choice == "2":
            await self._find_and_connect()
        elif choice == "3":
            await self._connect_by_address()
        elif choice == "4":
            await self._disconnect()
        elif choice == "5":
            await self._refresh()
        elif choice == "6":
            await self._live_dashboard()
        elif choice == "7":
            await self._off()
        elif choice in ("8", "9", "10"):
            preset = {"8": "silent", "9": "soft", "10": "strong"}[choice]
            await self._smart(preset)
        elif choice == "11":
            await self._fixed()
        elif choice == "12":
            await self._pump()
        else:
            print("  Unknown choice.")
        return True

    # --- actions ---
    async def _scan(self) -> None:
        print("  Scanning (10s)...")
        results = await self._ble.scan(timeout=10.0)
        if not results:
            print("  No devices found.")
            return
        for i, r in enumerate(results, 1):
            tag = "  *Frostbay*" if r.is_frostbay else ""
            print(f"  {i}) {r.name!r} {r.address} RSSI={r.rssi}{tag}")

    async def _find_and_connect(self) -> None:
        print(f"  Scanning for a device named *{FROSTBAY_NAME_PART}* ...")
        try:
            found = await self._ble.find_and_connect(timeout=10.0)
            self._address = found.address
            print(f"  Connected to {found.name!r} ({found.address})")
            _print_state(self._history.last(), self._history)
        except Exception as exc:
            print(f"  Could not find/connect: {exc}")

    async def _connect_by_address(self) -> None:
        addr = (await _ainput("  Address> ")).strip()
        if not addr:
            print("  No address given.")
            return
        try:
            await self._ble.connect(address=addr)
            self._address = addr
            print(f"  Connected to {addr}")
            await self._refresh()
        except Exception as exc:
            print(f"  Connect failed: {exc}")

    async def _disconnect(self) -> None:
        await self._ble.disconnect()
        print("  Disconnected.")

    async def _refresh(self) -> None:
        if not self._ble.is_connected:
            print("  Not connected.")
            return
        s = await self._ble.read_state()
        _print_state(s, self._history)

    async def _live_dashboard(self) -> None:
        if not self._ble.is_connected:
            print("  Not connected. Use 2) to connect first.")
            return
        from .dashboard import run_dashboard
        print("  Starting live dashboard. Polling the device every 1s.")
        print("  Press Ctrl+C to return to the menu.\n")
        try:
            await self._ble.start_polling(interval=1.0)
            await run_dashboard(self._history, poll_interval=1.0, use_curses=True)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                await self._ble.stop_polling()
            except Exception:
                pass
            print("\n  Returned to menu.")

    async def _off(self) -> None:
        if not self._ble.is_connected:
            print("  Not connected.")
            return
        s = await self._ble.set_off()
        print("  -> OFF")
        _print_state(s, self._history)

    async def _smart(self, preset: str) -> None:
        if not self._ble.is_connected:
            print("  Not connected.")
            return
        s = await self._ble.set_smart(preset)
        print(f"  -> Smart {preset}")
        _print_state(s, self._history)

    async def _fixed(self) -> None:
        if not self._ble.is_connected:
            print("  Not connected.")
            return
        fan = (await _ainput("  Fan % (0-100)> ")).strip()
        pump = (await _ainput("  Pump % (50-100)> ")).strip()
        try:
            s = await self._ble.set_fixed(int(fan), int(pump))
            print(f"  -> Fixed fan {fan}% / pump {pump}%")
            _print_state(s, self._history)
        except ValueError as exc:
            print(f"  Invalid value: {exc}")

    async def _pump(self) -> None:
        if not self._ble.is_connected:
            print("  Not connected.")
            return
        val = (await _ainput("  Pump % (50-100)> ")).strip()
        try:
            s = await self._ble.set_pump(int(val))
            print(f"  -> Pump {val}%")
            _print_state(s, self._history)
        except ValueError as exc:
            print(f"  Invalid value: {exc}")


def run_console(address: Optional[str] = None) -> int:
    """Run the interactive console controller."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    console = FrostbayConsole(address=address)
    try:
        asyncio.run(console.run())
    except (KeyboardInterrupt, EOFError):
        print("\nExit.")
    return 0
