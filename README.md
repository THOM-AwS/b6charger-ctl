# b6charger-ctl

[![test](https://github.com/THOM-AwS/b6charger-ctl/actions/workflows/test.yml/badge.svg)](https://github.com/THOM-AwS/b6charger-ctl/actions/workflows/test.yml)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

> **Disclaimer**: every effort has been made to make this as safe as
> possible, but use of this code is at your own risk. The wrong
> configuration could cause a house fire. You have been warned. See
> [`DISCLAIMER.md`](DISCLAIMER.md) and the **Safety** section below.

Linux Headless Battery Charger Network Controller/exporter

I use this library to control my LiPo battery charger in my garage,
which is hooked up to a Raspberry Pi Zero W. I maintain a list of the
different types of batteries I have that I can invoke when I connect
them, and monitor them with Prometheus/Grafana over the network.

Read/write control for SkyRC B6-family USB HID balance chargers - iMAX
B6 / B6AC / B6 mini and rebadged clones sold under other brands (Jaycar
POWERTECH PLUS MB-3633 / JMB-3633 is the one this was built and tested
against).

## Limitations

**Single device only, at this point.** This project assumes exactly
one charger connected to one host. Device discovery (auto-probe or
`--device`) always resolves to one `/dev/hidraw*` path - `_discover()`
returns the *first* device to answer, not a list - and nothing in the
CLI, the HTTP API, or the `packs.toml` schema has any concept of
addressing multiple simultaneously-connected chargers from a single
`b6ctl`/`b6charger-httpd` instance. If you need to run more than one
charger, run a separate instance per charger, each with its own
`--device` and (for `serve`) its own `--listen` port.

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
- [Limitations](#limitations)
- [Install](#install)
- [Hardware access (Linux permissions)](#hardware-access-linux-permissions)
- [Configure your batteries](#configure-your-batteries)
- [Command reference](#command-reference)
- [HTTP API](#http-api)
- [Running as a systemd service](#running-as-a-systemd-service)
- [Grafana dashboard](#grafana-dashboard)
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

This installs one command, `b6ctl`. Try it immediately with zero risk,
no hardware required:

```bash
b6ctl --fake status
```

If that prints a status block, the install worked. Everything below
that touches real hardware, `--fake` runs against an in-memory
simulated charger instead - useful for trying commands out safely
before you ever point them at your device.

## Hardware access (Linux permissions)

`--fake` needs nothing beyond Python. Real hardware is a separate
step: `/dev/hidraw*` device nodes are root-owned (`0600`) on most
distros by default, so `b6ctl status` against the real charger will
fail with a plain `PermissionError` until you either run as root
(not recommended for a long-running `serve` daemon) or grant your own
user access.

The clean fix is a udev rule scoped to just this device, not a blanket
hidraw permission change:

```bash
cd udev
cp 99-b6charger.rules.example 99-b6charger.rules
# edit idVendor/idProduct in the copy - see the comments in the file
# for how to find them with lsusb
sudo cp 99-b6charger.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG plugdev "$USER"   # then log out and back in
```

See [`udev/99-b6charger.rules.example`](udev/99-b6charger.rules.example)
for the full walkthrough. This project doesn't ship a fixed
vendor/product ID - the charger is sold under several rebadges (see
[Why this exists](#why-this-exists)) that aren't confirmed to share
one USB ID, so guessing one here would either silently not match your
device or match the wrong one.

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

`b6ctl` looks for it in this order: the `B6CTL_PACKS` environment
variable (an explicit path override - set this for `b6ctl serve`
specifically if it runs under systemd or similar, where
`WorkingDirectory` might not be wherever `packs.toml` actually lives),
then `packs.toml` in the current directory (the default for running
`b6ctl` interactively from wherever you keep it), then
`~/.config/b6charger-ctl/packs.toml`, then finally
`packs.example.toml`. If it ever falls back to the example file, every
command that would start a real charge from `--pack` prints/logs a
warning saying so - starting a real charge against the placeholder
roster instead of your own is exactly the kind of mistake this file
exists to prevent.

**2. Edit `packs.toml`** - one `[[pack]]` block per battery. Every
value comes straight off the label printed on the pack itself; you're
not guessing or measuring anything:

```toml
[[pack]]
name = "pack3s"              # what you'll type as --pack pack3s
description = "2200mAh 3S, standard LiPo"
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
b6ctl start --pack pack3s
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

Every table below is exhaustive - every flag each command accepts, its
valid values, and its default. You shouldn't need to read the source
or run `--help` to know what's available.

### Global flags (every command)

| Flag | Values | Default | Meaning |
|---|---|---|---|
| `--fake` | (boolean) | off | Use an in-memory simulated charger - no hardware touched at all |
| `--device` | a path, e.g. `/dev/hidraw0` | auto-discover | Use a specific device instead of probing every `/dev/hidraw*` |
| `-v`, `--verbose` | (boolean) | off | Log every frame sent, as hex, before it's sent |

### `status` - read live charge info

```bash
b6ctl status
b6ctl status --json
```

| Flag | Values | Default | Meaning |
|---|---|---|---|
| `--json` | (boolean) | off | Machine-readable output instead of the human-readable block |

Reports: state, pack voltage, current, capacity delivered, elapsed
time, internal/external temperature, impedance, per-cell voltages, and
cell spread. Always safe - read-only, never writes anything.

### `sysinfo` - read current system settings

```bash
b6ctl sysinfo
b6ctl sysinfo --json
```

| Flag | Values | Default | Meaning |
|---|---|---|---|
| `--json` | (boolean) | off | Machine-readable output instead of the human-readable block |

Reports: cycle time, time limit (on/off + minutes), capacity limit
(on/off + mAh), temp limit, low-DC cutoff, key/system buzzer state.
This is how you verify a `set-limits` write actually took effect,
rather than trusting that it didn't error. Read-only.

### `packs list` / `packs show` - inspect your battery roster

```bash
b6ctl packs list
b6ctl packs show pack3s
```

`packs show` takes one positional argument: the pack `name` from
`packs.toml`. Neither subcommand takes any flags beyond the globals.
See [Configure your batteries](#configure-your-batteries) for the full
`packs.toml` field reference (`chemistry`, `cells`, `capacity_mah`,
`default_current_ma`, `max_current_ma`).

### `start` - start a charge

**Recommended**, using a configured pack:

```bash
b6ctl start --pack pack3s
b6ctl start --pack pack3s --current-ma 1500   # override the default current
```

**Manual**, without a registry entry:

```bash
b6ctl start --chemistry lipo --cells 3 --current-ma 1500
```

| Flag | Values | Default | Meaning |
|---|---|---|---|
| `--pack` | a name from `packs.toml` | - | Use a configured pack. Mutually exclusive with `--chemistry`/`--cells`; runs the live cell-count safety check (see above) |
| `--chemistry` | `lipo`, `lihv` | - | Standard LiPo (4.20V/cell) or High-Voltage (4.35V/cell). Required if not using `--pack`; error if used with `--pack` |
| `--cells` | integer, 1-16 | - | Cell count ("S" number). Required if not using `--pack`; error if used with `--pack` |
| `--current-ma` | integer, milliamps | pack's `default_current_ma` with `--pack`; **required** without it | Charge current. With `--pack`, capped at that pack's `max_current_ma` |
| `--discharge-current-ma` | integer, milliamps | `1000` | Discharge current (only relevant in discharge/storage modes) |
| `--mode` | `standard`, `discharge`, `storage`, `fast`, `balance` | `balance` | Lithium charging mode - `balance` (per-cell balancing) is what you want for normal charging |
| `--dry-run` | (boolean) | off | Build and log the frame (including running the `--pack` cell-count check) but send nothing |
| `--auto-approve`, `--yes` | (boolean) | off | Skip the interactive confirmation prompt. The profile is always printed first regardless |

**After sending, `start` verifies the charger actually accepted it** -
a passing pre-flight cell-count check only confirms a plausible pack
was connected before the command was sent, not that the charger did
anything with it. A few seconds after `START_CHARGING`, `b6ctl`
re-reads live status: if the charger confirms it's charging with the
expected cell count, you'll see `sent and confirmed: ...`. If the
charger reports an explicit error (e.g. a cell-count mismatch it
detected itself), or never confirms after one retry, `b6ctl` sends
`STOP_CHARGING` immediately and exits non-zero rather than leaving you
uncertain whether a real charge is running. `--dry-run` skips this
entirely, same as everything else it doesn't send.

`chemistry` values in full, for reference (only `lipo`/`lihv` are
exposed via this flag - the protocol layer in `protocol.py` also
supports `LIION`/`LIFE`/`NIMH`/`NICD`/`PB` for anyone extending the
CLI, see `BatteryType` in `protocol.py`):

| Chemistry | Cutoff voltage | CLI flag value |
|---|---|---|
| Standard LiPo / LiIon / LiFe | 4.20V/cell | `lipo` |
| High-Voltage LiPo (LiHV) | 4.35V/cell | `lihv` |

`--mode` values in full:

| Mode | Meaning |
|---|---|
| `standard` | Plain charge, no balancing |
| `discharge` | Discharge the pack |
| `storage` | Charge/discharge to storage voltage (~3.8V/cell) for long-term storage |
| `fast` | Faster charge, less precise termination |
| `balance` | Charge with per-cell balancing (the normal choice, and the default) |

### `stop` - stop the current charge

```bash
b6ctl stop
b6ctl stop --dry-run
```

| Flag | Values | Default | Meaning |
|---|---|---|---|
| `--dry-run` | (boolean) | off | Log the STOP frame but send nothing |

### `set-limits` - configure safety cutoffs

```bash
b6ctl set-limits --temp-limit 50 --time-limit 200 --capacity-limit 6000
```

| Flag | Values | Default | Meaning |
|---|---|---|---|
| `--cycle-time` | integer, 1-60 (minutes) | not sent unless given | Cyclic charge/discharge cycle count |
| `--time-limit` | integer, 1-720 (minutes) | not sent unless given | Charge time-limit safety cutoff (enabled when set) |
| `--capacity-limit` | integer, 100-50000 (mAh) | not sent unless given | Charge capacity-limit safety cutoff (enabled when set) |
| `--temp-limit` | integer, 20-80 (°C) | not sent unless given | Internal temperature cutoff |
| `--key-buzzer` / `--no-key-buzzer` | (boolean) | not sent unless given | Key-press buzzer |
| `--system-buzzer` / `--no-system-buzzer` | (boolean) | not sent unless given | System buzzer |
| `--dry-run` | (boolean) | off | Log the SET frame(s) but send nothing |

Only the limits you actually pass are sent - this command doesn't
touch settings you didn't mention. Verify any change with
`b6ctl sysinfo` afterwards.

The buzzer command sets both key and system buzzers in one frame - there's
no way to write just one. If you only pass one of `--key-buzzer`/
`--system-buzzer`, `b6ctl` reads the other's current value first and resends
it unchanged, so setting one never silently flips the other.

## HTTP API

`b6ctl serve` runs the same `b6ctl` binary as a long-running daemon,
serving a Prometheus `/metrics` endpoint plus control endpoints
(`/start`, `/stop`) - fully self-contained in this repo, no external
services required, and no second binary to install: `serve` is one
more subcommand alongside `status`/`start`/`stop`/etc, just one that
doesn't exit.

Two independent safety levers rather than one:

- **Network exposure** (`--host`/`--port`/`--listen`) defaults to
  `0.0.0.0`, since `/metrics` and `/status` are read-only and safe to
  expose broadly - e.g. for Prometheus scraping from another host.
- **Write capability** (`--enable-writes`) is **off by default,
  regardless of bind address**. Without it, `POST /start` and
  `POST /stop` return `403` immediately, before touching the device at
  all - `b6ctl serve` can sit on the network answering `/metrics` all
  day with zero ability for anyone to command the charger, unless you
  deliberately started it with `--enable-writes`.

`--enable-writes` widens *who* can act, not just what's possible: with
the default `0.0.0.0` bind, it turns "anyone who can reach this port"
into "anyone who can command the charger" - a bigger population than
this project otherwise assumes (one operator, one device). Set the
`B6CTL_WRITE_TOKEN` environment variable to close that gap: once set,
`POST /start` and `POST /stop` additionally require
`Authorization: Bearer <token>`, checked with a constant-time
comparison. It's an env var rather than a flag deliberately - a flag's
value is visible to any other local user via `ps`. **Strongly
recommended** whenever `--enable-writes` is used outside a fully
trusted LAN; without it, `_cmd_serve` logs a loud warning at startup
rather than silently proceeding unauthenticated.

```bash
b6ctl serve                                          # metrics/status only - writes always 403
b6ctl serve --enable-writes                          # also allow /start and /stop - no auth
B6CTL_WRITE_TOKEN=$(openssl rand -hex 32) \
  b6ctl serve --enable-writes                        # also allow /start and /stop - token required
b6ctl serve --listen 0.0.0.0:9111 --enable-writes    # combined interface:port form
b6ctl serve --listen [::1]:9111                      # IPv6 needs bracket notation
```

### `b6ctl serve` flags

`--fake` and `--device` are the same global flags every other
subcommand uses (see the flag tables above) - `serve` doesn't define
its own copies.

| Flag | Values | Default | Meaning |
|---|---|---|---|
| `--host` | an interface address, e.g. `0.0.0.0` | `0.0.0.0` | Interface to bind. Mutually exclusive with `--listen` |
| `--port` | integer, 1-65535 | `9111` | Port to bind. Mutually exclusive with `--listen` |
| `--listen` | `HOST:PORT`, e.g. `0.0.0.0:9111`; IPv6 as `[HOST]:PORT`, e.g. `[::1]:9111` | - | Combined interface+port in one flag. Mutually exclusive with `--host`/`--port` - use one form or the other |
| `--enable-writes` | (boolean) | **off** | Allow `POST /start`/`POST /stop`. Without this, both return `403` regardless of bind address |
| `--dry-run` | (boolean) | off | With `--enable-writes`, log writes but send nothing to the device |
| `--cache-seconds` | float, seconds | `5.0` | How long a rendered `/metrics` body is reused before polling the device again |

`B6CTL_WRITE_TOKEN` (environment variable, not a flag - see above) -
when set alongside `--enable-writes`, requires a matching
`Authorization: Bearer <token>` header on `POST /start`/`POST /stop`.

### Endpoints

```
GET  /metrics             -> Prometheus text format (always available)
GET  /status               -> same fields as `b6ctl status --json` (always available)
POST /start {"chemistry": "lipo", "cells": 3, "current_ma": 1500, "mode": "balance"}
POST /start {"pack": "name", "current_ma": 1500}   # equivalent to `b6ctl start --pack`
POST /stop
```

`POST /start` and `POST /stop` return `403` unless the daemon was
started with `--enable-writes`, and `401` if `B6CTL_WRITE_TOKEN` is
set and the request's `Authorization: Bearer <token>` header is
missing or wrong. Every write that does go through is logged with the
caller's address first.

`/metrics` reports the same metric names this project has always used
(`charger_state`, `charger_cell_millivolts{cell="N"}`,
`charger_cell_count`, `charger_cell_spread_millivolts`, etc.) plus
`charger_impedance_milliohms` - the data was always in
`GET_CHARGE_INFO`, it just wasn't decoded before. Point Prometheus (or
`curl`) at `http://<host>:9111/metrics`.

> **Note: several metrics only populate once a charge is actively
> running.** Confirmed 2026-08-03 against real hardware, in two
> different ways:
>
> - `charger_pack_millivolts`, `charger_current_milliamps`,
>   `charger_capacity_mah`, `charger_cell_millivolts`, and
>   `charger_impedance_milliohms` read `0` while idle (even with a
>   battery connected), then populate normally once charging starts -
>   and correctly **stay at their final value** after the charge
>   completes, showing that session's results. That freeze is
>   intentional/correct: it's the final summary, not a bug.
> - `charger_temp_internal_celsius`/`charger_temp_external_celsius`
>   are different: they're **absent entirely (not `0`) any time
>   `charger_state` isn't `1` (CHARGING)** - including after a charge
>   completes. Unlike the fields above, a frozen temp reading looks
>   exactly like a live one (a real capture stayed at a plausible 24C
>   for hours after the pack was disconnected), so this project treats
>   it as unavailable rather than risk showing a stale reading as
>   current. **There is no known way to read temperature outside an
>   active charge on this hardware family** - not just this clone, see
>   [Protocol notes](#protocol-notes--findings-worth-knowing-about).
>
> Pack voltage and per-cell voltage DO have a working idle-time
> alternative: the `charger_sysinfo_*` metrics below. Don't mistake a
> `0` on the plain metrics for "nothing connected" while idle - check
> `charger_sysinfo_pack_millivolts`/`charger_state` instead.

It also reports `charger_sysinfo_pack_millivolts`/
`charger_sysinfo_cell_millivolts{cell="N"}`/`charger_sysinfo_cell_count`/
`charger_sysinfo_cell_spread_millivolts`, sourced from `GET_SYS_INFO`
rather than `GET_CHARGE_INFO` - these stay live while the charger is
idle (confirmed 2026-08-02 with a real pack connected), unlike the
`GET_CHARGE_INFO`-derived `charger_pack_millivolts`/
`charger_cell_millivolts`, which read zero until a charge actually
starts. Use the `sysinfo` metrics for "is a battery connected and
what's its voltage right now"; use the plain ones for in-session
charge telemetry once charging begins.

`GET_SYS_INFO`'s *configured* safety cutoffs (the same values
`set-limits` writes and `sysinfo` prints) are exposed too:
`charger_sysinfo_temp_limit_celsius`,
`charger_sysinfo_low_dc_limit_millivolts`,
`charger_sysinfo_time_limit_minutes` +
`charger_sysinfo_time_limit_enabled`, and
`charger_sysinfo_capacity_limit_mah` +
`charger_sysinfo_capacity_limit_enabled` - these are settings, not
live telemetry, so they only change when you run `set-limits`.

`POST /start` with `{"pack": "name", ...}` is the HTTP equivalent of
`b6ctl start --pack` - it runs the exact same live cell-count
cross-check against `packs.toml` and returns `409` on a mismatch,
instead of sending anything. A raw body (`{"chemistry", "cells",
"current_ma", ...}`) skips that check, the same way manual
`--chemistry`/`--cells` flags do on the CLI.

`/metrics` also reports `charger_last_commanded_info{battery_type,
cells, mode, pack}`, `charger_last_commanded_timestamp_seconds`, and
`charger_last_commanded_current_milliamps` whenever a `start` has ever
been sent - see "Can this identify the battery automatically?" below
for why this exists and what it does and doesn't mean.

## Running as a systemd service

`serve` is meant to run long-lived, so this ships a generic,
fill-in-the-placeholders unit template rather than leaving you to
write one from scratch:
[`systemd/b6charger-httpd.service.example`](systemd/b6charger-httpd.service.example).

```bash
cd systemd
cp b6charger-httpd.service.example b6charger-httpd.service
# fill in every CHANGE_ME - your install path, a non-root User= that
# has hidraw access (see Hardware access above), and B6CTL_PACKS
sudo cp b6charger-httpd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now b6charger-httpd
journalctl -u b6charger-httpd -f   # tail the logs
```

It defaults to the same safe posture the rest of this project uses:
writes stay off until you deliberately uncomment `--enable-writes` in
the unit and set `B6CTL_WRITE_TOKEN` via the commented-out
`EnvironmentFile=` line - see [HTTP API](#http-api) above before
turning that on. The template only covers a single charger, per
[Limitations](#limitations) - install a second copy under a different
unit name, with its own `--device`/`--listen`, if you have more than
one.

## Grafana dashboard

[`grafana/dashboard.json`](grafana/dashboard.json) is a ready-to-import
starter dashboard for everything `/metrics` exposes - state (as both a
current-value panel and a line graph over time), pack/per-cell
voltage, cell balance, charge current, capacity delivered, internal
temperature, and the configured safety cutoffs.

To import it: Grafana → Dashboards → New → Import → upload the file (or
paste its contents). It uses a datasource **template variable**
(`DS_PROMETHEUS`), not a hardcoded datasource UID, so Grafana will
prompt you to pick your own Prometheus datasource on import rather
than requiring you to hand-edit the JSON first.

It assumes the metric names this README documents above, scraped from
`b6ctl serve`'s `/metrics` - point your Prometheus (or Grafana Cloud
agent) at it the same way described in [HTTP API](#http-api) and the
panels should populate without further changes. The `State` panel's
value mapping and the `Pack voltage`/`Per-cell voltage` panels'
`charger_sysinfo_*` queries assume the same `IDLE`=2/`CHARGING`=1/
`COMPLETE`=3/`ERROR`=4 numbering this project uses - see
[Protocol notes](#protocol-notes--findings-worth-knowing-about) if
you're adapting this for a fork with different state values.

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
- **Chemistry, once a charge is running**: also no. `GET_CHARGE_INFO`
  (the only live-status read this protocol has) has no field reporting
  back what chemistry the charger is currently treating a charge as -
  confirmed against libb6's own parsing code, which never reads one,
  and against every byte offset this project has verified on real
  hardware. The only chemistry information that exists anywhere is
  what a `start` command itself sent - one-way, host to charger, never
  echoed back. To make that visible on a dashboard anyway, every
  `start` (CLI or HTTP) records what it sent to
  `/tmp/b6charger-ctl-last-start.json`, which `serve` exposes as
  `charger_last_commanded_info{battery_type, cells, mode, pack}` in
  `/metrics`. **Read this metric for what it is**: a log of what this
  tool told the charger to do, labeled honestly as "last commanded",
  not a confirmation from the charger itself - if you fat-finger
  `--pack`, this metric will confidently show the wrong thing, exactly
  as confidently as the charger would if it could report chemistry at
  all.

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
- **State 2 is IDLE, not an error - libb6's naming was wrong**:
  `libb6`'s `Enum.hh` names charger state `2` `ERROR_1`, which this
  project trusted until 2026-08-02. Confirmed wrong via an independent
  reverse-engineering project (`buxtronix/b6max`, a separately-typed Go
  implementation) whose own state enum names value `2` `StateIdle` and
  value `4` `StateError` - i.e. there's only one real error state, not
  two. This also matched everything already observed independently:
  the charger's own panel showing nothing wrong while state read `2`,
  an unmapped `error_code` of `0` decoded there, and the charger
  booting directly into state `2` with no battery connected. `State`
  in `protocol.py` now names these `IDLE` (2) and `ERROR` (4).
- **IDLE responses aren't fully trusted either, for a different
  reason**: `libb6`'s reference implementation reads a 2-byte error
  code right after the state byte during `ERROR` and stops - it never
  reads capacity/voltage/current/temp/impedance/cells in that case, so
  there's no verified layout for them. `IDLE` gets the same
  conservative treatment, but not because the layout is unverified -
  an independent project (`buxtronix/b6max`) decodes idle with the
  full normal layout, and it may well be correct on genuine SkyRC
  hardware. On this clone, though, a live test with a real battery
  connected while idle (2026-08-02) showed `GET_CHARGE_INFO` reporting
  all-zero pack telemetry anyway - the charger's front panel can read
  cell voltages directly, but doesn't expose them over this specific
  command until a charge actually starts. `parse_charge_info()`
  reflects that: only `state` and (ERROR only) `error_code`/`error_name`
  are populated during IDLE/ERROR, everything pack-derived (and, as of
  2026-08-03, temp too - see below) is zeroed rather than assumed. An
  earlier version of this project guessed that those fields still
  applied and reported stale, misleading values in production - see
  [`DRY_RUN.md`](DRY_RUN.md) for the full story and why "zero when
  unknown" is the honest choice.
- **Cell voltages ARE available while idle - just not from
  `GET_CHARGE_INFO`**: `GET_SYS_INFO` has its own separate
  voltage/cell fields, and unlike `GET_CHARGE_INFO`'s, these stay live
  while idle - confirmed 2026-08-02 with a real pack connected,
  reading real, gently-drifting values. `/metrics` exposes these as
  `charger_sysinfo_pack_millivolts`/`charger_sysinfo_cell_millivolts`,
  separately from the `GET_CHARGE_INFO`-derived `charger_pack_millivolts`/
  `charger_cell_millivolts` (which stay zero until a charge starts).
- **Temp is only trusted while `state == CHARGING`, full stop - not
  IDLE, not COMPLETE either**: this took two rounds to get right.
  First (2026-08-02) temp was decoded during IDLE/ERROR as a
  "charger-hardware sensor, independent of pack state" - a restart
  test disproved that (it read `0` right after boot, not a plausible
  value). Then (2026-08-03) a plausibility filter was added
  (`TEMP_MIN_C`/`TEMP_MAX_C`) after `buxtronix/b6max`'s own README
  showed genuine SkyRC hardware reading `TempInt=248C` while idle -
  physically impossible, confirming this isn't a clone quirk. But a
  filter alone wasn't enough: capacity/voltage/cells legitimately
  freeze at their final value once a charge completes (that's the
  correct "final session result"), and it turns out temp freezes
  there too - except a frozen temp reads as completely plausible,
  since it's just whatever the real temperature was when charging
  stopped. A live Grafana dashboard was observed stuck at a real
  looking `24C` for hours after the pack was disconnected. No range
  filter can catch "plausible but hours old", so `temp_ext_c`/
  `temp_int_c` are now `None` in every state except `CHARGING`,
  regardless of what the raw byte says.

## Safety

See [`DISCLAIMER.md`](DISCLAIMER.md) first.

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
