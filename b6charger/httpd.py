# SPDX-License-Identifier: GPL-3.0-or-later
"""Thin HTTP control surface next to ht-infra's read-only b6_poller
exporter. Deliberately separate from that exporter (different port,
different process) rather than folded in - a monitoring endpoint and a
control endpoint have very different blast radii if either has a bug.

Binds to 127.0.0.1 by default. Widening that is a real decision (this
process can command a LiPo charger over the network) - pass --host
explicitly if you actually want that, don't default to it.

Every write is logged before being sent, same as the CLI - this is the
audit trail for "what did this thing tell the charger to do and when".
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

DEFAULT_PORT = 9111


def make_handler(device: Device) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
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

        def do_POST(self) -> None:  # noqa: N802
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

        def log_message(self, *args) -> None:  # quiet the default access log
            pass

    return Handler


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="b6httpd")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--device", help="explicit /dev/hidrawN (default: auto-discover)")
    p.add_argument("--fake", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="log writes, send nothing")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    transport = FakeChargerTransport() if args.fake else HidRawTransport(args.device)
    device = Device(transport, dry_run=args.dry_run)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(device))
    log.info("listening on %s:%s (dry_run=%s)", args.host, args.port, args.dry_run)
    server.serve_forever()


if __name__ == "__main__":
    main()
