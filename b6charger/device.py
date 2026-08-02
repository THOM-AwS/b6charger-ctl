# SPDX-License-Identifier: GPL-3.0-or-later
"""High-level device API.

Every WRITE method goes through `_send()`, which honours `dry_run` -
the single choke point that makes "build the frame, print it, send
nothing" possible everywhere at once.
"""

from __future__ import annotations

import logging

from b6charger import protocol
from b6charger.transport import Transport

log = logging.getLogger("b6charger.device")


class Device:
    """A charger, reachable through some Transport (real hardware or fake)."""

    def __init__(self, transport: Transport, dry_run: bool = False) -> None:
        """Wrap `transport`; if `dry_run` is True, no write method ever sends anything."""
        self._t = transport
        self.dry_run = dry_run

    def _send(self, frame: bytes, description: str) -> bytes | None:
        """Log `frame` under `description`, then send it unless dry_run is set.

        Returns the response bytes, or None if nothing was sent.
        """
        log.info("%s: %s", description, frame.hex())
        if self.dry_run:
            return None
        return self._t.transact(frame)

    # --- reads -----------------------------------------------------

    def get_charge_info(self) -> protocol.ChargeInfo:
        """Read live charge telemetry (state/voltage/current/cells/...).

        Always performed for real, even in dry_run mode - reads are
        safe, and dry_run should tell the truth about what a real send
        would have done, which requires actually reading current state.
        """
        resp = self._t.transact(protocol.build_get_charge_info())
        return protocol.parse_charge_info(resp)

    def get_sys_info(self) -> protocol.SysInfo:
        """Read the charger's currently-configured system settings.

        The way to verify a `set_*` write actually took effect, rather
        than trusting that it didn't error. Always performed for real,
        same reasoning as `get_charge_info`.
        """
        resp = self._t.transact(protocol.build_get_sys_info())
        return protocol.parse_sys_info(resp)

    # --- charge control ---------------------------------------------

    def start_charging(self, profile: protocol.ChargeProfile) -> None:
        """Send START_CHARGING with `profile` (no-op in dry_run mode)."""
        frame = protocol.build_start_charging(profile)
        self._send(
            frame,
            f"START battery_type={profile.battery_type.name} "
            f"cells={profile.cell_count} "
            f"current={profile.charge_current_ma}mA "
            f"end_voltage={profile.end_voltage_mv}mV/cell",
        )

    def stop_charging(self) -> None:
        """Send STOP_CHARGING (no-op in dry_run mode)."""
        self._send(protocol.build_stop_charging(), "STOP")

    # --- system settings ---------------------------------------------

    def set_cycle_time(self, minutes: int) -> None:
        """Set the cyclic charge/discharge cycle count, 1-60 (no-op in dry_run mode)."""
        self._send(protocol.build_set_cycle_time(minutes), f"SET cycle_time={minutes}min")

    def set_time_limit(self, enabled: bool, minutes: int) -> None:
        """Set the charge time-limit safety cutoff, 1-720 min (no-op in dry_run mode)."""
        self._send(
            protocol.build_set_time_limit(enabled, minutes),
            f"SET time_limit enabled={enabled} minutes={minutes}",
        )

    def set_capacity_limit(self, enabled: bool, mah: int) -> None:
        """Set the charge capacity-limit safety cutoff, 100-50000mAh (no-op in dry_run)."""
        self._send(
            protocol.build_set_capacity_limit(enabled, mah),
            f"SET capacity_limit enabled={enabled} mah={mah}",
        )

    def set_temp_limit(self, celsius: int) -> None:
        """Set the internal temperature cutoff, 20-80C (no-op in dry_run mode)."""
        self._send(protocol.build_set_temp_limit(celsius), f"SET temp_limit={celsius}C")

    def set_buzzers(self, key: bool, system: bool) -> None:
        """Enable/disable the key-press and system buzzers (no-op in dry_run mode)."""
        self._send(
            protocol.build_set_buzzers(key, system),
            f"SET buzzers key={key} system={system}",
        )
