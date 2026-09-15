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
