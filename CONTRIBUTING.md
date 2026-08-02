# Contributing

Thanks for looking at this. A few things that make this project a bit
different from a typical CLI tool: it sends write commands to hardware
that regulates LiPo/LiHV battery charging, so correctness bugs here have
real physical consequences, not just "the program crashed."

## Before you start

Read [`DRY_RUN.md`](DRY_RUN.md) - it documents exactly what has and
hasn't been validated against real hardware, and the worked byte-level
trace for the START_CHARGING frame. Understanding that context will
save you from re-deriving (or mis-deriving) things that are already
carefully checked.

## Setup

```bash
git clone https://github.com/THOM-AwS/b6charger-ctl
cd b6charger-ctl
pip install -e ".[dev]"
pre-commit install  # optional, but catches issues before CI does
```

## Making a change

1. **Write tests first where practical**, especially for anything in
   `b6charger/protocol.py` - frame encoding bugs are exactly the class
   of mistake this project exists to prevent, not reintroduce.
2. **Never assume a protocol detail - verify it.** Everything in
   `protocol.py` is either (a) independently hand-traced against
   [libb6](https://github.com/maciek134/libb6)'s C++ source
   (`Device.cc`/`Packet.cc`/`Enum.hh`), or (b) confirmed against real
   hardware output and logged in `DRY_RUN.md`. If you're adding a new
   command, do the same: cite the source you derived the byte layout
   from, and add a hand-traced literal-bytes test (see
   `test_start_charging_frame_matches_hand_traced_bytes` in
   `tests/test_protocol.py` for the pattern).
3. **New write paths need a dry-run mode and a confirmation step**,
   matching the existing `start`/`stop`/`set-limits` commands. Don't
   add a way to write to the device that skips both.
4. **Run the full check locally** before opening a PR:
   ```bash
   pytest -v
   ruff check .
   black --check .
   bandit -r b6charger/
   ```
   All of this also runs in CI, but it's much faster to catch locally.
5. **Every function gets a docstring.** `ruff`'s pydocstyle rules
   (`D` in `pyproject.toml`) enforce this - `ruff check .` will tell you
   what's missing.

## If you test against real hardware

Please update `DRY_RUN.md` with what you tested and what happened -
that file is the project's memory of what's actually been proven
against a physical charger versus what's still "implemented from the
spec, unverified." A PR that changes protocol.py's write path without
a corresponding hardware note is a reasonable thing for a reviewer to
push back on.

## Reporting a protocol difference on your hardware

This was built and tested against a Jaycar POWERTECH PLUS MB-3633
(JMB-3633), a SkyRC iMAX B6-family clone. If you're running this
against a different charger and something doesn't match (a different
byte layout, a command that doesn't respond the way this project
expects), please open an issue with:

- The exact charger model/brand.
- What command you ran (`b6ctl -v ...` so the frame hex is logged).
- What you expected versus what actually happened.

Given the safety stakes, protocol mismatches are exactly the kind of
issue worth over-reporting rather than working around silently.
