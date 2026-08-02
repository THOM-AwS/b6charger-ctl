# SPDX-License-Identifier: GPL-3.0-or-later
"""One daemon serving both read (metrics/status) and write (start/stop) HTTP endpoints.

Two independent safety levers, not one:

1. **Network exposure** (`--host`/`--port`/`--listen`) - defaults to
   `0.0.0.0`, since `/metrics` and `/status` are read-only and safe to
   expose broadly (e.g. for Prometheus scraping from another host).
2. **Write capability** (`--enable-writes`) - OFF by default,
   regardless of bind address. Without it, `POST /start` and
   `POST /stop` return 403 immediately, before touching the device at
   all. This daemon can sit on the network answering `/metrics` all
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from b6charger import packs, protocol
from b6charger.device import Device
from b6charger.transport import FakeChargerTransport, HidRawTransport

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


def render_metrics(device: Device) -> str:
    """Render the full Prometheus text-format /metrics body for `device`.

    Returns `charger_up 0` (and nothing else) if the device can't be
    read at all, matching Prometheus convention of a present-but-zero
    `up`-style metric rather than a scrape failure for "device
    unplugged" - that's an expected, common state, not an error.
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
            f"# HELP charger_error_code Error code while state is ERROR_1/ERROR_2 "
            f"({info.error_name}). Only present in an error state.",
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
            """
            try:
                registry = packs.load_registry()
                pack = registry.get(str(body["pack"]))
            except packs.PackConfigError as e:
                self._json(400, {"error": str(e)})
                return None

            try:
                info = device.get_charge_info()
            except Exception as e:  # noqa: BLE001 - surface to caller, don't swallow
                log.exception("get_charge_info failed during pack cell-count check")
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


def build_parser() -> argparse.ArgumentParser:
    """Construct the `b6httpd` argument parser."""
    p = argparse.ArgumentParser(prog="b6httpd")
    p.add_argument("--host", default=None, help=f"interface to bind (default: {DEFAULT_HOST})")
    p.add_argument(
        "--port", type=int, default=None, help=f"port to bind (default: {DEFAULT_PORT})"
    )
    p.add_argument(
        "--listen",
        type=parse_listen_address,
        default=None,
        metavar="HOST:PORT",
        help=(
            "combined interface:port socket, e.g. --listen 0.0.0.0:9111 or "
            "--listen [::1]:9111 for IPv6. Mutually exclusive with --host/--port"
        ),
    )
    p.add_argument(
        "--enable-writes",
        action="store_true",
        help="allow POST /start and POST /stop - without this, they return 403 "
        "regardless of bind address. OFF by default.",
    )
    p.add_argument("--device", help="explicit /dev/hidrawN (default: auto-discover)")
    p.add_argument("--fake", action="store_true")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="with --enable-writes, log writes but send nothing",
    )
    p.add_argument(
        "--cache-seconds",
        type=float,
        default=DEFAULT_CACHE_S,
        help=f"reuse a rendered /metrics body for this many seconds "
        f"(default: {DEFAULT_CACHE_S})",
    )
    return p


def resolve_host_port(args: argparse.Namespace) -> tuple[str, int]:
    """Work out the (host, port) to bind from parsed args.

    --listen and --host/--port are mutually exclusive - exits with a
    usage error (via ArgumentParser.error, so it looks like any other
    argparse mistake) if both forms were given. Defaults to
    DEFAULT_HOST:DEFAULT_PORT if neither is given.
    """
    if args.listen is not None and (args.host is not None or args.port is not None):
        build_parser().error(
            "--listen cannot be combined with --host/--port - use one or the other"
        )
    if args.listen is not None:
        return args.listen
    return (
        args.host if args.host is not None else DEFAULT_HOST,
        args.port if args.port is not None else DEFAULT_PORT,
    )


def main(argv: list[str] | None = None) -> None:
    """Entry point for the `b6httpd` console script: parse args and serve forever."""
    args = build_parser().parse_args(argv)
    host, port = resolve_host_port(args)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    transport = FakeChargerTransport() if args.fake else HidRawTransport(args.device)
    device = Device(transport, dry_run=args.dry_run)
    metrics_cache = MetricsCache(lambda: render_metrics(device), args.cache_seconds)

    server = ThreadingHTTPServer(
        (host, port), make_handler(device, metrics_cache, args.enable_writes)
    )
    log.info(
        "listening on %s:%s (enable_writes=%s, dry_run=%s)",
        host,
        port,
        args.enable_writes,
        args.dry_run,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
