from __future__ import annotations

import pytest

from b6charger import charge_request, packs, protocol
from b6charger.device import Device
from b6charger.transport import FakeChargerTransport


def _registry(tmp_path, monkeypatch) -> packs.PackRegistry:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "packs.toml").write_text("""
        [[pack]]
        name = "matches_fake"
        description = "3S, matches FakeChargerTransport's default 3 cells"
        chemistry = "lipo"
        cells = 3
        capacity_mah = 2200
        default_current_ma = 1100
        """)
    return packs.load_registry()


# --- build_raw_profile(): the manual/raw path ----------------------------


def test_build_raw_profile_builds_a_valid_profile():
    profile = charge_request.build_raw_profile(
        {"chemistry": "lipo", "cells": 3, "current_ma": 1500}
    )
    assert profile.cell_count == 3
    assert profile.charge_current_ma == 1500
    assert profile.battery_type == protocol.BatteryType.LIPO


@pytest.mark.parametrize(
    "body",
    [
        {"chemistry": "lipo", "current_ma": 1500},  # missing cells
        {"chemistry": "lipo", "cells": 3},  # missing current_ma
        {"chemistry": "lipo", "cells": "abc", "current_ma": 1500},  # non-numeric cells
        {"chemistry": "lipo", "cells": [3], "current_ma": 1500},  # wrong JSON type
        {"chemistry": "lipo", "cells": 3, "current_ma": 1500, "mode": "turbo"},  # bad mode
        {"chemistry": "lipo", "cells": 3, "current_ma": 1500, "mode": 7},  # non-string mode
    ],
)
def test_build_raw_profile_rejects_bad_input_without_crashing(body):
    with pytest.raises(charge_request.InvalidStartRequest):
        charge_request.build_raw_profile(body)


# --- build_pack_profile(): the shared pack path used by both cli.py -----
# --- and httpd.py - see charge_request.py's module docstring -------------


def test_build_pack_profile_builds_a_valid_profile(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    dev = Device(FakeChargerTransport())  # 3-cell default
    profile = charge_request.build_pack_profile(dev, registry, "matches_fake")
    assert profile.cell_count == 3
    assert profile.charge_current_ma == 1100  # pack's default_current_ma


def test_build_pack_profile_raises_on_unknown_pack(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    dev = Device(FakeChargerTransport())
    with pytest.raises(packs.PackConfigError):
        charge_request.build_pack_profile(dev, registry, "does_not_exist")


def test_build_pack_profile_raises_on_cell_count_mismatch(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    fake = FakeChargerTransport()
    fake.cells_mv = (3800, 3805)  # 2 cells, pack wants 3
    dev = Device(fake)
    with pytest.raises(packs.PackCellMismatch):
        charge_request.build_pack_profile(dev, registry, "matches_fake")


def test_build_pack_profile_raises_on_current_above_pack_ceiling(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    dev = Device(FakeChargerTransport())
    with pytest.raises(charge_request.InvalidStartRequest):
        charge_request.build_pack_profile(dev, registry, "matches_fake", current_ma=50000)


def test_build_pack_profile_raises_on_discharge_current_above_pack_ceiling(
    tmp_path, monkeypatch
):
    # Previously unchecked entirely on the pack path - a POST could set
    # an unbounded discharge_current_ma even via a registered pack.
    registry = _registry(tmp_path, monkeypatch)
    dev = Device(FakeChargerTransport())
    with pytest.raises(charge_request.InvalidStartRequest):
        charge_request.build_pack_profile(
            dev, registry, "matches_fake", discharge_current_ma=50000
        )


def test_build_pack_profile_raises_on_unknown_mode_without_crashing(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    dev = Device(FakeChargerTransport())
    with pytest.raises(charge_request.InvalidStartRequest):
        charge_request.build_pack_profile(dev, registry, "matches_fake", mode="turbo")


def test_build_pack_profile_raises_on_non_string_mode_without_crashing(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    dev = Device(FakeChargerTransport())
    with pytest.raises(charge_request.InvalidStartRequest):
        charge_request.build_pack_profile(dev, registry, "matches_fake", mode=7)


def test_build_pack_profile_raises_on_non_numeric_current_ma_without_crashing(
    tmp_path, monkeypatch
):
    registry = _registry(tmp_path, monkeypatch)
    dev = Device(FakeChargerTransport())
    with pytest.raises(charge_request.InvalidStartRequest):
        charge_request.build_pack_profile(dev, registry, "matches_fake", current_ma="abc")
