from __future__ import annotations

import json

from b6charger import cli


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

    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--fake",
            "start",
            "--chemistry",
            "lihv",
            "--cells",
            "4",
            "--current-ma",
            "1000",
            "--yes",
        ]
    )
    args.func(args)
    out = capsys.readouterr().out
    assert "sent." in out
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
    _write_test_registry(tmp_path, monkeypatch)
    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "start", "--pack", "matches_fake", "--yes"])
    args.func(args)
    out = capsys.readouterr().out
    assert "sent." in out
    assert "cells        = 3" in out
    assert "charge_current   = 1100mA" in out  # the pack's default_current_ma


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

    _write_test_registry(tmp_path, monkeypatch)
    fake = FakeChargerTransport()
    fake.state = protocol.State.IDLE
    monkeypatch.setattr("b6charger.cli.FakeChargerTransport", lambda: fake)

    parser = cli.build_parser()
    args = parser.parse_args(["--fake", "start", "--pack", "matches_fake", "--yes"])
    args.func(args)
    assert "sent." in capsys.readouterr().out


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
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "--fake",
            "start",
            "--chemistry",
            "lihv",
            "--cells",
            "4",
            "--current-ma",
            "1000",
            "--yes",
        ]
    )
    args.func(args)

    entry = last_start.read()
    assert entry is not None
    assert entry.battery_type == "LIHV"
    assert entry.cells == 4
    assert entry.current_ma == 1000
    assert entry.pack is None


def test_start_pack_yes_records_the_pack_name(tmp_path, monkeypatch, capsys):
    from b6charger import last_start

    monkeypatch.setattr("b6charger.last_start.DEFAULT_PATH", str(tmp_path / "last_start.json"))
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
