# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the pack registry - both the loading/validation logic and,
importantly, that the actual packs.example.toml shipped in the repo is
itself valid (a broken example file would be a bad first experience for
anyone cloning this).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from b6charger.packs import EXAMPLE_CONFIG_PATH, PackConfigError, load_registry


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "packs.toml"
    p.write_text(content)
    return p


def test_shipped_example_file_is_itself_valid():
    # The actual file a fresh clone falls back to - if this is broken,
    # every new user's first command fails.
    registry = load_registry(EXAMPLE_CONFIG_PATH)
    assert registry.is_example is True
    pack = registry.get("example_pack")
    assert pack.chemistry == "lipo"
    assert pack.cells == 1


def test_load_registry_falls_back_to_example_when_no_packs_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "packs.example.toml").write_text("""
        [[pack]]
        name = "example_pack"
        description = "placeholder"
        chemistry = "lipo"
        cells = 1
        capacity_mah = 1000
        default_current_ma = 500
        """)
    registry = load_registry()
    assert registry.is_example is True
    assert registry.source_path.name == "packs.example.toml"


def test_load_registry_prefers_real_packs_toml_over_example(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        """
        [[pack]]
        name = "my_pack"
        description = "a real one"
        chemistry = "lipo"
        cells = 3
        capacity_mah = 2200
        default_current_ma = 1100
        """,
    )
    registry = load_registry()
    assert registry.is_example is False
    assert registry.get("my_pack").cells == 3


def test_valid_pack_with_explicit_max_current(tmp_path):
    path = _write(
        tmp_path,
        """
        [[pack]]
        name = "gnb1500"
        description = "GNB 1500mAh 4S HV"
        chemistry = "lihv"
        cells = 4
        capacity_mah = 1500
        default_current_ma = 1000
        max_current_ma = 1500
        """,
    )
    pack = load_registry(path).get("gnb1500")
    assert pack.is_hv is True
    assert pack.max_current_ma == 1500


def test_max_current_defaults_to_one_c_when_omitted(tmp_path):
    path = _write(
        tmp_path,
        """
        [[pack]]
        name = "p"
        description = "d"
        chemistry = "lipo"
        cells = 3
        capacity_mah = 2200
        default_current_ma = 1100
        """,
    )
    assert load_registry(path).get("p").max_current_ma == 2200


@pytest.mark.parametrize(
    "bad_field_toml,expected_message_fragment",
    [
        (
            """
            [[pack]]
            name = "p"
            description = "d"
            chemistry = "nicd"
            cells = 3
            capacity_mah = 2200
            default_current_ma = 1100
            """,
            "chemistry must be one of",
        ),
        (
            """
            [[pack]]
            name = "p"
            description = "d"
            chemistry = "lipo"
            cells = 0
            capacity_mah = 2200
            default_current_ma = 1100
            """,
            "cells must be 1-16",
        ),
        (
            """
            [[pack]]
            name = "p"
            description = "d"
            chemistry = "lipo"
            cells = 3
            capacity_mah = -100
            default_current_ma = 1100
            """,
            "capacity_mah must be positive",
        ),
        (
            """
            [[pack]]
            name = "p"
            description = "d"
            chemistry = "lipo"
            cells = 3
            capacity_mah = 2200
            default_current_ma = 1100
            max_current_ma = 5000
            """,
            "exceeds 1C",
        ),
        (
            """
            [[pack]]
            name = "p"
            description = "d"
            chemistry = "lipo"
            cells = 3
            capacity_mah = 2200
            default_current_ma = 9999
            """,
            "must be > 0 and <= max_current_ma",
        ),
        (
            """
            [[pack]]
            name = "p"
            chemistry = "lipo"
            cells = 3
            capacity_mah = 2200
            default_current_ma = 1100
            """,
            "missing field.s.: description",
        ),
    ],
)
def test_invalid_pack_fields_are_rejected_with_clear_message(
    tmp_path, bad_field_toml, expected_message_fragment
):
    path = _write(tmp_path, bad_field_toml)
    with pytest.raises(PackConfigError, match=expected_message_fragment):
        load_registry(path)


def test_duplicate_pack_names_are_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
        [[pack]]
        name = "p"
        description = "one"
        chemistry = "lipo"
        cells = 3
        capacity_mah = 2200
        default_current_ma = 1100

        [[pack]]
        name = "p"
        description = "two"
        chemistry = "lipo"
        cells = 4
        capacity_mah = 1500
        default_current_ma = 750
        """,
    )
    with pytest.raises(PackConfigError, match="two packs both named"):
        load_registry(path)


def test_pack_missing_name_field_is_rejected(tmp_path):
    path = _write(
        tmp_path,
        """
        [[pack]]
        description = "no name field"
        chemistry = "lipo"
        cells = 3
        capacity_mah = 2200
        default_current_ma = 1100
        """,
    )
    with pytest.raises(PackConfigError, match="no 'name' field"):
        load_registry(path)


def test_empty_file_is_rejected(tmp_path):
    path = _write(tmp_path, "")
    with pytest.raises(PackConfigError, match="no \\[\\[pack\\]\\] entries"):
        load_registry(path)


def test_malformed_toml_is_rejected(tmp_path):
    path = _write(tmp_path, "this is not [ valid toml")
    with pytest.raises(PackConfigError, match="not valid TOML"):
        load_registry(path)


def test_unknown_pack_name_lists_known_packs(tmp_path):
    path = _write(
        tmp_path,
        """
        [[pack]]
        name = "zeee2200"
        description = "d"
        chemistry = "lipo"
        cells = 3
        capacity_mah = 2200
        default_current_ma = 1100
        """,
    )
    registry = load_registry(path)
    with pytest.raises(PackConfigError, match="zeee2200"):
        registry.get("nonexistent")


def test_missing_registry_file_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # neither packs.toml nor packs.example.toml exist in this empty tmp_path
    with pytest.raises(PackConfigError, match="no pack registry found"):
        load_registry()


def test_registry_is_iterable():
    registry = load_registry(EXAMPLE_CONFIG_PATH)
    names = [p.name for p in registry]
    assert names == ["example_pack"]
