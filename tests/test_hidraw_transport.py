# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for HidRawTransport's timeout/locking behaviour - the exact
gap that caused a real hang during hardware testing (see DRY_RUN.md).
Uses a regular temp file standing in for /dev/hidraw*, with
select.select monkeypatched to simulate "device never responds" -
select() on a real file is always ready regardless of content, so this
is the only way to exercise the timeout branch without real hardware.
"""

from __future__ import annotations

import pytest

from b6charger.transport import DeviceTimeout, HidRawTransport


def test_transact_raises_device_timeout_when_no_response(tmp_path, monkeypatch):
    device_path = tmp_path / "hidraw_fake"
    device_path.touch()
    monkeypatch.setattr("b6charger.transport.select.select", lambda *a, **k: ([], [], []))

    transport = HidRawTransport(
        device_path=str(device_path),
        timeout_s=0.01,
        lock_path=str(tmp_path / "lock"),
    )
    with pytest.raises(DeviceTimeout):
        transport.transact(b"\x0f\x03\x55\x00\x55\xff\xff")


def test_transact_returns_without_raising_when_select_says_ready(tmp_path, monkeypatch):
    device_path = tmp_path / "hidraw_fake"
    device_path.touch()
    monkeypatch.setattr(
        "b6charger.transport.select.select", lambda rlist, w, x, timeout: (rlist, [], [])
    )

    transport = HidRawTransport(
        device_path=str(device_path),
        timeout_s=1.0,
        lock_path=str(tmp_path / "lock"),
    )
    # doesn't raise - the actual bytes are meaningless here (a regular
    # file isn't a real HID device), this only proves the non-timeout
    # path completes and releases the lock cleanly.
    transport.transact(b"\x0f\x03\x55\x00\x55\xff\xff")


def test_lock_is_released_so_sequential_transacts_both_succeed(tmp_path, monkeypatch):
    device_path = tmp_path / "hidraw_fake"
    device_path.touch()
    monkeypatch.setattr(
        "b6charger.transport.select.select", lambda rlist, w, x, timeout: (rlist, [], [])
    )

    transport = HidRawTransport(
        device_path=str(device_path),
        timeout_s=1.0,
        lock_path=str(tmp_path / "lock"),
    )
    transport.transact(b"\x0f\x03\x55\x00\x55\xff\xff")
    transport.transact(b"\x0f\x03\x55\x00\x55\xff\xff")  # would hang if the lock leaked


def test_transact_recovers_from_a_stale_unwritable_lock_file(tmp_path, monkeypatch):
    # Confirmed in production 2026-08-02: a lock file created by one user
    # (mode masked by their umask down to 0664) blocked a later process
    # running as a different user (root) with PermissionError on open.
    # Since the lock file holds no meaningful state, recovering by
    # removing and recreating it is safe.
    device_path = tmp_path / "hidraw_fake"
    device_path.touch()
    monkeypatch.setattr(
        "b6charger.transport.select.select", lambda rlist, w, x, timeout: (rlist, [], [])
    )

    lock_path = tmp_path / "lock"
    lock_path.touch()
    lock_path.chmod(0o000)  # simulates "some other user's file, unwritable to us"

    transport = HidRawTransport(
        device_path=str(device_path),
        timeout_s=1.0,
        lock_path=str(lock_path),
    )
    # doesn't raise - recovers by unlinking and recreating the lock file.
    transport.transact(b"\x0f\x03\x55\x00\x55\xff\xff")


def test_transact_raises_a_clear_error_when_lock_recovery_also_fails(tmp_path, monkeypatch):
    # If the unlink-and-recreate recovery above ITSELF fails (e.g. a
    # concurrent process, or a permission problem on the directory),
    # this must raise a clear, actionable OSError - not silently
    # continue without a working lock (this file's whole purpose is
    # serializing concurrent hardware access, unlike last_start.py's
    # best-effort file) and not a raw, contextless errno from whichever
    # of unlink/reopen happened to fail.
    device_path = tmp_path / "hidraw_fake"
    device_path.touch()
    monkeypatch.setattr(
        "b6charger.transport.select.select", lambda rlist, w, x, timeout: (rlist, [], [])
    )

    lock_path = tmp_path / "lock"
    lock_path.touch()
    lock_path.chmod(0o000)  # forces the first open() to fail, triggering recovery

    def raise_on_unlink(_path):
        raise OSError("simulated: can't remove the lock file either")

    monkeypatch.setattr("b6charger.transport.os.unlink", raise_on_unlink)

    transport = HidRawTransport(
        device_path=str(device_path),
        timeout_s=1.0,
        lock_path=str(lock_path),
    )
    with pytest.raises(OSError, match="could not recover a stale"):
        transport.transact(b"\x0f\x03\x55\x00\x55\xff\xff")
