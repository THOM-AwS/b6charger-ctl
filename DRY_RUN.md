# Dry-run status

**Nothing in this repo has been sent to real hardware yet.** Everything
below was validated against `FakeChargerTransport` (an in-memory
simulated charger) and hand-traced arithmetic, not a physical device.
The one thing that *has* been proven against a real Jaycar POWERTECH
PLUS MB-3633 is the `GET_CHARGE_INFO` read path - via a separate,
independently-developed read-only exporter for this same protocol,
which uses the identical frame this repo's `build_get_charge_info()`
produces.

## What's verified, and how

- **Frame encoding correctness**: `tests/test_protocol.py` hand-traces
  two frames byte-by-byte against libb6's `Device.cc`/`Packet.cc` and
  asserts the literal expected bytes (not just "does it run"). All
  other command builders are checked structurally (length field matches
  actual payload length, checksum matches a recomputed sum) across a
  range of chemistries/cell-counts/modes.
- **State machine plumbing**: `tests/test_transport.py` and
  `tests/test_device.py` exercise start -> read status -> stop against
  the fake charger, confirming the CLI/device/transport layers agree
  with each other about frame layout.
- **CLI safety behaviour**: `tests/test_cli.py` confirms `--dry-run`
  never calls `input()` or touches the transport, `--yes` skips the
  prompt, and declining the prompt exits non-zero without sending
  anything.

## A bug this caught before it mattered

First implementation of `FakeChargerTransport.write()` read the charge
current out of the wrong frame offset (`frame[5:7]`, which is actually
`cell_count` + `mode`, not `charge_current`) - `test_start_stop_round_trip`
caught it immediately (`current_ma == 772` instead of `1500`, where 772
== `(cell_count << 8) | mode` for a 3S BALANCE-mode profile). Fixed by
reading `frame[7:9]` instead, matching the real payload layout:
`[battery_type, cell_count, mode, current_hi, current_lo, ...]` starting
at `frame[4]`. This is exactly the class of mistake dry-run testing
exists to catch before it reaches a device that regulates LiPo charge
current.

## Worked byte trace: `build_start_charging(lipo_profile(cells=3, current_ma=1500))`

```
battery_type = LIPO = 0x00
cell_count   = 3
mode         = BALANCE = 0x04
charge_current_ma    = 1500 = 0x05DC
discharge_current_ma = 1000 = 0x03E8  (library default)
cell_discharge_voltage_mv = 3200 = 0x0C80
end_voltage_mv            = 4200 = 0x1068
(not NiMH repeak/cycle)   = 0x00 0x00
trickle_current_ma   = 0    = 0x0000
reserved              = 0x00 0x00 0x00 0x00

payload = 00 03 04 05 DC 03 E8 0C 80 10 68 00 00 00 00 00 00 00 00
         (19 bytes)

length = len(payload) + 3 = 22 = 0x16
header = 0F 16 05 00

checksum = sum(header[2:] + payload) & 0xFF
         = sum(05 00 00 03 04 05 DC 03 E8 0C 80 10 68 00 00 00 00 00 00 00 00)
         = 732 & 0xFF = 0xDC

frame = 0F 16 05 00 00 03 04 05 DC 03 E8 0C 80 10 68 00 00 00 00 00 00 00 00 DC FF FF
       (26 bytes total)
```

## Hardware test plan (do this after the current charge finishes)

Run in this order, on the target host, with any other exporter/poller
process for this device stopped first (they also open the hidraw
device - don't run two at once against the same charger):

1. `b6ctl --fake status` - sanity check the CLI itself works (no
   hardware involved).
2. `b6ctl status` - confirm the real read path still works via this
   repo's own parser (should match what your monitoring already shows,
   if any).
3. **With the charger connected to power but NO battery plugged in**:
   `b6ctl stop --dry-run` then `b6ctl stop` (without `--dry-run`) -
   confirm it doesn't error and the front panel doesn't show anything
   alarming, since there's nothing to actually stop.
4. **Still no battery connected**: `b6ctl set-limits --temp-limit 50
   --dry-run`, inspect the printed frame, then run it for real and
   confirm the front panel's temp cutoff setting actually changed to
   match.
5. **Only once 1-4 are clean**: connect a battery you're comfortable
   watching closely, `b6ctl start --chemistry lipo --cells <N>
   --current-ma <X> --dry-run` first, review the printed profile
   carefully against what you'd set by hand on the panel, then drop
   `--dry-run` and stay present for the whole first real charge.

Update this file with the outcome once run - if anything above
disagrees with what actually happens on hardware, that's the protocol
assumption to fix first, before trusting anything else in this repo.

## Progress log

**2026-08-02, on the reference test host (package deployed alongside a
separate, independently-developed exporter for this same protocol,
left running throughout - no conflicts observed):**

- Step 1 (`b6ctl --fake status`) - clean, matches expected fake-charger
  output.
- Step 3-equivalent dry-run (`b6ctl -v stop --dry-run`) - printed
  `0f03fe00feffff`, exactly matching `test_stop_charging_frame_matches_
  known_good_bytes`'s hardcoded fixture. Confirmed nothing was sent
  (`dry_run` short-circuit held).
- Step 2 (`b6ctl status`, real device, read-only) - **this repo's own
  `parse_charge_info` correctly decoded a real response independently
  of the other exporter**: `state=COMPLETE(3)`, `capacity_mah=2019`,
  `time_s=5616`, `pack_voltage_mv=5`, no cells populated (all below the
  2000mV noise floor). Reads as: the charge that was running earlier
  this session finished (2019mAh delivered, plausible for a 2200mAh 3S
  pack) and the battery has since been disconnected from the charger.
  Also confirms the empty-cells edge case (no battery connected)
  doesn't crash the parser.

**Same session, after fixing the transact()/timeout/lock bug above:**
first real `stop` (no battery, the other exporter still running)
succeeded instantly. A second `stop` sent right after **hung
indefinitely** - traced to split write()/read() file descriptors plus
no read timeout, likely compounded by the other exporter's own 30s
poll grabbing the response. Fixed in commit dfd8dd8 (single-fd
`transact()`, `select`-based timeout, flock). Retested with the other
exporter stopped: two `stop` calls back-to-back both returned
`sent STOP` immediately, no hang. Only one audible beep (on the first
call) - no beep on the redundant second stop, no error state either
time.

**Step 4, completed and independently verified:** `set-limits
--temp-limit 50` sent cleanly (no hang, matching the fix above). Initial
observation was "no sound or change" on the panel - rather than hunt
for the right screen, added `GET_SYS_INFO` support (`sysinfo` command)
to verify programmatically instead. Readback confirmed
`temp_limit_c == 50` exactly as sent. Every other decoded field
(`cycle_time=10min`, `time_limit=on 180min`, `capacity_limit=on
8000mAh`, `low_dc_limit=11.00V`, both buzzers True) was internally
consistent and within valid range, which validates the whole
`GET_SYS_INFO` offset mapping (`protocol.parse_sys_info`), not just the
one field being tested.

**Step 5, completed:** `start --chemistry lipo --cells 3 --current-ma 2200`
sent to a real 2200mAh 3S pack. Sent cleanly (no hang - the
transact()/timeout/lock fix held under a real write, not just reads).
Panel independently confirmed the exact intended profile: `LP3s 2.2A
12.45V BAL`. Charge progressed correctly and observably: current
started at 2.2A (commanded value), cells climbed steadily toward
4.20V/cell, current then tapered down (2.2A -> 1.53A) as cells
approached the target - textbook CC-to-CV transition, self-terminating,
no manual `stop` needed.

**Bug found and fixed along the way:** this was the first real read
taken during an ACTIVE charge (every previous real read was against an
idle charger) - `status` showed 5 "cells", with cells 4/5 at a stable
~9000mV. Cross-checked against the panel, which showed a clean 3S
pack - confirmed this was a decode artifact, not a real electrical
fault. Root cause: the charger's balance socket supports more cells
than the connected pack; the unused pins read this stable phantom
voltage once real current was flowing (not present when idle, which is
why no earlier read caught it). `CELL_MIN_MV`'s floor-only check
(inherited from the other exporter's original implementation) let it
through. Fixed by adding `CELL_MAX_MV = 4400` (LiHV tops out at
4350mV/cell, so this never excludes a real reading) - verified fixed
live: cells 1-3 correctly shown (4.202/4.204/4.196V, 8mV spread), no
phantom cells.

**Everything in the original test plan is now verified against real
hardware.** Follow-up done: the `CELL_MAX_MV` fix was ported to the
other exporter's codebase too (same floor-only check, same gap),
merged, and confirmed live in production monitoring - see the finding
below for what that surfaced.

## Real production finding: stale voltage/current after disconnect (2026-08-02)

With no battery physically connected to the charger, production
monitoring showed `charger_state = 2` (ERROR per both this project's
and the other exporter's state mapping) alongside a non-zero, unmoving
pack voltage (~12.6V) and current (~292mA) - looking like a fault.
Checked the charger's own front panel directly: **it showed nothing
wrong, no error code, looked completely normal.**

That mismatch (exporter says ERROR, panel says fine) plus the
non-zero voltage while genuinely disconnected (a real "no battery"
read earlier in this project correctly showed ~0.005V, see above)
points at the charger's own response still carrying stale/leftover
values from before disconnection, rather than fresh zeros - not
confirmed to be a byte-offset parsing bug (libb6's `Device.cc` inserts
a 2-byte error code after the state byte during `ERROR_1`/`ERROR_2`
that neither this project nor the other exporter has ever decoded, so
that remains a real, separate, still-open gap - just not confirmed as
the cause here).

**Fix applied regardless of root cause**: `parse_charge_info()` now
zeroes `voltage_mv`/`current_ma` whenever no real cells are detected
(`cells_mv` empty) - no real cells means no battery is actually
connected, so a pack voltage/current reading isn't meaningful
regardless of what raw bytes the charger returns.

**Superseded by a more precise fix, same day**: the cells-based
zeroing above was a reasonable first reaction but the wrong mechanism
- it couldn't have caught this case reliably, since a stale-but-still-
"real-looking" cell reading (within the normal 2000-4400mV range)
would pass the cell-presence check without being fresh data.
`parse_charge_info()` now branches on `state` instead: when it's
`ERROR_1`/`ERROR_2`, only `state` and a newly-decoded `error_code` are
returned - `libb6`'s `Device.cc` confirms it never reads
capacity/voltage/current/temp/impedance/cells during an error
response (it throws immediately after the error code), so there's no
verified layout to guess at for those fields, and none is guessed at
here either. `ChargeInfo` gained `error_code`/`error_name` properties,
surfaced through `b6ctl status`, the HTTP `/status` endpoint, and a new
`charger_error_code` metric (only emitted while actually in an error
state). Verified via
`test_parse_charge_info_zeroes_everything_but_state_and_error_in_error_state`
and `test_parse_charge_info_does_not_force_zero_voltage_in_normal_state_with_no_cells`
(confirming the normal-state, no-battery case is untouched - it never
needed a heuristic, since a genuine idle read has always reported a
real near-zero voltage on its own, see above).

**Resolved (the fabrication problem), still open (the actual mystery)**:
decoding the actual `ERROR` code turns `charger_state=2` from a bare
number into an actionable message - or would, if the code decoded to
one. Redeployed and re-checked against the real device in the exact
same state (no battery, state=`ERROR_1`): every numeric field
correctly zeroed (fix confirmed working), but **the decoded error code
came back as `0`**, which isn't in `libb6`'s `Enum.hh` at all - every
defined error code is `0x000B` (11) or higher. Combined with the panel
showing nothing wrong, this now looks less like "a real error code we
haven't mapped yet" and more like **this specific charger's firmware
may not follow `libb6`'s state semantics for value `2` at all** -
`libb6` was reverse-engineered against genuine SkyRC hardware, and
this project has already found one other place this rebadge diverges
from it (the floating-pin cell noise, see above). Genuinely unresolved
- logged honestly rather than claimed as fixed. The real, confirmed
improvement either way: the tool now reports "unknown" instead of
fabricating plausible-looking wrong numbers, which is correct
regardless of what state `2` actually turns out to mean on this
hardware.
