# SPDX-License-Identifier: GPL-3.0-or-later
"""Wire protocol for SkyRC B6-family balance chargers.

Covers iMAX B6 / B6AC / B6 mini and rebadged clones (e.g. Jaycar
POWERTECH PLUS MB-3633 / JMB-3633). Frame layout, reverse-engineered by
maciek134/libb6 (GPL-3) and confirmed here against a live Jaycar
POWERTECH PLUS MB-3633 for the read path:

    [0x0F, LEN, CMD, 0x00, ...PAYLOAD..., CHECKSUM, 0xFF, 0xFF]

- byte 0 is a constant sync byte.
- LEN (byte 1) is the number of bytes AFTER this 4-byte header, i.e.
  len(PAYLOAD) + 3 (the checksum trailer). For a bare GET/control command
  with no payload, LEN is always 3.
- CHECKSUM = sum(all bytes from index 2 up to but not including the
  checksum trailer) & 0xFF ("chinese checksum" per libb6's own comment),
  followed by two padding 0xFF bytes.

Two command families share this framing:

1. Simple commands (GET_DEV_INFO, GET_SYS_INFO, GET_CHARGE_INFO,
   STOP_CHARGING, UNK1): header is [0x0F, 0x03, CMD, 0x00], no payload.
2. SET_SYSTEM commands (cycle time / time limit / capacity limit / temp
   limit / buzzers): header is [0x0F, LEN, 0x11, PARAM_ID], payload
   starts with a reserved 0x00 then the parameter's value bytes.
3. START_CHARGING: header is [0x0F, LEN, 0x05, 0x00], payload is the
   full ChargeProfile.

GET_CHARGE_INFO, GET_SYS_INFO, STOP_CHARGING, SET_SYSTEM (temp limit),
and START_CHARGING (LiPo/LiHV) have all been validated against a real
Jaycar POWERTECH PLUS MB-3633 - see DRY_RUN.md for the live trace of
each. Everything else (GET_DEV_INFO, the other SET_SYSTEM parameters,
NiMH/NiCd/Pb chemistries) is implemented strictly from libb6's
Device.cc/Packet.cc/Enum.hh and is unverified against physical
hardware - see DRY_RUN.md's hardware test plan before trusting an
unverified command for real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Cmd(IntEnum):
    """Top-level command bytes (frame byte index 2) for simple, no-payload commands."""

    GET_DEV_INFO = 0x57
    GET_SYS_INFO = 0x5A
    GET_CHARGE_INFO = 0x55
    UNK1 = 0x5F
    STOP_CHARGING = 0xFE


class SetSystemParam(IntEnum):
    """Sub-command IDs for the 0x11 SET_SYSTEM command family."""

    CYCLE_TIME = 0x00
    TIME_LIMIT = 0x01
    CAPACITY_LIMIT = 0x02
    BUZZERS = 0x03
    TEMP_LIMIT = 0x05


START_CHARGING_CMD = 0x05

#: Hard device-capability ceiling for charge/discharge current, enforced
#: unconditionally in ChargeProfile.__post_init__ regardless of how the
#: profile was built. This hardware family (iMAX B6/B6AC/B6 mini and
#: rebadged clones) is commonly rated up to 5A/50W; 6A leaves headroom
#: for the handful of higher-rated variants without this project having
#: separately verified each one's exact spec sheet. This is deliberately
#: NOT a substitute for packs.py's tighter, chemistry-derived 1C ceiling
#: (MAX_SAFE_C_RATE) - it exists to catch a manual/raw-body current that
#: has no pack to derive a C-rate from at all, not to replace that check.
MAX_DEVICE_CURRENT_MA = 6000


class ChargingModeLi(IntEnum):
    """Charging mode for lithium chemistries (LiPo/LiIon/LiFe/LiHV)."""

    STANDARD = 0x00
    DISCHARGE = 0x01
    STORAGE = 0x02
    FAST = 0x03
    BALANCE = 0x04


class ChargingModeNi(IntEnum):
    """Charging mode for NiMH/NiCd."""

    STANDARD = 0x00
    AUTO = 0x01
    DISCHARGE = 0x02
    REPEAK = 0x03
    CYCLE = 0x04


class ChargingModePb(IntEnum):
    """Charging mode for lead-acid (Pb)."""

    CHARGE = 0x00
    DISCHARGE = 0x01


class BatteryType(IntEnum):
    """Battery chemistry, as sent in a START_CHARGING profile."""

    LIPO = 0x00
    LIION = 0x01
    LIFE = 0x02
    LIHV = 0x03
    NIMH = 0x04
    NICD = 0x05
    PB = 0x06

    @property
    def is_lithium(self) -> bool:
        """Whether this chemistry uses ChargingModeLi (LiPo/LiIon/LiFe/LiHV)."""
        return self in (
            BatteryType.LIPO,
            BatteryType.LIION,
            BatteryType.LIFE,
            BatteryType.LIHV,
        )

    @property
    def is_nickel(self) -> bool:
        """Whether this chemistry uses ChargingModeNi (NiMH/NiCd)."""
        return self in (BatteryType.NIMH, BatteryType.NICD)


class State(IntEnum):
    """Charger state, as reported in GET_CHARGE_INFO's state byte.

    libb6's Enum.hh names values 2 and 4 ERROR_1/ERROR_2 - this project
    trusted that naming until 2026-08-02, when it turned out to be
    wrong. Confirmed via an independent reverse-engineering project
    (buxtronix/b6max, a separately-typed Go implementation) whose own
    State enum names value 2 StateIdle and value 4 StateError - i.e.
    there is only ONE real error state, not two. This matches
    everything this project had already observed independently: the
    charger's own panel showing nothing wrong while state read 2, an
    error_code of 0 (not a valid code in Error below) decoded there,
    and the charger booting directly into state 2 with no battery
    connected - not something a device does if that state were a
    genuine fault. IDLE=2 was previously ERROR_1; ERROR=4 was
    previously ERROR_2. See DRY_RUN.md for the full finding.
    """

    CHARGING = 1
    IDLE = 2
    COMPLETE = 3
    ERROR = 4


class Error(IntEnum):
    """Error codes returned alongside the ERROR state, per Enum.hh."""

    CONNECTION_BROKEN_1 = 0x000B
    CELL_VOLTAGE_INVALID = 0x000C
    BALANCE_CONNECTION = 0x000D
    NO_BATTERY = 0x000E
    CELL_NUMBER_INCORRECT = 0x000F
    CONNECTION_MAIN_PORT = 0x0010
    BATTERY_FULL = 0x0011
    CHARGE_NOT_NEEDED = 0x0012
    CELL_HIGH_VOLTAGE = 0x0013
    CONNECTION_BROKEN_2 = 0x0014
    CONNECTION_BROKEN_3 = 0x0015
    CONNECTION_BROKEN_4 = 0x0016
    INT_TEMP_TOO_HIGH = 0x0100
    EXT_TEMP_TOO_HIGH = 0x0200
    DC_IN_TOO_LOW = 0x0300
    DC_IN_TOO_HIGH = 0x0400
    OVER_TIME_LIMIT = 0x0500
    OVER_CAPACITY_LIMIT = 0x0600
    REVERSE_POLARITY = 0x0700
    CONTROL_FAIL = 0x0800
    BREAK_DOWN = 0x0900
    INPUT_FAIL = 0x1000
    UNKNOWN = 0xFFFF


class ProtocolError(Exception):
    """A frame couldn't be built or parsed - bad input, or an unexpected response."""


def checksum_trailer(payload_from_cmd: bytes) -> bytes:
    """Compute the 3-byte checksum trailer for a frame.

    `payload_from_cmd` is everything from the CMD byte onward (i.e. the
    frame with the leading [0x0F, LEN] stripped). Mirrors libb6's
    Packet::writeChecksum(), which sums from buffer index 2 onward.
    """
    total = sum(payload_from_cmd) & 0xFF
    return bytes([total, 0xFF, 0xFF])


def _simple_frame(cmd: int) -> bytes:
    """Build a no-payload command frame: [0x0F, 0x03, cmd, 0x00, checksum...]."""
    body = bytes([0x0F, 0x03, cmd, 0x00])
    return body + checksum_trailer(body[2:])


def build_get_dev_info() -> bytes:
    """Build a GET_DEV_INFO request frame.

    Exposed for completeness (same as build_unk1 below) - not currently
    called by device.py, so there's no `b6ctl` command that reaches it.
    """
    return _simple_frame(Cmd.GET_DEV_INFO)


def build_get_sys_info() -> bytes:
    """Build a GET_SYS_INFO request frame."""
    return _simple_frame(Cmd.GET_SYS_INFO)


def build_get_charge_info() -> bytes:
    """Build a GET_CHARGE_INFO request frame."""
    return _simple_frame(Cmd.GET_CHARGE_INFO)


def build_stop_charging() -> bytes:
    """Build a STOP_CHARGING request frame."""
    return _simple_frame(Cmd.STOP_CHARGING)


def build_unk1() -> bytes:
    """Build a request frame for the UNK1 (0x5F) command.

    Purpose undocumented in libb6 beyond "sent before startCharging to
    check if a charge is already in progress" - exposed here for
    completeness, not currently used by device.py.
    """
    return _simple_frame(Cmd.UNK1)


def _u16(val: int) -> bytes:
    """Encode `val` as a big-endian u16, raising ProtocolError if out of range."""
    if not 0 <= val <= 0xFFFF:
        raise ProtocolError(f"value {val} out of u16 range")
    return bytes([(val >> 8) & 0xFF, val & 0xFF])


def _set_system_frame(param_id: int, value_bytes: bytes) -> bytes:
    """Build a SET_SYSTEM (0x11) frame for one parameter and its value bytes."""
    payload = bytes([0x00]) + value_bytes
    length = len(payload) + 3
    header = bytes([0x0F, length, 0x11, param_id])
    frame = header + payload
    return frame + checksum_trailer(frame[2:])


def build_set_cycle_time(cycle_time: int) -> bytes:
    """Build a frame setting the cycle count (1-60) for cyclic charge/discharge."""
    if not 1 <= cycle_time <= 60:
        raise ProtocolError("cycle_time must be 1-60")
    return _set_system_frame(SetSystemParam.CYCLE_TIME, bytes([cycle_time]))


def build_set_time_limit(enabled: bool, limit_minutes: int) -> bytes:
    """Build a frame setting the charge time-limit safety cutoff (1-720 minutes)."""
    if not 1 <= limit_minutes <= 720:
        raise ProtocolError("limit_minutes must be 1-720")
    return _set_system_frame(
        SetSystemParam.TIME_LIMIT, bytes([int(enabled)]) + _u16(limit_minutes)
    )


def build_set_capacity_limit(enabled: bool, limit_mah: int) -> bytes:
    """Build a frame setting the charge capacity-limit safety cutoff (100-50000mAh)."""
    if not 100 <= limit_mah <= 50000:
        raise ProtocolError("limit_mah must be 100-50000")
    return _set_system_frame(
        SetSystemParam.CAPACITY_LIMIT, bytes([int(enabled)]) + _u16(limit_mah)
    )


def build_set_temp_limit(limit_celsius: int) -> bytes:
    """Build a frame setting the internal temperature cutoff (20-80C)."""
    if not 20 <= limit_celsius <= 80:
        raise ProtocolError("limit_celsius must be 20-80")
    return _set_system_frame(SetSystemParam.TEMP_LIMIT, bytes([limit_celsius]))


def build_set_buzzers(key: bool, system: bool) -> bytes:
    """Build a frame enabling/disabling the key-press and system buzzers."""
    return _set_system_frame(SetSystemParam.BUZZERS, bytes([int(key), int(system)]))


@dataclass(frozen=True)
class ChargeProfile:
    """The full profile sent in a START_CHARGING command.

    Mirrors libb6's ChargeProfile struct. Construct via the chemistry
    helpers below (e.g. `lipo_profile`) rather than directly, unless you
    know exactly what you're doing - endVoltage/cellDischargeVoltage are
    in millivolts PER CELL, and an incorrect value here is exactly the
    failure mode this project exists to prevent, not create.
    """

    battery_type: BatteryType
    cell_count: int
    mode_li: ChargingModeLi | None = None
    mode_ni: ChargingModeNi | None = None
    mode_pb: ChargingModePb | None = None
    charge_current_ma: int = 1500
    discharge_current_ma: int = 1000
    cell_discharge_voltage_mv: int = 3200
    end_voltage_mv: int = 4200
    trickle_current_ma: int = 0
    r_peak_count: int = 3
    cycle_type: int = 1
    cycle_count: int = 1

    def __post_init__(self) -> None:
        """Validate cell count, mode, and current against the hardware's own ceiling.

        The current checks are a backstop, not a substitute for
        packs.py's tighter, chemistry-derived 1C ceiling: packs.toml's
        MAX_SAFE_C_RATE only applies to profiles built via `--pack`
        (see packs.check_cell_count/_validate_pack). A profile built
        from manual CLI flags or a raw HTTP body has no pack to derive
        a C-rate from, so without a check here the only bound left is
        `_u16`'s wire-format range (0-65535mA) - not a safety limit,
        just what fits in the frame. This rejects anything above what
        the charger hardware itself is rated for, regardless of how
        the profile was built.
        """
        if not 1 <= self.cell_count <= 16:
            raise ProtocolError("cell_count must be 1-16")
        if self.battery_type.is_lithium and self.mode_li is None:
            raise ProtocolError("lithium battery types require mode_li")
        if self.battery_type.is_nickel and self.mode_ni is None:
            raise ProtocolError("NiMH/NiCd require mode_ni")
        if self.battery_type == BatteryType.PB and self.mode_pb is None:
            raise ProtocolError("Pb requires mode_pb")
        if not 0 <= self.charge_current_ma <= MAX_DEVICE_CURRENT_MA:
            raise ProtocolError(
                f"charge_current_ma {self.charge_current_ma} exceeds this hardware "
                f"family's rated output ({MAX_DEVICE_CURRENT_MA}mA) - if you're using "
                "--pack, its max_current_ma should already be well under this; if not, "
                "this is the hard device ceiling, not a chemistry-safe one"
            )
        if not 0 <= self.discharge_current_ma <= MAX_DEVICE_CURRENT_MA:
            raise ProtocolError(
                f"discharge_current_ma {self.discharge_current_ma} exceeds this "
                f"hardware family's rated output ({MAX_DEVICE_CURRENT_MA}mA)"
            )


# Defaults per libb6's Device::getDefaultChargeProfile - useful starting
# points, not a substitute for setting current/capacity for YOUR pack.
#
# No NIMH/NICD/PB entries here on purpose: this project only ships a
# lithium profile builder (lipo_profile, below), so those chemistries'
# defaults would be dead code - worse, actively wrong dead code. An
# earlier version of this table DID carry a NIMH end-voltage entry of
# "4" (millivolts), which is libb6's peak-detect delta in a different
# unit entirely, not a real per-cell end voltage (real NiMH end voltage
# is roughly 1.5-1.8V/cell) - exactly the kind of landmine an unused-
# but-present entry sets for whoever adds a nimh_profile() next and
# reasonably assumes this table is trustworthy the way lipo_profile
# does. If NiMH/NiCd/Pb support is ever added, populate real,
# hardware-verified defaults for it then - see this project's own
# verification discipline everywhere else in this file for why that
# matters here specifically.
DEFAULT_DISCHARGE_VOLTAGE_MV: dict[BatteryType, int] = {
    BatteryType.LIPO: 3200,
    BatteryType.LIION: 3100,
    BatteryType.LIFE: 2900,
    BatteryType.LIHV: 3200,
}
DEFAULT_END_VOLTAGE_MV: dict[BatteryType, int] = {
    BatteryType.LIPO: 4200,
    BatteryType.LIION: 4200,
    BatteryType.LIFE: 3700,
    BatteryType.LIHV: 4350,
}


DEFAULT_DISCHARGE_CURRENT_MA = 1000  # libb6 Device::getDefaultChargeProfile default


def lipo_profile(
    cell_count: int,
    charge_current_ma: int,
    mode: ChargingModeLi = ChargingModeLi.BALANCE,
    hv: bool = False,
    discharge_current_ma: int = DEFAULT_DISCHARGE_CURRENT_MA,
) -> ChargeProfile:
    """Build a standard LiPo (4.20V/cell) or LiHV (4.35V/cell) charge profile."""
    bt = BatteryType.LIHV if hv else BatteryType.LIPO
    return ChargeProfile(
        battery_type=bt,
        cell_count=cell_count,
        mode_li=mode,
        charge_current_ma=charge_current_ma,
        discharge_current_ma=discharge_current_ma,
        cell_discharge_voltage_mv=DEFAULT_DISCHARGE_VOLTAGE_MV[bt],
        end_voltage_mv=DEFAULT_END_VOLTAGE_MV[bt],
    )


def build_start_charging(profile: ChargeProfile) -> bytes:
    """Build the START_CHARGING request frame for a given ChargeProfile."""
    payload = bytearray()
    payload.append(profile.battery_type)
    payload.append(profile.cell_count)

    # ChargeProfile.__post_init__ already enforces these, but that check
    # is bypassable if a profile is ever mutated via object.__setattr__
    # (frozen dataclasses can be) - re-checking here with real
    # exceptions rather than `assert` means the guarantee survives
    # running under `python -O`, which strips assertions.
    if profile.battery_type.is_lithium:
        if profile.mode_li is None:
            raise ProtocolError("lithium battery types require mode_li")
        payload.append(profile.mode_li)
    elif profile.battery_type.is_nickel:
        if profile.mode_ni is None:
            raise ProtocolError("NiMH/NiCd require mode_ni")
        payload.append(profile.mode_ni)
    else:
        if profile.mode_pb is None:
            raise ProtocolError("Pb requires mode_pb")
        payload.append(profile.mode_pb)

    if not 0 <= profile.charge_current_ma <= MAX_DEVICE_CURRENT_MA:
        raise ProtocolError(
            f"charge_current_ma {profile.charge_current_ma} exceeds this hardware "
            f"family's rated output ({MAX_DEVICE_CURRENT_MA}mA)"
        )
    if not 0 <= profile.discharge_current_ma <= MAX_DEVICE_CURRENT_MA:
        raise ProtocolError(
            f"discharge_current_ma {profile.discharge_current_ma} exceeds this "
            f"hardware family's rated output ({MAX_DEVICE_CURRENT_MA}mA)"
        )

    payload += _u16(profile.charge_current_ma)
    payload += _u16(profile.discharge_current_ma)
    payload += _u16(profile.cell_discharge_voltage_mv)
    payload += _u16(profile.end_voltage_mv)

    if profile.battery_type == BatteryType.NIMH and profile.mode_ni in (
        ChargingModeNi.REPEAK,
        ChargingModeNi.CYCLE,
    ):
        if profile.mode_ni == ChargingModeNi.REPEAK:
            payload += bytes([profile.r_peak_count, 0x00])
        else:
            payload += bytes([profile.cycle_type, profile.cycle_count])
    else:
        payload += bytes([0x00, 0x00])

    payload += _u16(profile.trickle_current_ma)
    payload += bytes([0x00, 0x00, 0x00, 0x00])

    length = len(payload) + 3
    header = bytes([0x0F, length, START_CHARGING_CMD, 0x00])
    frame = header + bytes(payload)
    return frame + checksum_trailer(frame[2:])


@dataclass(frozen=True)
class ChargeInfo:
    """Decoded GET_CHARGE_INFO response: live charge telemetry.

    When `state` is IDLE or ERROR, `capacity`/`time`/`voltage`/
    `current`/`impedance`/`cells_mv` are all zeroed/empty rather than
    guessed at. For ERROR this is because libb6's Device.cc reads the
    error code and then STOPS - it never reads any further fields
    during an error response, so there is no authoritative layout to
    point at. For IDLE it's a separate, confirmed finding, not just
    caution: this project's own hardware showed stale/implausible data
    in this exact state, and - independently, on genuine (non-clone)
    SkyRC hardware - buxtronix/b6max's own README example output shows
    the identical failure mode while Idle: `TempInt=248C`,
    `20.483v`/`53.506v` "cell" readings - all physically impossible.
    Cross-referencing 10+ independent implementations found none that
    special-case IDLE vs CHARGING for these fields, and none reporting
    a working live-idle reading. This isn't a clone-specific quirk or
    something this project's own concurrent access caused - it's how
    this whole B6 hardware family behaves. `error_code` is ONLY set
    for the real ERROR state - IDLE has no error, decoding u16(5) as
    one there would be actively wrong now that the two are known
    apart. See DRY_RUN.md for the full finding.

    `temp_ext_c`/`temp_int_c` are `None` whenever `state != CHARGING` -
    confirmed 2026-08-03: unlike capacity/time/voltage/cells (which
    legitimately freeze at their final value once a charge completes -
    that's what "final session results" means), temp freezes too, but
    stays completely plausible-looking while doing it (a real capture
    read a frozen 24C for hours after the pack was disconnected). A
    range filter can't catch a frozen-but-plausible value, so temp is
    only trusted during the one state confirmed genuinely live -
    CHARGING - regardless of what the raw byte says elsewhere.
    `TEMP_MIN_C`/`TEMP_MAX_C` (the same plausibility-filter pattern
    `cells_mv` uses for `CELL_MIN_MV`/`CELL_MAX_MV`) still apply within
    CHARGING as defense in depth. See DRY_RUN.md for the full timeline.
    """

    state: int
    capacity_mah: int
    time_s: int
    voltage_mv: int
    current_ma: int
    temp_ext_c: int | None
    temp_int_c: int | None
    impedance_mohm: int
    cells_mv: tuple[int, ...] = field(default_factory=tuple)
    error_code: int | None = None

    @property
    def state_name(self) -> str:
        """Human-readable State name, or "UNKNOWN(n)" for an unrecognised value."""
        try:
            return State(self.state).name
        except ValueError:
            return f"UNKNOWN({self.state})"

    @property
    def error_name(self) -> str | None:
        """Human-readable Error name, or "UNKNOWN(n)"; None if not in an error state."""
        if self.error_code is None:
            return None
        try:
            return Error(self.error_code).name
        except ValueError:
            return f"UNKNOWN({self.error_code})"


CELL_MIN_MV = 2000  # below this a cell slot is unpopulated noise
CELL_MAX_MV = 4400  # above this it's noise on a floating balance pin, not a
# real cell - LiHV tops out at 4350mV/cell, so 4400 leaves headroom without
# ever excluding a genuine reading. Confirmed necessary 2026-08-02: with a
# 3S pack on a charger whose balance socket supports more cells, the unused
# pins read a STABLE ~9000mV once real charge current was flowing (not
# noticed on any earlier idle-charger read) - a floor-only check let it
# through as a phantom 4th/5th "cell". See DRY_RUN.md for the live trace.


def _is_real_cell(mv: int) -> bool:
    """Whether a raw cell-slot reading is plausibly a real, connected cell."""
    return CELL_MIN_MV <= mv <= CELL_MAX_MV


TEMP_MIN_C = -20  # below this a decoded temp byte is noise, not a real reading
TEMP_MAX_C = 100  # above this too - confirmed necessary 2026-08-03: this whole
# hardware family (including genuine, non-clone SkyRC units - see
# buxtronix/b6max's own README capture) can return flatly impossible
# temp bytes (248C observed) while idle, not just stale-but-plausible
# ones. 100 leaves headroom above the highest configurable temp_limit_c
# (80, see build_set_temp_limit) for a genuine "about to overheat"
# reading without excluding it, while still rejecting 248-style noise.
# See DRY_RUN.md for the live trace.


def _plausible_temp(raw_c: int) -> int | None:
    """Return `raw_c`, or None if it's outside the plausible temp range."""
    return raw_c if TEMP_MIN_C <= raw_c <= TEMP_MAX_C else None


#: The only two states with a CONFIRMED, verified-against-real-hardware
#: full pack-telemetry layout - everything else (IDLE, ERROR, and any
#: state byte this project has never seen, e.g. noise from a stuck read
#: buffer, see DRY_RUN.md) gets the conservative "short frame" decode:
#: state/temp only, everything pack-derived zeroed/empty. Deliberately
#: an allowlist, not a denylist of the states known to need the
#: conservative treatment - an unrecognized state byte defaulting to
#: the FULL decode would report pack telemetry as trustworthy for a
#: state this project has no verified layout for at all, which is
#: exactly the failure mode this whole zero-when-unverified approach
#: exists to prevent everywhere else in this function.
_FULL_FRAME_STATES = (State.CHARGING, State.COMPLETE)


def parse_charge_info(resp: bytes) -> ChargeInfo:
    """Parse a GET_CHARGE_INFO response frame into a ChargeInfo.

    Only CHARGING and COMPLETE get the full pack-telemetry decode -
    every other state, including IDLE, ERROR, and any state byte this
    project has never seen (see `_FULL_FRAME_STATES`), gets the
    conservative "short frame" treatment: `capacity`/`time`/`voltage`/
    `current`/`impedance`/`cells_mv` are zeroed/empty rather than
    decoded. For ERROR specifically: libb6's Device.cc confirms the
    wire format inserts a 2-byte error code immediately after the
    state byte, but it never reads anything past that (it throws
    immediately), so there is no verified layout for those pack-
    derived fields there. IDLE gets the same conservative treatment
    for a different reason (see ChargeInfo's docstring) rather than
    guessing at a shifted continuation of the normal layout, which is
    exactly what produced a real stale-data bug in production before
    (see DRY_RUN.md). An unrecognized state byte gets it for the same
    reason as both: no verified layout to trust, full stop - this is
    deliberately an allowlist of the states known safe to fully
    decode, not a denylist of the states known to need caution.
    `error_code` is only decoded for ERROR - IDLE and unrecognized
    states aren't errors, so u16(5) there isn't one either.

    `temp_ext_c`/`temp_int_c` are ONLY decoded (non-None) when
    `state == CHARGING` - confirmed 2026-08-03 (see ChargeInfo's
    docstring and DRY_RUN.md) that temp freezes at whatever it read
    when the charge session ended, the same as capacity/voltage/cells
    do. Freezing is CORRECT for those session-summary fields (that's
    what "final results" means), but temp isn't a session statistic -
    it reads as a live sensor, so a frozen value is actively
    misleading rather than merely stale (a live-looking dashboard
    showing a temperature from hours ago, not now). COMPLETE still
    trusts capacity/time/voltage/cells/impedance (legitimate final
    session results) but not temp specifically. `_plausible_temp()`
    is still applied as defense in depth even within CHARGING, in case
    genuinely-live reads can also produce noise.

    Raises ProtocolError if `resp` isn't shaped like a GET_CHARGE_INFO
    response at all (wrong length or command byte).
    """
    if len(resp) < 33 or resp[0] != 0x0F or resp[2] != Cmd.GET_CHARGE_INFO:
        raise ProtocolError(f"not a GET_CHARGE_INFO response: {resp[:8].hex()}")

    def u16(i: int) -> int:
        return (resp[i] << 8) | resp[i + 1]

    state = resp[4]
    if state == State.CHARGING:
        temp_ext_c = _plausible_temp(resp[13])
        temp_int_c = _plausible_temp(resp[14])
    else:
        temp_ext_c = None
        temp_int_c = None

    if state not in (s.value for s in _FULL_FRAME_STATES):
        return ChargeInfo(
            state=state,
            capacity_mah=0,
            time_s=0,
            voltage_mv=0,
            current_ma=0,
            temp_ext_c=temp_ext_c,
            temp_int_c=temp_int_c,
            impedance_mohm=0,
            cells_mv=(),
            error_code=u16(5) if state == State.ERROR else None,
        )

    cells = tuple(u16(17 + 2 * i) for i in range(8) if _is_real_cell(u16(17 + 2 * i)))
    return ChargeInfo(
        state=state,
        capacity_mah=u16(5),
        time_s=u16(7),
        voltage_mv=u16(9),
        current_ma=u16(11),
        temp_ext_c=temp_ext_c,
        temp_int_c=temp_int_c,
        impedance_mohm=u16(15),
        cells_mv=cells,
    )


@dataclass(frozen=True)
class SysInfo:
    """Decoded GET_SYS_INFO response: currently-configured system settings.

    Mirrors libb6's SysInfo struct - as opposed to ChargeInfo's live-
    charge telemetry, this is the only way to verify a set_* write
    actually took effect without relying on finding the right screen on
    the front panel.
    """

    cycle_time: int
    time_limit_on: bool
    time_limit_minutes: int
    capacity_limit_on: bool
    capacity_limit_mah: int
    key_buzzer: bool
    system_buzzer: bool
    low_dc_limit_mv: int
    temp_limit_c: int
    voltage_mv: int
    cells_mv: tuple[int, ...] = field(default_factory=tuple)


def parse_sys_info(resp: bytes) -> SysInfo:
    """Parse a GET_SYS_INFO response frame into a SysInfo.

    Raises ProtocolError if `resp` isn't shaped like a GET_SYS_INFO
    response at all (wrong length or command byte).
    """
    if len(resp) < 36 or resp[0] != 0x0F or resp[2] != Cmd.GET_SYS_INFO:
        raise ProtocolError(f"not a GET_SYS_INFO response: {resp[:8].hex()}")

    def u16(i: int) -> int:
        return (resp[i] << 8) | resp[i + 1]

    # offsets per Device::getSysInfo(), after the 4-byte header:
    # cycleTime(u8) timeLimitOn(u8) timeLimit(u16) capLimitOn(u8)
    # capLimit(u16) keyBuzzer(u8) systemBuzzer(u8) lowDCLimit(u16)
    # <skip 2> tempLimit(u8) voltage(u16) cells[8](u16 each)
    cells = tuple(u16(20 + 2 * i) for i in range(8) if _is_real_cell(u16(20 + 2 * i)))
    return SysInfo(
        cycle_time=resp[4],
        time_limit_on=bool(resp[5]),
        time_limit_minutes=u16(6),
        capacity_limit_on=bool(resp[8]),
        capacity_limit_mah=u16(9),
        key_buzzer=bool(resp[11]),
        system_buzzer=bool(resp[12]),
        low_dc_limit_mv=u16(13),
        temp_limit_c=resp[17],
        voltage_mv=u16(18),
        cells_mv=cells,
    )
