"""Tests for iButton emulation (R12 flipper_ibutton_emulate).

TDD: written before the implementation in clipper/hardware/ibutton.py.

R12 flipper_ibutton_emulate:
- EMISSIVE (gated + audited): EmissionBlocked when the gate is off, no command
  written.
- The CLI verb is `ikey` (NOT `ibutton`): builds `ikey emulate <type> <data>`
  and runs it via run_bounded_command (run-until-ETX). Local write-gated
  bounded fake (the conftest FakeSerial can't model run-until-ETX — L4).
- key_type must be one of Dallas | Cyfral | Metakom (case-insensitive); an
  unknown type is rejected at the param level (ActionParamError).
- duration_seconds defaults to 10 and is capped at 60.
- Device error lines (`err:` / `error:`) are surfaced as ActionRuntimeError.
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
    """Write-gated fake modeling a run-until-ETX command (`ikey emulate ...`)."""

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


async def test_ibutton_emulate_uses_ikey_verb(monkeypatch):
    """The CLI verb is `ikey` (NOT `ibutton`): `ikey emulate <type> <data>`."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    cmd = "ikey emulate Dallas 0102030405060708"
    fake = _BoundedFakeSerial(cmd, b"")
    flipper = _make_bounded_flipper(fake)

    result = await get("flipper_ibutton_emulate").invoke(
        flipper,
        {
            "key_type": "Dallas",
            "key_data": "0102030405060708",
            "duration_seconds": 0.05,
        },
        transport="test",
    )

    joined = b"".join(fake.written)
    assert b"ikey emulate Dallas 0102030405060708\r" in joined
    assert b"ibutton emulate" not in joined, "must use `ikey`, not `ibutton`"
    assert fake.stop_sent, "ETX stop byte was never sent"
    assert result["emulated"] is True
    assert result["key_type"] == "Dallas"
    assert result["duration_s"] == 0.05


@pytest.mark.parametrize(
    "given,expected",
    [("dallas", "Dallas"), ("CYFRAL", "Cyfral"), ("MetaKom", "Metakom")],
)
async def test_ibutton_emulate_key_type_case_insensitive(monkeypatch, given, expected):
    """key_type is accepted case-insensitively and normalized to canonical."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    cmd = f"ikey emulate {expected} AABB"
    fake = _BoundedFakeSerial(cmd, b"")
    flipper = _make_bounded_flipper(fake)

    result = await get("flipper_ibutton_emulate").invoke(
        flipper,
        {"key_type": given, "key_data": "AABB", "duration_seconds": 0.05},
        transport="test",
    )
    assert result["key_type"] == expected
    assert any(f"ikey emulate {expected} AABB".encode() in w for w in fake.written)


async def test_ibutton_emulate_blocked_without_allow_emit(monkeypatch):
    """Emissive: EmissionBlocked when the gate is off, no command written."""
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)
    monkeypatch.delenv("CLIPPER_SAFETY", raising=False)
    cmd = "ikey emulate Dallas 0102030405060708"
    fake = _BoundedFakeSerial(cmd, b"")
    flipper = _make_bounded_flipper(fake)

    with pytest.raises(EmissionBlocked):
        await get("flipper_ibutton_emulate").invoke(
            flipper,
            {
                "key_type": "Dallas",
                "key_data": "0102030405060708",
                "duration_seconds": 0.05,
            },
            transport="test",
        )
    assert not any(b"ikey emulate" in w for w in fake.written)


@pytest.mark.parametrize("bad_type", ["iButton", "Em4100", "", "Wiegand", "dallasx"])
async def test_ibutton_emulate_invalid_key_type_rejected(monkeypatch, bad_type):
    """Unknown key_type is rejected at the param level (ActionParamError)."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    fake = _BoundedFakeSerial("ikey emulate X AABB", b"")
    flipper = _make_bounded_flipper(fake)

    with pytest.raises(ActionParamError):
        await get("flipper_ibutton_emulate").invoke(
            flipper,
            {"key_type": bad_type, "key_data": "AABB", "duration_seconds": 0.05},
            transport="test",
        )
    assert not any(b"ikey emulate" in w for w in fake.written)


async def test_ibutton_emulate_device_error_raises(monkeypatch):
    """A device `err:` line is surfaced as ActionRuntimeError."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    cmd = "ikey emulate Cyfral AA"
    fake = _BoundedFakeSerial(cmd, b"err: invalid key length\r\n")
    flipper = _make_bounded_flipper(fake)

    with pytest.raises(ActionRuntimeError) as excinfo:
        await get("flipper_ibutton_emulate").invoke(
            flipper,
            {"key_type": "Cyfral", "key_data": "AA", "duration_seconds": 0.05},
            transport="test",
        )
    assert "invalid key length" in str(excinfo.value)


async def test_ibutton_emulate_duration_capped_at_60(monkeypatch):
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

    result = await get("flipper_ibutton_emulate").invoke(
        flipper,
        {"key_type": "Metakom", "key_data": "01020304", "duration_seconds": 999.0},
        transport="test",
    )
    assert captured["duration_s"] == 60.0
    assert captured["cmd"] == "ikey emulate Metakom 01020304"
    assert result["duration_s"] == 60.0
