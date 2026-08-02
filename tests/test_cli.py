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
