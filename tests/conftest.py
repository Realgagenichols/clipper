"""Shared fixtures for clipper tests."""

from __future__ import annotations

import threading
from collections import deque
from typing import Any
from unittest.mock import MagicMock

import pytest
import serial

# ---------------------------------------------------------------------------
# FakeSerial — drop-in replacement for serial.Serial
# ---------------------------------------------------------------------------


class FakeSerial:
    """Minimal fake that mimics the serial.Serial subset clipper.flipper uses.

    Supports:
    - write(bytes) — accumulates bytes in ``written``
    - read_until(expected, size=None) — returns queued response chunks
    - read(n) — reads n bytes from the queue
    - close()
    - is_open, in_waiting, port properties
    """

    def __init__(self, port: str = "/dev/fake", **kwargs: Any) -> None:
        self.port = port
        self.is_open = True
        self._responses: deque[bytes] = deque()
        self._disconnected = False
        # When set, read_until() raises OSError(errno) — models a rebooted /
        # re-enumerated CDC port surfacing ENXIO (6, "Device not configured")
        # rather than serial.SerialException.
        self._oserror_errno: int | None = None
        self._lock = threading.Lock()
        # Public list of raw bytes written by the code under test
        self.written: list[bytes] = []
        # Running buffer for read_until
        self._buf = bytearray()
        # Trailing-byte buffer drained by read() (used by the post-operation
        # quiesce in exclusive_serial). Empty by default: these tests model a
        # device that leaves no in-flight residue, so the quiesce is a no-op
        # and never consumes the scripted read_until() responses.
        self._residue = bytearray()

    # --- write ---

    def write(self, data: bytes) -> int:
        if self._disconnected:
            raise serial.SerialException("fake disconnect: write failed")
        with self._lock:
            self.written.append(data)
        return len(data)

    # --- queue responses ---

    def queue_response(self, data: bytes) -> None:
        """Queue bytes that read_until / read will return."""
        with self._lock:
            self._responses.append(data)

    def simulate_disconnect(self) -> None:
        """Subsequent reads and writes will raise SerialException."""
        self._disconnected = True

    # --- read ---

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        if self._disconnected:
            raise serial.SerialException("fake disconnect: read_until failed")
        if self._oserror_errno is not None:
            raise OSError(self._oserror_errno, "Device not configured")
        with self._lock:
            if self._responses:
                chunk = self._responses.popleft()
                self._buf.extend(chunk)
            # Check if expected terminator is now in buffer
            idx = self._buf.find(expected)
            if idx != -1:
                end = idx + len(expected)
                result = bytes(self._buf[:end])
                self._buf = self._buf[end:]
                return result
            # Nothing yet — return empty (caller will poll)
            return b""

    def read(self, n: int = 1) -> bytes:
        # read() serves only the residue buffer (trailing bytes), kept separate
        # from the read_until() response queue so the post-operation quiesce
        # drain never eats a scripted command response. Tests that want to model
        # trailing bytes can append to ``_residue``.
        if self._disconnected:
            raise serial.SerialException("fake disconnect: read failed")
        with self._lock:
            if not self._residue:
                return b""
            result = bytes(self._residue[:n])
            del self._residue[:n]
            return result

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return sum(len(r) for r in self._responses) + len(self._buf)

    def close(self) -> None:
        self.is_open = False

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        """No-op — test fixtures pre-queue responses per command, no stale state."""
        pass


# ---------------------------------------------------------------------------
# FakeListPortInfo — mimics a pyserial ListPortInfo object
# ---------------------------------------------------------------------------


def make_port_info(
    device: str,
    vid: int | None = None,
    pid: int | None = None,
) -> Any:
    """Return a mock object shaped like serial.tools.list_ports ListPortInfo."""
    p = MagicMock()
    p.device = device
    p.vid = vid
    p.pid = pid
    p.description = f"Fake port {device}"
    return p


# ---------------------------------------------------------------------------
# FakeFlipperHarness — test helper that wires FakeSerial into clipper.flipper
# ---------------------------------------------------------------------------


class FakeFlipperHarness:
    """Orchestrates a FakeSerial as a fake Flipper Zero.

    Usage in tests::

        harness.expect("device info", "Hardware Name: ClipperDev\\n>: ")
        harness.expect("power info", "Battery Charge: 88%\\n>: ")
        conn = FlipperConnection(port_factory=harness.port_factory)
        await conn.start()
        assert conn.connected
    """

    FLIPPER_VID = 0x0483
    FLIPPER_PID = 0x5740

    def __init__(self) -> None:
        self._fake_serial: FakeSerial | None = None
        self._responses: dict[str, bytes] = {}
        self._port: str | None = "/dev/tty.usbmodemflip_Test1"
        self._port_list: list[Any] = []
        self._open_raises: Exception | None = None
        self._oserror_on_open: int | None = None

    # --- configuration API ---

    def add_port(
        self,
        device: str | None = None,
        vid: int | None = None,
        pid: int | None = None,
    ) -> str:
        """Add a fake port to the list that comports() returns."""
        if device is None:
            device = self._port or "/dev/tty.usbmodemflip_Test1"
        if vid is None:
            vid = self.FLIPPER_VID
        if pid is None:
            pid = self.FLIPPER_PID
        self._port_list.append(make_port_info(device, vid, pid))
        self._port = device
        return device

    def remove_ports(self) -> None:
        """Clear the port list (simulate unplug)."""
        self._port_list.clear()

    def restore_ports(self) -> None:
        """Re-add a default port (simulate plug-in after _port was set)."""
        if self._port:
            self.add_port(self._port)

    def expect(self, cmd: str, response: str | bytes) -> None:
        """Queue a scripted response for a given command string."""
        if isinstance(response, str):
            response = response.encode("utf-8")
        self._responses[cmd] = response

    def set_open_raises(self, exc: Exception) -> None:
        """Make serial.Serial(...) raise *exc* when next opened."""
        self._open_raises = exc

    def simulate_disconnect(self) -> None:
        """Trigger a read/write failure on the current FakeSerial."""
        if self._fake_serial is not None:
            self._fake_serial.simulate_disconnect()

    def simulate_oserror(self, errno_: int = 6) -> None:
        """Make the current FakeSerial's read_until raise OSError(errno_).

        Models a rebooted/re-enumerated Flipper CDC port surfacing ENXIO
        (6, "Device not configured") instead of serial.SerialException.
        """
        if self._fake_serial is not None:
            self._fake_serial._oserror_errno = errno_

    def fail_reads_on_open(self, errno_: int = 6) -> None:
        """Make EVERY subsequently-opened FakeSerial raise OSError(errno_) on read.

        Models a dead/lingering device node that opens but never responds — used
        to verify _try_connect does not declare a false connection.
        """
        self._oserror_on_open = errno_

    # --- port factory injected into FlipperConnection ---

    def port_factory(self) -> str | None:
        if not self._port_list:
            return None
        return self._port_list[0].device

    # --- serial.Serial replacement ---

    def serial_factory(self, port: str, **kwargs: Any) -> FakeSerial:
        """Replacement for serial.Serial constructor."""
        if self._open_raises is not None:
            exc = self._open_raises
            self._open_raises = None
            raise exc

        fs = FakeSerial(port=port)
        if self._oserror_on_open is not None:
            fs._oserror_errno = self._oserror_on_open
        self._fake_serial = fs
        # Pre-queue responses for every registered command
        for _cmd, resp in self._responses.items():
            fs.queue_response(resp)
        return fs

    # --- introspection ---

    @property
    def fake_serial(self) -> FakeSerial | None:
        return self._fake_serial

    def all_written(self) -> list[str]:
        """All lines written to the fake serial, decoded as UTF-8."""
        if self._fake_serial is None:
            return []
        result = []
        for raw in self._fake_serial.written:
            result.append(raw.decode("utf-8", errors="replace").strip())
        return result


# ---------------------------------------------------------------------------
# fake_flipper fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_flipper(monkeypatch: pytest.MonkeyPatch) -> FakeFlipperHarness:
    """Pytest fixture that monkey-patches serial so clipper.flipper never touches hardware.

    Patches:
    - ``serial.Serial`` → ``harness.serial_factory``
    - ``serial.tools.list_ports.comports`` → returns ``harness._port_list``
    - ``FlipperConnection._drain_welcome_banner`` → no-op (the welcome-banner
      drain is a real-hardware concern; in tests we control the response queue
      explicitly per command).
    """
    harness = FakeFlipperHarness()

    # Patch comports to return harness._port_list
    import serial.tools.list_ports as lp

    monkeypatch.setattr(lp, "comports", lambda: harness._port_list)

    # Patch serial.Serial with harness.serial_factory
    monkeypatch.setattr(serial, "Serial", harness.serial_factory)

    # Skip the welcome-banner drain in tests: the fake serial doesn't simulate
    # a real Flipper's startup banner, and the drain's read_until would consume
    # the first queued response intended for `device info`.
    from clipper import flipper as _flipper_mod

    async def _no_drain(self: Any) -> None:
        return None

    monkeypatch.setattr(_flipper_mod.FlipperConnection, "_drain_welcome_banner", _no_drain)

    # Replace activity_indicator with a no-op CM in tests. Scan handlers
    # (NFC, RFID, IR rx) wrap their core call in this CM to drive the
    # on-device LED — but each LED command is a serial round-trip the
    # fake_flipper fixture can't satisfy without explicit `harness.expect`
    # for every test. Stubbing it out keeps the tests focused on the action
    # logic, not the visual feedback.
    from contextlib import asynccontextmanager

    from clipper.hardware import feedback as _feedback_mod

    @asynccontextmanager
    async def _noop_indicator(*_args: Any, **_kwargs: Any) -> Any:
        yield

    monkeypatch.setattr(_feedback_mod, "activity_indicator", _noop_indicator)
    # The scan modules imported the symbol by name; patch each rebind.
    from clipper.hardware import ir as _ir_mod
    from clipper.hardware import nfc as _nfc_mod
    from clipper.hardware import rfid as _rfid_mod

    monkeypatch.setattr(_nfc_mod, "activity_indicator", _noop_indicator)
    monkeypatch.setattr(_rfid_mod, "activity_indicator", _noop_indicator)
    # ir.py imports activity_indicator lazily inside the handler, so it
    # reads through the patched module reference — no extra patch needed.
    _ = _ir_mod  # silence unused-import linter

    return harness


# ---------------------------------------------------------------------------
# Legacy factory fixture (kept from project scaffold)
# ---------------------------------------------------------------------------


@pytest.fixture
def make_sample_data():
    """Factory fixture -- customize per project.

    Factory fixtures let tests construct domain objects without
    depending on real files, APIs, or databases. Each test specifies
    only the fields it cares about; everything else gets sensible defaults.
    """

    def _factory(**kwargs: Any) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            # Add project-specific defaults here
        }
        defaults.update(kwargs)
        return defaults

    return _factory


# ---------------------------------------------------------------------------
# Event-loop management
# ---------------------------------------------------------------------------

# pytest-asyncio 0.24 with asyncio_mode=auto creates a fresh loop per test.
# No extra fixtures needed here.
