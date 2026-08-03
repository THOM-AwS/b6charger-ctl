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


def test_set_temp_limit_is_visible_via_get_sys_info():
    # mirrors the actual hardware verification step in DRY_RUN.md -
    # set a limit, then confirm it reads back changed rather than just
    # trusting that the write didn't error.
    dev = Device(FakeChargerTransport())
    before = dev.get_sys_info()
    assert before.temp_limit_c != 45

    dev.set_temp_limit(45)
    after = dev.get_sys_info()
    assert after.temp_limit_c == 45


# --- start_charging_verified(): confirming a start actually took, -----
# --- not just that it was sent - see DRY_RUN.md for why this exists ----


def test_start_charging_verified_confirms_immediately_on_match():
    dev = Device(FakeChargerTransport())
    profile = protocol.lipo_profile(cell_count=3, charge_current_ma=1500)
    result = dev.start_charging_verified(profile, confirm_delay_s=0, retry_delay_s=0)
    assert result.confirmed is True
    assert result.stopped is False
    assert result.info.state == protocol.State.CHARGING


def test_start_charging_verified_returns_none_in_dry_run():
    transport = FakeChargerTransport()
    dev = Device(transport, dry_run=True)
    profile = protocol.lipo_profile(cell_count=3, charge_current_ma=1500)
    result = dev.start_charging_verified(profile)
    assert result is None
    # dry_run must not have touched the fake's state either
    assert transport.state == protocol.State.COMPLETE


def test_start_charging_verified_stops_immediately_on_charger_reported_error():
    # An explicit ERROR state from the charger (e.g. CELL_NUMBER_INCORRECT
    # - a real protocol-defined code, the charger's own preflight) is
    # authoritative - no retry, stop right away.
    fake = FakeChargerTransport()
    real_transact = FakeChargerTransport.transact

    def transact(self, frame, n=64):
        if frame[2] == protocol.Cmd.GET_CHARGE_INFO:
            self.state = protocol.State.ERROR
            self.error_code = protocol.Error.CELL_NUMBER_INCORRECT
        return real_transact(self, frame, n)

    fake.transact = transact.__get__(fake)
    dev = Device(fake)
    profile = protocol.lipo_profile(cell_count=4, charge_current_ma=1000, hv=True)
    result = dev.start_charging_verified(profile, confirm_delay_s=0, retry_delay_s=0)

    assert result.confirmed is False
    assert result.stopped is True
    assert "CELL_NUMBER_INCORRECT" in result.reason
    assert fake._last_write == protocol.build_stop_charging()


def test_start_charging_verified_retries_once_before_confirming():
    # First post-start read is ambiguous (charger hasn't registered the
    # new state yet) - must not abort on that alone, retry once.
    fake = FakeChargerTransport()
    real_transact = FakeChargerTransport.transact
    reads = {"n": 0}

    def transact(self, frame, n=64):
        if frame[2] == protocol.Cmd.GET_CHARGE_INFO:
            reads["n"] += 1
            self.state = protocol.State.IDLE if reads["n"] == 1 else protocol.State.CHARGING
        return real_transact(self, frame, n)

    fake.transact = transact.__get__(fake)
    dev = Device(fake)
    profile = protocol.lipo_profile(cell_count=3, charge_current_ma=1500)
    result = dev.start_charging_verified(profile, confirm_delay_s=0, retry_delay_s=0)

    assert result.confirmed is True
    assert reads["n"] == 2


def test_start_charging_verified_stops_if_still_unconfirmed_after_retry():
    # Never resolves (no explicit error, but never reaches CHARGING with
    # the right cell count either) - must stop rather than assume success.
    fake = FakeChargerTransport()
    real_transact = FakeChargerTransport.transact

    def transact(self, frame, n=64):
        if frame[2] == protocol.Cmd.GET_CHARGE_INFO:
            self.state = protocol.State.IDLE
        return real_transact(self, frame, n)

    fake.transact = transact.__get__(fake)
    dev = Device(fake)
    profile = protocol.lipo_profile(cell_count=3, charge_current_ma=1500)
    result = dev.start_charging_verified(profile, confirm_delay_s=0, retry_delay_s=0)

    assert result.confirmed is False
    assert result.stopped is True
    assert "did not confirm charging" in result.reason
    assert fake._last_write == protocol.build_stop_charging()


def test_start_charging_verified_stops_on_cell_count_mismatch_even_if_charging():
    # State flips to CHARGING but with the WRONG number of cells - a
    # real mismatch, not just slow state registration. Must still stop.
    fake = FakeChargerTransport()  # 3-cell default
    dev = Device(fake)
    profile = protocol.lipo_profile(cell_count=4, charge_current_ma=1000, hv=True)
    result = dev.start_charging_verified(profile, confirm_delay_s=0, retry_delay_s=0)

    assert result.confirmed is False
    assert result.stopped is True
    assert fake._last_write == protocol.build_stop_charging()
