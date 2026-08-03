# SPDX-License-Identifier: GPL-3.0-or-later
"""Persists the most recently commanded START_CHARGING profile to disk.

`GET_CHARGE_INFO` has no field reporting back what chemistry the
charger is currently charging (see protocol.py's `ChargeInfo`
docstring - it isn't in the byte ranges libb6 or this project has ever
verified). The only chemistry information that exists anywhere is
what THIS tool told it to start with. `record()` is called from both
cli.py's `start` and httpd.py's `POST /start` (the two places a charge
actually gets commanded); `read()` is used by httpd.py's
render_metrics() to expose it as an "info"-style gauge - the standard
Prometheus convention for string data: a metric that's always 1, with
the real value carried in labels.

Deliberately a file, not in-memory daemon state: `serve` alone has no
visibility into a charge started via a separate `b6ctl start`
invocation (a different OS process, e.g. run by hand over SSH) - which
is how this tool is actually used day to day.

Labeled honestly as "what we last commanded", not "what the charger
confirms" - there's no confirmation available (see above), so a wrong
command (e.g. a typo'd --pack) shows up here exactly as sent, same as
it would on the charger itself.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass

from b6charger import protocol

# Same /tmp threat-model reasoning as transport.py's DEFAULT_LOCK_PATH:
# single-purpose device, one operator account, not a shared multi-user
# host - mitigated with O_NOFOLLOW against a pre-existing symlink.
DEFAULT_PATH = "/tmp/b6charger-ctl-last-start.json"  # nosec B108


@dataclass(frozen=True)
class LastStart:
    """What was last commanded to start a charge, and when."""

    timestamp: float
    battery_type: str
    cells: int
    current_ma: int
    mode: str | None
    pack: str | None


def _mode_name(profile: protocol.ChargeProfile) -> str | None:
    """Whichever chemistry-family mode enum `profile` actually set, by name."""
    for mode in (profile.mode_li, profile.mode_ni, profile.mode_pb):
        if mode is not None:
            return mode.name
    return None


def record(
    profile: protocol.ChargeProfile,
    pack: str | None = None,
    path: str | None = None,
) -> None:
    """Persist `profile` (and `pack`'s name, if `start --pack` was used).

    `path` defaults to the module-level DEFAULT_PATH, looked up at call
    time (not bound as a default argument) so tests can monkeypatch
    last_start.DEFAULT_PATH and have every caller pick it up.

    Best-effort: failing to record this is a monitoring-accuracy
    problem, not a reason to fail an already-sent charge command, so
    any OSError is swallowed rather than raised. Recovers from a stale
    file left unwritable by a different user the same way
    transport.py's lock file does - see that module's
    `_open_lock_fd()` for why this happens on a shared /tmp path.
    """
    path = path if path is not None else DEFAULT_PATH
    entry = LastStart(
        timestamp=time.time(),
        battery_type=profile.battery_type.name,
        cells=profile.cell_count,
        current_ma=profile.charge_current_ma,
        mode=_mode_name(profile),
        pack=pack,
    )
    body = json.dumps(asdict(entry)).encode()
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o644)
    except PermissionError:
        try:
            os.unlink(path)
            fd = os.open(path, flags, 0o644)
        except OSError:
            return
    except OSError:
        return
    try:
        os.write(fd, body)
    except OSError:
        pass
    finally:
        os.close(fd)


def read(path: str | None = None) -> LastStart | None:
    """Return the last-recorded start, or None if there isn't one / it can't be read.

    `path` defaults to DEFAULT_PATH, looked up at call time - see
    record()'s docstring for why.
    """
    path = path if path is not None else DEFAULT_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return LastStart(**data)
    except TypeError:
        return None
