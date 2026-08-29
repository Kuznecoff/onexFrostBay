# onexFrostBay

Cross-platform system-tray application to test and control the **Frostbay** BLE
device described in [`specification.md`](specification.md).

It connects over Bluetooth Low Energy (service `FFE0`, characteristic `FFE1`),
shows the device connection status with a **colored tray icon** (green =
connected, red = disconnected, grey = idle, orange = error), reads the current
operating parameters, and sends control commands.

Tested targets: **Windows 10/11**, **macOS**, **Fedora Linux**.

---

## Features

- Colored tray icon reflecting connection state:
  - 🟢 green  — connected
  - 🔴 red    — disconnected / connection error
  - ⚪ grey   — idle / scanning
  - 🟠 orange — error
- BLE scan and connect (via `bleak`, works on Windows/macOS/Fedora)
- **Automatic device discovery** by advertised name substring `ONEC1`
  (e.g. `CoolingSystem_ONEC1`). On startup the app scans for a device whose
  name contains `ONEC1` (case-insensitive) and connects to the first match.
  You can still pass `--address <MAC>` to skip discovery.
- Reads the current Frostbay state from `FFE1`:
  - mode (OFF / Smart Fan / Fixed Fan)
  - runtime running indicator (`state[12]`)
  - fan percentage
  - flow rate (mL/min, decoded from `state[6..7]`)
  - pump percentage
  - input / output water temperatures
- **Live auto-refresh**: after connecting, the device is polled automatically
  (default every 2 s) and the dashboard/menu reflects the latest parameters
  without manual refresh. Polling is also started while the **Live dashboard**
  view is open (every 1 s).
- **Live dashboard with graphs**: a rolling real-time view of temperatures,
  flow rate, fan % and pump % with unicode sparkline time-series. Available
  both from the tray menu (`Live dashboard…`) and the console menu (`6)`).
- Pretty state block with inline sparklines in both the tray tooltip and the
  console status line, so you can see trends at a glance.
- Sends control commands using the verified 3-chunk `1C/2C/3C` transport:
  - **Turn OFF**
  - **Smart Fan**: `silent`, `soft`, `strong` presets
  - **Fixed Fan** with custom fan % and pump %
  - **Pump speed** control (clamped to the supported `50..100` range)

## Install

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### Platform notes

- **Windows**: no extra system packages needed; `bleak` uses WinRT.
- **macOS**: no extra system packages needed; `bleak` uses CoreBluetooth.
- **Fedora / Linux**: `bleak` uses BlueZ over D-Bus. Install:
  ```bash
  sudo dnf install python3-dbus python3-gobject bluez
  ```
  You may need to run with a Bluetooth adapter that exposes the full Frostbay
  GATT tree (see `specification.md` notes about adapter choice).

## Run

```bash
# Auto-discover and connect to the device whose name contains "ONEC1"
python -m frostbay

# Or specify a known BLE address (skips discovery)
python -m frostbay --address <BLE-MAC>

# Disable startup auto-connect; use the tray menu Scan/Reconnect instead
python -m frostbay --no-auto

# Verbose logging
python -m frostbay --debug
```
## Console mode (no system tray)

On systems without a system-tray host (e.g. **WSL**, headless servers), the app
falls back automatically to an interactive console controller. You can also
force it explicitly:

```bash
python -m frostbay --no-tray
```

It exposes the same control surface via a numeric menu: scan, find &
connect (`*ONEC1*`), connect by address, disconnect, refresh state, OFF, smart
presets, fixed fan, and pump control.
## Tray menu

- **Status**: current connection state
- **State block**: mode, running, fan %, flow, pump %, temperatures
- **Scan for devices...** — perform a BLE scan
- **Connect** — connect to the first found device or the given address
- **Disconnect**
- **Refresh state** — re-read the `FFE1` state blob
- **Turn OFF**
- **Smart: Silent / Soft / Strong**
- **Fixed Fan 50% / Pump 80%**
- **Fixed Fan 100% / Pump 100%**
- **Pump → 80% / 100%**
- **Exit**

## Project layout

```
onexFrostBay/
├── specification.md          # Frostbay BLE protocol reference
├── requirements.txt
├── README.md
├── .gitignore
└── frostbay/
    ├── __init__.py
    ├── __main__.py           # entry point for `python -m frostbay`
    ├── protocol.py           # FFE1 state blob parser + command builders
    ├── ble.py                # bleak-based cross-platform BLE client + auto-polling
    ├── history.py            # ring-buffer telemetry history for graphs
    ├── dashboard.py          # live curses/text dashboard with sparkline graphs
    ├── console.py            # interactive console controller (WSL / no-tray)
    ├── icons.py              # PIL tray-icon rendering (per-state color)
    └── app.py                # pystray tray menu + asyncio bridge
```

## Implementation notes

- The `FFE1` characteristic is read as a 64-byte state blob and parsed per
  `specification.md`.
- Writes are never a single 64-byte write; the protocol splits the patched
  payload into three 20-byte chunks prefixed with `0x1C`, `0x2C`, `0x3C`,
  sent with ~20 ms gaps, using *Write Without Response*.
- Pump speed is clamped to the supported `50..100` range; the recommended
  practical range is `80..100`.
- The tray icon runs on the main thread (required by `pystray`), while BLE I/O
  runs on a background asyncio loop. Actions are dispatched to that loop via
  `asyncio.run_coroutine_threadsafe`.
