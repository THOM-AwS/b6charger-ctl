# SPDX-License-Identifier: GPL-3.0-or-later
"""High-level device API.

Every WRITE method goes through `_send()`, which honours `dry_run` -
the single choke point that makes "build the frame, print it, send
nothing" possible everywhere at once.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from b6charger import protocol
from b6charger.transport import Transport

log = logging.getLogger("b6charger.device")

#: How long to wait after START_CHARGING before the first confirmation
#: read, and how long to wait before a single retry if that read is
#: ambiguous. Not user-tunable (yet) - these are best-guess defaults,
#: not measured against how long this hardware actually takes to
#: register a new state. See Device.start_charging_verified().
DEFAULT_CONFIRM_DELAY_S = 3.0
DEFAULT_RETRY_DELAY_S = 2.0


@dataclass(frozen=True)
class StartVerification:
    """Result of start_charging_verified(): did the charger actually confirm it started.

    `info` is the most recent successful GET_CHARGE_INFO read - always
    present when `confirmed` is True, and also present on most failure
    paths (a mismatched state/cell-count, or an explicit ERROR). It's
    only None when even reading back the charger's state failed (a
    transport-level failure, not a protocol-level one) - there's
    nothing to report in that case beyond `reason`.
    """

    confirmed: bool
    info: protocol.ChargeInfo | None
    stopped: bool
    reason: str | None


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

    def start_charging_verified(
        self,
        profile: protocol.ChargeProfile,
        confirm_delay_s: float = DEFAULT_CONFIRM_DELAY_S,
        retry_delay_s: float = DEFAULT_RETRY_DELAY_S,
    ) -> StartVerification | None:
        """Send START_CHARGING, then verify the charger actually accepted it.

        The pre-start pack/cell-count check (packs.check_cell_count,
        via GET_SYS_INFO) confirms a plausible pack is connected BEFORE
        sending anything, but a passing pre-check doesn't guarantee the
        charger actually did anything with the START_CHARGING command
        that follows - confirmed possible even with fully correct code,
        see DRY_RUN.md. This closes that loop.

        Returns None in dry_run mode - nothing was sent, so there's
        nothing to verify. Otherwise: waits `confirm_delay_s`, reads
        GET_CHARGE_INFO, and checks state==CHARGING with a live cell
        count matching `profile.cell_count`.

        An explicit ERROR state from the charger (e.g.
        CELL_NUMBER_INCORRECT - a real, protocol-defined error code,
        see protocol.Error) is treated as immediately authoritative:
        the charger has its own preflight validation, so there's no
        reason to second-guess it with a retry - STOP_CHARGING is sent
        right away. Any other non-matching read (still IDLE, wrong
        cell count, no error reported) gets exactly ONE retry after
        `retry_delay_s` before concluding a genuine mismatch and
        stopping - this guards specifically against a single transient
        bad read causing an unnecessary abort of an otherwise-fine
        charge, not against a real fault the charger itself reports.

        A transport failure (DeviceTimeout, or any other exception)
        during either confirmation read is treated the same as an
        explicit ERROR - immediately terminal, no retry, STOP_CHARGING
        sent as a precaution - rather than propagating out of this
        function. START_CHARGING has already gone out on the wire by
        this point; letting a read failure escape unhandled would
        leave that command's outcome completely unaddressed, which is
        exactly what this function exists to prevent (see above). If
        the precautionary STOP_CHARGING itself also fails, that's
        folded into the returned `reason` rather than raised - see
        `_stop_with_reason`.
        """
        self.start_charging(profile)
        if self.dry_run:
            return None

        time.sleep(confirm_delay_s)
        info = self._read_after_start()
        if isinstance(info, StartVerification):
            return info
        result = self._evaluate_start(info, profile)
        if result is not None:
            return result

        time.sleep(retry_delay_s)
        info = self._read_after_start()
        if isinstance(info, StartVerification):
            return info
        result = self._evaluate_start(info, profile)
        if result is not None:
            return result

        return self._stop_with_reason(
            f"charger did not confirm charging after "
            f"{confirm_delay_s + retry_delay_s:.0f}s (state={info.state_name}, "
            f"cells detected={len(info.cells_mv)}, expected={profile.cell_count})",
            info=info,
        )

    def _read_after_start(self) -> protocol.ChargeInfo | StartVerification:
        """Read GET_CHARGE_INFO for start-verification, or fail safe if the read itself fails.

        Returns the ChargeInfo on success. On any transport failure,
        returns an already-final, failed StartVerification (via
        `_stop_with_reason`) instead of letting the exception escape -
        see start_charging_verified's docstring for why.
        """
        try:
            return self.get_charge_info()
        except (
            Exception
        ) as e:  # noqa: BLE001 - any transport failure fails safe here, not just DeviceTimeout
            log.exception("read failed during start verification")
            return self._stop_with_reason(
                f"could not read back charger state after START_CHARGING ({e})"
            )

    def _stop_with_reason(
        self, reason: str, info: protocol.ChargeInfo | None = None
    ) -> StartVerification:
        """Send STOP_CHARGING as a precaution and return a failed, final StartVerification.

        `info` is the most recent successful GET_CHARGE_INFO read, if
        any - omitted when even the read itself failed. If
        STOP_CHARGING itself also fails, that failure is folded into
        `reason` rather than raised: the caller already knows
        `confirmed` is False, and needs to know the charger might
        still be running, which raising here would obscure rather
        than surface.
        """
        try:
            self.stop_charging()
            stopped = True
        except (
            Exception
        ) as e:  # noqa: BLE001 - report, don't compound one failure with a crash
            log.exception("STOP_CHARGING itself failed after a failed start verification")
            stopped = False
            reason = (
                f"{reason} - STOP_CHARGING ALSO FAILED ({e}), charger may still be charging"
            )
        return StartVerification(confirmed=False, info=info, stopped=stopped, reason=reason)

    def _evaluate_start(
        self, info: protocol.ChargeInfo, profile: protocol.ChargeProfile
    ) -> StartVerification | None:
        """Definitively resolve a post-start read, or None if it needs a retry.

        Returns a confirmed StartVerification on success. On an
        explicit ERROR state, stops immediately and returns a failed
        StartVerification WITHOUT retrying - see start_charging_verified's
        docstring for why an explicit error is treated differently from
        an ambiguous non-matching read. Returns None (caller should
        retry once) for anything else that doesn't yet match.
        """
        if info.state == protocol.State.CHARGING and len(info.cells_mv) == profile.cell_count:
            return StartVerification(confirmed=True, info=info, stopped=False, reason=None)

        if info.state == protocol.State.ERROR:
            return self._stop_with_reason(
                f"charger reported an error: {info.error_name} ({info.error_code})", info=info
            )

        return None

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
