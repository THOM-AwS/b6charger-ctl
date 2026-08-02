# SPDX-License-Identifier: GPL-3.0-or-later
"""HID transport for talking to the charger.

Also provides a fake implementation for testing the whole stack with no
hardware attached.

Lesson from the first real hardware test (2026-08-02, see DRY_RUN.md):
a `stop` sent seconds after a successful one hung forever. Two design
issues contributed, both fixed here:

1. write() and read() used to open and close SEPARATE file descriptors.
   A separate, independently-developed read-only poller for this same
   protocol - the only code confirmed to have reliably talked to this
   hardware before this project existed - always does open -> write ->
   read -> close on ONE fd. transact() below matches that exactly
   instead of assuming split write/read is equivalent.
2. There was no timeout, so if a command genuinely doesn't produce a
   response in some device state (or another process polling the same
   device grabs the reply first), the read blocked forever. A control
   tool where `stop` can hang is a real reliability problem - it now
   raises DeviceTimeout instead.

A per-process-pair flock also serializes transact() calls against
OTHER b6charger-ctl invocations (e.g. two `b6ctl` commands run close
together). It does NOT protect against a separate, independent
exporter process also polling the same device - stop any other
poller/monitoring process before manual write testing (see
DRY_RUN.md).
"""

from __future__ import annotations

import fcntl
import glob
import os
import select
from typing import Protocol

from b6charger import protocol

DEFAULT_TIMEOUT_S = 3.0
# /tmp is shared across all local users, which normally makes a fixed
# path there a symlink-attack risk (CWE-377: another user could
# pre-create a symlink at this path pointing somewhere sensitive).
# Mitigated in HidRawTransport.transact() by opening with O_NOFOLLOW,
# which refuses to follow a symlink rather than silently opening
# through it - kept as a fixed /tmp path (rather than a per-user
# XDG_RUNTIME_DIR) since this tool's target environment is a
# single-purpose device (e.g. a Pi wired to one charger) with one
# operator account, not a shared multi-user host.
DEFAULT_LOCK_PATH = "/tmp/b6charger-ctl.lock"  # nosec B108


class DeviceTimeout(Exception):
    """No response arrived from the device within the transport's timeout."""


class NoChargerFound(Exception):
    """No /dev/hidraw* device answered a GET_CHARGE_INFO probe during discovery."""


class Transport(Protocol):
    """Minimal interface Device needs: send a frame, get the response back."""

    def transact(self, frame: bytes, n: int = 64) -> bytes:
        """Send `frame` and return up to `n` bytes of the device's response."""
        ...


class HidRawTransport:
    """Real hardware, over /dev/hidraw*."""

    def __init__(
        self,
        device_path: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        lock_path: str = DEFAULT_LOCK_PATH,
    ) -> None:
        """Open (or auto-discover, if `device_path` is None) the charger's hidraw device."""
        self._path = device_path or self._discover(timeout_s)
        self._timeout_s = timeout_s
        self._lock_path = lock_path

    @staticmethod
    def _discover(timeout_s: float) -> str:
        """Probe every /dev/hidraw* with GET_CHARGE_INFO, returning the first to answer."""
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
        """Open `path`, write `frame`, wait up to `timeout_s` for a reply, and close.

        This exact open -> write -> select -> read -> close sequence on
        ONE file descriptor is the pattern proven reliable against real
        hardware (see the module docstring) - don't split it across
        separate opens.
        """
        fd = os.open(path, os.O_RDWR)
        try:
            os.write(fd, frame)
            ready, _, _ = select.select([fd], [], [], timeout_s)
            if not ready:
                raise DeviceTimeout(
                    f"no response within {timeout_s}s to frame {frame.hex()} - "
                    "check nothing else (e.g. another exporter or poller process) "
                    "is also talking to the device right now"
                )
            return os.read(fd, n)
        finally:
            os.close(fd)

    def transact(self, frame: bytes, n: int = 64) -> bytes:
        """Send `frame` and return the response, serialized against other b6ctl calls.

        Holds an flock for the duration so two `b6ctl` invocations running
        close together can't interleave their open/write/read/close
        sequences on the same device (see the module docstring for why
        that matters).
        """
        # O_NOFOLLOW refuses to open through a pre-existing symlink at this
        # path rather than silently following it - the actual mitigation
        # for the /tmp shared-path risk noted on DEFAULT_LOCK_PATH above.
        lock_fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o666)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return self._raw_transact(self._path, frame, self._timeout_s, n)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


class FakeChargerTransport:
    """A simulated charger for exercising the whole stack without hardware.

    Tracks both live charge state (cells/current/state) and system
    settings (limits/buzzers), and updates them the way the real
    protocol implies a real charger would in response to writes. NOT a
    claim that this matches real firmware timing or edge-case behaviour
    - it exists so frame encoding, response parsing, and the CLI/HTTP
    plumbing can be tested before a real command is sent. It
    deliberately has no timing/concurrency quirks of its own (those are
    exactly what real hardware testing is for - see DRY_RUN.md).
    """

    def __init__(self) -> None:
        """Start in a plausible at-rest state: a complete, healthy, idle 3S pack."""
        self.state = protocol.State.COMPLETE
        self.cells_mv = (3800, 3805, 3798)
        self.current_ma = 0
        self.pack_voltage_mv = sum(self.cells_mv)
        self.capacity_mah = 0
        self.elapsed_s = 0
        #: Only meaningful (and only encoded) when `state` is ERROR_1/
        #: ERROR_2 - see protocol.py's parse_charge_info() docstring for
        #: why nothing past state+error_code is encoded in that case.
        self.error_code: int | None = None
        self.last_profile: protocol.ChargeProfile | None = None
        self._last_write: bytes = b""

        # system settings - defaults per libb6's Device::getDefaultChargeProfile
        # and the charger's own factory defaults where known.
        self.cycle_time = 1
        self.time_limit_on = False
        self.time_limit_minutes = 200
        self.capacity_limit_on = False
        self.capacity_limit_mah = 5000
        self.key_buzzer = True
        self.system_buzzer = True
        self.low_dc_limit_mv = 11000
        self.temp_limit_c = 50

    def write(self, frame: bytes) -> None:
        """Apply a command frame's effect to the simulated charger's state."""
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
        elif cmd == 0x11:  # SET_SYSTEM - param_id at frame[3], value(s) from frame[5]
            param_id = frame[3]
            if param_id == protocol.SetSystemParam.CYCLE_TIME:
                self.cycle_time = frame[5]
            elif param_id == protocol.SetSystemParam.TIME_LIMIT:
                self.time_limit_on = bool(frame[5])
                self.time_limit_minutes = (frame[6] << 8) | frame[7]
            elif param_id == protocol.SetSystemParam.CAPACITY_LIMIT:
                self.capacity_limit_on = bool(frame[5])
                self.capacity_limit_mah = (frame[6] << 8) | frame[7]
            elif param_id == protocol.SetSystemParam.BUZZERS:
                self.key_buzzer = bool(frame[5])
                self.system_buzzer = bool(frame[6])
            elif param_id == protocol.SetSystemParam.TEMP_LIMIT:
                self.temp_limit_c = frame[5]

    def read(self, n: int = 64) -> bytes:
        """Return the response appropriate to the most recent write()."""
        cmd = self._last_write[2] if self._last_write else protocol.Cmd.GET_CHARGE_INFO
        if cmd == protocol.Cmd.GET_SYS_INFO:
            return self._encode_sys_info()
        if cmd == protocol.Cmd.GET_CHARGE_INFO:
            return self._encode_charge_info()
        # STOP/START/SET_SYSTEM don't return a GET_*_INFO-shaped body on
        # real hardware either (per libb6, the response is read but not
        # parsed) - echo back something read()-safe.
        return bytes(64)

    def transact(self, frame: bytes, n: int = 64) -> bytes:
        """Apply `frame` (via write()) and return the resulting response (via read())."""
        self.write(frame)
        return self.read(n)

    def _encode_charge_info(self) -> bytes:
        """Encode current state as a GET_CHARGE_INFO response frame.

        Mirrors real hardware behaviour (per libb6's Device.cc) when
        `state` is an error state: only the state byte and a 2-byte
        error code at offset 5-6 are populated, everything after that
        stays zeroed - there's no verified real layout to simulate.
        """
        buf = bytearray(64)
        buf[0] = 0x0F
        buf[2] = protocol.Cmd.GET_CHARGE_INFO
        buf[4] = int(self.state)

        if self.state in (protocol.State.ERROR_1, protocol.State.ERROR_2):
            code = self.error_code if self.error_code is not None else 0xFFFF
            buf[5], buf[6] = divmod(code, 256)
            return bytes(buf)

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

    def _encode_sys_info(self) -> bytes:
        """Encode current settings as a GET_SYS_INFO response frame."""
        buf = bytearray(64)
        buf[0] = 0x0F
        buf[2] = protocol.Cmd.GET_SYS_INFO
        buf[4] = self.cycle_time
        buf[5] = int(self.time_limit_on)
        buf[6], buf[7] = divmod(self.time_limit_minutes, 256)
        buf[8] = int(self.capacity_limit_on)
        buf[9], buf[10] = divmod(self.capacity_limit_mah, 256)
        buf[11] = int(self.key_buzzer)
        buf[12] = int(self.system_buzzer)
        buf[13], buf[14] = divmod(self.low_dc_limit_mv, 256)
        # bytes 15-16 skipped per Device::getSysInfo()
        buf[17] = self.temp_limit_c
        buf[18], buf[19] = divmod(self.pack_voltage_mv, 256)
        for i, mv in enumerate(self.cells_mv):
            buf[20 + 2 * i], buf[21 + 2 * i] = divmod(mv, 256)
        return bytes(buf)
