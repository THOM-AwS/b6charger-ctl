# SPDX-License-Identifier: GPL-3.0-or-later
"""High-level device API. Every WRITE method goes through _send(), which
honours dry_run - the single choke point that makes "build the frame,
print it, send nothing" possible everywhere at once.
"""

from __future__ import annotations

import logging

from b6charger import protocol
from b6charger.transport import Transport

log = logging.getLogger("b6charger.device")


class Device:
    def __init__(self, transport: Transport, dry_run: bool = False) -> None:
        self._t = transport
        self.dry_run = dry_run

    def _send(self, frame: bytes, description: str) -> bytes | None:
        log.info("%s: %s", description, frame.hex())
        if self.dry_run:
            return None
        return self._t.transact(frame)

    # --- reads -----------------------------------------------------

    def get_charge_info(self) -> protocol.ChargeInfo:
        resp = self._t.transact(protocol.build_get_charge_info())
        return protocol.parse_charge_info(resp)

    def get_sys_info(self) -> protocol.SysInfo:
        resp = self._t.transact(protocol.build_get_sys_info())
        return protocol.parse_sys_info(resp)

    # --- charge control ---------------------------------------------

    def start_charging(self, profile: protocol.ChargeProfile) -> None:
        frame = protocol.build_start_charging(profile)
        self._send(
            frame,
            f"START battery_type={profile.battery_type.name} "
            f"cells={profile.cell_count} "
            f"current={profile.charge_current_ma}mA "
            f"end_voltage={profile.end_voltage_mv}mV/cell",
        )

    def stop_charging(self) -> None:
        self._send(protocol.build_stop_charging(), "STOP")

    # --- system settings ---------------------------------------------

    def set_cycle_time(self, minutes: int) -> None:
        self._send(
            protocol.build_set_cycle_time(minutes), f"SET cycle_time={minutes}min"
        )

    def set_time_limit(self, enabled: bool, minutes: int) -> None:
        self._send(
            protocol.build_set_time_limit(enabled, minutes),
            f"SET time_limit enabled={enabled} minutes={minutes}",
        )

    def set_capacity_limit(self, enabled: bool, mah: int) -> None:
        self._send(
            protocol.build_set_capacity_limit(enabled, mah),
            f"SET capacity_limit enabled={enabled} mah={mah}",
        )

    def set_temp_limit(self, celsius: int) -> None:
        self._send(
            protocol.build_set_temp_limit(celsius), f"SET temp_limit={celsius}C"
        )

    def set_buzzers(self, key: bool, system: bool) -> None:
        self._send(
            protocol.build_set_buzzers(key, system),
            f"SET buzzers key={key} system={system}",
        )
