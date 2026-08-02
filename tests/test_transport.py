from __future__ import annotations

from b6charger import protocol
from b6charger.transport import FakeChargerTransport


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
