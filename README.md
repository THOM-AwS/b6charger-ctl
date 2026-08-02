# b6charger-ctl

Read/write control for SkyRC B6-family USB HID balance chargers -
iMAX B6 / B6AC / B6 mini and rebadged clones sold under other brands
(Jaycar POWERTECH PLUS MB-3633 / JMB-3633 is the one this was built and
tested against).

The vendor PC app for this hardware family ("Charge Master") is
Windows-only, GUI-only, and closed source. This is a small, dependency-free
Python library + CLI + optional HTTP wrapper meant to run headless on
whatever's already plugged into the charger's USB port (a Raspberry Pi
Zero W, in the reference setup).

**Status: the write path (start/stop/profile/limits) has not been
tested against real hardware yet.** See [`DRY_RUN.md`](DRY_RUN.md) for
exactly what has and hasn't been validated, and the test plan for when
it can be. The read path (`status`) reuses the same frame this project's
sibling exporter has already sent successfully to a real charger.

## Why this exists

The charger's own manual advertises real PC control ("initiate, control
charging and update firmware from Charge Master") over its Micro USB
port. That capability is real; the official software to use it just
doesn't run on Linux. This reimplements the wire protocol directly
against `/dev/hidraw`, based on
[libb6](https://github.com/maciek134/libb6) (GPL-3, targets "SkyRC B6xx
series chargers") - the same protocol family this hardware speaks.

## Install

```bash
pip install -e ".[dev]"
```

No runtime dependencies beyond the Python standard library.

## CLI

```bash
# read-only, safe to run any time
b6ctl status
b6ctl status --json

# always prints the exact profile and requires confirmation unless --yes
b6ctl start --chemistry lipo --cells 3 --current-ma 1500
b6ctl start --chemistry lihv --cells 4 --current-ma 1000 --mode fast --yes

# --dry-run never touches the device, on any subcommand
b6ctl start --chemistry lipo --cells 3 --current-ma 1500 --dry-run

b6ctl stop

b6ctl set-limits --temp-limit 50 --time-limit 200 --capacity-limit 6000
```

Every subcommand accepts `--fake` to run against an in-memory simulated
charger instead of hardware - useful for trying out the CLI itself with
zero risk.

## HTTP

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

## Protocol notes / findings worth knowing about

- **Frame format**: `[0x0F, LEN, CMD, 0x00, ...payload..., checksum, 0xFF,
  0xFF]`, checksum = `sum(bytes from index 2 onward) & 0xFF`. Documented in
  full in `b6charger/protocol.py`'s module docstring.
- **Impedance field**: the charger's `GET_CHARGE_INFO` response includes a
  per-pack internal-resistance reading (the manual's "Battery Internal
  Resistance Meter" feature) at a fixed offset right after the
  temperature bytes. ht-infra's `b6_poller.py` currently reads past this
  field without exposing it - `protocol.py` here decodes it as
  `impedance_mohm`. Worth porting back if useful on the Grafana side.
- **STATE 4 discrepancy - unverified, worth checking empirically**:
  libb6's `Enum.hh` defines charger state `4` as `ERROR_2`, a second
  error condition. ht-infra's `b6_poller.py` labels state `4` as
  `"idle"` in its own `STATES` dict. These can't both be right for the
  same firmware. If `b6_poller.py`'s labeling was empirically verified
  against this exact clone (plausible - clone firmware can genuinely
  diverge from real SkyRC firmware), it's correct and this note is
  moot. If not, `ht-infra/monitoring/alert_rules.yaml`'s `ChargerError`
  alert (`charger_state == 2`) would silently miss a real second error
  state being reported as "idle" on the dashboard instead. Worth
  triggering state 4 deliberately (e.g. disconnect the balance lead
  mid-charge, a documented `BALANCE_CONNECTION` error trigger per
  `Enum.hh`) and checking what the front panel actually shows next to
  what this repo's `status` command reports.

## License

GPL-3.0-or-later - see [`LICENSE`](LICENSE). Chosen because the frame
encoding here closely follows libb6's own internal structure rather
than being independently derived from a black-box wire capture, so
treating this as a derivative work and matching its license is the
correct call, not just a style preference.

## Safety

This controls a device whose entire job is regulating LiPo/LiHV charge
voltage and current. A bug here is a different risk class than a bug in
a read-only monitoring tool. Don't trust the write path for unattended
automation until you've personally run through
[`DRY_RUN.md`](DRY_RUN.md)'s hardware test plan and are confident the
decoded/encoded values match what the front panel shows for the same
settings.
