from __future__ import annotations

import pytest

from b6charger import protocol
from b6charger.transport import FakeChargerTransport, _encode_u16


def test_fake_transport_starts_in_complete_state():
    fake = FakeChargerTransport()
    info = protocol.parse_charge_info(fake.read())
    assert info.state == protocol.State.COMPLETE


def test_fake_transport_start_charging_transitions_to_charging():
    fake = FakeChargerTransport()
    profile = protocol.lipo_profile(cell_count=3, charge_current_ma=1500)
    fake.write(protocol.build_start_charging(profile))
    fake.read()  # ack, not parsed as charge_info on real hardware either

    fake.write(protocol.build_get_charge_info())
    info = protocol.parse_charge_info(fake.read())
    assert info.state == protocol.State.CHARGING
    assert info.current_ma == 1500


def test_fake_transport_stop_charging_transitions_to_complete():
    fake = FakeChargerTransport()
    fake.write(protocol.build_start_charging(protocol.lipo_profile(3, 1500)))
    fake.read()
    fake.write(protocol.build_stop_charging())
    fake.read()

    fake.write(protocol.build_get_charge_info())
    info = protocol.parse_charge_info(fake.read())
    assert info.state == protocol.State.COMPLETE
    assert info.current_ma == 0


def test_fake_transport_transact_matches_write_then_read():
    # this is the method Device actually calls - confirm it's equivalent
    # to the write()/read() pair the tests above exercise separately.
    fake = FakeChargerTransport()
    resp = fake.transact(protocol.build_start_charging(protocol.lipo_profile(3, 1500)))
    assert resp == bytes(64)
    info = protocol.parse_charge_info(fake.transact(protocol.build_get_charge_info()))
    assert info.state == protocol.State.CHARGING
    assert info.current_ma == 1500


# --- _encode_u16(): the shared, bounds-checked byte-pair encoder used --
# --- throughout FakeChargerTransport's response encoding ----------------


def test_encode_u16_matches_divmod_for_in_range_values():
    assert _encode_u16(0) == (0, 0)
    assert _encode_u16(255) == (0, 255)
    assert _encode_u16(256) == (1, 0)
    assert _encode_u16(0xFFFF) == (0xFF, 0xFF)


def test_encode_u16_rejects_out_of_range_with_a_clear_message():
    # A bare `divmod(70000, 256)` still "succeeds" (5, 240) - the whole
    # point of this helper is catching that BEFORE it's silently
    # assigned into a bytearray slot, where it would instead raise a
    # contextless "bytearray must be in range(0, 256)" ValueError.
    with pytest.raises(ValueError, match="out of u16 range"):
        _encode_u16(0x10000)
    with pytest.raises(ValueError, match="out of u16 range"):
        _encode_u16(-1)
