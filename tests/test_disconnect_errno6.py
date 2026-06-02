"""Regression tests: a rebooted/re-enumerated Flipper surfaces a bare OSError
(errno 6, ENXIO "Device not configured"), not serial.SerialException.

The disconnect detection used to catch only serial.SerialException, so an
OSError from a yanked/rebooted CDC port slipped past _mark_disconnected():
``_connected`` stayed True, flipper_state kept reporting connected, and the
reconnect loop (which only engages once _connected is False) never fired — the
server wedged on a dead file descriptor until the process was killed.

These tests assert that an OSError on read or write — on both the CLI and RPC
paths — marks the connection disconnected and raises FlipperDisconnected, and
that the reconnect path then re-opens a fresh (re-enumerated) port.
"""

from __future__ import annotations

import pytest

import clipper.actions  # noqa: F401 — fully initialize the hardware registry first
from clipper.flipper import FlipperConnection, FlipperDisconnected

ENXIO = 6  # "Device not configured"


class _ErrSerial:
    """Fake serial that raises OSError(ENXIO) on a chosen operation."""

    def __init__(self, raise_on: str) -> None:
        # raise_on ∈ {"read_until", "write", "read"}
        self.raise_on = raise_on
        self.is_open = True

    def _boom(self) -> None:
        raise OSError(ENXIO, "Device not configured")

    def reset_input_buffer(self) -> None:
        pass

    def flush(self) -> None:
        if self.raise_on == "write":
            self._boom()

    def close(self) -> None:
        self.is_open = False

    def write(self, data: bytes) -> int:
        if self.raise_on == "write":
            self._boom()
        return len(data)

    def read(self, n: int = 1) -> bytes:
        if self.raise_on == "read":
            self._boom()
        return b""

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        if self.raise_on == "read_until":
            self._boom()
        return b""


def _connected_flipper(serial: _ErrSerial) -> FlipperConnection:
    flipper = FlipperConnection(port_factory=lambda: "/dev/fake", reconnect_interval=60)
    flipper._connected = True
    flipper._serial = serial  # type: ignore[assignment]
    return flipper


# ---------------------------------------------------------------------------
# CLI path (_send_locked)
# ---------------------------------------------------------------------------


async def test_cli_read_oserror_marks_disconnected():
    """OSError(6) from read_until → FlipperDisconnected and _connected False."""
    flipper = _connected_flipper(_ErrSerial("read_until"))
    with pytest.raises(FlipperDisconnected):
        await flipper.send_command("device info")
    assert flipper.connected is False


async def test_cli_write_oserror_marks_disconnected():
    """OSError(6) from write → FlipperDisconnected and _connected False."""
    flipper = _connected_flipper(_ErrSerial("write"))
    with pytest.raises(FlipperDisconnected):
        await flipper.send_command("device info")
    assert flipper.connected is False


# ---------------------------------------------------------------------------
# RPC path (rpc_request)
# ---------------------------------------------------------------------------


async def test_rpc_write_oserror_marks_disconnected():
    """OSError(6) during the RPC session → FlipperDisconnected, _connected False."""
    flipper = _connected_flipper(_ErrSerial("write"))
    with pytest.raises(FlipperDisconnected):
        await flipper.rpc_request(
            request_field_num=20,
            request_payload=b"",
            response_field_num=21,
            timeout=2.0,
        )
    assert flipper.connected is False


async def test_rpc_read_oserror_marks_disconnected():
    """OSError(6) from read during the RPC session → FlipperDisconnected."""
    flipper = _connected_flipper(_ErrSerial("read"))
    with pytest.raises(FlipperDisconnected):
        await flipper.rpc_request(
            request_field_num=20,
            request_payload=b"",
            response_field_num=21,
            timeout=2.0,
        )
    assert flipper.connected is False


# ---------------------------------------------------------------------------
# Recovery: reconnect loop re-opens the re-enumerated port
# ---------------------------------------------------------------------------

_DEVICE_RESP = "device info\r\nHardware Name: TestFlipper\r\nFirmware Version: 1.0.0\r\n>: "
_POWER_RESP = "power info\r\nBattery Charge: 88%\r\n>: "


async def test_reconnect_after_oserror_reopens_reenumerated_port(fake_flipper):
    """After an OSError disconnect, _try_connect re-opens a fresh device."""
    fake_flipper.add_port()
    fake_flipper.expect("device info", _DEVICE_RESP)
    fake_flipper.expect("info power", _POWER_RESP)

    flipper = FlipperConnection(port_factory=fake_flipper.port_factory, reconnect_interval=60)
    await flipper.start()
    assert flipper.connected is True

    # Device reboots: the current FD now raises OSError(6) on read.
    fake_flipper.simulate_oserror(ENXIO)
    with pytest.raises(FlipperDisconnected):
        await flipper.send_command("storage info /ext")
    assert flipper.connected is False

    # The reconnect loop's tick re-opens the re-enumerated port (a fresh
    # FakeSerial with no error flag) and the handshake succeeds.
    reconnected = await flipper._try_connect()
    assert reconnected is True
    assert flipper.connected is True

    await flipper.stop()
