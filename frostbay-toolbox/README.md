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

## Textual Connection Behavior

The Textual dashboard launched by `./frostbay-toolbox/run.sh` uses the
legacy 0.4.1 protocol path: Bleak with FFE0-scoped discovery.

On Linux it first looks for an already connected BlueZ device with resolved
services and FFE1, and passes that device's adapter path to Bleak.
Otherwise it uses Bleak's normal scan/connect flow. Windows behavior is
unchanged. The FFE0 filter does not limit BlueZ's over-the-air service
discovery.

Connection attempts can take up to four minutes including retries. A timeout
cancels the operation and waits for cleanup before another action runs.
If `GATT Protocol Error: Unlikely Error` (0x0E) persists, try connecting the
device through the Linux Bluetooth settings first. This requires BlueZ to
expose FFE1; the application cannot use an incomplete service tree. Capture
`bluetoothctl info <address>` and
`journalctl -u bluetooth -b --since "5 minutes ago" --no-pager` after a failure
to distinguish discovery/adapter problems from application errors.

## Settings Persistence

The Textual dashboard saves its settings to
`~/.config/frostbay/config.json` (override the location with the
`FROSTBAY_CONFIG` environment variable):

- `deviceUUID` — the address of the last successfully connected device,
  used as the default address on the next launch
- `auto_restart` — Auto-restart on stop switch
- `auto_temp` — Auto temp switch
- `ble_log` — Show BLE errors in log switch
- `thermal_off_c` — Auto temp OFF threshold
- `thermal_resend` — Auto temp resend policy: `1s`, `2s` (default) or
  `on_stop`
- `thermal_stages` — the list of thermal steps (`on_c` + `mode`), so the
  step count, temperatures and modes survive restarts

The address is saved on every successful connect; switch changes are saved
immediately. Missing or corrupt config falls back to the defaults.

## Thermal Stage Editor

The settings panel lets you configure up to 5 auto-temp steps. Each step
panel shows its ON °C input and a mode select (Smart presets plus fixed
fan/pump pairs, including `20-40` and `20-50`, sorted weakest to
strongest with Smart modes first), and a `[X]` delete button
in the top-right corner. The first step is required and cannot be deleted.

While a step is active, the ON command is re-sent according to the
**RESEND** policy selected in settings: every 1 s, every 2 s (default,
fire-and-forget) or the legacy `On stop (wait)` mode which only re-sends
when the device reports it stopped. In the timed modes a pump that stalls
on a low water flow recovers immediately without waiting for a stopped
reading. Commands are executed sequentially under the BLE operation lock:
at most one command is in flight, so a slow device delays the next send
instead of building a queue.

## Dashboard Sparklines

Each dashboard sparkline has a label with the current value and the
min/max over the recorded window. Temp IN/OUT use a fixed 25–50 °C scale
so small variations stay visible; Flow, Fan and Pump auto-scale to their
data.
