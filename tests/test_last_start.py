# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for last_start.py: recording/reading what was last commanded, and
recovering from the same stale-unwritable-file scenario transport.py's lock
file already had to handle (see that module's tests for the original bug).
"""

from __future__ import annotations

from b6charger import last_start, protocol


def _profile(**overrides):
    defaults = dict(
        cell_count=3,
        charge_current_ma=1500,
        mode=protocol.ChargingModeLi.BALANCE,
        hv=False,
    )
    defaults.update(overrides)
    return protocol.lipo_profile(**defaults)


def test_read_returns_none_when_nothing_recorded_yet(tmp_path):
    path = str(tmp_path / "last_start.json")
    assert last_start.read(path=path) is None


def test_record_then_read_round_trips(tmp_path):
    path = str(tmp_path / "last_start.json")
    profile = _profile(cell_count=4, hv=True, charge_current_ma=1000)

    last_start.record(profile, pack="hvpack4s", path=path)
    entry = last_start.read(path=path)

    assert entry is not None
    assert entry.battery_type == "LIHV"
    assert entry.cells == 4
    assert entry.current_ma == 1000
    assert entry.mode == "BALANCE"
    assert entry.pack == "hvpack4s"
    assert entry.timestamp > 0


def test_record_without_pack_leaves_pack_none(tmp_path):
    path = str(tmp_path / "last_start.json")
    last_start.record(_profile(), pack=None, path=path)
    entry = last_start.read(path=path)
    assert entry is not None
    assert entry.pack is None


def test_record_overwrites_the_previous_entry(tmp_path):
    path = str(tmp_path / "last_start.json")
    last_start.record(_profile(cell_count=3), pack="first", path=path)
    last_start.record(_profile(cell_count=4, hv=True), pack="second", path=path)

    entry = last_start.read(path=path)
    assert entry.cells == 4
    assert entry.battery_type == "LIHV"
    assert entry.pack == "second"


def test_read_returns_none_for_corrupt_json(tmp_path):
    path = tmp_path / "last_start.json"
    path.write_text("not json")
    assert last_start.read(path=str(path)) is None


def test_read_returns_none_for_json_with_unexpected_shape(tmp_path):
    path = tmp_path / "last_start.json"
    path.write_text('{"unexpected": "shape"}')
    assert last_start.read(path=str(path)) is None


def test_record_recovers_from_a_stale_unwritable_file(tmp_path):
    # Same class of bug as transport.py's lock file (see DRY_RUN.md /
    # test_hidraw_transport.py): a file created by one user/process can
    # be left unwritable to a later one. Since this file holds no
    # meaningful state beyond "the last thing recorded", recovering by
    # removing and recreating it is safe.
    path = tmp_path / "last_start.json"
    path.write_text('{"stale": true}')
    path.chmod(0o000)

    last_start.record(_profile(), pack="recovered", path=str(path))

    path.chmod(0o644)  # restore so the test process can read it back
    entry = last_start.read(path=str(path))
    assert entry is not None
    assert entry.pack == "recovered"


def test_record_swallows_errors_when_recovery_is_impossible(tmp_path):
    # A directory that doesn't exist can't be recovered from by
    # unlinking a file - record() must not raise regardless.
    path = str(tmp_path / "no" / "such" / "dir" / "last_start.json")
    last_start.record(_profile(), pack="whatever", path=path)  # must not raise
