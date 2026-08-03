from __future__ import annotations

import json

from b6charger import cli
from b6charger.transport import FakeChargerTransport


def test_fake_and_device_together_is_rejected(capsys):
    # Previously --fake silently won with no warning - a leftover
    # --fake from an earlier dry run, or a leftover --device from a
    # real-hardware test, would silently do the wrong thing.
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "--device", "/dev/hidraw0", "status"])
    try:
        args.func(args)
        raised = False
    except SystemExit as e:
        raised = True
        assert e.code == 1
    assert raised
    assert "mutually exclusive" in capsys.readouterr().err


def test_status_json_against_fake(capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "status", "--json"])
    args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert out["state_name"] == "COMPLETE"
    assert "cells_mv" in out


def test_sysinfo_json_against_fake(capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "sysinfo", "--json"])
    args.func(args)
    out = json.loads(capsys.readouterr().out)
    assert "temp_limit_c" in out
    assert "cells_mv" in out


def test_start_dry_run_does_not_prompt(capsys, monkeypatch):
    def fail_input(_prompt):  # dry-run must never call input()
        raise AssertionError("input() called during --dry-run")

    monkeypatch.setattr("builtins.input", fail_input)

    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--fake",
            "start",
            "--chemistry",
            "lipo",
            "--cells",
            "3",
            "--current-ma",
            "1500",
            "--dry-run",
        ]
    )
    args.func(args)
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()
    assert "LIPO" in out


def test_start_yes_skips_prompt(capsys, monkeypatch):
    def fail_input(_prompt):
        raise AssertionError("input() called despite --yes")

    monkeypatch.setattr("builtins.input", fail_input)
    monkeypatch.setattr("b6charger.device.time.sleep", lambda _s: None)

    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--fake",
            "start",
            "--chemistry",
            "lihv",
            # matches FakeChargerTransport's default 3-cell setup, so the
            # post-start confirmation (which checks the live cell count
            # against what was requested) actually passes here.
            "--cells",
            "3",
            "--current-ma",
            "1000",
            "--yes",
        ]
    )
    args.func(args)
    out = capsys.readouterr().out
    assert "sent and confirmed" in out
    assert "LIHV" in out


def test_start_declined_at_prompt_exits_nonzero(capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--fake",
            "start",
            "--chemistry",
            "lipo",
            "--cells",
            "3",
            "--current-ma",
            "1500",
        ]
    )
    try:
        args.func(args)
        raised = False
    except SystemExit as e:
        raised = True
        assert e.code == 1
    assert raised
    assert "aborted" in capsys.readouterr().out


def test_stop_dry_run(capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "stop", "--dry-run"])
    args.func(args)
    assert "dry-run" in capsys.readouterr().out.lower()


def _write_test_registry(tmp_path, monkeypatch) -> None:
    """Point the CLI's default registry lookup at a throwaway packs.toml
    with one 3S pack (matching FakeChargerTransport's 3-cell default) and
    one 4S pack (deliberately mismatched, for the refusal test)."""
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


def test_packs_list_shows_configured_packs(tmp_path, monkeypatch, capsys):
    _write_test_registry(tmp_path, monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["packs", "list"])
    args.func(args)
    out = capsys.readouterr().out
    assert "matches_fake" in out
    assert "wrong_cell_count" in out


def test_packs_show_prints_full_pack_detail(tmp_path, monkeypatch, capsys):
    _write_test_registry(tmp_path, monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["packs", "show", "matches_fake"])
    args.func(args)
    out = capsys.readouterr().out
    assert "cells:              3S" in out
    assert "chemistry:          lipo" in out


def test_start_with_pack_using_default_current_succeeds(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("b6charger.device.time.sleep", lambda _s: None)
    _write_test_registry(tmp_path, monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "start", "--pack", "matches_fake", "--yes"])
    args.func(args)
    out = capsys.readouterr().out
    assert "sent and confirmed" in out
    assert "cells        = 3" in out
    assert "charge_current   = 1100mA" in out  # the pack's default_current_ma


def test_start_with_pack_warns_when_using_example_registry(tmp_path, monkeypatch, capsys):
    # Previously only `packs list` surfaced this - the path where it
    # matters most (about to actually command a charge) said nothing.
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
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "start", "--pack", "example_pack", "--yes"])
    try:
        args.func(args)
    except SystemExit:
        pass  # the 1S/3S cell-count mismatch is expected and irrelevant here
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "example/placeholder" in err


def test_start_with_pack_succeeds_while_idle_using_sysinfo_cells(
    tmp_path, monkeypatch, capsys
):
    # Confirmed 2026-08-02 against real hardware (see DRY_RUN.md):
    # GET_CHARGE_INFO's cells_mv is always empty while the charger is
    # IDLE - exactly the state it's always in right before a charge
    # starts. The safety check must read GET_SYS_INFO instead, which
    # stays live regardless of state. This simulates that split: state
    # is IDLE (so charge_info's own cells would read empty) while the
    # fake's cells_mv (which sys_info encodes unconditionally) is real.
    from b6charger import protocol
    from b6charger.transport import FakeChargerTransport

    monkeypatch.setattr("b6charger.device.time.sleep", lambda _s: None)
    _write_test_registry(tmp_path, monkeypatch)
    fake = FakeChargerTransport()
    fake.state = protocol.State.IDLE
    monkeypatch.setattr("b6charger.cli.FakeChargerTransport", lambda: fake)

    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "start", "--pack", "matches_fake", "--yes"])
    args.func(args)
    assert "sent and confirmed" in capsys.readouterr().out


def test_start_with_pack_mismatched_cell_count_refuses(tmp_path, monkeypatch, capsys):
    _write_test_registry(tmp_path, monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "start", "--pack", "wrong_cell_count", "--yes"])
    try:
        args.func(args)
        raised = False
    except SystemExit as e:
        raised = True
        assert e.code == 1
    assert raised
    err = capsys.readouterr().err
    assert "wrong_cell_count" in err
    assert "4S" in err
    assert "detects 3 real cell" in err


def test_start_pack_current_override_above_max_is_rejected(tmp_path, monkeypatch, capsys):
    _write_test_registry(tmp_path, monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--fake", "start", "--pack", "matches_fake", "--current-ma", "50000", "--yes"]
    )
    try:
        args.func(args)
        raised = False
    except SystemExit as e:
        raised = True
        assert e.code == 1
    assert raised
    assert "exceeds" in capsys.readouterr().err


def test_start_combining_pack_and_chemistry_is_rejected(capsys):
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--fake", "start", "--pack", "whatever", "--chemistry", "lipo", "--yes"]
    )
    try:
        args.func(args)
        raised = False
    except SystemExit as e:
        raised = True
        assert e.code == 1
    assert raised
    assert "cannot be combined" in capsys.readouterr().err


def test_start_with_neither_pack_nor_manual_flags_is_rejected(capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "start", "--yes"])
    try:
        args.func(args)
        raised = False
    except SystemExit as e:
        raised = True
        assert e.code == 1
    assert raised
    assert "specify either --pack" in capsys.readouterr().err


def test_start_manual_without_current_ma_is_rejected(capsys):
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--fake", "start", "--chemistry", "lipo", "--cells", "3", "--yes"]
    )
    try:
        args.func(args)
        raised = False
    except SystemExit as e:
        raised = True
        assert e.code == 1
    assert raised
    assert "--current-ma is required" in capsys.readouterr().err


def test_start_pack_dry_run_still_runs_the_cell_count_check(tmp_path, monkeypatch, capsys):
    # --dry-run must not skip the safety check - it's a live read either
    # way, and the whole point of --dry-run is telling the truth about
    # what a real run would do.
    _write_test_registry(tmp_path, monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "start", "--pack", "wrong_cell_count", "--dry-run"])
    try:
        args.func(args)
        raised = False
    except SystemExit as e:
        raised = True
        assert e.code == 1
    assert raised
    assert "detects 3 real cell" in capsys.readouterr().err


# --- `serve` subcommand: argument parsing / host+port resolution -------
# httpd.py's own tests cover render_metrics/MetricsCache/make_handler -
# this is just the argparse wiring, folded in here alongside every
# other subcommand's parsing tests since `serve` is no longer a
# separate binary with its own parser.


def test_serve_uses_global_device_and_fake_flags():
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "serve"])
    assert args.fake is True
    assert args.func is cli._cmd_serve


def test_serve_enable_writes_defaults_to_off():
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "serve"])
    assert args.enable_writes is False


def test_resolve_serve_host_port_defaults_when_nothing_given():
    from b6charger import httpd

    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "serve"])
    assert cli._resolve_serve_host_port(args) == (httpd.DEFAULT_HOST, httpd.DEFAULT_PORT)


def test_resolve_serve_host_port_with_separate_host_and_port():
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "serve", "--host", "127.0.0.1", "--port", "1234"])
    assert cli._resolve_serve_host_port(args) == ("127.0.0.1", 1234)


def test_resolve_serve_host_port_with_host_only_keeps_default_port():
    from b6charger import httpd

    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "serve", "--host", "127.0.0.1"])
    assert cli._resolve_serve_host_port(args) == ("127.0.0.1", httpd.DEFAULT_PORT)


def test_resolve_serve_host_port_with_listen():
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "serve", "--listen", "0.0.0.0:1234"])
    assert cli._resolve_serve_host_port(args) == ("0.0.0.0", 1234)


def test_resolve_serve_host_port_with_listen_and_host_is_rejected(capsys):
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--fake", "serve", "--listen", "0.0.0.0:1234", "--host", "127.0.0.1"]
    )
    try:
        cli._resolve_serve_host_port(args)
        raised = False
    except SystemExit as e:
        raised = True
        assert e.code == 2
    assert raised
    assert "cannot be combined" in capsys.readouterr().err


def test_resolve_serve_host_port_with_listen_and_port_is_rejected(capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "serve", "--listen", "0.0.0.0:1234", "--port", "9999"])
    try:
        cli._resolve_serve_host_port(args)
        raised = False
    except SystemExit:
        raised = True
    assert raised
    assert "cannot be combined" in capsys.readouterr().err


# --- `start` records what it sent, for dashboards - see last_start.py --


def test_start_yes_records_last_start(tmp_path, monkeypatch, capsys):
    from b6charger import last_start

    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    monkeypatch.setattr("b6charger.device.time.sleep", lambda _s: None)
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--fake",
            "start",
            "--chemistry",
            "lihv",
            # matches FakeChargerTransport's default 3-cell setup, so the
            # post-start confirmation actually passes.
            "--cells",
            "3",
            "--current-ma",
            "1000",
            "--yes",
        ]
    )
    args.func(args)

    entry = last_start.read()
    assert entry is not None
    assert entry.battery_type == "LIHV"
    assert entry.cells == 3
    assert entry.current_ma == 1000
    assert entry.pack is None


def test_start_pack_yes_records_the_pack_name(tmp_path, monkeypatch, capsys):
    from b6charger import last_start

    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    monkeypatch.setattr("b6charger.device.time.sleep", lambda _s: None)
    _write_test_registry(tmp_path, monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "start", "--pack", "matches_fake", "--yes"])
    args.func(args)

    entry = last_start.read()
    assert entry is not None
    assert entry.pack == "matches_fake"


def test_start_dry_run_does_not_record_last_start(tmp_path, monkeypatch, capsys):
    from b6charger import last_start

    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--fake",
            "start",
            "--chemistry",
            "lipo",
            "--cells",
            "3",
            "--current-ma",
            "1500",
            "--dry-run",
        ]
    )
    args.func(args)

    assert last_start.read() is None


# --- main(): translates exceptions into a friendly message, not a -------
# --- raw traceback - previously untested, since every other test here --
# --- calls args.func(args) directly rather than going through main() ----


def test_main_reports_oserror_as_friendly_message_not_traceback(monkeypatch, capsys):
    # HidRawTransport does raw os.open/os.write/os.read - a mid-command
    # hardware failure (permission error, device unplugged) is a bare
    # OSError, not a DeviceTimeout, and main() used to only catch the
    # latter.
    def raise_oserror(self, frame, n=64):
        raise OSError("no such device")

    monkeypatch.setattr("b6charger.transport.FakeChargerTransport.transact", raise_oserror)

    try:
        cli.main(["--fake", "status"])
        raised = False
    except SystemExit as e:
        raised = True
        assert e.code == 1
    assert raised
    err = capsys.readouterr().err
    assert "error:" in err
    assert "no such device" in err


def test_main_reports_device_timeout_as_friendly_message(monkeypatch, capsys):
    from b6charger.transport import DeviceTimeout

    def raise_timeout(self, frame, n=64):
        raise DeviceTimeout("no response within 3.0s")

    monkeypatch.setattr("b6charger.transport.FakeChargerTransport.transact", raise_timeout)

    try:
        cli.main(["--fake", "status"])
        raised = False
    except SystemExit as e:
        raised = True
        assert e.code == 1
    assert raised
    assert "no response within 3.0s" in capsys.readouterr().err


def test_start_prompt_aborts_cleanly_on_eof(capsys, monkeypatch):
    # Piped/non-interactive stdin (e.g. `b6ctl start ... < /dev/null`)
    # makes input() raise EOFError rather than returning - must abort
    # cleanly, not crash with a raw traceback.
    def raise_eof(_prompt):
        raise EOFError()

    monkeypatch.setattr("builtins.input", raise_eof)

    parser = cli.build_parser()
    args = parser.parse_args(
        ["--fake", "start", "--chemistry", "lipo", "--cells", "3", "--current-ma", "1500"]
    )
    try:
        args.func(args)
        raised = False
    except SystemExit as e:
        raised = True
        assert e.code == 1
    assert raised
    assert "no input available" in capsys.readouterr().err


# --- set-limits --key-buzzer/--system-buzzer: previously implemented ---
# --- end-to-end (device.py/protocol.py) but unreachable from the CLI ----


def _fake_dev_for(monkeypatch):
    """Patch cli._make_transport to hand back one fixed FakeChargerTransport.

    _cmd_set_limits (like every subcommand) builds its own transport via
    _make_transport(args), which normally constructs a fresh
    FakeChargerTransport per call under --fake - this intercepts that so
    the test can inspect the fake's state afterwards.
    """
    fake = FakeChargerTransport()
    monkeypatch.setattr(cli, "_make_transport", lambda args: fake)
    return fake


def test_set_limits_sets_both_buzzers_explicitly(monkeypatch, capsys):
    fake = _fake_dev_for(monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "set-limits", "--key-buzzer", "--no-system-buzzer"])
    args.func(args)
    assert "sent" in capsys.readouterr().out
    assert fake.key_buzzer is True
    assert fake.system_buzzer is False


def test_set_limits_only_key_buzzer_preserves_current_system_buzzer(monkeypatch, capsys):
    fake = _fake_dev_for(monkeypatch)
    assert fake.system_buzzer is True  # FakeChargerTransport's own default
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "set-limits", "--no-key-buzzer"])
    args.func(args)
    capsys.readouterr()
    assert fake.key_buzzer is False
    assert fake.system_buzzer is True  # untouched - not silently flipped


def test_set_limits_with_neither_buzzer_flag_does_not_touch_buzzers(monkeypatch, capsys):
    fake = _fake_dev_for(monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "set-limits", "--cycle-time", "5"])
    assert args.key_buzzer is None
    assert args.system_buzzer is None
    args.func(args)
    capsys.readouterr()
    assert fake.key_buzzer is True  # FakeChargerTransport's own default, untouched
    assert fake.system_buzzer is True
