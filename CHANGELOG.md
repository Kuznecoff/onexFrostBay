# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Kuznecoff/onexFrostBay/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/Kuznecoff/onexFrostBay/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Kuznecoff/onexFrostBay/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Kuznecoff/onexFrostBay/compare/5befb3c...v0.2.0
