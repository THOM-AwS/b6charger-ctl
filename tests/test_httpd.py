# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for b6charger.httpd: metrics rendering/caching, --listen parsing, and
the --enable-writes gate on the write endpoints (tested against a real
running server, not just the internal handler logic, since the actual HTTP
status codes are the thing that matters here).

Argument-parsing tests for the `serve` subcommand itself live in
test_cli.py alongside every other subcommand - this module only covers
httpd.py's internals (parse_listen_address, MetricsCache, render_metrics,
make_handler), which have no argparse dependency of their own.
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from b6charger.device import Device
from b6charger.httpd import (
    DEFAULT_HOST,
    MetricsCache,
    make_handler,
    parse_listen_address,
    render_metrics,
)
from b6charger.transport import FakeChargerTransport

# --- --listen parsing -----------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0.0.0.0:9111", ("0.0.0.0", 9111)),
        ("127.0.0.1:1234", ("127.0.0.1", 1234)),
        ("localhost:8080", ("localhost", 8080)),
        ("[::1]:9111", ("::1", 9111)),
        ("[fe80::1]:8080", ("fe80::1", 8080)),
    ],
)
def test_parse_listen_address_valid(value, expected):
    assert parse_listen_address(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "no-colon-here",
        "0.0.0.0:not-a-port",
        "0.0.0.0:0",
        "0.0.0.0:70000",
        "[::1]missing-colon-9111",
    ],
)
def test_parse_listen_address_invalid(value):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_listen_address(value)


def test_default_host_is_wide_open_by_design():
    # metrics/status are read-only and meant to be scrapeable - the write
    # endpoints are what --enable-writes exists to gate, independently.
    assert DEFAULT_HOST == "0.0.0.0"


# --- metrics rendering -------------------------------------------------


def test_render_metrics_reports_charger_up_1_and_core_fields(tmp_path, monkeypatch):
    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    device = Device(FakeChargerTransport())
    body = render_metrics(device)
    assert "charger_up 1" in body
    assert "charger_state 3" in body  # FakeChargerTransport starts COMPLETE
    assert "charger_cell_count 3" in body
    assert 'charger_cell_millivolts{cell="1"}' in body
    assert "charger_impedance_milliohms 12" in body


def test_render_metrics_includes_error_code_only_in_error_state(tmp_path, monkeypatch):
    from b6charger import protocol

    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    device = Device(FakeChargerTransport())
    assert "charger_error_code" not in render_metrics(device)

    device._t.state = protocol.State.ERROR
    device._t.error_code = protocol.Error.NO_BATTERY
    body = render_metrics(device)
    assert "charger_error_code 14" in body  # NO_BATTERY = 0x000E = 14


def test_render_metrics_reports_charger_up_0_on_failure(tmp_path, monkeypatch):
    # Isolated from the real DEFAULT_PATH so this test doesn't depend on
    # whatever last_start.json (if any) happens to exist on the host
    # running it - see the charger_last_commanded_* tests below for why
    # that file can be non-empty.
    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))

    class BrokenTransport:
        def transact(self, frame, n=64):
            raise OSError("no such device")

    device = Device(BrokenTransport())
    body = render_metrics(device)
    assert body.strip().endswith("charger_up 0")
    assert "charger_state" not in body


# --- charger_last_commanded_* - what WE told the charger, not a --------
# --- confirmation from it (see last_start.py) ---------------------------


def test_render_metrics_omits_last_commanded_when_nothing_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    device = Device(FakeChargerTransport())
    assert "charger_last_commanded" not in render_metrics(device)


def test_render_metrics_includes_last_commanded_when_recorded(tmp_path, monkeypatch):
    from b6charger import last_start, protocol

    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    profile = protocol.lipo_profile(
        cell_count=4,
        charge_current_ma=1000,
        mode=protocol.ChargingModeLi.BALANCE,
        hv=True,
    )
    last_start.record(profile, pack="hvpack4s")

    device = Device(FakeChargerTransport())
    body = render_metrics(device)
    assert 'battery_type="LIHV"' in body
    assert 'cells="4"' in body
    assert 'pack="hvpack4s"' in body
    assert "charger_last_commanded_current_milliamps 1000" in body


def test_render_metrics_includes_last_commanded_even_when_charger_up_0(tmp_path, monkeypatch):
    from b6charger import last_start, protocol

    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    profile = protocol.lipo_profile(
        cell_count=3,
        charge_current_ma=1500,
        mode=protocol.ChargingModeLi.BALANCE,
    )
    last_start.record(profile, pack=None)

    class BrokenTransport:
        def transact(self, frame, n=64):
            raise OSError("no such device")

    device = Device(BrokenTransport())
    body = render_metrics(device)
    assert "charger_up 0" in body
    assert 'battery_type="LIPO"' in body


# --- charger_sysinfo_* - live voltage/cells from GET_SYS_INFO, unlike ---
# --- GET_CHARGE_INFO's own fields these stay populated while idle -------


def test_render_metrics_includes_sysinfo_pack_voltage_and_cells(tmp_path, monkeypatch):
    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    device = Device(FakeChargerTransport())
    body = render_metrics(device)
    assert "charger_sysinfo_pack_millivolts 11403" in body  # 3800+3805+3798
    assert 'charger_sysinfo_cell_millivolts{cell="1"} 3800' in body
    assert "charger_sysinfo_cell_count 3" in body
    assert "charger_sysinfo_cell_spread_millivolts 7" in body  # 3805-3798


def test_render_metrics_includes_configured_limits(tmp_path, monkeypatch):
    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    device = Device(FakeChargerTransport())
    body = render_metrics(device)
    assert "charger_sysinfo_temp_limit_celsius 50" in body
    assert "charger_sysinfo_low_dc_limit_millivolts 11000" in body
    assert "charger_sysinfo_time_limit_minutes 200" in body
    assert "charger_sysinfo_time_limit_enabled 0" in body
    assert "charger_sysinfo_capacity_limit_mah 5000" in body
    assert "charger_sysinfo_capacity_limit_enabled 0" in body


def test_render_metrics_reflects_enabled_limits(tmp_path, monkeypatch):
    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    fake = FakeChargerTransport()
    fake.time_limit_on = True
    fake.capacity_limit_on = True
    device = Device(fake)
    body = render_metrics(device)
    assert "charger_sysinfo_time_limit_enabled 1" in body
    assert "charger_sysinfo_capacity_limit_enabled 1" in body


def test_render_metrics_includes_sysinfo_even_when_charger_up_0(tmp_path, monkeypatch):
    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))

    class SysInfoOnlyTransport:
        def __init__(self):
            self._fake = FakeChargerTransport()

        def transact(self, frame, n=64):
            if frame[2] == 0x55:  # GET_CHARGE_INFO - simulate unreachable
                raise OSError("no such device")
            return self._fake.transact(frame, n)

    device = Device(SysInfoOnlyTransport())
    body = render_metrics(device)
    assert "charger_up 0" in body
    assert "charger_sysinfo_pack_millivolts 11403" in body


def test_render_metrics_omits_sysinfo_cells_when_sysinfo_itself_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))

    class ChargeInfoOnlyTransport:
        def __init__(self):
            self._fake = FakeChargerTransport()

        def transact(self, frame, n=64):
            if frame[2] == 0x5A:  # GET_SYS_INFO - simulate unreachable
                raise OSError("no such device")
            return self._fake.transact(frame, n)

    device = Device(ChargeInfoOnlyTransport())
    body = render_metrics(device)
    assert "charger_up 1" in body  # GET_CHARGE_INFO still worked
    assert "charger_sysinfo" not in body


# --- MetricsCache --------------------------------------------------


def test_metrics_cache_reuses_body_within_window(monkeypatch):
    calls = []

    def render():
        calls.append(1)
        return f"call {len(calls)}"

    times = iter([100.0, 100.1, 100.2])
    monkeypatch.setattr("b6charger.httpd.time.monotonic", lambda: next(times))

    cache = MetricsCache(render, cache_s=5.0)
    first = cache.get()
    second = cache.get()
    assert first == second == "call 1"
    assert len(calls) == 1


def test_metrics_cache_refreshes_after_expiry(monkeypatch):
    calls = []

    def render():
        calls.append(1)
        return f"call {len(calls)}"

    # MetricsCache.get() calls time.monotonic() twice per refresh (once to
    # check staleness, once to stamp the new cache time) - two refreshes
    # needs four values.
    times = iter([100.0, 100.0, 200.0, 200.0])
    monkeypatch.setattr("b6charger.httpd.time.monotonic", lambda: next(times))

    cache = MetricsCache(render, cache_s=5.0)
    first = cache.get()
    second = cache.get()
    assert first == "call 1"
    assert second == "call 2"
    assert len(calls) == 2


# --- end-to-end server: the --enable-writes gate ------------------------


@pytest.fixture
def running_server():
    """Start a real `serve` HTTP server (--fake charger) on an OS-assigned port.

    Yields (base_url, enable_writes_setter) - tests choose enable_writes
    per-case by constructing their own server, so this fixture is a
    factory-style helper via a small local function instead.
    """

    servers = []

    def start(
        enable_writes: bool, device: Device | None = None, write_token: str | None = None
    ):
        device = device or Device(FakeChargerTransport())
        cache = MetricsCache(lambda: render_metrics(device), cache_s=0.0)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(device, cache, enable_writes, write_token)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        port = server.server_address[1]
        return f"http://127.0.0.1:{port}"

    yield start

    for server in servers:
        server.shutdown()
        server.server_close()


def test_metrics_and_status_work_without_enable_writes(running_server):
    base_url = running_server(enable_writes=False)
    with urllib.request.urlopen(f"{base_url}/metrics") as resp:
        assert resp.status == 200
        assert "charger_up 1" in resp.read().decode()
    with urllib.request.urlopen(f"{base_url}/status") as resp:
        assert resp.status == 200


def test_start_and_stop_return_403_without_enable_writes(running_server):
    base_url = running_server(enable_writes=False)
    body = json.dumps({"chemistry": "lipo", "cells": 3, "current_ma": 1500}).encode()

    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 403

    req = urllib.request.Request(f"{base_url}/stop", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 403


def test_start_and_stop_work_with_enable_writes(running_server, monkeypatch):
    monkeypatch.setattr("b6charger.device.time.sleep", lambda _s: None)
    base_url = running_server(enable_writes=True)
    body = json.dumps({"chemistry": "lipo", "cells": 3, "current_ma": 1500}).encode()

    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["ok"] is True

    req = urllib.request.Request(f"{base_url}/stop", data=b"{}", method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200


# --- POST /start with {"pack": "name"} - the HTTP equivalent of ---------
# --- `b6ctl start --pack`, including the live cell-count cross-check ----


def _write_test_registry(tmp_path, monkeypatch) -> None:
    """Same throwaway registry shape as tests/test_cli.py's helper: one
    pack matching FakeChargerTransport's 3-cell default, one deliberately
    mismatched at 4S."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "packs.toml").write_text("""
        [[pack]]
        name = "matches_fake"
        description = "3S, matches FakeChargerTransport's default 3 cells"
        chemistry = "lipo"
        cells = 3
        capacity_mah = 2200
        default_current_ma = 1100

        [[pack]]
        name = "wrong_cell_count"
        description = "4S - deliberately does not match the fake's 3 cells"
        chemistry = "lihv"
        cells = 4
        capacity_mah = 1500
        default_current_ma = 750
        """)


def test_post_start_with_matching_pack_succeeds(running_server, tmp_path, monkeypatch):
    monkeypatch.setattr("b6charger.device.time.sleep", lambda _s: None)
    _write_test_registry(tmp_path, monkeypatch)
    base_url = running_server(enable_writes=True)

    body = json.dumps({"pack": "matches_fake"}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["ok"] is True


def test_post_start_with_pack_succeeds_while_idle_using_sysinfo_cells(
    running_server, tmp_path, monkeypatch
):
    # Same fix as cli.py's equivalent test: GET_CHARGE_INFO's cells_mv
    # is always empty while IDLE on real hardware (see DRY_RUN.md), so
    # this must pass using GET_SYS_INFO's cells, not GET_CHARGE_INFO's.
    from b6charger import protocol

    monkeypatch.setattr("b6charger.device.time.sleep", lambda _s: None)
    _write_test_registry(tmp_path, monkeypatch)
    fake = FakeChargerTransport()
    fake.state = protocol.State.IDLE
    base_url = running_server(enable_writes=True, device=Device(fake))

    body = json.dumps({"pack": "matches_fake"}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["ok"] is True


def test_post_start_with_mismatched_pack_returns_409(running_server, tmp_path, monkeypatch):
    _write_test_registry(tmp_path, monkeypatch)
    base_url = running_server(enable_writes=True)

    body = json.dumps({"pack": "wrong_cell_count"}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 409
    error = json.loads(exc_info.value.read())["error"]
    assert "wrong_cell_count" in error
    assert "4S" in error
    assert "detects 3 real cell" in error


def test_post_start_with_unknown_pack_returns_400(running_server, tmp_path, monkeypatch):
    _write_test_registry(tmp_path, monkeypatch)
    base_url = running_server(enable_writes=True)

    body = json.dumps({"pack": "does_not_exist"}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400


def test_post_start_with_pack_current_override_above_max_returns_400(
    running_server, tmp_path, monkeypatch
):
    _write_test_registry(tmp_path, monkeypatch)
    base_url = running_server(enable_writes=True)

    body = json.dumps({"pack": "matches_fake", "current_ma": 50000}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400
    assert "exceeds" in json.loads(exc_info.value.read())["error"]


def test_post_start_with_pack_is_also_blocked_without_enable_writes(
    running_server, tmp_path, monkeypatch
):
    # the --enable-writes gate applies before the pack lookup even runs.
    _write_test_registry(tmp_path, monkeypatch)
    base_url = running_server(enable_writes=False)

    body = json.dumps({"pack": "matches_fake"}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 403


def test_post_start_records_last_start_with_pack_name(running_server, tmp_path, monkeypatch):
    from b6charger import last_start

    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    monkeypatch.setattr("b6charger.device.time.sleep", lambda _s: None)
    _write_test_registry(tmp_path, monkeypatch)
    base_url = running_server(enable_writes=True)

    body = json.dumps({"pack": "matches_fake"}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200

    entry = last_start.read()
    assert entry is not None
    assert entry.pack == "matches_fake"
    assert entry.battery_type == "LIPO"


def test_post_start_with_raw_body_records_last_start_with_no_pack(
    running_server, tmp_path, monkeypatch
):
    from b6charger import last_start

    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    monkeypatch.setattr("b6charger.device.time.sleep", lambda _s: None)
    base_url = running_server(enable_writes=True)

    # cells=3 matches FakeChargerTransport's default 3-cell setup, so
    # the post-start confirmation (checking live cells against what was
    # requested) actually passes.
    body = json.dumps({"chemistry": "lihv", "cells": 3, "current_ma": 1000}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200

    entry = last_start.read()
    assert entry is not None
    assert entry.pack is None
    assert entry.battery_type == "LIHV"


# --- malformed request bodies must 400, never crash the handler ---------


def test_post_start_with_non_dict_body_returns_400(running_server):
    base_url = running_server(enable_writes=True)
    for bad_body in (b"[1, 2, 3]", b'"just a string"', b"42", b"null"):
        req = urllib.request.Request(f"{base_url}/start", data=bad_body, method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400


def test_post_start_with_pack_and_unknown_mode_returns_400_not_crash(
    running_server, tmp_path, monkeypatch
):
    # Regression: _build_profile_from_pack used to parse `mode` with no
    # try/except at all, unlike the raw-body path - an invalid mode name
    # raised an uncaught KeyError that crashed the request handler.
    _write_test_registry(tmp_path, monkeypatch)
    base_url = running_server(enable_writes=True)

    body = json.dumps({"pack": "matches_fake", "mode": "turbo"}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400
    assert "turbo" in json.loads(exc_info.value.read())["error"]


def test_post_start_with_pack_and_non_string_mode_returns_400_not_crash(
    running_server, tmp_path, monkeypatch
):
    # Same bug, different trigger: a non-string mode (valid JSON, e.g. a
    # number) raised an uncaught AttributeError on `.upper()`.
    _write_test_registry(tmp_path, monkeypatch)
    base_url = running_server(enable_writes=True)

    body = json.dumps({"pack": "matches_fake", "mode": 7}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400


def test_post_start_with_pack_and_non_numeric_current_ma_returns_400_not_crash(
    running_server, tmp_path, monkeypatch
):
    # Same bug class again: a non-numeric current_ma raised an uncaught
    # ValueError from the pack path's bare int() call.
    _write_test_registry(tmp_path, monkeypatch)
    base_url = running_server(enable_writes=True)

    body = json.dumps({"pack": "matches_fake", "current_ma": "not-a-number"}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400


# --- device I/O failures during a write must 502, never crash the -------
# --- handler - do_GET already did this; do_POST didn't -------------------


def _raising_device() -> Device:
    class RaisingTransport:
        def transact(self, frame, n=64):
            raise OSError("device disconnected")

    return Device(RaisingTransport())


def test_post_stop_returns_502_when_device_fails(running_server):
    base_url = running_server(enable_writes=True, device=_raising_device())
    req = urllib.request.Request(f"{base_url}/stop", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 502


def test_post_start_returns_502_when_start_charging_verified_fails(running_server):
    base_url = running_server(enable_writes=True, device=_raising_device())
    body = json.dumps({"chemistry": "lipo", "cells": 3, "current_ma": 1500}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 502


def test_post_start_with_pack_returns_502_when_get_sys_info_fails(
    running_server, tmp_path, monkeypatch
):
    _write_test_registry(tmp_path, monkeypatch)
    base_url = running_server(enable_writes=True, device=_raising_device())
    body = json.dumps({"pack": "matches_fake"}).encode()
    req = urllib.request.Request(f"{base_url}/start", data=body, method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 502


# --- B6CTL_WRITE_TOKEN: optional bearer-token auth on write endpoints ---


def test_write_endpoints_reject_missing_token_when_configured(running_server):
    base_url = running_server(enable_writes=True, write_token="s3cret")
    req = urllib.request.Request(f"{base_url}/stop", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 401


def test_write_endpoints_reject_wrong_token_when_configured(running_server):
    base_url = running_server(enable_writes=True, write_token="s3cret")
    req = urllib.request.Request(
        f"{base_url}/stop",
        data=b"{}",
        method="POST",
        headers={"Authorization": "Bearer wrong"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 401


def test_write_endpoints_accept_correct_token_when_configured(running_server):
    base_url = running_server(enable_writes=True, write_token="s3cret")
    req = urllib.request.Request(
        f"{base_url}/stop",
        data=b"{}",
        method="POST",
        headers={"Authorization": "Bearer s3cret"},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200


def test_write_endpoints_ignore_token_header_when_no_token_configured(running_server):
    # No write_token set at all - old enable_writes-only behavior must
    # still work, token header or not.
    base_url = running_server(enable_writes=True, write_token=None)
    req = urllib.request.Request(f"{base_url}/stop", data=b"{}", method="POST")
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200


def test_write_endpoints_check_enable_writes_before_token(running_server):
    # The --enable-writes gate is primary - checked before the token,
    # so a disabled daemon still 403s regardless of any token supplied.
    base_url = running_server(enable_writes=False, write_token="s3cret")
    req = urllib.request.Request(
        f"{base_url}/stop",
        data=b"{}",
        method="POST",
        headers={"Authorization": "Bearer s3cret"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 403
