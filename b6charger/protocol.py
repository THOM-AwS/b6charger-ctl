# SPDX-License-Identifier: GPL-3.0-or-later
"""Wire protocol for SkyRC B6-family balance chargers (iMAX B6 / B6AC /
B6 mini and rebadged clones, e.g. Jaycar POWERTECH PLUS MB-3633 / JMB-3633).

Frame layout, reverse-engineered by maciek134/libb6 (GPL-3) and confirmed
here against a live Jaycar POWERTECH PLUS MB-3633 for the read path:

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

Only GET_CHARGE_INFO has been exercised against real hardware so far
(via ht-infra's b6_poller.py). Everything else here is implemented
strictly from libb6's Device.cc/Packet.cc/Enum.hh and has NOT been
validated against a physical charger yet - see README.md's hardware
test checklist before trusting the write path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Cmd(IntEnum):
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
    STANDARD = 0x00
    DISCHARGE = 0x01
    STORAGE = 0x02
    FAST = 0x03
    BALANCE = 0x04


class ChargingModeNi(IntEnum):
    STANDARD = 0x00
    AUTO = 0x01
    DISCHARGE = 0x02
    REPEAK = 0x03
    CYCLE = 0x04


class ChargingModePb(IntEnum):
    CHARGE = 0x00
    DISCHARGE = 0x01


class BatteryType(IntEnum):
    LIPO = 0x00
    LIION = 0x01
    LIFE = 0x02
    LIHV = 0x03
    NIMH = 0x04
    NICD = 0x05
    PB = 0x06

    @property
    def is_lithium(self) -> bool:
        return self in (
            BatteryType.LIPO,
            BatteryType.LIION,
            BatteryType.LIFE,
            BatteryType.LIHV,
        )

    @property
    def is_nickel(self) -> bool:
        return self in (BatteryType.NIMH, BatteryType.NICD)


class State(IntEnum):
    """Per libb6's Enum.hh. NOTE: ht-infra's b6_poller.py currently
    labels state 4 as "idle" - this enum (the only source that's been
    read directly, rather than inferred from panel behaviour) says 4 is
    a SECOND error state, not idle. Unverified which is correct on this
    specific clone's firmware; see README.md "STATE 4 discrepancy"."""

    CHARGING = 1
    ERROR_1 = 2
    COMPLETE = 3
    ERROR_2 = 4


class Error(IntEnum):
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
    pass


def checksum_trailer(payload_from_cmd: bytes) -> bytes:
    """payload_from_cmd is everything from the CMD byte onward (i.e. the
    frame with the leading [0x0F, LEN] stripped). Mirrors libb6
    Packet::writeChecksum(), which sums from buffer index 2 onward."""
    total = sum(payload_from_cmd) & 0xFF
    return bytes([total, 0xFF, 0xFF])


def _simple_frame(cmd: int) -> bytes:
    body = bytes([0x0F, 0x03, cmd, 0x00])
    return body + checksum_trailer(body[2:])


def build_get_dev_info() -> bytes:
    return _simple_frame(Cmd.GET_DEV_INFO)


def build_get_sys_info() -> bytes:
    return _simple_frame(Cmd.GET_SYS_INFO)


def build_get_charge_info() -> bytes:
    return _simple_frame(Cmd.GET_CHARGE_INFO)


def build_stop_charging() -> bytes:
    return _simple_frame(Cmd.STOP_CHARGING)


def build_unk1() -> bytes:
    return _simple_frame(Cmd.UNK1)


def _u16(val: int) -> bytes:
    if not 0 <= val <= 0xFFFF:
        raise ProtocolError(f"value {val} out of u16 range")
    return bytes([(val >> 8) & 0xFF, val & 0xFF])


def _set_system_frame(param_id: int, value_bytes: bytes) -> bytes:
    payload = bytes([0x00]) + value_bytes
    length = len(payload) + 3
    header = bytes([0x0F, length, 0x11, param_id])
    frame = header + payload
    return frame + checksum_trailer(frame[2:])


def build_set_cycle_time(cycle_time: int) -> bytes:
    if not 1 <= cycle_time <= 60:
        raise ProtocolError("cycle_time must be 1-60")
    return _set_system_frame(SetSystemParam.CYCLE_TIME, bytes([cycle_time]))


def build_set_time_limit(enabled: bool, limit_minutes: int) -> bytes:
    if not 1 <= limit_minutes <= 720:
        raise ProtocolError("limit_minutes must be 1-720")
    return _set_system_frame(
        SetSystemParam.TIME_LIMIT, bytes([int(enabled)]) + _u16(limit_minutes)
    )


def build_set_capacity_limit(enabled: bool, limit_mah: int) -> bytes:
    if not 100 <= limit_mah <= 50000:
        raise ProtocolError("limit_mah must be 100-50000")
    return _set_system_frame(
        SetSystemParam.CAPACITY_LIMIT, bytes([int(enabled)]) + _u16(limit_mah)
    )


def build_set_temp_limit(limit_celsius: int) -> bytes:
    if not 20 <= limit_celsius <= 80:
        raise ProtocolError("limit_celsius must be 20-80")
    return _set_system_frame(SetSystemParam.TEMP_LIMIT, bytes([limit_celsius]))


def build_set_buzzers(key: bool, system: bool) -> bytes:
    return _set_system_frame(SetSystemParam.BUZZERS, bytes([int(key), int(system)]))


@dataclass(frozen=True)
class ChargeProfile:
    """Mirrors libb6's ChargeProfile struct. Construct via the chemistry
    helpers below rather than directly, unless you know exactly what
    you're doing - endVoltage/cellDischargeVoltage are in millivolts
    PER CELL and an incorrect value is exactly the failure mode this
    project exists to prevent, not create."""

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
    """Standard LiPo (4.20V/cell) or LiHV (4.35V/cell) profile."""
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
    payload = bytearray()
    payload.append(profile.battery_type)
    payload.append(profile.cell_count)

    if profile.battery_type.is_lithium:
        assert profile.mode_li is not None
        payload.append(profile.mode_li)
    elif profile.battery_type.is_nickel:
        assert profile.mode_ni is not None
        payload.append(profile.mode_ni)
    else:
        assert profile.mode_pb is not None
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
    state: int
    capacity_mah: int
    time_s: int
    voltage_mv: int
    current_ma: int
    temp_ext_c: int
    temp_int_c: int
    impedance_mohm: int
    cells_mv: tuple[int, ...] = field(default_factory=tuple)

    @property
    def state_name(self) -> str:
        try:
            return State(self.state).name
        except ValueError:
            return f"UNKNOWN({self.state})"


CELL_MIN_MV = 2000  # below this a cell slot is unpopulated noise
CELL_MAX_MV = 4400  # above this it's noise on a floating balance pin, not a
# real cell - LiHV tops out at 4350mV/cell, so 4400 leaves headroom without
# ever excluding a genuine reading. Confirmed necessary 2026-08-02: with a
# 3S pack on a charger whose balance socket supports more cells, the unused
# pins read a STABLE ~9000mV once real charge current was flowing (not
# noticed on any earlier idle-charger read) - a floor-only check let it
# through as a phantom 4th/5th "cell". See DRY_RUN.md for the live trace.


def _is_real_cell(mv: int) -> bool:
    return CELL_MIN_MV <= mv <= CELL_MAX_MV


def parse_charge_info(resp: bytes) -> ChargeInfo:
    if len(resp) < 33 or resp[0] != 0x0F or resp[2] != Cmd.GET_CHARGE_INFO:
        raise ProtocolError(f"not a GET_CHARGE_INFO response: {resp[:8].hex()}")

    def u16(i: int) -> int:
        return (resp[i] << 8) | resp[i + 1]

    cells = tuple(u16(17 + 2 * i) for i in range(8) if _is_real_cell(u16(17 + 2 * i)))
    return ChargeInfo(
        state=resp[4],
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
    """Mirrors libb6's SysInfo struct - the currently-configured system
    settings, as opposed to ChargeInfo's live-charge telemetry. This is
    the only way to verify a set_* write actually took effect without
    relying on finding the right screen on the front panel."""

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
