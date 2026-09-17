# Frostbay terminal TUI

This is the Linux-first, terminal-only version of Frostbay.

It intentionally avoids any tray or desktop integration and uses the Python standard library `curses` module for a keyboard- and mouse-driven TUI.

## Launch

From the repository root:

```bash
python frostbay-toolbox/main.py
```

Or:

```bash
cd frostbay-toolbox
python main.py
```

## Features

- No `pystray` / tray / app-indicator dependency
- Terminal-only usage for headless or heavily loaded Linux systems
- Mouse support in the menu: click an action to trigger it
- Keyboard navigation: arrow keys, `j`/`k`, Enter, `q`
- BLE connect / scan / read / command interface for the Frostbay device

## Notes

This app reuses the core Frostbay BLE protocol implementation from the main project, but it does not depend on any system tray backend.

## Textual Connection Modes

The Textual dashboard launched by `./frostbay-toolbox/run.sh` has a
**Legacy 0.4.1 (Bleak)** switch in **CONNECTION**:

- Off (default): automatic transport selection, direct BlueZ first on Linux
	with Bleak as fallback.
- On: Bleak only, with FFE0-scoped discovery as in version 0.4.1.

Changing modes disconnects the current session and clears its telemetry.
Use **Connect** or **Find & connect** afterwards. The selection lasts for the
current application session. To start in legacy mode:

```bash
./frostbay-toolbox/run.sh --legacy-041
```

The Frostbay command format is unchanged from 0.4.1; this option selects the
older connection backend, retaining the current connection checks and fixes.

On Linux, legacy mode first looks for an already connected BlueZ device with
resolved services and FFE1, and passes that device's adapter path to Bleak.
Otherwise it uses Bleak's normal scan/connect flow. Reads and writes remain
on Bleak in both cases; Windows behavior is unchanged. The FFE0 filter does
not limit BlueZ's over-the-air service discovery.

Connection attempts can take up to four minutes including retries. A timeout
cancels the operation and waits for cleanup before another action runs.
If `GATT Protocol Error: Unlikely Error` (0x0E) persists, try connecting the
device through the Linux Bluetooth settings before starting legacy mode.
This requires BlueZ to expose FFE1; the application cannot use an incomplete
service tree. Capture `bluetoothctl info <address>` and
`journalctl -u bluetooth -b --since "5 minutes ago" --no-pager` after a failure
to distinguish discovery/adapter problems from application errors.
