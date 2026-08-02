# SPDX-License-Identifier: GPL-3.0-or-later
"""b6ctl - CLI for SkyRC B6-family chargers.

Safety design: `start` always prints the exact profile it's about to
send and requires either an interactive "yes" or --yes before writing
anything, unless --dry-run is given (which never touches the device at
all). This is deliberately more friction than a monitoring tool needs,
because this one can command a device that regulates LiPo charge
voltage/current.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from b6charger import protocol
from b6charger.device import Device
from b6charger.transport import FakeChargerTransport, HidRawTransport, NoChargerFound


def _make_transport(args: argparse.Namespace):
    if args.fake:
        return FakeChargerTransport()
    try:
        return HidRawTransport(args.device)
    except NoChargerFound as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_status(args: argparse.Namespace) -> None:
    dev = Device(_make_transport(args))
    info = dev.get_charge_info()
    if args.json:
        print(
            json.dumps(
                {
                    "state": info.state,
                    "state_name": info.state_name,
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
    print(f"pack:       {info.voltage_mv / 1000:.3f}V")
    print(f"current:    {info.current_ma / 1000:.3f}A")
    print(f"capacity:   {info.capacity_mah}mAh in {info.time_s}s")
    print(f"temp:       int={info.temp_int_c}C ext={info.temp_ext_c}C")
    print(f"impedance:  {info.impedance_mohm}mOhm")
    for i, mv in enumerate(info.cells_mv, 1):
        print(f"  cell {i}:   {mv / 1000:.3f}V")
    if info.cells_mv:
        spread = max(info.cells_mv) - min(info.cells_mv)
        print(f"  spread:   {spread}mV")


def _cmd_sysinfo(args: argparse.Namespace) -> None:
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
    print(f"time_limit:      {'on' if info.time_limit_on else 'off'} {info.time_limit_minutes}min")
    print(f"capacity_limit:  {'on' if info.capacity_limit_on else 'off'} {info.capacity_limit_mah}mAh")
    print(f"temp_limit:      {info.temp_limit_c}C")
    print(f"low_dc_limit:    {info.low_dc_limit_mv / 1000:.2f}V")
    print(f"key_buzzer:      {info.key_buzzer}")
    print(f"system_buzzer:   {info.system_buzzer}")
    print(f"voltage:         {info.voltage_mv / 1000:.3f}V")
    for i, mv in enumerate(info.cells_mv, 1):
        print(f"  cell {i}:   {mv / 1000:.3f}V")


def _confirm_and_send_start(dev: Device, profile: protocol.ChargeProfile, args) -> None:
    print("About to send START with:")
    print(f"  battery_type = {profile.battery_type.name}")
    print(f"  cells        = {profile.cell_count}")
    print(f"  charge_current   = {profile.charge_current_ma}mA")
    print(f"  end_voltage      = {profile.end_voltage_mv}mV/cell")
    print(f"  discharge_voltage= {profile.cell_discharge_voltage_mv}mV/cell")
    if dev.dry_run:
        print("(--dry-run: nothing will be sent)")
        dev.start_charging(profile)
        return
    if not args.yes:
        reply = input("Send this to the charger? [y/N] ")
        if reply.strip().lower() != "y":
            print("aborted")
            sys.exit(1)
    dev.start_charging(profile)
    print("sent.")


def _cmd_start(args: argparse.Namespace) -> None:
    dev = Device(_make_transport(args), dry_run=args.dry_run)
    mode = protocol.ChargingModeLi[args.mode.upper()]
    profile = protocol.lipo_profile(
        cell_count=args.cells,
        charge_current_ma=args.current_ma,
        mode=mode,
        hv=(args.chemistry == "lihv"),
        discharge_current_ma=args.discharge_current_ma,
    )
    _confirm_and_send_start(dev, profile, args)


def _cmd_stop(args: argparse.Namespace) -> None:
    dev = Device(_make_transport(args), dry_run=args.dry_run)
    dev.stop_charging()
    print("dry-run: would send STOP" if args.dry_run else "sent STOP")


def _cmd_set_limits(args: argparse.Namespace) -> None:
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="b6ctl")
    p.add_argument("--device", help="explicit /dev/hidrawN (default: auto-discover)")
    p.add_argument(
        "--fake", action="store_true", help="use the in-memory fake charger, no hardware"
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="log every frame sent (hex)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    st = sub.add_parser("status", help="read current charge info")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=_cmd_status)

    sysinfo = sub.add_parser(
        "sysinfo", help="read current system settings (verify set-limits took effect)"
    )
    sysinfo.add_argument("--json", action="store_true")
    sysinfo.set_defaults(func=_cmd_sysinfo)

    start = sub.add_parser("start", help="start a charge (LiPo/LiHV)")
    start.add_argument("--chemistry", choices=["lipo", "lihv"], required=True)
    start.add_argument("--cells", type=int, required=True)
    start.add_argument("--current-ma", type=int, required=True, dest="current_ma")
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
    start.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
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

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
    )
    args.func(args)


if __name__ == "__main__":
    main()
