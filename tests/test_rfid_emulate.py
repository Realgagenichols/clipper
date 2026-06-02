"""Tests for 125 kHz RFID emulation (R11 flipper_rfid_emulate).

TDD: written before the implementation in clipper/hardware/rfid.py.

R11 flipper_rfid_emulate:
- EMISSIVE (gated + audited): EmissionBlocked when the gate is off, no command
  written.
- Builds `rfid emulate <key_type> <key_data>` and runs it via
  run_bounded_command (run-until-ETX). The conftest FakeSerial can't model
  run-until-ETX, so we use a local write-gated bounded fake (see
  tests/test_bounded_command.py / test_subghz_rx_replay.py — L4).
- duration_seconds defaults to 10 and is capped at 60.
- If the device prints `Unknown protocol:` / `Available protocols:` the
  emulation never started → ActionRuntimeError surfacing that message.
"""

from __future__ import annotations

import threading

import pytest

import clipper.flipper as flipper_mod
from clipper.actions import ActionParamError, ActionRuntimeError, EmissionBlocked, get
from clipper.flipper import FlipperConnection

ETX = b"\x03"
PROMPT = b"\r\n>: "


@pytest.fixture(autouse=True)
def _fast_quiesce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the post-byte quiet window so drain-based tests stay fast."""
    monkeypatch.setattr(flipper_mod, "_QUIESCE_QUIET", 0.01)
    monkeypatch.setattr(flipper_mod, "_QUIESCE_MAX", 0.1)


class _BoundedFakeSerial:
    """Write-gated fake modeling a run-until-ETX command (`rfid emulate ...`)."""

    def __init__(self, cmd: str, output: bytes) -> None:
        self._cmd = cmd
        self._output = output
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.is_open = True
        self.written: list[bytes] = []
        self.stop_sent = False

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.append(data)
            text = data.decode("utf-8", "replace")
            if text.strip() == self._cmd:
                self._buf.extend(self._cmd.encode() + b"\r\n")
                self._buf.extend(self._output)
            if ETX in data:
                self.stop_sent = True
                self._buf.extend(PROMPT)
        return len(data)

    def read(self, n: int = 1) -> bytes:
        with self._lock:
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        with self._lock:
            idx = self._buf.find(expected)
            if idx == -1:
                out = bytes(self._buf)
                self._buf.clear()
                return out
            end = idx + len(expected)
            out = bytes(self._buf[:end])
            del self._buf[:end]
            return out

    def reset_input_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)


def _make_bounded_flipper(fake: _BoundedFakeSerial) -> FlipperConnection:
    flipper = FlipperConnection(port_factory=lambda: "/dev/fake", reconnect_interval=60)
    flipper._connected = True
    flipper._serial = fake  # type: ignore[assignment]
    return flipper


# ---------------------------------------------------------------------------
# Command format + success path (gate ON)
# ---------------------------------------------------------------------------


async def test_rfid_emulate_command_format(monkeypatch):
    """`rfid emulate <key_type> <key_data>` then the ETX stop byte."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    cmd = "rfid emulate EM4100 0123456789"
    fake = _BoundedFakeSerial(cmd, b"")
    flipper = _make_bounded_flipper(fake)

    result = await get("flipper_rfid_emulate").invoke(
        flipper,
        {"key_type": "EM4100", "key_data": "0123456789", "duration_seconds": 0.05},
        transport="test",
    )

    joined = b"".join(fake.written)
    assert b"rfid emulate EM4100 0123456789\r" in joined
    assert fake.stop_sent, "ETX stop byte was never sent"
    assert result["emulated"] is True
    assert result["key_type"] == "EM4100"
    assert result["key_data"] == "0123456789"
    assert result["duration_s"] == 0.05
    assert "raw" in result


async def test_rfid_emulate_blocked_without_allow_emit(monkeypatch):
    """Emissive: EmissionBlocked when the gate is off, no command written."""
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)
    monkeypatch.delenv("CLIPPER_SAFETY", raising=False)
    cmd = "rfid emulate EM4100 0123456789"
    fake = _BoundedFakeSerial(cmd, b"")
    flipper = _make_bounded_flipper(fake)

    with pytest.raises(EmissionBlocked):
        await get("flipper_rfid_emulate").invoke(
            flipper,
            {"key_type": "EM4100", "key_data": "0123456789", "duration_seconds": 0.05},
            transport="test",
        )
    assert not any(b"rfid emulate" in w for w in fake.written)


async def test_rfid_emulate_unknown_protocol_raises(monkeypatch):
    """`Unknown protocol:` / `Available protocols:` output → ActionRuntimeError."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    cmd = "rfid emulate NopeProto AABBCC"
    device_msg = (
        b"Unknown protocol: NopeProto\r\n"
        b"Available protocols: EM4100, H10301, Indala26\r\n"
    )
    fake = _BoundedFakeSerial(cmd, device_msg)
    flipper = _make_bounded_flipper(fake)

    with pytest.raises(ActionRuntimeError) as excinfo:
        await get("flipper_rfid_emulate").invoke(
            flipper,
            {"key_type": "NopeProto", "key_data": "AABBCC", "duration_seconds": 0.05},
            transport="test",
        )
    # The device's available-protocols list must be surfaced to the caller.
    assert "Available protocols" in str(excinfo.value)


async def test_rfid_emulate_duration_capped_at_60(monkeypatch):
    """duration_seconds is capped at 60.0."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    captured: dict = {}

    async def _fake_bounded(self, cmd: str, duration_s: float) -> str:
        captured["cmd"] = cmd
        captured["duration_s"] = duration_s
        return ""

    monkeypatch.setattr(
        flipper_mod.FlipperConnection, "run_bounded_command", _fake_bounded
    )
    flipper = FlipperConnection(port_factory=lambda: "/dev/fake", reconnect_interval=60)
    flipper._connected = True

    result = await get("flipper_rfid_emulate").invoke(
        flipper,
        {"key_type": "EM4100", "key_data": "0123456789", "duration_seconds": 999.0},
        transport="test",
    )
    assert captured["duration_s"] == 60.0
    assert result["duration_s"] == 60.0


@pytest.mark.parametrize("bad_data", ["", "  ", "XYZ", "12 34", "0xAB"])
async def test_rfid_emulate_bad_key_data_rejected(monkeypatch, bad_data):
    """Non-hex / empty key_data is rejected before any serial I/O."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    fake = _BoundedFakeSerial("rfid emulate EM4100 X", b"")
    flipper = _make_bounded_flipper(fake)

    with pytest.raises(ActionParamError):
        await get("flipper_rfid_emulate").invoke(
            flipper,
            {"key_type": "EM4100", "key_data": bad_data, "duration_seconds": 0.05},
            transport="test",
        )
    assert not any(b"rfid emulate" in w for w in fake.written)
