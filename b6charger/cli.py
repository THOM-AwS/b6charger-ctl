# SPDX-License-Identifier: GPL-3.0-or-later
"""b6ctl - CLI for SkyRC B6-family chargers.

Safety design, in order of how much friction each adds:

1. `start` always prints the exact profile it's about to send and
   requires either an interactive "yes" or --auto-approve (alias
   --yes) before writing anything, unless --dry-run is given (which
   never touches the device at all).
2. `start --pack NAME` (the recommended way to start a charge - see
   "Configure your batteries" in README.md) reads the pack's
   registered cell count from packs.toml and REFUSES to send anything
   if the charger currently detects a different number of real cells
   connected. There is deliberately no flag to skip this check - if
   you need to bypass it, use the manual --chemistry/--cells/
   --current-ma flags directly, which is its own explicit action
   rather than a checkbox on the "safe" path.
3. Every write-capable subcommand accepts --dry-run and --fake, so the
   full command can be reviewed/tried against a simulated charger
   before it ever touches hardware.

This is deliberately more friction than a monitoring tool needs,
because this one can command a device that regulates LiPo/LiHV charge
voltage and current.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from http.server import ThreadingHTTPServer

from b6charger import charge_request, httpd, last_start, packs, protocol
from b6charger.device import Device
from b6charger.packs import PackConfigError
from b6charger.transport import (
    DeviceTimeout,
    FakeChargerTransport,
    HidRawTransport,
    NoChargerFound,
)

log = logging.getLogger("b6charger.cli")


def _make_transport(args: argparse.Namespace):
    """Build the Transport a subcommand should use.

    The in-memory fake if --fake was given, otherwise a real
    HidRawTransport (auto-discovering the device unless --device was
    given). Exits with a friendly one-line message (not a traceback) if
    no real charger can be found, since this is the first thing every
    real-hardware command hits.
    """
    if args.fake:
        return FakeChargerTransport()
    try:
        return HidRawTransport(args.device)
    except NoChargerFound as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_status(args: argparse.Namespace) -> None:
    """Handle `b6ctl status`: read and print the current live charge info."""
    dev = Device(_make_transport(args))
    info = dev.get_charge_info()
    if args.json:
        print(
            json.dumps(
                {
                    "state": info.state,
                    "state_name": info.state_name,
                    "error_code": info.error_code,
                    "error_name": info.error_name,
                    "capacity_mah": info.capacity_mah,
                    "time_s": info.time_s,
                    "voltage_mv": info.voltage_mv,
                    "current_ma": info.current_ma,
                    "temp_ext_c": info.temp_ext_c,
                    "temp_int_c": info.temp_int_c,
                    "impedance_mohm": info.impedance_mohm,
                    "cells_mv": list(info.cells_mv),
                }
            )
        )
        return
    print(f"state:      {info.state_name} ({info.state})")
    if info.error_name is not None:
        print(f"error:      {info.error_name} ({info.error_code})")
    print(f"pack:       {info.voltage_mv / 1000:.3f}V")
    print(f"current:    {info.current_ma / 1000:.3f}A")
    print(f"capacity:   {info.capacity_mah}mAh in {info.time_s}s")
    temp_int = f"{info.temp_int_c}C" if info.temp_int_c is not None else "unknown"
    temp_ext = f"{info.temp_ext_c}C" if info.temp_ext_c is not None else "unknown"
    print(f"temp:       int={temp_int} ext={temp_ext}")
    print(f"impedance:  {info.impedance_mohm}mOhm")
    for i, mv in enumerate(info.cells_mv, 1):
        print(f"  cell {i}:   {mv / 1000:.3f}V")
    if info.cells_mv:
        spread = max(info.cells_mv) - min(info.cells_mv)
        print(f"  spread:   {spread}mV")


def _cmd_sysinfo(args: argparse.Namespace) -> None:
    """Handle `b6ctl sysinfo`.

    Reads and prints the charger's current system settings (limits/
    buzzers/etc) - the way to verify a `set-limits` write actually took
    effect, rather than trusting it didn't error.
    """
    dev = Device(_make_transport(args))
    info = dev.get_sys_info()
    if args.json:
        print(
            json.dumps(
                {
                    "cycle_time_min": info.cycle_time,
                    "time_limit_on": info.time_limit_on,
                    "time_limit_minutes": info.time_limit_minutes,
                    "capacity_limit_on": info.capacity_limit_on,
                    "capacity_limit_mah": info.capacity_limit_mah,
                    "key_buzzer": info.key_buzzer,
                    "system_buzzer": info.system_buzzer,
                    "low_dc_limit_mv": info.low_dc_limit_mv,
                    "temp_limit_c": info.temp_limit_c,
                    "voltage_mv": info.voltage_mv,
                    "cells_mv": list(info.cells_mv),
                }
            )
        )
        return
    print(f"cycle_time:      {info.cycle_time}min")
    print(
        f"time_limit:      {'on' if info.time_limit_on else 'off'} "
        f"{info.time_limit_minutes}min"
    )
    print(
        f"capacity_limit:  {'on' if info.capacity_limit_on else 'off'} "
        f"{info.capacity_limit_mah}mAh"
    )
    print(f"temp_limit:      {info.temp_limit_c}C")
    print(f"low_dc_limit:    {info.low_dc_limit_mv / 1000:.2f}V")
    print(f"key_buzzer:      {info.key_buzzer}")
    print(f"system_buzzer:   {info.system_buzzer}")
    print(f"voltage:         {info.voltage_mv / 1000:.3f}V")
    for i, mv in enumerate(info.cells_mv, 1):
        print(f"  cell {i}:   {mv / 1000:.3f}V")


def _cmd_packs_list(args: argparse.Namespace) -> None:
    """Handle `b6ctl packs list`: print every pack in the registry."""
    registry = packs.load_registry()
    warning = packs.example_registry_warning(registry)
    if warning is not None:
        print(f"({warning})\n")
    for pack in registry:
        hv = " (HV)" if pack.is_hv else ""
        print(
            f"{pack.name:20s} {pack.cells}S{hv:5s} {pack.capacity_mah:6d}mAh  "
            f"default {pack.default_current_ma}mA, max {pack.max_current_ma}mA"
        )
        print(f"{'':20s} {pack.description}")


def _cmd_packs_show(args: argparse.Namespace) -> None:
    """Handle `b6ctl packs show NAME`: print one pack's full configuration."""
    registry = packs.load_registry()
    pack = registry.get(args.name)
    print(f"name:               {pack.name}")
    print(f"description:        {pack.description}")
    print(f"chemistry:          {pack.chemistry}")
    print(f"cells:              {pack.cells}S")
    print(f"capacity_mah:       {pack.capacity_mah}")
    print(f"default_current_ma: {pack.default_current_ma}")
    print(f"max_current_ma:     {pack.max_current_ma}")


def _confirm_and_send_start(
    dev: Device, profile: protocol.ChargeProfile, args, pack_name: str | None = None
) -> None:
    """Print the profile about to be sent, then send it or log it under --dry-run.

    Sends after an interactive confirmation (unless --auto-approve); in
    --dry-run mode, builds and logs the frame without sending anything
    at all. This is the single choke point every `start` path goes
    through (manual flags or --pack), so the confirmation behaviour
    can't accidentally diverge between them.

    Uses start_charging_verified() (see device.py), not start_charging()
    directly - a passing pre-start check doesn't guarantee the charger
    actually accepted the command, only that a plausible pack was
    connected before it was sent. Records the profile via
    last_start.record() only once the charger has confirmed it's
    actually charging - not merely sent, and not under --dry-run.
    """
    print("About to send START with:")
    print(f"  battery_type = {profile.battery_type.name}")
    print(f"  cells        = {profile.cell_count}")
    print(f"  charge_current   = {profile.charge_current_ma}mA")
    print(f"  end_voltage      = {profile.end_voltage_mv}mV/cell")
    print(f"  discharge_voltage= {profile.cell_discharge_voltage_mv}mV/cell")
    if dev.dry_run:
        print("(--dry-run: nothing will be sent)")
        dev.start_charging_verified(profile)
        return
    if not args.yes:
        try:
            reply = input("Send this to the charger? [y/N] ")
        except EOFError:
            print(
                "\naborted: no input available to confirm - use --auto-approve/--yes "
                "if running non-interactively",
                file=sys.stderr,
            )
            sys.exit(1)
        if reply.strip().lower() != "y":
            print("aborted")
            sys.exit(1)
    result = dev.start_charging_verified(profile)
    if result.confirmed:
        last_start.record(profile, pack=pack_name)
        print(
            f"sent and confirmed: {result.info.state_name}, "
            f"{len(result.info.cells_mv)} cells, "
            f"{result.info.voltage_mv / 1000:.3f}V"
        )
    else:
        print(f"error: {result.reason} - sent STOP_CHARGING as a precaution.", file=sys.stderr)
        sys.exit(1)


def _validate_start_args(args: argparse.Namespace) -> None:
    """Validate the --pack vs manual-flags argument combination for `start`.

    Exits with a friendly message on any invalid combination (both
    --pack and --chemistry/--cells given, or neither a pack nor manual
    chemistry/cells given, or manual flags without --current-ma).
    This is pure argument-shape validation, CLI-specific and not shared
    with httpd.py - the actual pack lookup, live cell-count check, and
    current ceiling are enforced identically for both interfaces by
    charge_request.build_pack_profile.
    """
    if args.pack and (args.chemistry or args.cells is not None):
        print(
            "error: --pack cannot be combined with --chemistry/--cells - "
            "either name a configured pack, or specify chemistry/cells "
            "manually, not both",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.pack:
        return
    if not args.chemistry or args.cells is None:
        print(
            "error: specify either --pack NAME, or both --chemistry and --cells",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.current_ma is None:
        print(
            "error: --current-ma is required when not using --pack "
            "(a configured pack supplies a safe default automatically)",
            file=sys.stderr,
        )
        sys.exit(1)


def _cmd_start(args: argparse.Namespace) -> None:
    """Handle `b6ctl start`.

    Builds a ChargeProfile (from --pack or manual flags) then hands off
    to _confirm_and_send_start. The --pack path delegates to
    charge_request.build_pack_profile - the same function httpd.py's
    POST /start uses - so this interface and the HTTP one can't drift
    apart the way they have before (see charge_request.py's module
    docstring).
    """
    _validate_start_args(args)
    dev = Device(_make_transport(args), dry_run=args.dry_run)

    if args.pack:
        try:
            registry = packs.load_registry()
            warning = packs.example_registry_warning(registry)
            if warning is not None:
                print(f"warning: {warning}", file=sys.stderr)
            profile = charge_request.build_pack_profile(
                dev,
                registry,
                args.pack,
                current_ma=args.current_ma,
                mode=args.mode,
                discharge_current_ma=args.discharge_current_ma,
            )
        except packs.PackCellMismatch as e:
            print(
                f"error: {e} - refusing to start. Check the physical connection "
                "and the pack you meant to select before retrying. (No flag "
                "skips this check on purpose - use the manual --chemistry/"
                "--cells/--current-ma flags directly if you're certain and need "
                "to override.)",
                file=sys.stderr,
            )
            sys.exit(1)
        except (PackConfigError, charge_request.InvalidStartRequest) as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        pack_name = args.pack
    else:
        mode = protocol.ChargingModeLi[args.mode.upper()]
        try:
            profile = protocol.lipo_profile(
                cell_count=args.cells,
                charge_current_ma=args.current_ma,
                mode=mode,
                hv=(args.chemistry == "lihv"),
                discharge_current_ma=args.discharge_current_ma,
            )
        except protocol.ProtocolError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        pack_name = None

    _confirm_and_send_start(dev, profile, args, pack_name=pack_name)


def _cmd_stop(args: argparse.Namespace) -> None:
    """Handle `b6ctl stop`: send STOP_CHARGING (or log it, in --dry-run)."""
    dev = Device(_make_transport(args), dry_run=args.dry_run)
    dev.stop_charging()
    print("dry-run: would send STOP" if args.dry_run else "sent STOP")


def _cmd_set_limits(args: argparse.Namespace) -> None:
    """Handle `b6ctl set-limits`.

    Sends whichever of cycle-time/time-limit/capacity-limit/temp-limit
    were given as flags. Verify with `b6ctl sysinfo` afterwards - a
    successful send doesn't by itself prove the value was accepted,
    only that the write didn't error.
    """
    dev = Device(_make_transport(args), dry_run=args.dry_run)
    if args.cycle_time is not None:
        dev.set_cycle_time(args.cycle_time)
    if args.time_limit is not None:
        dev.set_time_limit(True, args.time_limit)
    if args.capacity_limit is not None:
        dev.set_capacity_limit(True, args.capacity_limit)
    if args.temp_limit is not None:
        dev.set_temp_limit(args.temp_limit)
    print("dry-run: would send SET commands above" if args.dry_run else "sent")


def _resolve_serve_host_port(args: argparse.Namespace) -> tuple[str, int]:
    """Work out the (host, port) `serve` should bind from parsed args.

    --listen and --host/--port are mutually exclusive. Defaults to
    httpd.DEFAULT_HOST:httpd.DEFAULT_PORT if neither form is given.
    """
    if args.listen is not None and (args.host is not None or args.port is not None):
        print(
            "error: --listen cannot be combined with --host/--port - " "use one or the other",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.listen is not None:
        return args.listen
    return (
        args.host if args.host is not None else httpd.DEFAULT_HOST,
        args.port if args.port is not None else httpd.DEFAULT_PORT,
    )


def _cmd_serve(args: argparse.Namespace) -> None:
    """Handle `b6ctl serve`: run the HTTP daemon forever.

    Serves GET /metrics (Prometheus) and GET /status always; POST
    /start and POST /stop only with --enable-writes (403 otherwise),
    additionally requiring the B6CTL_WRITE_TOKEN env var as a bearer
    token if it's set (see httpd.py's module docstring). Uses the same
    --device/--fake as every other subcommand - this is just another
    mode of the one program, not a separate tool with its own flags
    for device selection.
    """
    host, port = _resolve_serve_host_port(args)
    dev = Device(_make_transport(args), dry_run=args.dry_run)
    metrics_cache = httpd.MetricsCache(lambda: httpd.render_metrics(dev), args.cache_seconds)

    write_token = os.environ.get(httpd.WRITE_TOKEN_ENV_VAR) if args.enable_writes else None

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", force=True)
    if args.enable_writes and not write_token:
        log.warning(
            "starting with --enable-writes and no %s set - POST /start and /stop "
            "will be reachable by ANYONE who can connect to %s:%s, with no per-"
            "request authentication. Set %s to require a bearer token.",
            httpd.WRITE_TOKEN_ENV_VAR,
            host,
            port,
            httpd.WRITE_TOKEN_ENV_VAR,
        )
    server = ThreadingHTTPServer(
        (host, port), httpd.make_handler(dev, metrics_cache, args.enable_writes, write_token)
    )
    log.info(
        "listening on %s:%s (enable_writes=%s, write_token_set=%s, dry_run=%s)",
        host,
        port,
        args.enable_writes,
        write_token is not None,
        args.dry_run,
    )
    server.serve_forever()


def build_parser() -> argparse.ArgumentParser:
    """Construct the full `b6ctl` argument parser.

    Global flags (--device/--fake/--verbose) plus one subparser per
    subcommand.
    """
    p = argparse.ArgumentParser(prog="b6ctl")
    p.add_argument("--device", help="explicit /dev/hidrawN (default: auto-discover)")
    p.add_argument(
        "--fake", action="store_true", help="use the in-memory fake charger, no hardware"
    )
    p.add_argument("-v", "--verbose", action="store_true", help="log every frame sent (hex)")
    sub = p.add_subparsers(dest="command", required=True)

    st = sub.add_parser("status", help="read current charge info")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=_cmd_status)

    sysinfo = sub.add_parser(
        "sysinfo", help="read current system settings (verify set-limits took effect)"
    )
    sysinfo.add_argument("--json", action="store_true")
    sysinfo.set_defaults(func=_cmd_sysinfo)

    packs_parser = sub.add_parser("packs", help="inspect your configured battery packs")
    packs_sub = packs_parser.add_subparsers(dest="packs_command", required=True)
    packs_list = packs_sub.add_parser("list", help="list every configured pack")
    packs_list.set_defaults(func=_cmd_packs_list)
    packs_show = packs_sub.add_parser("show", help="show one pack's full configuration")
    packs_show.add_argument("name")
    packs_show.set_defaults(func=_cmd_packs_show)

    start = sub.add_parser(
        "start",
        help="start a charge - EITHER --pack NAME (recommended) OR "
        "--chemistry/--cells/--current-ma manually",
    )
    start.add_argument(
        "--pack", help="name of a pack from packs.toml (see 'b6ctl packs list')"
    )
    start.add_argument("--chemistry", choices=["lipo", "lihv"])
    start.add_argument("--cells", type=int)
    start.add_argument(
        "--current-ma",
        type=int,
        dest="current_ma",
        help="required with manual flags; optional override with --pack "
        "(defaults to the pack's configured default_current_ma)",
    )
    start.add_argument(
        "--discharge-current-ma",
        type=int,
        default=protocol.DEFAULT_DISCHARGE_CURRENT_MA,
    )
    start.add_argument(
        "--mode",
        choices=[m.name.lower() for m in protocol.ChargingModeLi],
        default="balance",
    )
    start.add_argument("--dry-run", action="store_true")
    start.add_argument(
        "--auto-approve",
        "--yes",
        dest="yes",
        action="store_true",
        help="skip the interactive confirmation prompt (still prints the profile first)",
    )
    start.set_defaults(func=_cmd_start)

    stop = sub.add_parser("stop", help="stop the current charge")
    stop.add_argument("--dry-run", action="store_true")
    stop.set_defaults(func=_cmd_stop)

    limits = sub.add_parser("set-limits", help="configure safety limits")
    limits.add_argument("--cycle-time", type=int, help="1-60 minutes")
    limits.add_argument("--time-limit", type=int, help="1-720 minutes")
    limits.add_argument("--capacity-limit", type=int, help="100-50000 mAh")
    limits.add_argument("--temp-limit", type=int, help="20-80 C")
    limits.add_argument("--dry-run", action="store_true")
    limits.set_defaults(func=_cmd_set_limits)

    serve = sub.add_parser(
        "serve",
        help="run the HTTP daemon forever: GET /metrics (Prometheus) + GET /status "
        "always, POST /start + POST /stop only with --enable-writes",
    )
    serve.add_argument(
        "--host", default=None, help=f"interface to bind (default: {httpd.DEFAULT_HOST})"
    )
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"port to bind (default: {httpd.DEFAULT_PORT})",
    )
    serve.add_argument(
        "--listen",
        type=httpd.parse_listen_address,
        default=None,
        metavar="HOST:PORT",
        help="combined interface:port socket, e.g. --listen 0.0.0.0:9111 or "
        "--listen [::1]:9111 for IPv6. Mutually exclusive with --host/--port",
    )
    serve.add_argument(
        "--enable-writes",
        action="store_true",
        help="allow POST /start and POST /stop - without this, they return 403 "
        "regardless of bind address. OFF by default. Set the "
        f"{httpd.WRITE_TOKEN_ENV_VAR} env var to also require a bearer token on "
        "those requests - strongly recommended given the default 0.0.0.0 bind.",
    )
    serve.add_argument(
        "--dry-run",
        action="store_true",
        help="with --enable-writes, log writes but send nothing",
    )
    serve.add_argument(
        "--cache-seconds",
        type=float,
        default=httpd.DEFAULT_CACHE_S,
        help=f"reuse a rendered /metrics body for this many seconds "
        f"(default: {httpd.DEFAULT_CACHE_S})",
    )
    serve.set_defaults(func=_cmd_serve)

    return p


def main(argv: list[str] | None = None) -> None:
    """Entry point for the `b6ctl` console script.

    Parses args, configures logging, dispatches to the selected
    subcommand's handler, and turns any of this project's own
    exceptions - plus a bare OSError, the shape a mid-command hardware
    failure actually takes (e.g. PermissionError on /dev/hidraw* with
    no udev rule, or ENODEV/ENXIO from a device unplugged mid-command;
    HidRawTransport does raw os.open/os.write/os.read, none of which
    is a DeviceTimeout) - into a one-line stderr message instead of a
    raw traceback.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
    )
    try:
        args.func(args)
    except (PackConfigError, protocol.ProtocolError, DeviceTimeout, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
