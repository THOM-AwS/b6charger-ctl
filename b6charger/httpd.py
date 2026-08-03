# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP daemon internals for `b6ctl serve` - metrics plus optional start/stop control.

Everything here is used by the `serve` subcommand in `cli.py`; there is
no separate daemon binary or argument parser in this module - `b6ctl`
is the only entry point, and `serve` is one more mode of it alongside
`status`/`start`/`stop`/etc.

Two independent safety levers, not one:

1. **Network exposure** (`--host`/`--port`/`--listen`) - defaults to
   `0.0.0.0`, since `/metrics` and `/status` are read-only and safe to
   expose broadly (e.g. for Prometheus scraping from another host).
2. **Write capability** (`--enable-writes`) - OFF by default,
   regardless of bind address. Without it, `POST /start` and
   `POST /stop` return 403 immediately, before touching the device at
   all. `b6ctl serve` can sit on the network answering `/metrics` all
   day with zero ability for anyone to command the charger, unless you
   deliberately started it with `--enable-writes`.

`--dry-run` is a third, independent layer on top of `--enable-writes`:
with both set, the write endpoints are reachable and behave normally
except nothing is actually sent to the device - useful for exercising
the HTTP write path against real (or `--fake`) hardware state without
commanding anything for real.

Every write is logged with the caller's address before being sent -
the audit trail for "what did this thing tell the charger to do and
when".

Metrics use the same names this project's exporter has always used
(`charger_state`, `charger_cell_millivolts{cell="N"}`, etc.), plus
`charger_impedance_milliohms` (the data was always in `GET_CHARGE_INFO`,
just not decoded before) and the `CELL_MIN_MV`/`CELL_MAX_MV` range
filter from `protocol.py` instead of a floor-only check - see that
module's docstring for why the floor alone lets floating-pin noise
through.

`POST /start` accepts either a raw `{"chemistry", "cells",
"current_ma", ...}` body, or `{"pack": "name", ...}` to use a
`packs.toml` entry - the latter runs the exact same live cell-count
cross-check as `b6ctl start --pack` (packs.check_cell_count), returning
409 on a mismatch instead of exiting a process.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler

from b6charger import last_start, packs, protocol
from b6charger.device import Device

log = logging.getLogger("b6charger.httpd")

# Deliberate default, not an oversight: this is a read-only-by-default
# daemon (write endpoints need --enable-writes regardless of bind
# address - see module docstring), so binding all interfaces by
# default is intentional for making /metrics conveniently scrapeable.
DEFAULT_HOST = "0.0.0.0"  # nosec B104
DEFAULT_PORT = 9111
DEFAULT_CACHE_S = 5.0

STATE_HELP = {s.value: s.name for s in protocol.State}


def parse_listen_address(value: str) -> tuple[str, int]:
    """Parse a HOST:PORT socket address, as used by --listen.

    Accepts plain `HOST:PORT` (e.g. "0.0.0.0:9111") or bracketed IPv6
    (e.g. "[::1]:9111" - required for IPv6 since the address itself
    contains colons). Raises argparse.ArgumentTypeError with a clear
    message on anything that doesn't parse, so argparse reports it as
    a normal usage error rather than a traceback.
    """
    if value.startswith("["):
        host, sep, port_str = value[1:].partition("]:")
    else:
        host, sep, port_str = value.rpartition(":")

    if not sep:
        raise argparse.ArgumentTypeError(
            f"invalid --listen value {value!r} - expected HOST:PORT "
            "(e.g. 0.0.0.0:9111) or [HOST]:PORT for IPv6 (e.g. [::1]:9111)"
        )

    try:
        port = int(port_str)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid --listen value {value!r} - {port_str!r} is not a valid port number"
        ) from None

    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            f"invalid --listen value {value!r} - port {port} out of range 1-65535"
        )

    return host, port


class MetricsCache:
    """Caches the last rendered /metrics body for `cache_s` seconds.

    Scrapers (Prometheus, or two requests close together) can otherwise
    trigger overlapping reads of the same physical device - the cache
    means a burst of near-simultaneous requests only actually polls
    hardware once. `HidRawTransport`'s own flock keeps concurrent polls
    safe either way; this just avoids the redundant hardware chatter.
    """

    def __init__(self, render, cache_s: float = DEFAULT_CACHE_S) -> None:
        """Wrap `render` (a zero-arg callable returning the metrics text)."""
        self._render = render
        self._cache_s = cache_s
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._body = ""

    def get(self) -> str:
        """Return a rendered metrics body, reusing a recent one if still fresh."""
        with self._lock:
            if time.monotonic() - self._cached_at < self._cache_s:
                return self._body
            self._body = self._render()
            self._cached_at = time.monotonic()
            return self._body


def _escape_label(value: str) -> str:
    """Escape a Prometheus label value's backslashes and double quotes."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _last_commanded_lines() -> list[str]:
    """Render the charger_last_commanded_* metrics from last_start.read().

    See last_start.py's module docstring for why this exists: the
    charger has no field reporting back what chemistry it's currently
    charging, so this is what WE told it to start with, not a
    confirmation from the device. Returns an empty list (nothing
    rendered) if `start` has never been called against this state
    directory - there's nothing honest to report yet.
    """
    entry = last_start.read()
    if entry is None:
        return []
    labels = (
        f'battery_type="{_escape_label(entry.battery_type)}",'
        f'cells="{entry.cells}",'
        f'mode="{_escape_label(entry.mode or "")}",'
        f'pack="{_escape_label(entry.pack or "")}"'
    )
    return [
        "# HELP charger_last_commanded_info What this tool last told the charger to "
        "start with - NOT read back from the charger, which has no field for it.",
        "# TYPE charger_last_commanded_info gauge",
        f"charger_last_commanded_info{{{labels}}} 1",
        "# HELP charger_last_commanded_timestamp_seconds Unix time that command was sent.",
        "# TYPE charger_last_commanded_timestamp_seconds gauge",
        f"charger_last_commanded_timestamp_seconds {entry.timestamp}",
        "# HELP charger_last_commanded_current_milliamps Charge current last commanded.",
        "# TYPE charger_last_commanded_current_milliamps gauge",
        f"charger_last_commanded_current_milliamps {entry.current_ma}",
    ]


def _sysinfo_lines(device: Device) -> list[str]:
    """Render charger_sysinfo_* metrics from GET_SYS_INFO.

    A live cell-voltage source GET_CHARGE_INFO doesn't have. Confirmed
    2026-08-02 (see DRY_RUN.md): GET_CHARGE_INFO's own voltage/cells
    stay zero while the charger is IDLE, even with a real
    battery connected - this clone's firmware just doesn't populate
    them there. GET_SYS_INFO (already parsed by this project for
    verifying set-limits writes, but never wired into /metrics before)
    reports live, genuinely-drifting voltage/cells in that same state.
    Separate metric names from the GET_CHARGE_INFO-derived ones
    deliberately - they come from a different command and can diverge
    (e.g. mid-charge, GET_CHARGE_INFO's current/capacity are the
    authoritative in-session numbers; this is a general system read).

    Failure here is independent of GET_CHARGE_INFO's own success or
    failure - a separate command, separate try/except, called from
    both branches of render_metrics().
    """
    try:
        info = device.get_sys_info()
    except Exception:  # noqa: BLE001 - sysinfo is best-effort, not core to charger_up
        log.exception("get_sys_info failed")
        return []

    lines = [
        "# HELP charger_sysinfo_pack_millivolts Pack voltage from GET_SYS_INFO - "
        "unlike charger_pack_millivolts, stays live while idle.",
        "# TYPE charger_sysinfo_pack_millivolts gauge",
        f"charger_sysinfo_pack_millivolts {info.voltage_mv}",
    ]
    if info.cells_mv:
        lines.append(
            "# HELP charger_sysinfo_cell_millivolts Per-cell voltage from "
            "GET_SYS_INFO - stays live while idle."
        )
        lines.append("# TYPE charger_sysinfo_cell_millivolts gauge")
        for i, mv in enumerate(info.cells_mv, 1):
            lines.append(f'charger_sysinfo_cell_millivolts{{cell="{i}"}} {mv}')
        lines.append("# HELP charger_sysinfo_cell_count Number of real cells detected.")
        lines.append("# TYPE charger_sysinfo_cell_count gauge")
        lines.append(f"charger_sysinfo_cell_count {len(info.cells_mv)}")
        lines.append(
            "# HELP charger_sysinfo_cell_spread_millivolts Max-min cell voltage spread."
        )
        lines.append("# TYPE charger_sysinfo_cell_spread_millivolts gauge")
        spread = max(info.cells_mv) - min(info.cells_mv)
        lines.append(f"charger_sysinfo_cell_spread_millivolts {spread}")

    lines += [
        "# HELP charger_sysinfo_temp_limit_celsius Configured internal "
        "temperature cutoff - see 'set-limits --temp-limit'.",
        "# TYPE charger_sysinfo_temp_limit_celsius gauge",
        f"charger_sysinfo_temp_limit_celsius {info.temp_limit_c}",
        "# HELP charger_sysinfo_low_dc_limit_millivolts Configured low-input-"
        "voltage cutoff.",
        "# TYPE charger_sysinfo_low_dc_limit_millivolts gauge",
        f"charger_sysinfo_low_dc_limit_millivolts {info.low_dc_limit_mv}",
        "# HELP charger_sysinfo_time_limit_minutes Configured charge time-"
        "limit cutoff - see 'set-limits --time-limit'.",
        "# TYPE charger_sysinfo_time_limit_minutes gauge",
        f"charger_sysinfo_time_limit_minutes {info.time_limit_minutes}",
        "# HELP charger_sysinfo_time_limit_enabled Whether the time-limit "
        "cutoff above is currently enforced.",
        "# TYPE charger_sysinfo_time_limit_enabled gauge",
        f"charger_sysinfo_time_limit_enabled {int(info.time_limit_on)}",
        "# HELP charger_sysinfo_capacity_limit_mah Configured charge "
        "capacity-limit cutoff - see 'set-limits --capacity-limit'.",
        "# TYPE charger_sysinfo_capacity_limit_mah gauge",
        f"charger_sysinfo_capacity_limit_mah {info.capacity_limit_mah}",
        "# HELP charger_sysinfo_capacity_limit_enabled Whether the capacity-"
        "limit cutoff above is currently enforced.",
        "# TYPE charger_sysinfo_capacity_limit_enabled gauge",
        f"charger_sysinfo_capacity_limit_enabled {int(info.capacity_limit_on)}",
    ]
    return lines


def render_metrics(device: Device) -> str:
    """Render the full Prometheus text-format /metrics body for `device`.

    Returns `charger_up 0` (plus charger_last_commanded_*/
    charger_sysinfo_*, if any) if GET_CHARGE_INFO can't be read at all,
    matching Prometheus convention of a present-but-zero `up`-style
    metric rather than a scrape failure for "device unplugged" - that's
    an expected, common state, not an error. charger_last_commanded_*/
    charger_sysinfo_* are included even here since neither depends on
    GET_CHARGE_INFO succeeding - still useful context either way.
    """
    lines = [
        "# HELP charger_up Whether the charger is connected and answering.",
        "# TYPE charger_up gauge",
    ]
    try:
        info = device.get_charge_info()
    except Exception:  # noqa: BLE001 - any failure here means charger_up 0
        log.exception("get_charge_info failed")
        lines.append("charger_up 0")
        lines += _last_commanded_lines()
        lines += _sysinfo_lines(device)
        return "\n".join(lines) + "\n"

    lines.append("charger_up 1")
    states_help = ", ".join(f"{k}={v}" for k, v in sorted(STATE_HELP.items()))
    lines += [
        f"# HELP charger_state Charger state ({states_help}).",
        "# TYPE charger_state gauge",
        f"charger_state {info.state}",
    ]
    if info.error_code is not None:
        lines += [
            f"# HELP charger_error_code Error code while state is ERROR "
            f"({info.error_name}). Only present in the ERROR state.",
            "# TYPE charger_error_code gauge",
            f"charger_error_code {info.error_code}",
        ]
    lines += [
        "# HELP charger_capacity_mah Capacity delivered so far this charge, in mAh.",
        "# TYPE charger_capacity_mah gauge",
        f"charger_capacity_mah {info.capacity_mah}",
        "# HELP charger_elapsed_seconds Elapsed time this charge, in seconds.",
        "# TYPE charger_elapsed_seconds gauge",
        f"charger_elapsed_seconds {info.time_s}",
        "# HELP charger_pack_millivolts Total pack voltage, in millivolts.",
        "# TYPE charger_pack_millivolts gauge",
        f"charger_pack_millivolts {info.voltage_mv}",
        "# HELP charger_current_milliamps Charge/discharge current, in milliamps.",
        "# TYPE charger_current_milliamps gauge",
        f"charger_current_milliamps {info.current_ma}",
        "# HELP charger_temp_internal_celsius Charger's own internal temperature.",
        "# TYPE charger_temp_internal_celsius gauge",
        f"charger_temp_internal_celsius {info.temp_int_c}",
        "# HELP charger_temp_external_celsius External temp probe reading (0 if unplugged).",
        "# TYPE charger_temp_external_celsius gauge",
        f"charger_temp_external_celsius {info.temp_ext_c}",
        "# HELP charger_impedance_milliohms Pack internal resistance, in milliohms.",
        "# TYPE charger_impedance_milliohms gauge",
        f"charger_impedance_milliohms {info.impedance_mohm}",
    ]

    cells = info.cells_mv
    if cells:
        lines.append("# HELP charger_cell_millivolts Per-cell voltage, in millivolts.")
        lines.append("# TYPE charger_cell_millivolts gauge")
        for i, mv in enumerate(cells, 1):
            lines.append(f'charger_cell_millivolts{{cell="{i}"}} {mv}')
        lines.append("# HELP charger_cell_count Number of real cells detected.")
        lines.append("# TYPE charger_cell_count gauge")
        lines.append(f"charger_cell_count {len(cells)}")
        lines.append("# HELP charger_cell_spread_millivolts Max-min cell voltage spread.")
        lines.append("# TYPE charger_cell_spread_millivolts gauge")
        lines.append(f"charger_cell_spread_millivolts {max(cells) - min(cells)}")

    lines += _last_commanded_lines()
    lines += _sysinfo_lines(device)
    return "\n".join(lines) + "\n"


def make_handler(
    device: Device, metrics_cache: MetricsCache, enable_writes: bool
) -> type[BaseHTTPRequestHandler]:
    """Build a BaseHTTPRequestHandler subclass bound to a specific `device`.

    A class (not an instance) is what ThreadingHTTPServer expects; this
    closure is the standard way to give every request handler access to
    the same shared Device/cache/policy without using globals.
    """

    class Handler(BaseHTTPRequestHandler):
        """Handles one connection: GET /metrics, GET /status, POST /start, POST /stop."""

        def _json(self, code: int, body: dict) -> None:
            """Write `body` as a JSON response with the given status code."""
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _metrics(self) -> None:
            """Serve the cached Prometheus metrics body."""
            body = metrics_cache.get().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            """Handle GET /metrics and GET /status; anything else is a 404."""
            if self.path == "/metrics":
                self._metrics()
                return
            if self.path != "/status":
                self._json(404, {"error": "not found"})
                return
            try:
                info = device.get_charge_info()
            except Exception as e:  # noqa: BLE001 - surface to caller, don't swallow
                log.exception("get_charge_info failed")
                self._json(502, {"error": str(e)})
                return
            self._json(
                200,
                {
                    "state": info.state,
                    "state_name": info.state_name,
                    "error_code": info.error_code,
                    "error_name": info.error_name,
                    "voltage_mv": info.voltage_mv,
                    "current_ma": info.current_ma,
                    "capacity_mah": info.capacity_mah,
                    "time_s": info.time_s,
                    "temp_int_c": info.temp_int_c,
                    "temp_ext_c": info.temp_ext_c,
                    "impedance_mohm": info.impedance_mohm,
                    "cells_mv": list(info.cells_mv),
                },
            )

        def do_POST(self) -> None:  # noqa: N802 (stdlib API)
            """Handle POST /start and POST /stop; 403 if writes aren't enabled."""
            if self.path not in ("/start", "/stop"):
                self._json(404, {"error": "not found"})
                return

            if not enable_writes:
                log.warning(
                    "rejected POST %s from %s - started without --enable-writes",
                    self.path,
                    self.client_address[0],
                )
                self._json(
                    403,
                    {
                        "error": "write endpoints disabled - restart this daemon "
                        "with --enable-writes to allow start/stop"
                    },
                )
                return

            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json body"})
                return

            if self.path == "/stop":
                log.info("POST /stop from %s", self.client_address[0])
                device.stop_charging()
                self._json(200, {"ok": True})
                return

            if "pack" in body:
                profile_or_none = self._build_profile_from_pack(body)
            else:
                profile_or_none = self._build_profile_from_raw_body(body)
            if profile_or_none is None:
                return  # error response already sent by the helper above
            profile = profile_or_none

            log.info(
                "POST /start from %s: %s cells=%s current_ma=%s",
                self.client_address[0],
                profile.battery_type.name,
                profile.cell_count,
                profile.charge_current_ma,
            )
            device.start_charging(profile)
            if not device.dry_run:
                last_start.record(profile, pack=body.get("pack"))
            self._json(200, {"ok": True})

        def _build_profile_from_raw_body(self, body: dict) -> protocol.ChargeProfile | None:
            """Build a ChargeProfile from a raw {"chemistry","cells","current_ma",...} body.

            No pack-registry cross-check here - this is the manual/raw
            path, equivalent to `b6ctl start` without `--pack`. Sends a
            400 response and returns None on any validation failure.
            """
            try:
                mode = protocol.ChargingModeLi[body.get("mode", "balance").upper()]
                return protocol.lipo_profile(
                    cell_count=int(body["cells"]),
                    charge_current_ma=int(body["current_ma"]),
                    mode=mode,
                    hv=(body.get("chemistry") == "lihv"),
                    discharge_current_ma=int(
                        body.get(
                            "discharge_current_ma",
                            protocol.DEFAULT_DISCHARGE_CURRENT_MA,
                        )
                    ),
                )
            except (KeyError, ValueError, protocol.ProtocolError) as e:
                self._json(400, {"error": f"bad profile: {e}"})
                return None

        def _build_profile_from_pack(self, body: dict) -> protocol.ChargeProfile | None:
            """Build a ChargeProfile from a {"pack": "name", ...} body.

            The HTTP equivalent of `b6ctl start --pack NAME`: looks up
            the pack, runs the same live cell-count cross-check
            (packs.check_cell_count), and applies the same max-current
            ceiling - all as real HTTP error responses instead of exiting
            the process. Returns None (having already sent an error
            response) on any failure.

            Reads GET_SYS_INFO for the cell count, not GET_CHARGE_INFO -
            see cli.py's _check_pack_cell_count_matches_device for why
            (GET_CHARGE_INFO's cells_mv is always empty while idle on
            this hardware, which is exactly the state a pre-start check
            always runs in).
            """
            try:
                registry = packs.load_registry()
                pack = registry.get(str(body["pack"]))
            except packs.PackConfigError as e:
                self._json(400, {"error": str(e)})
                return None

            try:
                info = device.get_sys_info()
            except Exception as e:  # noqa: BLE001 - surface to caller, don't swallow
                log.exception("get_sys_info failed during pack cell-count check")
                self._json(502, {"error": str(e)})
                return None

            try:
                packs.check_cell_count(pack, len(info.cells_mv))
            except packs.PackCellMismatch as e:
                log.warning("rejected POST /start from %s: %s", self.client_address[0], e)
                self._json(409, {"error": str(e)})
                return None

            current_ma = int(body.get("current_ma", pack.default_current_ma))
            if current_ma > pack.max_current_ma:
                self._json(
                    400,
                    {
                        "error": f"current_ma {current_ma} exceeds pack "
                        f"'{pack.name}'s max_current_ma ({pack.max_current_ma})"
                    },
                )
                return None

            mode = protocol.ChargingModeLi[body.get("mode", "balance").upper()]
            return protocol.lipo_profile(
                cell_count=pack.cells,
                charge_current_ma=current_ma,
                mode=mode,
                hv=pack.is_hv,
                discharge_current_ma=int(
                    body.get("discharge_current_ma", protocol.DEFAULT_DISCHARGE_CURRENT_MA)
                ),
            )

        def log_message(self, *args) -> None:
            """Suppress BaseHTTPRequestHandler's default stderr access log."""

    return Handler
