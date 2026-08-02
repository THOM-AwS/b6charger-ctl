# Dry-run status

**Nothing in this repo has been sent to real hardware yet.** Everything
below was validated against `FakeChargerTransport` (an in-memory
simulated charger) and hand-traced arithmetic, not a physical device.
The one thing that *has* been proven against a real Jaycar POWERTECH
PLUS MB-3633 is the `GET_CHARGE_INFO` read path - via ht-infra's
`b6_poller.py`, which uses the identical frame this repo's
`build_get_charge_info()` produces.

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

Run in this order, on `charger-pi`, with `b6_poller.py`'s exporter
stopped first (it also opens the hidraw device - don't run both at
once against the same charger):

1. `b6ctl --fake status` - sanity check the CLI itself works (no
   hardware involved).
2. `b6ctl status` - confirm the real read path still works via this
   repo's own parser (should match what Grafana already shows).
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
