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

## Real production finding: internal temp wrongly zeroed in error state (2026-08-02)

Production monitoring showed `charger_temp_internal_celsius = 0`
whenever `charger_state = 2` (no battery connected) - reported as
wrong, correctly: the charger's internal case temperature is a
hardware sensor reading, not something that should depend on whether
a battery happens to be plugged in.

Captured the raw `GET_CHARGE_INFO` response directly against the real
charger, live, in this exact state (`state=2`, `error_code=0`,
no battery physically connected):

```
0f22550002000004ce313d01240018000010731066107101800153013f240123000100acffffecffff0000...

state=2  error_code=0 (unmapped, same open mystery as above)
capacity=0  time=1230s  voltage=12605mV  current=292mA
temp_ext=0  temp_int=24
impedance=0
cells: 4211, 4198, 4209, [noise], [noise], [noise], [floating-pin], [floating-pin]
```

Decoded at the same offsets the normal-state parser already trusts,
`temp_int_c=24` is a plausible room-temperature reading - strong
evidence this is a live sensor value, independent of `state`. At the
same time, this capture is a second, independent confirmation of the
"stale voltage/current after disconnect" finding above: voltage
(12.6V), current (292mA), time (1230s), and three real-looking
4.2V cell readings are ALSO present in this same no-battery frame -
almost certainly the frozen tail end of the last completed charge
session, not a live reading. That's exactly why those particular
fields stay zeroed by design (see above) - this capture is fresh
evidence that caution was correct, not overcautious.

**Fix**: `parse_charge_info()` now decodes `temp_ext_c`/`temp_int_c`
even in `ERROR_1`/`ERROR_2` states (they're charger-hardware sensor
readings, not pack telemetry), while `capacity_mah`/`time_s`/
`voltage_mv`/`current_ma`/`impedance_mohm`/`cells_mv` all stay
zeroed/empty exactly as before - the split is deliberate, not a
relaxation of the earlier fix. Verified via
`test_parse_charge_info_zeroes_pack_telemetry_but_not_temp_in_error_state`.

**Deliberately not touched, and a real open question**:
`check_cell_count` (the `start --pack` and HTTP `/start {"pack": ...}`
safety gate) reads `cells_mv` from a fresh `get_charge_info()` call,
which returns `()` empty whenever `state` is ERROR_1/ERROR_2 - so the
gate fails closed (refuses to start) any time the charger reports that
state, regardless of what's physically connected. That's a safe
default for the confirmed case (no battery, stale frozen data) - but
it is NOT yet verified what `state` this charger reports the moment a
real battery is freshly connected but a charge hasn't been started
yet. This frame's own LEN byte (`0x22` = 34) matches a full-length
response, not a short/truncated one - the device sends the same
amount of data in this "error" state as in a normal one, it's just
stale. If "idle, battery just connected, not yet started" *also*
reports `state=2` on this hardware (plausible, unverified), `start
--pack` would refuse to start with a perfectly good battery attached,
which would be a real functional problem, not just an edge case.
**Untested - needs a real hardware check**: connect a battery, read
`b6ctl status` *before* sending `start`, and see what `state` comes
back.

## Correction, same day: temp_int_c is NOT confirmed live after all

The "confirmed live" claim above didn't survive a follow-up test. The
charger was physically power-cycled (unplugged/replugged) later the
same day. Immediately after, in the identical state (`state=2`, no
battery), a fresh raw capture read:

```
0f2255000200000200000000...00000001005800...  (see full hex in git history)

state=2  error_code=0
capacity=0  time=0  voltage=0  current=0
temp_ext=0  temp_int=0
impedance=0
cells: (none)
```

Every field is genuinely zero this time - not the stale-but-plausible
values from before the restart, including `temp_int_c`, which now
reads `0` instead of the `24` captured pre-restart. Re-polled several
times over the following minutes; it stayed at `0`.

This contradicts the "independent live sensor" theory the earlier fix
was built on. If `temp_int_c` were a continuously-sampled hardware
reading unrelated to charge state, a charger sitting powered-on in a
garage should read something close to room temperature, not `0`. The
much more consistent explanation: `temp_int_c` is populated by
firmware **per charge session**, the same as voltage/current/cells -
the pre-restart `24` was likely also a frozen leftover from the last
real charge (when the charger genuinely was at ~24C), not a live
value at all. A power-cycle clears it back to `0` along with
everything else, and it presumably stays `0` until the next charge
actually runs.

**Not reverted**: `parse_charge_info()` still decodes `temp_ext_c`/
`temp_int_c` in error states. Unlike the fields the original fix
correctly left zeroed, nothing in this codebase uses temperature as a
safety input (`check_cell_count` gates on `cells_mv`, not temp) - so
a stale-or-zero temp reading is a monitoring-accuracy question, not a
fire-risk one, and decoding it is still strictly more honest than
unconditionally forcing `0` regardless of what the charger actually
holds. What changed is the *claim*: this is no longer documented as a
confirmed-live sensor, just as "whatever the charger currently
reports for this field, which may be stale or unpopulated." See
`ChargeInfo`'s docstring in `protocol.py` for the corrected language.

**Still open**: whether `temp_int_c` ever updates mid-charge (i.e.
whether it becomes genuinely live once a charge is actually running)
is untested - the restart test only proves it's zero at boot and was
non-zero after a past charge, not when in between it changes.

## State 2 is IDLE, not ERROR_1 - libb6's naming was wrong (2026-08-02)

Independent confirmation, from `buxtronix/b6max` (a separately-typed
Go reverse-engineering of this same protocol, found via a background
research pass): its own `State` enum names value `2` `StateIdle` and
value `4` `StateError` - there is only one real error state, not two.
This matches everything already observed independently in this
project: the charger's panel showing nothing wrong while state read
`2`, an unmapped `error_code` of `0` decoded there, and the charger
booting directly into state `2` with no battery connected.

`State.ERROR_1`/`ERROR_2` renamed to `IDLE`/`ERROR`. `error_code` is
now only decoded for the real `ERROR` state - `IDLE` never sets it
(it isn't an error, so `u16(5)` there isn't one either). The
pack-telemetry zeroing behaviour is unchanged for both states - see
the correction below for why IDLE stays conservative despite
`buxtronix/b6max` decoding it with the full normal layout.

**Consequence found in production**: `ht-infra`'s `ChargerError`
alert (`charger_state == 2`, severity critical) had been firing as a
false positive any time the charger was simply idle - likely for a
long time before this was caught, given how much of this session's
testing sat in state 2. Fixed to key on state `4`. The Grafana
dashboard's own `State` panel had the exact same bug independently
(a value mapping showing `2` as "ERROR" in red, `4` as "IDLE") -
also fixed, in `dash_battery.json`.

## Self-inflicted incident: manual protocol testing corrupted the live poller (2026-08-02)

While investigating the state-2/chemistry questions above, ran
`GET_SYS_INFO`, `GET_DEV_INFO`, and `UNK1` by hand against the same
physical charger the production `b6charger-httpd` service was also
polling. Immediately after, the production `/metrics` endpoint started
reporting `charger_temp_internal_celsius=255` and
`charger_temp_external_celsius=7` - not real readings. Traced byte for
byte: these are exactly `UNK1`'s raw response bytes at offsets 13/14,
meaning the production service's `GET_CHARGE_INFO` requests were
getting back `UNK1`'s stale reply instead of a fresh one. Verified
precisely (not just "looks similar"): `UNK1`'s own capture has
`state@4=2`, `temp_ext@13=7`, `temp_int@14=255`, matching the bogus
live reading exactly.

This survived several recovery attempts that would be reasonable
first guesses: fresh `GET_CHARGE_INFO` reads (still returned the same
stale payload, though with a *corrected* header/command byte -
suggesting whatever's stuck is a data-buffer issue rather than a
simple reply-queue mismatch), a benign `STOP_CHARGING` (no effect),
restarting the `b6charger-httpd` *process* (no effect - rules out a
host-side/Python-level cache, since a fresh process/fd didn't help),
and physically connecting a battery (no effect). Only a full physical
power-cycle of the charger cleared it. Root cause not fully
identified - plausibly the charger firmware's own HID reply buffer not
being refreshed by every command type, but this is inference, not
confirmed against firmware source.

**Practical lesson for this project going forward**: don't run ad hoc
protocol exploration commands against a charger that a production
poller is also actively polling, even though `HidRawTransport`'s flock
prevents literal simultaneous device access - it does NOT prevent this
class of cross-command reply confusion, which is a device/firmware
behaviour outside this project's control. Use `--fake` or a charger
with nothing else attached to it for exploratory command testing.

**A useful side effect**: this incident is what proved bytes 33-34 of
`GET_CHARGE_INFO` (previously an open "maybe this is a hidden field"
question) are NOT a real field of that command at all. Every one of
`GET_SYS_INFO`/`GET_DEV_INFO`/`UNK1`'s captures shared the exact same
trailing content beyond their own declared `LEN`, drawn from whichever
command had run most recently - i.e. bytes past a command's own
declared length are stale shared-buffer leftovers, not a hidden
per-command field. Cross-referencing `buxtronix/b6max`'s own typed
struct (which also stops at the last cell, and covers only 6 cells vs
this clone's 8 - meaning bytes 33-34 fall *within* this clone's longer
declared `LEN` purely because of the extra 2 cells, not because
they're a real field either) confirms this project's byte layout for
`GET_CHARGE_INFO` is now cross-checked against 11 independent
implementations, all agreeing where the real fields end.

## Cell voltages ARE available while idle - via GET_SYS_INFO, not GET_CHARGE_INFO (2026-08-02)

With the stuck-buffer incident above cleared by a power-cycle, and a
real 3S pack (`youme5200`, per the private `packs.toml` on charger-pi)
connected and idle (not yet charging, `state=2`), `GET_CHARGE_INFO`
still read all-zero pack telemetry - consistent with everything found
above, and now directly confirmed with a real, currently-connected
pack rather than inferred.

But `GET_SYS_INFO` - a different command, already parsed by this
project (`parse_sys_info()`) for verifying `set-limits` writes, but
never wired into `/metrics` - reported real, live data in the exact
same moment:

```
voltage_mv: 11139 -> 11140 (three reads, 1s apart)
cells_mv: (3816, 3831, 3852) -> (3816, 3831, 3852)
```

Genuinely live (small drift between reads, not the identical-garbage
signature of the stuck-buffer bug above) and a plausible 3S LiPo
reading. The charger's own front panel reads cell voltages directly
for its display - this confirms that same live data IS reachable over
USB, just from `GET_SYS_INFO`, not `GET_CHARGE_INFO`. `/metrics` was
updated to expose these as `charger_sysinfo_pack_millivolts`/
`charger_sysinfo_cell_millivolts`, separately from the
`GET_CHARGE_INFO`-derived metrics (which stay zero until a charge
actually starts) - see `httpd.py`'s `render_metrics()`.

## Fix: the pack-registry safety check was reading the wrong command (2026-08-03)

Direct consequence of the finding above, caught trying to actually use
`start --pack` against the real hardware: `_check_pack_cell_count_matches_device`
(cli.py) and `_build_profile_from_pack` (httpd.py) both read
`GET_CHARGE_INFO`'s `cells_mv` for the live cell-count cross-check.
Since that field is confirmed always empty while `IDLE` on this
hardware - and the check only ever runs *before* a charge starts,
i.e. always while `IDLE` - the check could never pass. `start --pack`
was structurally unusable on this specific charger, not just
overcautious: it would refuse every single time, regardless of what
was actually connected.

**Fix**: both call sites now read `GET_SYS_INFO` instead, which stays
live while idle (see above). Verified with a real pack (`youme5200`,
3S LiPo) connected and idle: the check now correctly detects 3 real
cells and lets `start --pack` proceed, where it previously refused
with "detects 0 real cell(s) connected" every time. Covered by
`test_start_with_pack_succeeds_while_idle_using_sysinfo_cells` (both
`test_cli.py` and `test_httpd.py`), which simulate the exact split:
`state=IDLE` (so `GET_CHARGE_INFO`'s own cells would be empty) while
`GET_SYS_INFO`'s cells are real.

**Not yet independently verified**: that `GET_SYS_INFO`'s `cells_mv`
correctly reads empty/filtered when NO battery is connected at all
(only the positive case - real battery connected - has been directly
tested). Structurally this should behave the same as `GET_CHARGE_INFO`'s
cell array (same ADC-based balance-tap read, same `CELL_MIN_MV`/
`CELL_MAX_MV` filter applied), so the floating-pin-noise and
no-battery-near-zero behaviour already proven for `GET_CHARGE_INFO`
should carry over - but this is inference from a shared code path, not
a separate confirmed test. Worth doing if a genuinely no-battery
moment is convenient to check.

## Feature: post-start verification, closing the "sent != confirmed" gap (2026-08-03)

Trying `start --pack youme5200` against real hardware (with the
`GET_SYS_INFO` fix above already deployed) surfaced a real gap:
the pre-start check passed on one attempt but the command still
appeared to not take effect, and on a later attempt the pre-start
check itself returned 0 cells despite `b6ctl sysinfo` showing 3 real
cells moments later via the identical code path. Root cause was the
still-active stuck-buffer corruption from the self-inflicted incident
above (not yet cleared by a restart at that point) - but the
underlying gap was real regardless: `start_charging()` sent a command
and declared "sent." with no confirmation the charger actually did
anything with it.

**Fix**: `Device.start_charging_verified()` (device.py) sends
`START_CHARGING`, waits, then re-reads `GET_CHARGE_INFO` to confirm
`state == CHARGING` with the expected cell count. An explicit `ERROR`
state from the charger (it has its own preflight - e.g.
`CELL_NUMBER_INCORRECT` is a real, protocol-defined code) is treated
as immediately authoritative, no retry - the charger's own validation
doesn't need second-guessing. Any other non-matching read gets exactly
one retry after a short delay before concluding a genuine mismatch and
sending `STOP_CHARGING` - this guards against a single transient bad
read (proven possible today, independent of this bug) causing an
unnecessary abort of an otherwise-fine charge, without ever leaving a
send unconfirmed. Both `b6ctl start` and `POST /start` use this now;
`b6ctl` prints `sent and confirmed: ...` or aborts with a clear reason,
`POST /start` returns `200 {"confirmed": true, ...}` or
`409 {"confirmed": false, "stopped": true, "error": ...}`.

## Finding + fix: temp is a session-scoped field, only trustworthy while CHARGING (2026-08-03)

Two rounds to get this right, both from the same underlying
misunderstanding.

**Round 1**: `parse_charge_info()` decoded `temp_ext_c`/`temp_int_c`
during IDLE/ERROR unconditionally, on the theory it was a
charger-hardware sensor independent of pack state (a restart test on
2026-08-02 had already partly disproven this - temp read `0` right
after boot, not a plausible value - but the fix at the time was only
a correction to documented confidence, not a code change).

**Round 2, triggered by a user question** ("go look at the other b6
charger apps and see where they get their internal temp from... it
used to work in all cases?"): a background research pass re-checked
10+ independent implementations and found `buxtronix/b6max`'s own
README contains a REAL captured example from genuine (non-clone)
SkyRC IMAX B6AC V2 hardware, while Idle:

```
State      Time    mAh       Voltage  Current   TempExt  TempInt  Imp ...
Idle           0       0    8.000v     0.26A     42C       248C      0Ω   20.483v  53.506v ...
```

`TempInt=248C` and `20-53v` "cell" readings - all physically
impossible, on hardware with no concurrent access at all (ruling out
this project's own earlier "cross-command reply confusion" theory as
the root cause of similar-looking corruption seen here - that
corruption was real, but this specific symptom - implausible values
while idle - turns out to be how the whole hardware family behaves,
not something this project's own concurrent polling caused).

**First fix**: a plausibility filter, `TEMP_MIN_C`/`TEMP_MAX_C` (-20 to
100), the same pattern `cells_mv` already uses via `CELL_MIN_MV`/
`CELL_MAX_MV`. `temp_ext_c`/`temp_int_c` became `int | None` on
`ChargeInfo` - `None` when outside the plausible range, `/metrics`
omits the metric entirely rather than emit a fake number (matching how
`charger_error_code` is already conditionally emitted).

**That wasn't enough, confirmed the same day**: with a charge complete
and the pack disconnected around 15:30, the Grafana dashboard's
internal-temp panel stayed pinned at a completely plausible-looking
`24C` for hours afterward - not garbage, just frozen. This is the
SAME staleness pattern already proven for capacity/voltage/cells (see
the "stale voltage/current after disconnect" finding above) - except
for those fields, freezing at the final value IS correct behaviour
(that's the session's final result, meant to persist). Temp isn't a
session statistic, though - it reads as a live sensor, so users
reasonably expect it to track current reality, and a frozen-but-
plausible value is actively misleading in a way a `0` or an
implausible `248` never was (you'd immediately distrust `248`; `24`
looks completely normal).

**Second, complete fix**: `temp_ext_c`/`temp_int_c` are now `None` in
every state except `CHARGING`, full stop - not just IDLE/ERROR, and
not merely "if implausible". `COMPLETE` still trusts
capacity/time/voltage/cells/impedance (legitimate final session
results, freezing there is correct) but not temp specifically. The
`TEMP_MIN_C`/`TEMP_MAX_C` filter still applies as defense in depth
within `CHARGING`, in case a genuinely-live read can also produce
noise - not yet observed, but not ruled out either.

**Confirmed working end-to-end**: `render_metrics()` correctly omits
`charger_temp_internal_celsius`/`charger_temp_external_celsius` when
`ChargeInfo` state isn't `CHARGING`, verified against `--fake` for
both `COMPLETE` (metric absent) and `CHARGING` (metric present, real
value) in the same test run.

**Still genuinely unknown, not just unconfirmed**: whether ANY command
in this protocol has a source for internal/external temperature
outside an active charge. `GET_SYS_INFO`'s two previously-unexplained
bytes (offset 15-16) were checked directly on real hardware and read
`0,0` - not a hidden temp field. No other command in the 10+
cross-referenced implementations offers an alternative either. As far
as this project can tell, there simply isn't a live-idle temp source
on this hardware family, not just this clone.
