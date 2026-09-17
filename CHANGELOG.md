# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.3] - 2026-09-17

### Fixed

- Linux legacy Bleak connections prefer an already connected, resolved BlueZ
  device exposing FFE1, preserving its adapter and avoiding an implicit scan.
  Without a ready session, the normal Bleak connection path remains in use.
- Toolbox allows up to 240 seconds for discovery and connection retries instead
  of abandoning them after 25 seconds. Timed-out operations are cancelled and
  awaited; cancelled connection attempts clean up their transport before exit.
- Detect a disconnect after Bleak connect before accessing its service cache.

### Notes

- Windows connection behavior and the device command format are unchanged.
  Physical-device validation is still required for Linux ATT error 0x0E.

## [0.5.2] - 2026-09-17

### Added

- Toolbox connection switch **Legacy 0.4.1 (Bleak)** and `--legacy-041`
  launch option. Legacy mode forces Bleak with FFE0-scoped discovery;
  automatic transport selection remains the default. Switching modes
  disconnects the current session and clears its telemetry.
- Headless Toolbox tests for mode selection, switching, and the launch flag,
  plus regression coverage for the legacy Bleak transport.

### Fixed

- A disconnect during notification setup now fails the connection attempt
  and triggers cleanup/retry instead of reporting a successful connection.
- Toolbox uses a separate BLE event-loop attribute so Textual cannot
  overwrite it with the UI loop and cause BLE command timeouts.
- Toolbox serializes BLE actions and background polling during mode changes.

### Notes

- The device command format is unchanged. Physical-device validation is
  still required for both connection modes.

## [0.5.1] - 2026-09-17

### Fixed

- **Linux BlueZ connections no longer always fail with FFE1 not found.**
  The 0.5.0 characteristic resolver was an async function called without
  awaiting it, so the characteristic map stayed empty even for a working
  adapter. It is now synchronous, matching its work and call site.
- BlueZ readiness now requires both an active, services-resolved connection
  and an actual FFE1 characteristic. Delayed characteristic publication is
  retried within the discovery timeout. When multiple adapters already see
  the device, a connected, resolved instance exposing FFE1 is preferred.
- BlueZ disconnect/service-loss signals invalidate connection state and cached
  characteristics. Property-read errors are reported instead of being mistaken
  for truthy connection flags.
- A failed initial state read now fails the connection attempt and enters the
  existing cleanup/retry path, instead of reporting a successful connection
  without telemetry.

### Notes

- Linux retains direct BlueZ D-Bus as the preferred transport and Bleak as a
  fallback. Windows/macOS retain Bleak. The Frostbay GATT UUIDs and command
  format are unchanged.
- Added mocked D-Bus and connection regression tests. Physical CachyOS/device
  validation is still required; this does not claim to fix controller/firmware
  causes of ATT error 0x0E.

## [0.5.0] - 2026-09-16

### Added

- **Direct BlueZ D-Bus transport on Linux.** GATT access is now behind a small
  transport abstraction (`frostbay/transports.py`): a `BleakTransport`
  (WinRT / CoreBluetooth / BlueZ) and a `BluezDbusTransport` that *attaches*
  to an already connected + `ServicesResolved` BlueZ device and drives `FFE1`
  with direct `ReadValue` / `WriteValue`, per the model in `specification.md`.
  On Linux the D-Bus transport is tried first (it avoids the fresh connect +
  full ATT discovery that the Frostbay firmware can reject with an
  `Unlikely Error` (0x0E) and drop the link), then falls back to bleak. A
  stale half-open link (connected but not resolved) is cleared before
  attaching. Backend can be forced with
  `FrostbayBLE(..., prefer_transport="bleak"|"bluez")`.

## [0.4.1] - 2026-09-15

### Fixed

- **Auto-restart no longer disrupts a fresh pump start.** The read-back inside a
  just-sent Smart/Fixed command reported the pump as still stopped (it had not
  spun up yet), which immediately triggered a re-apply that landed mid-startup
  and kept the device from ever reaching a running state. Auto-restart now uses
  two guards: a **startup grace window** (~10 s after any active-mode command,
  during which it stays silent so the pump can spin up) and a
  **running→stopped edge trigger** (it only restarts a pump that was actually
  observed running, leaving a freshly written mode alone until it comes up).
  Applied consistently to the tray, curses and Textual interfaces.
- **BLE connect now retries transient failures.** `connect()` attempts up to 3
  times and scopes service discovery to the Frostbay `FFE0` primary service, so
  a momentary BlueZ ATT "Unlikely Error" (0x0E) during enumeration no longer
  drops the link as a hard "failed to discover services".
- **Host CPU/GPU temperature is read reliably on more platforms.** hwmon sensors
  are classified by chip name (not just the entry label), covering AMD `k10temp`
  (`Tctl`/`Tcase`/`Tsi`) on Strix Halo / Zen 5, and a direct `/sys/class/hwmon`
  fallback surfaces temperatures when `psutil` returns an empty sensor map.

## [0.4.0] - 2026-09-15

### Added

- **Frostbay Toolbox** (`frostbay-toolbox/`): a terminal-only controller built
  with [Textual](https://github.com/Textualize/textual), styled with an
  orange theme (rounded orange borders, orange gauges and sparklines). It
  exposes the full tray control surface in the terminal — scan / find &
  connect / connect by address / disconnect / refresh / OFF / Smart
  (silent/soft/strong) / **Auto-restart on stop** / **Auto temp** toggles /
  manual fan-pump presets / set pump — plus a live dashboard (Fan/Pump
  progress bars, Temp IN/OUT, Flow, Fan, Pump sparklines), host CPU/GPU
  telemetry and an event log.
- Toolbox lifecycle scripts:
  - `frostbay-toolbox/install.sh` — system packages, venv + deps, a
    `frostbay-toolbox` launcher in `~/.local/bin`, and an applications-menu
    entry that opens the TUI in a terminal (`--autostart` also enables login
    autostart).
  - `frostbay-toolbox/install-autostart.sh` — install and enable autostart.
  - `frostbay-toolbox/disable-autostart.sh` — disable autostart (keeps the
    app installed).
  - `frostbay-toolbox/uninstall.sh` — remove launcher, menu entry and
    autostart (`--purge` also deletes the virtual environment).
- `textual` added to `requirements.txt`.

### Notes

- The previous curses-based terminal app is retained as
  `frostbay-toolbox/main.py` and can still be run directly; `run.sh` now
  launches the Textual app.

## [0.3.1] - 2026-09-07

### Changed

- **Manual settings** menu is now a flat list of fan/pump presets
  (`Fan/Pump:20-60` … `Fan/Pump:40-90`) instead of two nested speed submenus.

### Fixed

- **Auto-restart** now works in any active mode (Smart or Fixed), not only in
  Fixed: a stopped device is restarted with the settings of its current mode
  (matching smart preset for Smart, fan/pump pair for Fixed). A deliberate OFF
  is still never restarted. While **Auto temp** is enabled it handles restarts
  itself, so auto-restart stays idle and no duplicate commands are sent.
- **Auto-restart commands no longer pile up in a queue.** At most one restart
  command is in flight: while the previous attempt is still running (waiting on
  the BLE GATT lock behind polling reads and notification refreshes), further
  "stopped" detections are ignored. The cooldown now re-arms when the command
  *completes* instead of when it is queued, so a slow device response cannot
  build up a burst of delayed commands. Turning **Auto-restart** off also
  cancels any queued restart.

## [0.3.0] - 2026-09-07

### Added

- **Thermal auto-control mode** (`Auto temp >50°C / <45°C` tray-menu checkbox):
  every 3 s the app reads host CPU/GPU temperature. Above 50 °C, the device
  state is polled and, when it reports `stopped`, a start command is sent in
  **Smart Silent** mode. Below 45 °C a running pump is turned **OFF**. The
  45–50 °C band is a hysteresis zone, so no commands are sent and the pump
  never flaps.

## [0.2.0] - 2026-09-06

### Added

- One-command launcher `run.sh` (creates venv, installs dependencies, starts
  the app) and Fedora installer `install.sh` (system packages, apps-menu
  entry, optional autostart).
- Host **CPU/GPU telemetry** shown in the tray menu alongside device state.
- `--version` / `-V` flag reporting the application version.

### Fixed

- Tray menu width overflow on Windows/Linux: status is split into several
  short menu lines instead of one long line.

## [0.1.0] - 2026-08-29

### Added

- Initial release: cross-platform (Windows / macOS / Fedora) system-tray app
  for the Frostbay BLE cooling device (`FFE0`/`FFE1`).
- Colored tray icon reflecting connection state (green/red/grey/orange).
- BLE scan/connect with automatic device discovery by name substring `ONEC1`.
- Live state reading: mode, running indicator, fan %, flow rate, pump %,
  input/output water temperatures.
- Control commands via the verified 3-chunk `1C/2C/3C` transport: OFF,
  Smart Fan (silent/soft/strong), Fixed Fan, pump speed.
- Live auto-refresh polling and a live dashboard with unicode sparkline graphs.
- Console controller (`--no-tray`) for headless environments.

[Unreleased]: https://github.com/Kuznecoff/onexFrostBay/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/Kuznecoff/onexFrostBay/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/Kuznecoff/onexFrostBay/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Kuznecoff/onexFrostBay/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Kuznecoff/onexFrostBay/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Kuznecoff/onexFrostBay/compare/5befb3c...v0.2.0
