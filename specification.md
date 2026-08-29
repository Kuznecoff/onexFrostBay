# Apex Frostbay BLE Protocol

This document describes the verified Frostbay Bluetooth control protocol as a standalone developer reference.

It is intended to be sufficient to implement:

- ON
- OFF
- Smart Fan
- Smart Fan `silent`, `soft`, `strong`
- Fixed fan mode with percentage control
- Pump speed control in the `50..100` range, with `80..100` recommended

---

## Discovery Notes

The control protocol below is centered on `FFE1`, but successful Windows pair/connect captures add a few important discovery facts that are relevant to any implementation.

Verified Windows behavior:

- Primary services discovered include `1800`, `1801`, `180A`, `FFE0`, and `FFF0`.
- Under `FFE0`, Windows discovers `FFE1` at declaration handle `0x0040` and value handle `0x0041`.
- Windows reads `FFE1` as a long attribute: one ATT `Read Request`, then ATT `Read Blob Request` at offsets `0x0016` and `0x002C`.
- No BLE-side unlock write was observed before `FFE1` became readable.
- `1801` / `2A05` Service Changed is present, so cache invalidation and rediscovery may matter on non-Windows stacks.

The practical implication is that `FFE1` visibility should be treated as a normal GATT discovery and long-read problem, not as evidence that a proprietary pre-read write is required.

---

## Linux BlueZ Execution Model

Current Linux findings on a working BlueZ adapter are:

- Frostbay control does not require a plugin-managed pair or connect sequence once the OS already owns a valid session.
- The practical prerequisite is a ready BlueZ device object, not a proprietary Frostbay-side handshake.
- On Linux, the cleanest model is to wait until BlueZ reports:
	- `org.bluez.Device1.Connected = true`
	- `org.bluez.Device1.ServicesResolved = true`
	- `FFE0` in the device UUID list
	- an `org.bluez.GattCharacteristic1` object for `FFE1`
- Once that state exists, direct D-Bus `ReadValue` and `WriteValue` on the resolved `FFE1` object work correctly, including while GNOME owns the Bluetooth session.

Example BlueZ object paths on a working adapter look like:

```text
/org/bluez/hci1/dev_C8_17_17_F5_C8_93
/org/bluez/hci1/dev_C8_17_17_F5_C8_93/service003f/char0040
```

In other words: treat BlueZ as the transport owner and Frostbay as a GATT state blob exposed through that owner. Do not assume a second user-space GATT client must create a fresh connection just to read or write `FFE1`.

### Adapter-specific Linux notes

Observed adapter behavior on the Apex host was not equivalent across controllers.

#### Built-in Apex Bluetooth adapter (`hci0`)

The built-in adapter path was not reliable enough to treat as a valid Frostbay transport path.

Observed failure pattern:

- BlueZ could report the device as connected.
- BlueZ could sometimes report `ServicesResolved = true`.
- The managed object tree still failed to expose the expected Frostbay GATT subtree.
- In the broken state, the UUID list could collapse to a partial set such as only battery and HID-related services instead of the full Frostbay service set.
- Earlier privileged tracing on this path showed no normal ATT discovery sequence for Frostbay, which points at a controller or BlueZ path failure before ordinary GATT discovery completed.

Practical implication:

- Do not treat the built-in `hci0` path as authoritative if Frostbay fails to expose `FFE0` / `FFE1` there.
- If an external adapter exposes the full GATT tree and `FFE1`, prefer that adapter and bind Frostbay control to that working BlueZ object path.

#### External adapter (`hci1`) and HID side effects

The external adapter successfully exposed the full Frostbay GATT tree and allowed stable direct D-Bus access to `FFE1`.

However, the same connection also exposed HID service `1812`, and BlueZ created a UHID input device for Frostbay.

Observed behavior:

- BlueZ created an input node named `CoolingSystem_ONEC1`.
- The HID report map advertised a Consumer Control device containing only usages `0xE9` and `0xEA`, which correspond to Volume Up and Volume Down.
- In practice this could manifest as repeated volume key events while Frostbay was connected.

The report map read from the working adapter was:

```text
050c0901a1018501150025017508950109e9810609ea8106c0
```

That decodes as a Consumer Control application collection with only Volume Up and Volume Down inputs.

Recommended mitigation on Linux:

- Keep using the external adapter for Frostbay GATT control.
- Neutralize the bogus HID consumer usages through a targeted hwdb override instead of disabling Frostbay GATT access.

Example host-side hwdb override:

```text
evdev:input:b0005v07D7p0000*
	KEYBOARD_KEY_c00e9=reserved
	KEYBOARD_KEY_c00ea=reserved
```

Then rebuild and apply hwdb on the host, for example:

```bash
sudo systemd-hwdb update
sudo udevadm trigger /sys/class/input/event25
```

If the live trigger is not enough, disconnect and reconnect Frostbay once.

This mitigation only neutralizes the spurious volume-key HID path; it does not change the Frostbay GATT control path through `FFE1`.

---

## GATT Control Surface

Frostbay control is centered on the `FFE0` primary service:

| UUID | Role | Verified Windows mapping |
|---|---|---|
| `0000ffe0-0000-1000-8000-00805f9b34fb` | Primary Frostbay service | Service range `0x003F..0x004F` |
| `0000ffe1-0000-1000-8000-00805f9b34fb` | State and control | Declaration `0x0040`, value `0x0041` |
| `0000ffe2-0000-1000-8000-00805f9b34fb` | Read-only side characteristic | Declaration `0x0043`, value `0x0044` |
| `0000ffe3-0000-1000-8000-00805f9b34fb` | Command-side characteristic | Declaration `0x0046`, value `0x0047` |
| `0000ffe4-0000-1000-8000-00805f9b34fb` | Notify characteristic | Declaration `0x0049`, value `0x004A` |
| `0000ffe5-0000-1000-8000-00805f9b34fb` | Read-only side characteristic | Declaration `0x004D`, value `0x004E` |

Windows also discovers a secondary Frostbay service:

| UUID | Role | Verified Windows mapping |
|---|---|---|
| `0000fff0-0000-1000-8000-00805f9b34fb` | Secondary Frostbay service | Service range `0x0050..0x0060` |
| `0000fff1-0000-1000-8000-00805f9b34fb` | Secondary state-like characteristic | Declaration `0x0051`, value `0x0052` |
| `0000fff2-0000-1000-8000-00805f9b34fb` | Secondary read-only characteristic | Declaration `0x0054`, value `0x0055` |
| `0000fff3-0000-1000-8000-00805f9b34fb` | Secondary command characteristic | Declaration `0x0057`, value `0x0058` |
| `0000fff4-0000-1000-8000-00805f9b34fb` | Secondary notify characteristic | Declaration `0x005A`, value `0x005B` |
| `0000fff5-0000-1000-8000-00805f9b34fb` | Secondary read-only characteristic | Declaration `0x005E`, value `0x005F` |

No prerequisite unlock write is required.

---

## High-Level Model

Frostbay stores its current operating state internally.

- `OFF` is a stored state, not a disconnect event.
- `ON` is not a separate command family; the device is turned on by writing any non-off cooling state.
- Smart Fan and Fixed Fan are the two primary active cooling families.
- Pump speed is a separate writable field in the state blob.

The practical control model is:

1. Wait for BlueZ to expose a connected, services-resolved `FFE1` characteristic.
2. Read the current `FFE1` state blob.
3. Patch only the control bytes for the requested state.
4. Write the updated state back using the chunked `FFE1` transport.

This preserves unknown status, sensor, and device-specific bytes.

---

## Read Format

A read of `FFE1` returns a 64-byte state blob.

In successful Windows captures, the device is read as a long attribute:

1. ATT `Read Request` on handle `0x0041`
2. ATT `Read Blob Request` on handle `0x0041`, offset `0x0016`
3. ATT `Read Blob Request` on handle `0x0041`, offset `0x002C`

Implementations on Linux or other stacks should be prepared to continue long-read behavior if the first read does not return the full value in one response.

Verified readback bytes from live Windows and Linux observations:

```text
state[2]      protocol / frame version
state[4]      requested mode byte
state[5]      fan byte
state[6..7]   raw flow word, big-endian, tenths of mL/min
state[8]      pump byte
state[12]     runtime activity byte
state[13]     water temperature in
state[14]     water temperature out
state[23..40] smart preset curve bytes
```

Readback meanings:

| Byte | Meaning |
|------|---------|
| `state[2]` | protocol / frame version; currently observed as `0x06` |
| `state[4]` | requested mode family; this reflects the last commanded family, not always the current physical running state |
| `state[5]` | fixed-fan percentage, or smart baseline value |
| `state[6..7]` | raw flow word; live Linux reads indicate this is big-endian and currently scales as tenths of mL/min |
| `state[8]` | pump percentage |
| `state[12]` | runtime activity indicator; observed nonzero while Frostbay is actively running and `0x00` in a stopped low-flow frame |
| `state[13]` | input water temperature in whole degrees C |
| `state[14]` | output water temperature in whole degrees C |
| `state[23..40]` | current smart curve payload |

Observed live examples on a working BlueZ D-Bus path:

- `state[4] = 0xFE` while Frostbay is commanded into Smart Fan mode, even in a later low-flow stopped frame
- `state[6..7] = 0x0300`, `0x0330`, `0x02D0` which decode to about `77`, `82`, `72` mL/min after dividing the raw word by `10`
- `state[12]` is nonzero in running frames and `0x00` in an observed stopped frame caused by insufficient flow
- `state[13] = 0x1F` and `state[14] = 0x1E` which decode to `31 C` and `30 C`

Important distinction:

- `state[4]` is the requested mode, not a reliable runtime ON/OFF indicator.
- Current Linux evidence says `state[12] > 0` is a better display signal for whether Frostbay is actively running.
- Live readback does **not** preserve the old assumption that `state[6..7]` remain stable smart preset selector echoes.
- Current evidence says those two bytes are reused as runtime flow telemetry in readback.
- When controlling the device, preserve readback telemetry bytes unless you are intentionally replaying a known-good outbound recipe.

Known outbound control bytes:

```text
state[4]      mode byte
state[5]      fan byte
state[8]      pump byte
state[23..40] smart preset curve bytes
```

Known-good smart preset write recipes may also patch `state[6..7]`, but those bytes should be treated as outbound recipe bytes only, not as stable semantic fields in live readback.

Everything else should be treated as preserved state unless you have a newer verified reason to change it.

---

## Mode Values

The mode byte is `state[4]`.

| Value | Meaning |
|-------|---------|
| `0x00` | OFF |
| `0xFE` | Smart Fan |
| `0xFF` | Fixed Fan |

Interpretation:

- Writing `0x00` puts Frostbay into the stored off state.
- Writing `0xFE` selects Smart Fan mode.
- Writing `0xFF` selects Fixed Fan mode.
- Any non-zero mode is effectively an ON state.
- In live readback, this byte should be treated as the requested or stored mode family, not as a guaranteed runtime ON/OFF indicator.

---

## Transport Format

Frostbay does not accept a direct 64-byte write to `FFE1`.
Instead, the updated state must be sent as three 20-byte writes.

### Build the write payload

Given a patched 64-byte state blob named `state`:

```text
write_payload[0]     = 0x02
write_payload[1..57] = state[2..59]
```

This produces a 58-byte payload.

### Split into three writes

```text
chunk_1 = [0x1C] + write_payload[0..18]
chunk_2 = [0x2C] + write_payload[19..37]
chunk_3 = [0x3C] + write_payload[38..56] + zero padding to 20 bytes total
```

### Write timing

- Send `chunk_1`, then `chunk_2`, then `chunk_3`
- Wait about 20 ms between writes
- `Write Without Response` is the preferred write mode
- Reading back after about 300 ms is useful for verification

---

## OFF

To put Frostbay into the stored OFF state:

```text
state[4] = 0x00
```

Preserve all other bytes from the current state unless you have a verified reason to change them.

Behavior:

- OFF persists across reconnect.
- Reconnecting later should read back mode `0x00` until another state is written.
- This explicit OFF behavior is different from a runtime protective stop caused by insufficient flow; in that case, the requested mode may remain non-zero while `state[12]` drops to `0x00`.

---

## ON

There is no separate ON opcode.

To turn Frostbay on, write any active cooling state:

- Smart Fan: `state[4] = 0xFE`
- Fixed Fan: `state[4] = 0xFF`

The device stores the last written active state.

---

## Smart Fan

Smart Fan uses mode `0xFE` and requires both selector bytes and a curve payload.

### Shared Smart Fan fields

For all three smart presets:

```text
state[4] = 0xFE
state[5] = 0x32
```

Known-good smart preset baselines use:

```text
state[8] = 0x64
```

### Smart presets

| Preset | Known-good outbound `state[6..7]` bytes | `state[23..40]` |
|--------|---------------|-----------------|
| `silent` | `06 00` | `1E18201C2124222C233024342538263C2846` |
| `soft`   | `06 F0` | `1E222026212E2236233A243E254226462850` |
| `strong` | `08 10` | `1E2C20302138224023442448254C26502864` |

These `state[6..7]` values should be understood as known-good outbound recipe bytes only.
Live Linux D-Bus readback while the device is running repurposes `state[6..7]` as the flow telemetry word, so readback should not be interpreted as preserving these selector values.

### Smart Fan write rules

To set a smart preset:

1. Set `state[4] = 0xFE`
2. Set `state[5] = 0x32`
3. Optionally set `state[6..7]` to the known-good outbound recipe bytes for the preset
4. Set `state[23..40]` to the preset curve bytes
5. Set `state[8]` to the desired pump byte
6. Preserve all other bytes
7. Send the chunked `FFE1` write

Practical recommendation:

- Use `state[8] = 0x64` for the known-good smart baseline
- If you expose pump control in smart mode, keep it within `0x32..0x64` which is `50..100`
- For real hardware use, `80..100` is the safer recommended range

---

## Fixed Fan

Fixed Fan uses mode `0xFF`.

### Fixed Fan write rules

To set fixed/manual fan control:

```text
state[4] = 0xFF
state[5] = fan_percent
state[8] = pump_percent
```

Preserve all other bytes.

### Fan percentage encoding

`state[5]` is the fixed-fan target percentage encoded as one byte:

| Percent | Byte |
|---------|------|
| `0`   | `0x00` |
| `25`  | `0x19` |
| `50`  | `0x32` |
| `75`  | `0x4B` |
| `100` | `0x64` |

Any percentage in the `0..100` range is represented directly as the equivalent byte value.

---

## Pump Speed

Pump speed is encoded in `state[8]`.

### Pump encoding

| Percent | Byte |
|---------|------|
| `50`  | `0x32` |
| `80`  | `0x50` |
| `100` | `0x64` |

Recommended operating range:

- supported control range: `50..100`
- recommended practical range: `80..100`

Implementation guidance:

- Preserve the existing pump byte when you do not intend to change pump speed.
- When changing pump speed, patch only `state[8]`.
- In Smart Fan mode, the known-good preset baseline uses pump `100`, but the protocol field is still `state[8]`.

---

## Minimal Implementation Recipes

### Set OFF

```text
read FFE1
state[4] = 0x00
write patched state through the 3-chunk transport
```

### Set Smart Fan Silent

```text
read FFE1
state[4] = 0xFE
state[5] = 0x32
state[6] = 0x06
state[7] = 0x00
state[8] = desired pump byte
state[23..40] = 1E18201C2124222C233024342538263C2846
write patched state through the 3-chunk transport
```

### Set Smart Fan Soft

```text
read FFE1
state[4] = 0xFE
state[5] = 0x32
state[6] = 0x06
state[7] = 0xF0
state[8] = desired pump byte
state[23..40] = 1E222026212E2236233A243E254226462850
write patched state through the 3-chunk transport
```

### Set Smart Fan Strong

```text
read FFE1
state[4] = 0xFE
state[5] = 0x32
state[6] = 0x08
state[7] = 0x10
state[8] = desired pump byte
state[23..40] = 1E2C20302138224023442448254C26502864
write patched state through the 3-chunk transport
```

### Set Fixed Fan

```text
read FFE1
state[4] = 0xFF
state[5] = desired fan percent byte
state[8] = desired pump byte
write patched state through the 3-chunk transport
```

---

## Summary

The Frostbay protocol can be implemented by treating `FFE1` as a preserved state blob exposed through BlueZ with a small writable control surface:

- wait for BlueZ to expose a connected, services-resolved `FFE1` characteristic
- use direct BlueZ `ReadValue` / `WriteValue` on the resolved `FFE1` object when possible
- treat adapter choice as significant; on the Apex host, the built-in `hci0` path was broken while the external `hci1` path exposed the correct Frostbay GATT tree
- `state[4]` stores the requested `OFF`, `Smart Fan`, or `Fixed Fan` family
- `state[5]` holds fixed fan percentage, or the smart baseline value
- `state[6..7]` are live flow telemetry in readback and currently decode as a big-endian tenths-of-mL/min word
- `state[8]` is pump speed
- `state[12]` is currently the best observed runtime ON/OFF display signal
- `state[13]` and `state[14]` are water temperatures in whole degrees C
- `state[23..40]` carry the active smart curve payload
- if BlueZ instantiates Frostbay as a bogus HID Consumer Control keyboard, neutralize its Volume Up / Volume Down usages through a targeted hwdb override rather than disabling the GATT path
- the final write is always sent as the `1C`, `2C`, `3C` three-chunk `FFE1` transport

That is enough to implement ON, OFF, Smart Fan with `silent`/`soft`/`strong`, Fixed Fan percentage control, and pump speed control.