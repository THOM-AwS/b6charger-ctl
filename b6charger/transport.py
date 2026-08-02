# SPDX-License-Identifier: GPL-3.0-or-later
"""HID transport for talking to the charger, plus a fake implementation
for testing the whole stack with no hardware attached (i.e. right now,
while a real charge is in progress on charger-pi and shouldn't be
disturbed).
"""

from __future__ import annotations

import glob
import os
from typing import Protocol

from b6charger import protocol


class Transport(Protocol):
    def write(self, frame: bytes) -> None: ...

    def read(self, n: int = 64) -> bytes: ...


class NoChargerFound(Exception):
    pass


class HidRawTransport:
    """Real hardware, over /dev/hidraw*. Same discovery approach as
    ht-infra's b6_poller.py: try every hidraw device, keep the first one
    that answers a GET_CHARGE_INFO-shaped response."""

    def __init__(self, device_path: str | None = None) -> None:
        self._path = device_path or self._discover()

    @staticmethod
    def _discover() -> str:
        for dev in sorted(glob.glob("/dev/hidraw*")):
            try:
                fd = os.open(dev, os.O_RDWR)
            except OSError:
                continue
            try:
                os.write(fd, protocol.build_get_charge_info())
                resp = os.read(fd, 64)
            except OSError:
                continue
            finally:
                os.close(fd)
            if len(resp) >= 3 and resp[0] == 0x0F and resp[2] == protocol.Cmd.GET_CHARGE_INFO:
                return dev
        raise NoChargerFound("no /dev/hidraw* device answered GET_CHARGE_INFO")

    def write(self, frame: bytes) -> None:
        fd = os.open(self._path, os.O_RDWR)
        try:
            os.write(fd, frame)
        finally:
            os.close(fd)

    def read(self, n: int = 64) -> bytes:
        fd = os.open(self._path, os.O_RDWR)
        try:
            return os.read(fd, n)
        finally:
            os.close(fd)


class FakeChargerTransport:
    """Simulates enough charger behaviour to exercise the whole
    protocol/device/CLI stack without hardware. NOT a claim that this
    matches real firmware timing or edge-case behaviour - it exists so
    frame encoding, response parsing, and the CLI/HTTP plumbing can be
    tested before the first real command is ever sent.
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
