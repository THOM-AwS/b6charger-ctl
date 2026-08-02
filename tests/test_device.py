from __future__ import annotations

from b6charger import protocol
from b6charger.device import Device
from b6charger.transport import FakeChargerTransport


def test_start_stop_round_trip():
    dev = Device(FakeChargerTransport())
    dev.start_charging(protocol.lipo_profile(cell_count=3, charge_current_ma=1500))
    info = dev.get_charge_info()
    assert info.state == protocol.State.CHARGING
    assert info.current_ma == 1500

    dev.stop_charging()
    info = dev.get_charge_info()
    assert info.state == protocol.State.COMPLETE


def test_dry_run_never_writes_to_transport():
    transport = FakeChargerTransport()
    dev = Device(transport, dry_run=True)
    dev.start_charging(protocol.lipo_profile(cell_count=4, charge_current_ma=2000, hv=True))
    # the fake's state must be untouched - dry_run short-circuits before write()
    assert transport.state == protocol.State.COMPLETE
    assert transport.current_ma == 0


def test_set_limits_reach_transport_when_not_dry_run():
    transport = FakeChargerTransport()
    dev = Device(transport)
    dev.set_cycle_time(30)
    assert transport._last_write == protocol.build_set_cycle_time(30)


def test_set_limits_do_not_reach_transport_in_dry_run():
    transport = FakeChargerTransport()
    dev = Device(transport, dry_run=True)
    dev.set_cycle_time(30)
    assert transport._last_write == b""
