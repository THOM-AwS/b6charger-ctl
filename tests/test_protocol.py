"""Protocol-level tests. The two literal hex fixtures below were hand-
traced against libb6's Device.cc/Packet.cc byte-by-byte (see the repo's
DRY_RUN.md for the worked calculation) - they exist so any future
refactor of the frame builders trips a hard regression, not just the
structural property tests.
"""

from __future__ import annotations

import pytest

from b6charger import protocol


def test_decode_u16_matches_manual_shift():
    # Direct unit test for the shared decoder parse_charge_info and
    # parse_sys_info both delegate to, in addition to the extensive
    # indirect coverage those parsers already get below.
    assert protocol._decode_u16(bytes([0x00, 0x00]), 0) == 0
    assert protocol._decode_u16(bytes([0x01, 0x00]), 0) == 256
    assert protocol._decode_u16(bytes([0xFF, 0xFF]), 0) == 0xFFFF
    assert protocol._decode_u16(bytes([0x00, 0x12, 0x34]), 1) == 0x1234


def test_get_charge_info_frame_matches_known_good_bytes():
    # Independently verified against a separate read-only exporter for
    # this same protocol, which has sent this exact frame to a real
    # Jaycar POWERTECH PLUS MB-3633.
    expected = bytes.fromhex("0f035500" "55ffff")
    assert protocol.build_get_charge_info() == expected


def test_stop_charging_frame_matches_known_good_bytes():
    assert protocol.build_stop_charging() == bytes.fromhex("0f03fe00feffff")


def test_start_charging_frame_matches_hand_traced_bytes():
    profile = protocol.lipo_profile(cell_count=3, charge_current_ma=1500)
    frame = protocol.build_start_charging(profile)
    # Full trace (see module docstring): battery_type=LIPO(0x00),
    # cell_count=3, mode=BALANCE(0x04), charge_current=1500(0x05DC),
    # discharge_current=1000(0x03E8), cell_discharge_voltage=3200(0x0C80),
    # end_voltage=4200(0x1068), no repeak/cycle bytes, trickle=0, 4
    # reserved bytes, checksum=0xDC.
    expected = bytes.fromhex(
        "0f160500" "000304" "05dc" "03e8" "0c80" "1068" "0000" "0000" "00000000" "dcffff"
    )
    assert frame == expected
    assert len(frame) == 26


@pytest.mark.parametrize(
    "builder",
    [
        protocol.build_get_dev_info,
        protocol.build_get_sys_info,
        protocol.build_get_charge_info,
        protocol.build_stop_charging,
        protocol.build_unk1,
    ],
)
def test_simple_frames_are_seven_bytes_with_correct_trailer(builder):
    frame = builder()
    assert len(frame) == 7
    assert frame[0] == 0x0F
    assert frame[1] == 0x03
    assert frame[-2:] == b"\xff\xff"
    assert frame[4] == sum(frame[2:4]) & 0xFF


@pytest.mark.parametrize(
    "profile",
    [
        protocol.lipo_profile(cell_count=1, charge_current_ma=100),
        protocol.lipo_profile(cell_count=6, charge_current_ma=6000),
        protocol.lipo_profile(cell_count=4, charge_current_ma=1500, hv=True),
        protocol.ChargeProfile(
            battery_type=protocol.BatteryType.NIMH,
            cell_count=6,
            mode_ni=protocol.ChargingModeNi.REPEAK,
            charge_current_ma=2000,
        ),
        protocol.ChargeProfile(
            battery_type=protocol.BatteryType.PB,
            cell_count=6,
            mode_pb=protocol.ChargingModePb.CHARGE,
            charge_current_ma=500,
        ),
    ],
)
def test_start_charging_frame_length_and_checksum_are_self_consistent(profile):
    frame = protocol.build_start_charging(profile)
    assert frame[0] == 0x0F
    assert frame[2] == protocol.START_CHARGING_CMD
    length_field = frame[1]
    assert length_field == len(frame) - 4  # bytes after the 4-byte header
    assert frame[-2:] == b"\xff\xff"
    assert frame[-3] == sum(frame[2:-3]) & 0xFF


@pytest.mark.parametrize(
    "builder,args",
    [
        (protocol.build_set_cycle_time, (30,)),
        (protocol.build_set_time_limit, (True, 200)),
        (protocol.build_set_capacity_limit, (True, 5000)),
        (protocol.build_set_temp_limit, (50,)),
        (protocol.build_set_buzzers, (True, False)),
    ],
)
def test_set_system_frames_are_self_consistent(builder, args):
    frame = builder(*args)
    assert frame[0] == 0x0F
    assert frame[2] == 0x11
    assert frame[1] == len(frame) - 4
    assert frame[-2:] == b"\xff\xff"
    assert frame[-3] == sum(frame[2:-3]) & 0xFF


def test_set_cycle_time_rejects_out_of_range():
    with pytest.raises(protocol.ProtocolError):
        protocol.build_set_cycle_time(0)
    with pytest.raises(protocol.ProtocolError):
        protocol.build_set_cycle_time(61)


def test_charge_profile_requires_matching_mode_for_chemistry():
    with pytest.raises(protocol.ProtocolError):
        protocol.ChargeProfile(battery_type=protocol.BatteryType.LIPO, cell_count=3)
    with pytest.raises(protocol.ProtocolError):
        protocol.ChargeProfile(battery_type=protocol.BatteryType.NIMH, cell_count=6)
    with pytest.raises(protocol.ProtocolError):
        protocol.ChargeProfile(battery_type=protocol.BatteryType.PB, cell_count=6)


def test_charge_profile_rejects_bad_cell_count():
    with pytest.raises(protocol.ProtocolError):
        protocol.ChargeProfile(
            battery_type=protocol.BatteryType.LIPO,
            cell_count=17,
            mode_li=protocol.ChargingModeLi.BALANCE,
        )


def test_charge_profile_rejects_current_above_device_ceiling():
    """A manual/raw profile has no pack to derive a C-rate from - the
    device ceiling is the only thing standing between a typo/bad input
    and an unbounded current, so it must apply unconditionally."""
    with pytest.raises(protocol.ProtocolError):
        protocol.ChargeProfile(
            battery_type=protocol.BatteryType.LIPO,
            cell_count=3,
            mode_li=protocol.ChargingModeLi.BALANCE,
            charge_current_ma=protocol.MAX_DEVICE_CURRENT_MA + 1,
        )
    with pytest.raises(protocol.ProtocolError):
        protocol.ChargeProfile(
            battery_type=protocol.BatteryType.LIPO,
            cell_count=3,
            mode_li=protocol.ChargingModeLi.BALANCE,
            discharge_current_ma=protocol.MAX_DEVICE_CURRENT_MA + 1,
        )
    # exactly at the ceiling is still allowed
    protocol.ChargeProfile(
        battery_type=protocol.BatteryType.LIPO,
        cell_count=3,
        mode_li=protocol.ChargingModeLi.BALANCE,
        charge_current_ma=protocol.MAX_DEVICE_CURRENT_MA,
    )


def test_parse_charge_info_round_trips_through_fake_transport_encoding():
    from b6charger.transport import FakeChargerTransport

    fake = FakeChargerTransport()
    fake.cells_mv = (4100, 4102, 4098, 4099)
    fake.pack_voltage_mv = sum(fake.cells_mv)
    fake.current_ma = 1500
    fake.state = protocol.State.CHARGING
    resp = fake._encode_charge_info()

    info = protocol.parse_charge_info(resp)
    assert info.state == protocol.State.CHARGING
    assert info.state_name == "CHARGING"
    assert info.cells_mv == fake.cells_mv
    assert info.voltage_mv == fake.pack_voltage_mv
    assert info.current_ma == 1500


def test_parse_charge_info_filters_out_floating_pin_noise_above_max():
    # Real values observed 2026-08-02 on a 3S pack mid-charge: cells 4/5
    # read a stable ~9000mV (floating balance-socket pins on a charger
    # whose socket supports more cells than the connected pack), which
    # a floor-only filter let through as phantom cells. See DRY_RUN.md.
    from b6charger.transport import FakeChargerTransport

    fake = FakeChargerTransport()
    fake.cells_mv = (4197, 4201, 4192, 9217, 8960)
    fake.pack_voltage_mv = 4197 + 4201 + 4192  # real pack voltage, 3 real cells
    fake.state = protocol.State.CHARGING

    info = protocol.parse_charge_info(fake._encode_charge_info())
    assert info.cells_mv == (4197, 4201, 4192)


def test_parse_charge_info_zeroes_pack_telemetry_but_not_temp_in_idle_state():
    # Observed 2026-08-02: with no battery physically connected, the
    # charger's own panel showed nothing wrong, and charger_state read
    # 2 - originally (wrongly) trusted as libb6's ERROR_1, corrected
    # the same day to IDLE once an independent project (buxtronix/
    # b6max) confirmed 2 means Idle, not error - see DRY_RUN.md. The
    # raw response still carried stale non-zero pack voltage/current
    # from before disconnection despite genuinely being idle, which is
    # why pack-derived fields stay conservatively zeroed here rather
    # than guessed at. temp is NOT zeroed here (reverted 2026-08-04,
    # see test_parse_charge_info_decodes_temp_in_every_state) - it's
    # decoded the same as CHARGING, subject only to the plausibility
    # filter, which is a known, accepted tradeoff, not an oversight.
    from b6charger.transport import FakeChargerTransport

    fake = FakeChargerTransport()
    fake.state = protocol.State.IDLE
    # even if the fake's other fields hold stale-looking data, none of
    # it should be encoded/decoded while idle:
    fake.cells_mv = (4197, 4201, 4192)
    fake.pack_voltage_mv = 12605
    fake.current_ma = 292
    fake.temp_int_c = 24  # plausible-looking - decoded, not zeroed, see above

    info = protocol.parse_charge_info(fake._encode_charge_info())
    assert info.state_name == "IDLE"
    # IDLE isn't an error - it must not get an error_code, unlike ERROR:
    assert info.error_code is None
    assert info.error_name is None
    assert info.cells_mv == ()
    assert info.voltage_mv == 0
    assert info.current_ma == 0
    assert info.capacity_mah == 0
    assert info.time_s == 0
    assert info.impedance_mohm == 0
    assert info.temp_int_c == 24


def test_parse_charge_info_zeroes_pack_telemetry_for_unrecognized_state_byte():
    # An unrecognized/garbage state byte - e.g. noise from a stuck read
    # buffer, a proven failure mode on this hardware (see DRY_RUN.md) -
    # must NOT fall through to the full pack-telemetry decode just
    # because it isn't specifically IDLE or ERROR. There's no verified
    # layout for any state this project hasn't confirmed, unrecognized
    # ones very much included - trusting one by default would be the
    # exact failure mode this whole zero-when-unverified approach
    # exists to prevent everywhere else in this function.
    from b6charger.transport import FakeChargerTransport

    fake = FakeChargerTransport()
    fake.cells_mv = (4197, 4201, 4192)
    fake.pack_voltage_mv = 12605
    fake.current_ma = 292
    encoded = bytearray(fake._encode_charge_info())
    encoded[4] = 99  # not a real State value

    info = protocol.parse_charge_info(bytes(encoded))
    assert info.state == 99
    assert info.state_name == "UNKNOWN(99)"
    assert info.cells_mv == ()
    assert info.voltage_mv == 0
    assert info.current_ma == 0
    assert info.capacity_mah == 0
    assert info.error_code is None


def test_parse_charge_info_decodes_temp_in_every_state():
    # A CHARGING-only gate was added 2026-08-03 after a genuinely
    # frozen-but-plausible temp reading was observed staying pinned at
    # a real-looking 24C for hours after a pack was disconnected (see
    # DRY_RUN.md) - a range filter alone can't catch "plausible but
    # hours old." That gate was deliberately reverted 2026-08-04 by
    # explicit request, after being shown this exact history: temp is
    # decoded in every state again, so the frozen-value tradeoff above
    # is knowingly back in play, not an oversight. See DRY_RUN.md's
    # "Reversal, by explicit request" entry.
    from b6charger.transport import FakeChargerTransport

    fake = FakeChargerTransport()
    fake.temp_int_c = 22  # a perfectly plausible value

    for state in (protocol.State.CHARGING, protocol.State.COMPLETE, protocol.State.IDLE):
        fake.state = state
        assert protocol.parse_charge_info(fake._encode_charge_info()).temp_int_c == 22


def test_parse_charge_info_rejects_implausible_temp_even_while_charging():
    # Confirmed 2026-08-03: buxtronix/b6max's own README shows a real
    # capture from genuine (non-clone) SkyRC hardware reading
    # TempInt=248C while idle - physically impossible, and not
    # something this project's own concurrent access could have
    # caused, since buxtronix's tool never had concurrent access at
    # all. TEMP_MIN_C/TEMP_MAX_C stays in effect in every state
    # (temp decoding is no longer CHARGING-only, see
    # test_parse_charge_info_decodes_temp_in_every_state) - this
    # filter only ever catches flatly impossible values like this one,
    # never a frozen-but-plausible reading; that's a known, separate
    # limitation, not something this test claims to cover.
    from b6charger.transport import FakeChargerTransport

    fake = FakeChargerTransport()
    fake.state = protocol.State.CHARGING
    fake.temp_int_c = 248
    # 42 is itself a plausible temperature (if a warm one) - the range
    # filter can't distinguish "genuinely 42C" from "coincidentally
    # plausible-looking garbage"; that's an honest limitation of a
    # range filter, not a bug. Kept deliberately in-range here so this
    # test doesn't overstate what filtering can catch.
    fake.temp_ext_c = 42

    info = protocol.parse_charge_info(fake._encode_charge_info())
    assert info.temp_int_c is None
    assert info.temp_ext_c == 42


def test_parse_charge_info_decodes_error_code_only_in_error_state():
    from b6charger.transport import FakeChargerTransport

    fake = FakeChargerTransport()
    fake.state = protocol.State.ERROR
    fake.error_code = protocol.Error.BATTERY_FULL

    info = protocol.parse_charge_info(fake._encode_charge_info())
    assert info.state_name == "ERROR"
    assert info.error_code == protocol.Error.BATTERY_FULL
    assert info.error_name == "BATTERY_FULL"
    assert info.cells_mv == ()
    assert info.voltage_mv == 0


def test_parse_charge_info_reports_unknown_error_name_for_unmapped_code():
    from b6charger.transport import FakeChargerTransport

    fake = FakeChargerTransport()
    fake.state = protocol.State.ERROR
    fake.error_code = 0x1234  # not in the Error enum

    info = protocol.parse_charge_info(fake._encode_charge_info())
    assert info.error_code == 0x1234
    assert info.error_name == "UNKNOWN(4660)"


def test_parse_charge_info_error_name_is_none_outside_error_states():
    from b6charger.transport import FakeChargerTransport

    fake = FakeChargerTransport()  # default: COMPLETE, not an error state
    info = protocol.parse_charge_info(fake._encode_charge_info())
    assert info.error_code is None
    assert info.error_name is None


def test_parse_charge_info_keeps_voltage_and_current_when_cells_present():
    from b6charger.transport import FakeChargerTransport

    fake = FakeChargerTransport()  # default: 3 real cells, non-zero voltage

    info = protocol.parse_charge_info(fake._encode_charge_info())
    assert info.cells_mv != ()
    assert info.voltage_mv == fake.pack_voltage_mv
    assert info.current_ma == fake.current_ma


def test_parse_charge_info_does_not_force_zero_voltage_in_normal_state_with_no_cells():
    # A genuinely-idle, no-battery-connected read in a NORMAL (non-error)
    # state has always reported a real, naturally-near-zero voltage on
    # actual hardware (see DRY_RUN.md's very first hardware read) - no
    # heuristic override needed or wanted here, only the error-state
    # branch zeroes anything.
    from b6charger.transport import FakeChargerTransport

    fake = FakeChargerTransport()
    fake.cells_mv = ()
    fake.pack_voltage_mv = 5  # a real near-zero idle reading, not stale garbage

    info = protocol.parse_charge_info(fake._encode_charge_info())
    assert info.cells_mv == ()
    assert info.voltage_mv == 5


def test_parse_charge_info_rejects_wrong_command():
    bad = bytes([0x0F, 0x03, 0xFE, 0x00]) + bytes(60)
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_charge_info(bad)


def test_parse_sys_info_round_trips_through_fake_transport_encoding():
    from b6charger.transport import FakeChargerTransport

    fake = FakeChargerTransport()
    fake.temp_limit_c = 50
    fake.time_limit_on = True
    fake.time_limit_minutes = 200
    fake.capacity_limit_mah = 6000

    info = protocol.parse_sys_info(fake._encode_sys_info())
    assert info.temp_limit_c == 50
    assert info.time_limit_on is True
    assert info.time_limit_minutes == 200
    assert info.capacity_limit_mah == 6000
    assert info.cells_mv == fake.cells_mv


def test_parse_sys_info_rejects_wrong_command():
    bad = bytes([0x0F, 0x03, 0x55, 0x00]) + bytes(60)
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_sys_info(bad)
