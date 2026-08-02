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

    Per libb6's Enum.hh - the only source read directly here, rather
    than inferred from panel behaviour. Some other B6-family tooling in
    the wild labels state 4 as "idle" rather than a second error state;
    unverified which is correct on which firmware. See README.md's
    "STATE 4 discrepancy".
    """

    CHARGING = 1
    ERROR_1 = 2
    COMPLETE = 3
    ERROR_2 = 4


class Error(IntEnum):
    """Error codes returned alongside an ERROR_1/ERROR_2 state, per Enum.hh."""

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
    """Build a GET_DEV_INFO request frame."""
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
        """Validate cell count and that the mode matching this chemistry was set."""
        if not 1 <= self.cell_count <= 16:
            raise ProtocolError("cell_count must be 1-16")
        if self.battery_type.is_lithium and self.mode_li is None:
            raise ProtocolError("lithium battery types require mode_li")
        if self.battery_type.is_nickel and self.mode_ni is None:
            raise ProtocolError("NiMH/NiCd require mode_ni")
        if self.battery_type == BatteryType.PB and self.mode_pb is None:
            raise ProtocolError("Pb requires mode_pb")


# Defaults per libb6's Device::getDefaultChargeProfile - useful starting
# points, not a substitute for setting current/capacity for YOUR pack.
DEFAULT_DISCHARGE_VOLTAGE_MV: dict[BatteryType, int] = {
    BatteryType.LIPO: 3200,
    BatteryType.LIION: 3100,
    BatteryType.LIFE: 2900,
    BatteryType.LIHV: 3200,
    BatteryType.NIMH: 1100,
}
DEFAULT_END_VOLTAGE_MV: dict[BatteryType, int] = {
    BatteryType.LIPO: 4200,
    BatteryType.LIION: 4200,
    BatteryType.LIFE: 3700,
    BatteryType.LIHV: 4350,
    BatteryType.NIMH: 4,
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

    When `state` is ERROR_1/ERROR_2, only `state` and `error_code` are
    reliably real - capacity/time/voltage/current/impedance/cells_mv
    are all zeroed/empty rather than guessed at. libb6's own Device.cc
    reads the error code and then STOPS - it never reads any further
    fields during an error response, so there is no authoritative
    layout to point at here.

    `temp_ext_c`/`temp_int_c` ARE decoded even in this state, at the
    same offsets the normal-state parser uses, but the confidence level
    here is lower than the module previously claimed: a first no-battery
    capture (2026-08-02) decoded temp_int_c=24 (plausible) while
    voltage/current/cells in that same read were confirmed-stale
    leftovers from the prior charge - read at the time as evidence
    temp was a live, pack-independent sensor. A physical power-cycle
    the same day disproved that: post-restart, in the identical state,
    temp_int_c read 0 - alongside every pack field ALSO freshly zeroed
    instead of stale. That pattern is much more consistent with temp
    being populated by firmware only during/after an active charge
    session (frozen like the other fields, just cleared by the same
    restart) than with it being an independently-live sensor. Decoded
    anyway because, unlike voltage/current/cells, nothing in this
    codebase treats temp as a safety input - a stale-or-zero reading
    here is a monitoring-accuracy question, not a fire-risk one. See
    DRY_RUN.md for the full timeline, including this correction.
    """

    state: int
    capacity_mah: int
    time_s: int
    voltage_mv: int
    current_ma: int
    temp_ext_c: int
    temp_int_c: int
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


_ERROR_STATES = (State.ERROR_1, State.ERROR_2)


def parse_charge_info(resp: bytes) -> ChargeInfo:
    """Parse a GET_CHARGE_INFO response frame into a ChargeInfo.

    When the state byte is ERROR_1/ERROR_2, `capacity`/`time`/
    `voltage`/`current`/`impedance`/`cells_mv` are zeroed/empty rather
    than decoded - libb6's Device.cc confirms the wire format inserts a
    2-byte error code immediately after the state byte in that case,
    but it never reads anything past that error code (it throws
    immediately), so there is no verified layout for those
    pack-derived fields during an error response, and reporting them
    as zero/empty is honest about not knowing them (see DRY_RUN.md's
    "stale voltage/current after disconnect" finding - guessing at a
    shifted continuation of the normal layout would reintroduce
    exactly that bug).

    `temp_ext_c`/`temp_int_c` ARE decoded even in an error state, at
    the same offsets the normal-state path already trusts - but decode
    here does not mean "guaranteed live": a same-day physical restart
    test showed this field reads 0 right after power-on and only a
    plausible non-zero value once the charger has actually run a
    charge, suggesting it's populated per-session like the fields
    above rather than continuously sampled while idle. Decoded anyway
    since nothing here treats temp as a safety input, unlike
    voltage/current/cells_mv. See DRY_RUN.md for the full timeline.

    Raises ProtocolError if `resp` isn't shaped like a GET_CHARGE_INFO
    response at all (wrong length or command byte).
    """
    if len(resp) < 33 or resp[0] != 0x0F or resp[2] != Cmd.GET_CHARGE_INFO:
        raise ProtocolError(f"not a GET_CHARGE_INFO response: {resp[:8].hex()}")

    def u16(i: int) -> int:
        return (resp[i] << 8) | resp[i + 1]

    state = resp[4]

    if state in (s.value for s in _ERROR_STATES):
        return ChargeInfo(
            state=state,
            capacity_mah=0,
            time_s=0,
            voltage_mv=0,
            current_ma=0,
            temp_ext_c=resp[13],
            temp_int_c=resp[14],
            impedance_mohm=0,
            cells_mv=(),
            error_code=u16(5),
        )

    cells = tuple(u16(17 + 2 * i) for i in range(8) if _is_real_cell(u16(17 + 2 * i)))
    return ChargeInfo(
        state=state,
        capacity_mah=u16(5),
        time_s=u16(7),
        voltage_mv=u16(9),
        current_ma=u16(11),
        temp_ext_c=resp[13],
        temp_int_c=resp[14],
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
