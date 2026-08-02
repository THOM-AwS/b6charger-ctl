# SPDX-License-Identifier: GPL-3.0-or-later
"""Thin HTTP control surface next to ht-infra's read-only b6_poller exporter.

Deliberately separate from that exporter (different port, different
process) rather than folded in - a monitoring endpoint and a control
endpoint have very different blast radii if either has a bug.

Binds to 127.0.0.1 by default. Widening that is a real decision (this
process can command a LiPo charger over the network) - pass --host/
--port or --listen explicitly if you actually want that, don't default
to it.

Every write is logged before being sent, same as the CLI - this is the
audit trail for "what did this thing tell the charger to do and when".

Note: unlike the CLI, this doesn't currently support the packs.toml
registry / cell-count cross-check - /start here takes a raw chemistry/
cells/current body. If you build automation against this endpoint,
consider porting that same safety check here first.
"""

from __future__ import annotations

import argparse
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from b6charger import protocol
from b6charger.device import Device
from b6charger.transport import FakeChargerTransport, HidRawTransport

log = logging.getLogger("b6charger.httpd")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9111


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


def make_handler(device: Device) -> type[BaseHTTPRequestHandler]:
    """Build a BaseHTTPRequestHandler subclass bound to a specific `device`.

    A class (not an instance) is what ThreadingHTTPServer expects; this
    closure is the standard way to give every request handler access to
    the same shared Device without using a global.
    """

    class Handler(BaseHTTPRequestHandler):
        """Handles one HTTP connection: GET /status, POST /start, POST /stop."""

        def _json(self, code: int, body: dict) -> None:
            """Write `body` as a JSON response with the given status code."""
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            """Handle GET /status; anything else is a 404."""
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
            """Handle POST /start and POST /stop; anything else is a 404."""
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

            if self.path == "/start":
                try:
                    mode = protocol.ChargingModeLi[body.get("mode", "balance").upper()]
                    profile = protocol.lipo_profile(
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
                    return
                log.info(
                    "POST /start from %s: %s cells=%s current_ma=%s",
                    self.client_address[0],
                    profile.battery_type.name,
                    profile.cell_count,
                    profile.charge_current_ma,
                )
                device.start_charging(profile)
                self._json(200, {"ok": True})
                return

            self._json(404, {"error": "not found"})

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
    p.add_argument("--device", help="explicit /dev/hidrawN (default: auto-discover)")
    p.add_argument("--fake", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="log writes, send nothing")
    return p


def resolve_host_port(args: argparse.Namespace) -> tuple[str, int]:
    """Work out the (host, port) to bind from parsed args.

    --listen and --host/--port are mutually exclusive - exits with a
    usage error (via ArgumentParser.error, so it looks like any other
    argparse mistake) if both forms were given. Defaults to
    127.0.0.1:9111 (DEFAULT_HOST/DEFAULT_PORT) if neither is given.
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

    server = ThreadingHTTPServer((host, port), make_handler(device))
    log.info("listening on %s:%s (dry_run=%s)", host, port, args.dry_run)
    server.serve_forever()


if __name__ == "__main__":
    main()
