# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared start-request construction, used identically by cli.py and httpd.py.

`b6ctl start --pack NAME` and `POST /start {"pack": "NAME", ...}` are
meant to be the same operation reached two ways - but their profile-
building logic used to be two separate implementations (one in cli.py,
one in httpd.py) that had already drifted apart twice: once caught (the
pack cell-count check reading the wrong GET_*_INFO command in each place
independently, see DRY_RUN.md's 2026-08-03 entry) and once not (the HTTP
path was missing the same input-validation try/except the CLI path had,
so a malformed `mode` or `current_ma` in a pack-based POST body crashed
the request handler instead of returning a clean error).

This module is now the one place that logic lives. Both interfaces call
`build_pack_profile`/`build_raw_profile` and translate the resulting
typed exceptions to their own presentation layer (stderr+exit for the
CLI, an HTTP status code for the daemon) - neither reimplements the
validation itself.
"""

from __future__ import annotations

from typing import Any, cast

from b6charger import packs, protocol
from b6charger.device import Device


class InvalidStartRequest(Exception):
    """A start request (raw or pack-based) had a bad, missing, or wrong-typed field.

    Wraps whatever the underlying parsing would otherwise raise
    (KeyError, ValueError, TypeError, AttributeError - JSON's dynamic
    typing means all four are reachable from attacker- or typo-supplied
    input) into one exception type both interfaces can catch, instead of
    each needing to know and enumerate the raw parsing exception types
    itself. The message is written to be safe to return directly to an
    HTTP caller as well as to print to stderr.
    """


def _as_int(value: object, field_name: str) -> int:
    """Coerce `value` to int for a start-request field, or raise InvalidStartRequest.

    Guards every shape JSON can hand us for a field that's supposed to
    be numeric: a non-numeric string (ValueError), or a type int()
    can't coerce at all, like a list/dict/None (TypeError) - a gap the
    HTTP raw-body path used to have, since it only caught ValueError.
    `value` is deliberately `object`, not `int | str`, because JSON's
    dynamic typing means it could genuinely be anything at runtime -
    `cast(Any, ...)` here just tells the type checker that the
    following try/except is the real validation, not a shortcut around it.
    """
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        raise InvalidStartRequest(f"{field_name} must be a number, got {value!r}") from None


def parse_mode(raw_mode: object) -> protocol.ChargingModeLi:
    """Parse a chemistry mode name (e.g. "balance") into a ChargingModeLi.

    Raises InvalidStartRequest for anything that isn't a valid mode
    name - including a non-string value, which a bare `.upper()` call
    lets through as a raw, uncaught AttributeError.
    """
    if not isinstance(raw_mode, str):
        raise InvalidStartRequest(f"mode must be a string, got {raw_mode!r}")
    try:
        return protocol.ChargingModeLi[raw_mode.upper()]
    except KeyError:
        valid = ", ".join(m.name.lower() for m in protocol.ChargingModeLi)
        raise InvalidStartRequest(
            f"unknown mode {raw_mode!r} - must be one of: {valid}"
        ) from None


def build_raw_profile(body: dict[str, Any]) -> protocol.ChargeProfile:
    """Build a ChargeProfile from a raw {"chemistry","cells","current_ma",...} body.

    The manual/raw path - equivalent to `b6ctl start` without `--pack`.
    No pack-registry cross-check here (that's `build_pack_profile`).
    Raises InvalidStartRequest on any bad/missing field - never a raw
    KeyError/ValueError/TypeError/AttributeError.
    """
    if "cells" not in body:
        raise InvalidStartRequest("missing required field 'cells'")
    if "current_ma" not in body:
        raise InvalidStartRequest("missing required field 'current_ma'")

    cells = _as_int(body["cells"], "cells")
    current_ma = _as_int(body["current_ma"], "current_ma")
    discharge_current_ma = _as_int(
        body.get("discharge_current_ma", protocol.DEFAULT_DISCHARGE_CURRENT_MA),
        "discharge_current_ma",
    )
    mode = parse_mode(body.get("mode", "balance"))

    try:
        return protocol.lipo_profile(
            cell_count=cells,
            charge_current_ma=current_ma,
            mode=mode,
            hv=(body.get("chemistry") == "lihv"),
            discharge_current_ma=discharge_current_ma,
        )
    except protocol.ProtocolError as e:
        raise InvalidStartRequest(str(e)) from e


def build_pack_profile(
    device: Device,
    registry: packs.PackRegistry,
    pack_name: str,
    current_ma: object = None,
    mode: object = "balance",
    discharge_current_ma: object = None,
) -> protocol.ChargeProfile:
    """Build a ChargeProfile for `pack_name`, running the full pack safety pipeline.

    THE single implementation of "start --pack" / POST {"pack": ...}:
    look up the pack, run the live cell-count cross-check
    (packs.check_cell_count, via GET_SYS_INFO - not GET_CHARGE_INFO,
    which is always empty while idle on this hardware, see DRY_RUN.md),
    apply the pack's max_current_ma ceiling to BOTH charge and discharge
    current, and parse the mode - in that order, so a config/lookup
    problem is reported before any hardware I/O, and a live-hardware
    mismatch is reported before any bad-input problem masks it.

    Raises packs.PackConfigError (unknown pack), packs.PackCellMismatch
    (live cell count doesn't match), or InvalidStartRequest (bad mode /
    current_ma) - never a raw KeyError/ValueError/TypeError/
    AttributeError. Does NOT catch exceptions from `device.get_sys_info()`
    itself (e.g. DeviceTimeout, OSError) - a transport failure is a
    different class of problem than a bad request, and each caller maps
    it to its own presentation (an HTTP 502, or letting it propagate to
    the CLI's own top-level handler).
    """
    pack = registry.get(pack_name)  # raises PackConfigError

    info = device.get_sys_info()
    packs.check_cell_count(pack, len(info.cells_mv))  # raises PackCellMismatch

    resolved_current_ma = _as_int(
        pack.default_current_ma if current_ma is None else current_ma, "current_ma"
    )
    if resolved_current_ma > pack.max_current_ma:
        raise InvalidStartRequest(
            f"current_ma {resolved_current_ma} exceeds pack '{pack.name}'s "
            f"max_current_ma ({pack.max_current_ma})"
        )

    resolved_discharge_ma = _as_int(
        (
            protocol.DEFAULT_DISCHARGE_CURRENT_MA
            if discharge_current_ma is None
            else discharge_current_ma
        ),
        "discharge_current_ma",
    )
    if resolved_discharge_ma > pack.max_current_ma:
        raise InvalidStartRequest(
            f"discharge_current_ma {resolved_discharge_ma} exceeds pack '{pack.name}'s "
            f"max_current_ma ({pack.max_current_ma})"
        )

    chemistry_mode = parse_mode(mode)

    try:
        return protocol.lipo_profile(
            cell_count=pack.cells,
            charge_current_ma=resolved_current_ma,
            mode=chemistry_mode,
            hv=pack.is_hv,
            discharge_current_ma=resolved_discharge_ma,
        )
    except protocol.ProtocolError as e:
        raise InvalidStartRequest(str(e)) from e
