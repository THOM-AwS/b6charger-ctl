# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for b6httpd's --listen/--host/--port argument handling."""

from __future__ import annotations

import argparse

import pytest

from b6charger.httpd import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    build_parser,
    parse_listen_address,
    resolve_host_port,
)


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


def test_resolve_host_port_defaults_when_nothing_given():
    args = build_parser().parse_args(["--fake"])
    assert resolve_host_port(args) == (DEFAULT_HOST, DEFAULT_PORT)


def test_resolve_host_port_with_separate_host_and_port():
    args = build_parser().parse_args(["--fake", "--host", "0.0.0.0", "--port", "1234"])
    assert resolve_host_port(args) == ("0.0.0.0", 1234)


def test_resolve_host_port_with_host_only_keeps_default_port():
    args = build_parser().parse_args(["--fake", "--host", "0.0.0.0"])
    assert resolve_host_port(args) == ("0.0.0.0", DEFAULT_PORT)


def test_resolve_host_port_with_listen():
    args = build_parser().parse_args(["--fake", "--listen", "0.0.0.0:1234"])
    assert resolve_host_port(args) == ("0.0.0.0", 1234)


def test_resolve_host_port_with_listen_and_host_is_rejected(capsys):
    args = build_parser().parse_args(
        ["--fake", "--listen", "0.0.0.0:1234", "--host", "127.0.0.1"]
    )
    with pytest.raises(SystemExit) as exc_info:
        resolve_host_port(args)
    assert exc_info.value.code == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_resolve_host_port_with_listen_and_port_is_rejected(capsys):
    args = build_parser().parse_args(["--fake", "--listen", "0.0.0.0:1234", "--port", "9999"])
    with pytest.raises(SystemExit):
        resolve_host_port(args)
    assert "cannot be combined" in capsys.readouterr().err
