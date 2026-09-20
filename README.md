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
- BLE scanning and connections via `bleak` (the legacy 0.4.1 protocol path),
  with retries for connection/initial-read errors
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
  - **Manual settings**: a preset list of fixed fan/pump pairs, e.g.
    `Fan/Pump:20-60 … Fan/Pump:100-100` (each applies both values at once; the
    current one is checked in the menu)
- **Auto-restart on stop** (menu checkbox): when the pump stops on its own while
  the device is in an active mode (Smart or Fixed), the app re-applies that
  mode's settings to wake it back up. To avoid disrupting a fresh start it only
  reacts to a real running→stopped transition and stays silent for a short
  startup grace window (~10 s) after any command, so the pump can spin up
  undisturbed. A deliberate OFF is never restarted. While **Auto temp** is
  enabled it takes over restarts itself, so auto-restart stays idle and no
  duplicate commands are sent.
- **Auto temp mode** (menu checkbox): watches host **CPU/GPU temperature every
  3 s**. When it goes **above 50 °C** and the device reports `stopped`, the app
  sends a start command in **Smart Silent** mode; when it drops **below 45 °C**
  the pump is turned **OFF**. The 45–50 °C band is a hysteresis zone (no
  commands), so the pump never flaps.

## Quick start (one command)

```bash
git clone https://github.com/Kuznecoff/onexFrostBay.git
cd onexFrostBay
./run.sh
```

`run.sh` creates the virtual environment and installs dependencies on first
run, then starts the app. Pass-through arguments work as usual:
`./run.sh --address <MAC>`, `./run.sh --no-tray`, etc.

### Fedora installer

`install.sh` additionally sets up the system for a desktop user:

```bash
./install.sh                # system packages + venv + apps-menu entry, asks about autostart
./install.sh --autostart    # also start Frostbay on login
./install.sh --no-autostart
```

It installs `bluez`/python packages (sudo), creates the venv, adds a
**Frostbay** entry to the applications menu and can enable autostart.
On GNOME also enable the *AppIndicator and KStatusNotifierItem Support*
extension if the tray icon does not appear (KDE works out of the box).

## Terminal Toolbox (Textual TUI)

`frostbay-toolbox/` is a **terminal-only** controller built with
[Textual](https://github.com/Textualize/textual), styled with an orange
theme (rounded orange borders, orange gauges and sparklines). It exposes the
full tray control surface in the terminal — scan / find & connect / connect
by address / disconnect / OFF / Smart (silent/soft/strong) /
**Auto-restart on stop** / **Auto temp** toggles / manual fan-pump presets /
set pump — plus a live dashboard (Fan/Pump progress bars and Temp IN/OUT,
Flow, Fan, Pump sparklines with fixed scales and current/min/max labels), host
CPU/GPU telemetry and an event log.

The toolbox remembers its settings: the connected device address and the
automation switch states (auto-restart, auto temp, BLE error log) are saved
to `~/.config/frostbay/config.json` (override with `FROSTBAY_CONFIG`) and
restored on the next launch.

**Auto temp** in the toolbox supports up to 5 temperature steps: each step
has its own ON °C threshold and mode (Smart or fixed fan/pump, including
`20-40` and `20-50`), edited in the settings panel. The first step is
required; extra steps can be removed with the `[X]` button in the top-right
corner of each panel. The resend policy is configurable in settings:
re-send the ON command every 1 s, every 2 s (default, fire-and-forget,
serialized — no queue builds up), or the legacy wait-for-stop behavior
(re-send immediately when the device reports it stopped). A pump that
stalls on a low water flow recovers without waiting for a stopped reading
in the timed modes. The step count, temperatures and modes are persisted
to the config. The left-menu manual preset list is trimmed to `20-60`,
`30-70`, `50-80`, `100-100`.

Run it directly:

```bash
cd frostbay-toolbox
./run.sh
```

### Toolbox install scripts

The toolbox ships its own lifecycle scripts (Linux; `dnf` or `apt-get`):

```bash
cd frostbay-toolbox

./install.sh                 # system packages + venv + launcher + apps-menu entry
./install-autostart.sh       # the above + autostart on login
./disable-autostart.sh       # disable autostart (keeps the app installed)
./uninstall.sh               # remove launcher + menu entry + autostart
./uninstall.sh --purge       # also delete the virtual environment
```

`install.sh` puts a `frostbay-toolbox` launcher in `~/.local/bin` (make sure
it is on `PATH`) and adds an applications-menu entry that opens the TUI in
your default terminal. The virtual environment is shared with the main tray
app, so `uninstall.sh` keeps it unless you pass `--purge`.

## Install (manual)

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

# Print the application version
python -m frostbay --version
```

## Console mode (no system tray)

When the tray backend cannot initialize at all (e.g. `pystray`/`Pillow` are not
installed, or there is no display backend whatsoever), the app falls back
automatically to an interactive console controller. You can also force it
explicitly:

```bash
python -m frostbay --no-tray
```

It exposes the same control surface via a numeric menu: scan, find &
connect (`*ONEC1*`), connect by address, disconnect, refresh state, OFF, smart
presets, fixed fan, and pump control.

> **Important on Linux/X11.** The automatic fallback only covers the case where
> the tray backend fails to start. If a display *is* present but no system-tray
> host is running (no `StatusNotifierItem`/systray owner — common on minimal
> window managers and bare X sessions), `pystray` does **not** raise: it keeps
> retrying and logs `Failed to dock icon` on every attempt, so the tray icon
> never appears and the app does **not** switch to console on its own. In that
> situation run with `--no-tray` explicitly, or install a tray host (see
> [Troubleshooting](#troubleshooting)).

## Tray menu

- **Status**: current connection state
- **State block**: mode, running, fan %, flow, pump %, temperatures
- **CPU / GPU lines**: host temperature (or CPU load where sensors are
  unavailable, e.g. stock macOS). hwmon sensors are classified by chip name and
  read directly from `/sys/class/hwmon` as a fallback when `psutil` exposes
  nothing usable (e.g. AMD `k10temp` on Strix Halo / Zen 5)
- **Scan for devices...** — perform a BLE scan
- **Connect** — connect to the first found device or the given address
- **Disconnect**
- **Refresh state** — re-read the `FFE1` state blob
- **Live dashboard…** — real-time graphs (when connected)
- **Turn OFF**
- **Smart: Silent / Soft / Strong**
- **Auto-restart on stop** — checkbox, see [Features](#features)
- **Auto temp >50°C / <45°C** — checkbox, see [Features](#features)
- **Manual settings** — preset list:
  `Fan/Pump:20-60`, `20-70`, `20-80`, `30-60`, `30-70`, `30-80`,
  `40-70`, `40-80`, `40-90`, `100-100` (✓ marks the currently active pair)
- **Exit**

## Project layout

```
onexFrostBay/
├── specification.md          # Frostbay BLE protocol reference
├── CHANGELOG.md              # release history (Keep a Changelog)
├── requirements.txt
├── README.md
├── run.sh                    # one-command launcher (venv + deps + run)
├── install.sh                # Fedora installer (packages, menu entry, autostart)
├── .gitignore
├── frostbay-toolbox/         # terminal-only Textual TUI controller
│   ├── run.sh                # launches the Textual TUI
│   ├── textual_app.py        # Textual app (orange theme, dashboard, controls)
│   ├── main.py               # legacy curses TUI (still runnable directly)
│   ├── install.sh            # toolbox installer (packages, venv, launcher, menu)
│   ├── install-autostart.sh  # install + autostart on login
│   ├── disable-autostart.sh  # disable autostart
│   └── uninstall.sh          # remove launcher/menu/autostart (--purge venv)
└── frostbay/
    ├── __init__.py
    ├── __main__.py           # entry point for `python -m frostbay`
    ├── protocol.py           # FFE1 state blob parser + command builders
    ├── ble.py                # bleak-based cross-platform BLE client + auto-polling
    ├── config.py             # JSON settings (~/.config/frostbay/config.json)
    ├── history.py            # ring-buffer telemetry history for graphs
    ├── host.py               # host CPU/GPU temperature & load telemetry
    ├── dashboard.py          # live curses/text dashboard with sparkline graphs
    ├── console.py            # interactive console controller (WSL / no-tray)
    ├── icons.py              # PIL tray-icon rendering (per-state color)
    └── app.py                # pystray tray menu + asyncio bridge + auto-control
```

## Implementation notes

- GATT access goes through `BleakTransport` (`frostbay/transports.py`), the
  legacy 0.4.1 protocol path: a fresh `BleakClient` connect scoped to the
  `FFE0` primary service (WinRT / CoreBluetooth / BlueZ). On Linux it prefers
  an already connected + `ServicesResolved` BlueZ device exposing `FFE1`,
  preserving its adapter and avoiding an implicit scan.
- The `FFE1` characteristic is read as a 64-byte state blob and parsed per
  `specification.md`.
- Writes are never a single 64-byte write; the protocol splits the patched
  payload into three 20-byte chunks prefixed with `0x1C`, `0x2C`, `0x3C`,
  sent with ~20 ms gaps. The write mode is chosen from the characteristic's
  advertised properties at connect time: *Write With Response* when the device
  only declares `write` (macOS CoreBluetooth silently drops `write-command`
  there), otherwise *Write Without Response*. If a write fails, the client
  retries the other mode and keeps whichever worked.
- Pump speed is clamped to the supported `50..100` range; the recommended
  practical range is `80..100`.
- The **Manual settings** presets and both automation checkboxes (auto-restart,
  auto temp) all go through the same patched-state writes as the other commands.
- The tray icon runs on the main thread (required by `pystray`), while BLE I/O
  runs on a background asyncio loop. Actions are dispatched to that loop via
  `asyncio.run_coroutine_threadsafe`.

## Troubleshooting

### `Failed to dock icon` / `assert self._systray_manager` (Linux)

The app started but no tray icon appears, and the log repeats
`ERROR pystray._base: Failed to dock icon` with an `AssertionError` from
`pystray/_xorg.py`. This means there is **no system-tray host** (no
`StatusNotifierItem` / systray selection owner) on the current display. The
`pystray` X11 backend catches this internally and keeps retrying, so the app
does not fall back to console on its own.

Fix by either:

- running in console mode: `python -m frostbay --no-tray` (or `./run.sh --no-tray`), or
- providing a tray host:
  - **GNOME**: enable the *AppIndicator and KStatusNotifierItem Support*
    extension (`gnome-shell-extension-appindicator`, installed by `install.sh`),
    then restart GNOME Shell.
  - **KDE Plasma**: the tray is native — make sure the system tray is shown.
  - **Minimal WMs / bare X sessions**: install a StatusNotifier host, e.g.
    `sudo dnf install snix-embed` and run `snix-embed python -m frostbay`,
    or use a desktop session that provides a tray.

### `No powered Bluetooth adapters found`

`Auto-connect failed: 'No powered Bluetooth adapters found. Turn on Bluetooth
and try again.'` — the machine has no usable/powered Bluetooth adapter.

- Turn Bluetooth on (system menu, or `rfkill unblock bluetooth`).
- Check adapters: `bluetoothctl list` (should show at least one powered adapter;
  power it with `power on`).
- On a desktop without built-in Bluetooth, plug in a USB BLE dongle.
- The app keeps running with a red/orange icon; once the adapter is available,
  use **Scan for devices…** / **Reconnect** in the tray (or menu `2)` in console
  mode) to connect.

### `BleakGATTProtocolErrorCode.UNLIKELY_ERROR: 14` / connects then drops instantly (Linux)

ATT error `0x0E` ("Unlikely Error") reports a failed GATT operation; the
message alone does not identify whether firmware, controller/driver behavior,
or stale discovery state caused it. `Service Discovery has not been performed
yet` can follow when that session is lost. A successful Windows connection
does not validate the Linux controller/BlueZ path. On Linux the app prefers
an already connected + `ServicesResolved` BlueZ device exposing `FFE1`
(see `frostbay/transports.py`), preserving its adapter and avoiding an
implicit scan. Bleak on Linux uses BlueZ and the same BLE/GATT protocol;
it cannot bypass a broken controller or force missing services to exist.
An existing ready session is reused; a disconnected device still requires
BlueZ connection/discovery. A successful initial state read is required
before reporting connection success.

If you still hit `0x0E`:

1. **Let the system own the session first.** Pair and connect the device in
   the desktop Bluetooth UI (GNOME/KDE) or `bluetoothctl connect <MAC>`,
   so BlueZ reports `Connected=true` + `ServicesResolved=true`. The app then
   attaches without re-triggering discovery.
2. **Consider clearing a stale bond/cache** after collecting diagnostics:
  `bluetoothctl remove <MAC>`, then scan and reconnect/re-pair. This removes
  the Linux pairing for that device. Restarting Bluetooth, if needed, affects
  all Bluetooth connections; do not remove other devices or the whole cache.
3. **Use the adapter that exposes the full GATT tree.** The built-in `hci0`
   path was observed broken (UUID list collapses to battery/HID); an external
   USB BLE adapter (`hci1`) exposes the correct Frostbay tree. Check with
   `bluetoothctl list`.
4. **Neutralize the bogus HID** (`1812`) volume-key side effect via the
   `hwdb` override in `specification.md` (does not affect the GATT path).

If the app reports a timeout waiting for a connected, services-resolved
device exposing `FFE1`, collect the actual BlueZ state while the device is
connected:

```bash
bluetoothctl list
bluetoothctl info <MAC>
busctl tree org.bluez
journalctl -b -u bluetooth --no-pager -n 100
```

Look for the FFE0 UUID and a GATT characteristic whose UUID is FFE1;
`ServicesResolved` alone is insufficient. If only a partial battery/HID
tree appears, see the adapter-specific findings in
[`specification.md`](specification.md).

### Regression tests

With dependencies installed, run from the repository root (no BLE hardware
or desktop session required):

```bash
PYSTRAY_BACKEND=dummy .venv/bin/python -W error::RuntimeWarning -m unittest discover -s tests -v
```

### Tray icon not visible on GNOME

Enable the *AppIndicator and KStatusNotifierItem Support* extension (see above).
KDE shows the tray out of the box.
