# b6charger-ctl

[![test](https://github.com/THOM-AwS/b6charger-ctl/actions/workflows/test.yml/badge.svg)](https://github.com/THOM-AwS/b6charger-ctl/actions/workflows/test.yml)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Read/write control for SkyRC B6-family USB HID balance chargers - iMAX
B6 / B6AC / B6 mini and rebadged clones sold under other brands (Jaycar
POWERTECH PLUS MB-3633 / JMB-3633 is the one this was built and tested
against).

The vendor PC app for this hardware family ("Charge Master") is
Windows-only, GUI-only, and closed source. This is a small,
dependency-free Python library, CLI, and optional HTTP wrapper meant to
run headless on whatever's already plugged into the charger's USB port
(a Raspberry Pi Zero W, in the reference setup).

**⚠️ Status**: the write path (start/stop/profile/limits) has been
tested against real hardware and works - see
[`DRY_RUN.md`](DRY_RUN.md) for exactly what's been validated and how.
This still commands a device that regulates LiPo/LiHV charge voltage
and current. Read the **Safety** section below before your first real
`start`.

## Contents

- [Why this exists](#why-this-exists)
- [Install](#install)
- [Configure your batteries](#configure-your-batteries)
- [Command reference](#command-reference)
- [HTTP API](#http-api)
- [Can this identify the battery automatically?](#can-this-identify-the-battery-automatically)
- [Protocol notes](#protocol-notes--findings-worth-knowing-about)
- [Safety](#safety)
- [Contributing](#contributing)
- [License](#license)

## Why this exists

The charger's own manual advertises real PC control ("initiate, control
charging and update firmware from Charge Master") over its Micro USB
port. That capability is real; the official software to use it just
doesn't run on Linux. This reimplements the wire protocol directly
against `/dev/hidraw`, based on
[libb6](https://github.com/maciek134/libb6) (GPL-3, targets "SkyRC B6xx
series chargers") - the same protocol family this hardware speaks.

## Install

Requires Python 3.11+ (for the standard-library `tomllib` TOML
parser - no third-party dependencies at all for normal use).

```bash
git clone https://github.com/THOM-AwS/b6charger-ctl
cd b6charger-ctl
pip install -e .
```

This installs two commands: `b6ctl` (the CLI) and `b6httpd` (the
optional HTTP wrapper). Try it immediately with zero risk, no hardware
required:

```bash
b6ctl --fake status
```

If that prints a status block, the install worked. Everything below
that touches real hardware, `--fake` runs against an in-memory
simulated charger instead - useful for trying commands out safely
before you ever point them at your device.

## Configure your batteries

This is the part that makes `b6ctl` pleasant to use day to day instead
of re-typing chemistry/cell-count/current every single charge.

**1. Copy the example file:**

```bash
cp packs.example.toml packs.toml
```

`packs.toml` is in `.gitignore` - your battery roster stays on your
machine, it's never committed, and it's never included in this
repository (the shipped [`packs.example.toml`](packs.example.toml) is
a harmless one-entry placeholder, not real battery data).

**2. Edit `packs.toml`** - one `[[pack]]` block per battery. Every
value comes straight off the label printed on the pack itself; you're
not guessing or measuring anything:

```toml
[[pack]]
name = "zeee2200"              # what you'll type as --pack zeee2200
description = "Zeee 2200mAh 3S, standard LiPo"
chemistry = "lipo"             # "lipo" (4.20V/cell) or "lihv" (4.35V/cell)
cells = 3                      # the "S" number printed on the pack
capacity_mah = 2200             # the mAh rating printed on the pack
default_current_ma = 1100       # used when you don't pass --current-ma (0.5C here)
```

| Field | What it means | Where it comes from |
|---|---|---|
| `name` | Short id, no spaces - this is your `--pack NAME` argument | You choose it |
| `description` | Free text label, purely for your own reference | You choose it |
| `chemistry` | `"lipo"` (4.20V/cell) or `"lihv"` (4.35V/cell) - **the most important field** | Printed on the pack ("LiPo"/"LiHV"/"HV") |
| `cells` | The "S" number (3S = 3, 4S = 4, ...) | Printed on the pack |
| `capacity_mah` | Capacity in mAh | Printed on the pack |
| `default_current_ma` | Charge current used when `--current-ma` isn't given | Pick 0.5C (half the capacity) as a safe starting point |
| `max_current_ma` (optional) | Hard per-pack ceiling; defaults to 1C if omitted | Optional - the tool never lets this exceed 1C regardless |

Full field-by-field documentation, including what "not sure? use this"
guidance for chemistry, is in the comments inside
[`packs.example.toml`](packs.example.toml) itself.

**3. Charge by name:**

```bash
b6ctl start --pack zeee2200
```

This is the recommended way to start a charge, because it comes with a
safety check the manual `--chemistry`/`--cells` flags don't have (see
next section).

### Why `--pack` is safer than typing flags by hand

`start --pack NAME` reads the charger's **live** cell count and
refuses to send anything if it doesn't match what that pack is
registered as:

```
$ b6ctl start --pack wrong_cell_count
error: pack 'wrong_cell_count' is configured as 4S, but the charger
currently detects 3 real cell(s) connected - refusing to start. Check
the physical connection and the pack you meant to select before
retrying.
```

There's deliberately no flag to skip this check. If you're certain and
need to override it, use the manual `--chemistry`/`--cells`/
`--current-ma` flags directly (see below) - that's its own explicit
action, not a checkbox on the "safe" path.

The registry also enforces safety bounds **in code**, not just in the
file: `max_current_ma` can never exceed 1C for a pack's capacity, and
`default_current_ma` can never exceed `max_current_ma`, no matter what
you write in `packs.toml`. A typo that would set a dangerous current
gets rejected at load time with a clear error, not silently accepted.

## Command reference

Every command accepts these global flags:

| Flag | Meaning |
|---|---|
| `--fake` | Use an in-memory simulated charger - no hardware touched at all |
| `--device /dev/hidrawN` | Use a specific device instead of auto-discovering one |
| `-v`, `--verbose` | Log every frame sent, as hex |

### `status` - read live charge info

```bash
b6ctl status
b6ctl status --json
```

Pack voltage, current, per-cell voltages, cell spread, capacity
delivered, temperatures. Always safe - read-only.

### `sysinfo` - read current system settings

```bash
b6ctl sysinfo
```

Cycle time, time/capacity limits, temp limit, buzzer settings. This is
how you verify a `set-limits` write actually took effect, rather than
trusting that it didn't error.

### `packs list` / `packs show` - inspect your battery roster

```bash
b6ctl packs list
b6ctl packs show zeee2200
```

### `start` - start a charge

**Recommended**, using a configured pack:

```bash
b6ctl start --pack zeee2200
b6ctl start --pack zeee2200 --current-ma 1500   # override the default current
```

**Manual**, without a registry entry:

```bash
b6ctl start --chemistry lipo --cells 3 --current-ma 1500
```

Both forms:

- Always print the exact profile about to be sent and require typing
  `y` at a confirmation prompt, unless `--auto-approve` (alias `--yes`)
  is given.
- Support `--dry-run`, which builds and logs the frame but sends
  nothing - including the `--pack` cell-count check, so `--dry-run`
  tells you the truth about whether a real run would be blocked.
- Support `--mode` (`standard`/`discharge`/`storage`/`fast`/`balance`,
  default `balance`) and `--discharge-current-ma`.

### `stop` - stop the current charge

```bash
b6ctl stop
b6ctl stop --dry-run
```

### `set-limits` - configure safety cutoffs

```bash
b6ctl set-limits --temp-limit 50 --time-limit 200 --capacity-limit 6000
```

Verify any change with `b6ctl sysinfo` afterwards.

## HTTP API

A thin control surface next to (not merged into) ht-infra's read-only
`b6_poller.py` exporter - deliberately a separate process/port, since a
monitoring endpoint and a control endpoint have very different blast
radii if either has a bug.

```bash
b6httpd --port 9111          # binds 127.0.0.1 by default - widen deliberately
```

```
GET  /status              -> same fields as `b6ctl status --json`
POST /start {"chemistry": "lipo", "cells": 3, "current_ma": 1500, "mode": "balance"}
POST /stop
```

Every write is logged with the caller's address before being sent.
Note: the HTTP API doesn't currently support the `packs.toml` registry
or its cell-count cross-check - it takes a raw profile. If you build
automation against it, consider porting that same safety check here
first.

## Can this identify the battery automatically?

**No, and it's worth understanding why**, rather than assuming a future
version might:

- **Cell count**: yes, reliably - that's what `status`'s cell-voltage
  reading measures directly.
- **Which specific pack, or its capacity**: no. There's no ID chip and
  no data pins beyond the balance taps and main power leads - capacity
  and chemistry aren't electrically encoded anywhere this charger can
  read. Voltage at plug-in reflects state of charge, not pack identity
  (a 3S pack could rest anywhere from ~9.6V to 12.6V regardless of
  which physical pack it is).
- **The "cell count uniquely identifies pack X" trap**: if you only own
  one pack of a given cell count, its cell count might *look* like a
  reliable identifier today. It isn't a real capability - it's a
  coincidence of your current pack roster, and it breaks the moment you
  add a second pack with the same cell count but different chemistry.
  This is exactly why `packs.toml` is a cross-check against a name YOU
  provide, not an automatic identification system.

## Protocol notes / findings worth knowing about

- **Frame format**: `[0x0F, LEN, CMD, 0x00, ...payload..., checksum,
  0xFF, 0xFF]`, checksum = `sum(bytes from index 2 onward) & 0xFF`.
  Full detail in `b6charger/protocol.py`'s module docstring.
- **Cell-voltage noise filter**: the charger's balance socket supports
  more cells than most packs use, and the unused pins can read a
  stable, physically-impossible voltage (~9V observed) once real
  charge current is flowing - not visible on an idle read. Filtered out
  via a `[2000mV, 4400mV]` range check (`CELL_MIN_MV`/`CELL_MAX_MV` in
  `protocol.py`) rather than a floor-only check. Full live trace in
  [`DRY_RUN.md`](DRY_RUN.md).
- **Impedance field**: `GET_CHARGE_INFO`'s response includes a
  per-pack internal-resistance reading (the manual's "Battery Internal
  Resistance Meter" feature) - decoded here as `impedance_mohm`.
- **STATE 4 discrepancy - unverified**: libb6's `Enum.hh` defines
  charger state `4` as a second error state (`ERROR_2`); some other
  B6-family tooling labels it "idle". Unverified which is correct on
  which firmware. See the `State` enum's docstring in `protocol.py`.

## Safety

This controls a device whose entire job is regulating LiPo/LiHV charge
voltage and current. A bug here is a different risk class than a bug in
a read-only monitoring tool.

- Prefer `start --pack NAME` over manual flags - it has a live
  hardware cross-check the manual path doesn't.
- Always try `--dry-run` first when doing anything unfamiliar - it
  performs every read the real command would (so checks like the
  cell-count cross-check still run and tell you the truth) but sends
  nothing.
- Read [`DRY_RUN.md`](DRY_RUN.md) before trusting a code path you
  haven't personally exercised - it documents exactly what's been
  proven against real hardware versus what's implemented from the
  protocol spec but unverified.
- Don't trust the write path for unattended automation without having
  personally watched it behave correctly first.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

GPL-3.0-or-later - see [`LICENSE`](LICENSE). Chosen because the frame
encoding here closely follows libb6's own internal structure rather
than being independently derived from a black-box wire capture, so
treating this as a derivative work and matching its license is the
correct call, not just a style preference.
