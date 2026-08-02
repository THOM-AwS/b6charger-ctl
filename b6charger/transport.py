# SPDX-License-Identifier: GPL-3.0-or-later
"""HID transport for talking to the charger, plus a fake implementation
for testing the whole stack with no hardware attached.

Lesson from the first real hardware test (2026-08-02, see DRY_RUN.md):
a `stop` sent seconds after a successful one hung forever. Two design
issues contributed, both fixed here:

1. write() and read() used to open and close SEPARATE file descriptors.
   ht-infra's b6_poller.py - the only code that's ever reliably talked
   to this hardware - always does open -> write -> read -> close on ONE
   fd. transact() below matches that exactly instead of assuming
   split write/read is equivalent.
2. There was no timeout, so if a command genuinely doesn't produce a
   response in some device state (or another process, e.g. b6_poller's
   own 30s poll, grabs the reply first), the read blocked forever. A
   control tool where `stop` can hang is a real reliability problem -
   it now raises DeviceTimeout instead.

A per-process-pair flock also serializes transact() calls against
OTHER b6charger-ctl invocations (e.g. two `b6ctl` commands run close
together). It does NOT protect against ht-infra's b6_poller.py, which
is a separate codebase that doesn't know about this lock - stop that
service before any manual write testing (see DRY_RUN.md).
"""

from __future__ import annotations

import fcntl
import glob
import os
import select
from typing import Protocol

from b6charger import protocol

DEFAULT_TIMEOUT_S = 3.0
DEFAULT_LOCK_PATH = "/tmp/b6charger-ctl.lock"


class DeviceTimeout(Exception):
    pass


class NoChargerFound(Exception):
    pass


class Transport(Protocol):
    def transact(self, frame: bytes, n: int = 64) -> bytes: ...


class HidRawTransport:
    """Real hardware, over /dev/hidraw*."""

    def __init__(
        self,
        device_path: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        lock_path: str = DEFAULT_LOCK_PATH,
    ) -> None:
        self._path = device_path or self._discover(timeout_s)
        self._timeout_s = timeout_s
        self._lock_path = lock_path

    @staticmethod
    def _discover(timeout_s: float) -> str:
        for dev in sorted(glob.glob("/dev/hidraw*")):
            try:
                resp = HidRawTransport._raw_transact(
                    dev, protocol.build_get_charge_info(), timeout_s
                )
            except (OSError, DeviceTimeout):
                continue
            if len(resp) >= 3 and resp[0] == 0x0F and resp[2] == protocol.Cmd.GET_CHARGE_INFO:
                return dev
        raise NoChargerFound("no /dev/hidraw* device answered GET_CHARGE_INFO")

    @staticmethod
    def _raw_transact(path: str, frame: bytes, timeout_s: float, n: int = 64) -> bytes:
        fd = os.open(path, os.O_RDWR)
        try:
            os.write(fd, frame)
            ready, _, _ = select.select([fd], [], [], timeout_s)
            if not ready:
                raise DeviceTimeout(
                    f"no response within {timeout_s}s to frame {frame.hex()} - "
                    "check nothing else (e.g. ht-infra's b6_poller.py) is also "
                    "talking to the device right now"
                )
            return os.read(fd, n)
        finally:
            os.close(fd)

    def transact(self, frame: bytes, n: int = 64) -> bytes:
        lock_fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return self._raw_transact(self._path, frame, self._timeout_s, n)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


class FakeChargerTransport:
    """Simulates enough charger behaviour to exercise the whole
    protocol/device/CLI stack without hardware. NOT a claim that this
    matches real firmware timing or edge-case behaviour - it exists so
    frame encoding, response parsing, and the CLI/HTTP plumbing can be
    tested before a real command is sent, and it deliberately has no
    timing/concurrency quirks of its own (those are exactly what real
    hardware testing is for - see DRY_RUN.md).
    """

    def __init__(self) -> None:
        self.state = protocol.State.COMPLETE
        self.cells_mv = (3800, 3805, 3798)
        self.current_ma = 0
        self.pack_voltage_mv = sum(self.cells_mv)
        self.capacity_mah = 0
        self.elapsed_s = 0
        self.last_profile: protocol.ChargeProfile | None = None
        self._last_write: bytes = b""

    def write(self, frame: bytes) -> None:
        self._last_write = frame
        cmd = frame[2]
        if cmd == protocol.Cmd.STOP_CHARGING:
            self.state = protocol.State.COMPLETE
            self.current_ma = 0
        elif cmd == protocol.START_CHARGING_CMD:
            self.state = protocol.State.CHARGING
            # payload layout: [battery_type, cell_count, mode, charge_current_hi,
            # charge_current_lo, ...] starting at frame[4] - see protocol.py
            self.current_ma = (frame[7] << 8) | frame[8]

    def read(self, n: int = 64) -> bytes:
        cmd = self._last_write[2] if self._last_write else protocol.Cmd.GET_CHARGE_INFO
        if cmd in (protocol.Cmd.STOP_CHARGING, protocol.START_CHARGING_CMD):
            # These commands don't return a GET_CHARGE_INFO-shaped body on
            # real hardware either (per libb6, the response is read but
            # not parsed) - echo back something read()-safe.
            return bytes(64)
        return self._encode_charge_info()

    def transact(self, frame: bytes, n: int = 64) -> bytes:
        self.write(frame)
        return self.read(n)

    def _encode_charge_info(self) -> bytes:
        buf = bytearray(64)
        buf[0] = 0x0F
        buf[2] = protocol.Cmd.GET_CHARGE_INFO
        buf[4] = int(self.state)
        buf[5], buf[6] = divmod(self.capacity_mah, 256)
        buf[7], buf[8] = divmod(self.elapsed_s, 256)
        buf[9], buf[10] = divmod(self.pack_voltage_mv, 256)
        buf[11], buf[12] = divmod(self.current_ma, 256)
        buf[13] = 0  # temp_ext_c
        buf[14] = 25  # temp_int_c
        buf[15], buf[16] = divmod(12, 256)  # impedance_mohm
        for i, mv in enumerate(self.cells_mv):
            buf[17 + 2 * i], buf[18 + 2 * i] = divmod(mv, 256)
        return bytes(buf)
