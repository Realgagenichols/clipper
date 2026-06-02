"""Tests for Sub-GHz capture (R9 flipper_subghz_rx) and file replay
(R10 flipper_subghz_tx_from_file).

TDD: these tests are written before the implementation in
clipper/hardware/subghz.py.

R9 flipper_subghz_rx:
- NON-emissive (ungated): receiving is not transmitting, so it must work with
  the emit gate OFF and never touch the emission allow-list.
- Builds `subghz rx <freq_hz> <device>` (freq in Hz, device 0=int / 1=ext).
- Range-validates frequency for plausibility (~300-928 MHz); out-of-range
  raises ActionParamError.
- Runs via run_bounded_command and parses "Packets received: N" when present.

R10 flipper_subghz_tx_from_file:
- emissive (gated + audited): EmissionBlocked when the gate is off.
- Builds `subghz tx_from_file <path> <repeat> <device>` via one-shot
  send_command.
- Validates `path` with the storage path validator (traversal/bad paths
  rejected before any serial I/O).
"""

from __future__ import annotations

import threading

import pytest

import clipper.flipper as flipper_mod
from clipper.actions import ActionParamError, EmissionBlocked, get
from clipper.flipper import FlipperConnection

ETX = b"\x03"
PROMPT = b"\r\n>: "


# ---------------------------------------------------------------------------
# Bounded-command fake (mirrors tests/test_bounded_command.py) for subghz rx
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fast_quiesce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the post-byte quiet window so drain-based tests stay fast."""
    monkeypatch.setattr(flipper_mod, "_QUIESCE_QUIET", 0.01)
    monkeypatch.setattr(flipper_mod, "_QUIESCE_MAX", 0.1)


class _BoundedFakeSerial:
    """Write-gated fake modeling `subghz rx` (runs until the ETX stop byte)."""

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
# R9 — flipper_subghz_rx
# ---------------------------------------------------------------------------


async def test_subghz_rx_command_format_hz_and_device(monkeypatch):
    """`subghz rx <freq_hz> <device>` — freq in Hz, internal device 0."""
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)
    cmd = "subghz rx 433920000 0"
    fake = _BoundedFakeSerial(cmd, b"")
    flipper = _make_bounded_flipper(fake)

    result = await get("flipper_subghz_rx").invoke(
        flipper,
        {"frequency_mhz": 433.92, "duration_seconds": 0.05},
        transport="test",
    )

    joined = b"".join(fake.written)
    assert b"subghz rx 433920000 0\r" in joined
    assert fake.stop_sent, "ETX stop byte was never sent"
    assert result["frequency_mhz"] == 433.92
    assert result["duration_s"] == 0.05


async def test_subghz_rx_external_device_1(monkeypatch):
    """external=True selects device 1 (external CC1101)."""
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)
    cmd = "subghz rx 868000000 1"
    fake = _BoundedFakeSerial(cmd, b"")
    flipper = _make_bounded_flipper(fake)

    await get("flipper_subghz_rx").invoke(
        flipper,
        {"frequency_mhz": 868.0, "duration_seconds": 0.05, "external": True},
        transport="test",
    )

    joined = b"".join(fake.written)
    assert b"subghz rx 868000000 1\r" in joined


async def test_subghz_rx_not_gated(monkeypatch):
    """Receiving is non-emissive: it works with the emit gate OFF."""
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)
    monkeypatch.delenv("CLIPPER_SAFETY", raising=False)
    cmd = "subghz rx 315000000 0"
    fake = _BoundedFakeSerial(cmd, b"")
    flipper = _make_bounded_flipper(fake)

    # Must NOT raise EmissionBlocked.
    result = await get("flipper_subghz_rx").invoke(
        flipper,
        {"frequency_mhz": 315.0, "duration_seconds": 0.05},
        transport="test",
    )
    assert "raw" in result


async def test_subghz_rx_parses_packet_count(monkeypatch):
    """A 'Packets received: N' exit line is parsed into packets=N."""
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)
    cmd = "subghz rx 433920000 0"
    fake = _BoundedFakeSerial(cmd, b"Packets received: 7\r\n")
    flipper = _make_bounded_flipper(fake)

    result = await get("flipper_subghz_rx").invoke(
        flipper,
        {"frequency_mhz": 433.92, "duration_seconds": 0.05},
        transport="test",
    )
    assert result["packets"] == 7
    assert "Packets received: 7" in result["raw"]


async def test_subghz_rx_packets_none_when_unparseable(monkeypatch):
    """No packet-count line → packets is None (not an error)."""
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)
    cmd = "subghz rx 433920000 0"
    fake = _BoundedFakeSerial(cmd, b"")
    flipper = _make_bounded_flipper(fake)

    result = await get("flipper_subghz_rx").invoke(
        flipper,
        {"frequency_mhz": 433.92, "duration_seconds": 0.05},
        transport="test",
    )
    assert result["packets"] is None


@pytest.mark.parametrize("freq", [100.0, 299.0, 929.0, 2400.0])
async def test_subghz_rx_out_of_range_raises(monkeypatch, freq):
    """Implausible frequencies (~outside 300-928 MHz) raise ActionParamError."""
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)
    fake = _BoundedFakeSerial("subghz rx 0 0", b"")
    flipper = _make_bounded_flipper(fake)

    with pytest.raises(ActionParamError):
        await get("flipper_subghz_rx").invoke(
            flipper,
            {"frequency_mhz": freq, "duration_seconds": 0.05},
            transport="test",
        )
    # No command should have been written for a rejected param.
    assert not any(b"subghz rx" in w for w in fake.written)


async def test_subghz_rx_duration_capped_at_60(monkeypatch):
    """duration_seconds is capped at 60.0 (reported duration_s <= 60)."""
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)
    # We don't want to actually wait; patch run_bounded_command to capture args.
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

    result = await get("flipper_subghz_rx").invoke(
        flipper,
        {"frequency_mhz": 433.92, "duration_seconds": 999.0},
        transport="test",
    )
    assert captured["duration_s"] == 60.0
    assert result["duration_s"] == 60.0


# ---------------------------------------------------------------------------
# R10 — flipper_subghz_tx_from_file
# ---------------------------------------------------------------------------


async def test_tx_from_file_command_format(fake_flipper, monkeypatch):
    """`subghz tx_from_file <path> <repeat> <device>` one-shot via send_command."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    harness.expect("device_info", "hardware_name: TestFlipper\r\n>: ")
    harness.expect("info power", "Battery Charge: 88%\r\n>: ")
    path = "/ext/subghz/garage.sub"
    harness.expect(f"subghz tx_from_file {path} 2 0", ">: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_subghz_tx_from_file").invoke(
            flipper,
            {"path": path, "repeat": 2},
            transport="test",
        )
        assert result["ok"] is True
        assert result["path"] == path
        assert result["repeat"] == 2
        assert any(
            f"subghz tx_from_file {path} 2 0" in w for w in harness.all_written()
        )
    finally:
        await flipper.stop()


async def test_tx_from_file_external_device(fake_flipper, monkeypatch):
    """external=True selects device 1."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    harness.expect("device_info", "hardware_name: TestFlipper\r\n>: ")
    harness.expect("info power", "Battery Charge: 88%\r\n>: ")
    path = "/ext/subghz/x.sub"
    harness.expect(f"subghz tx_from_file {path} 1 1", ">: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        await get("flipper_subghz_tx_from_file").invoke(
            flipper,
            {"path": path, "external": True},
            transport="test",
        )
        assert any(
            f"subghz tx_from_file {path} 1 1" in w for w in harness.all_written()
        )
    finally:
        await flipper.stop()


async def test_tx_from_file_blocked_without_allow_emit(fake_flipper, monkeypatch):
    """Emissive: EmissionBlocked when the gate is off, no command written."""
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)
    monkeypatch.delenv("CLIPPER_SAFETY", raising=False)
    harness = fake_flipper
    harness.add_port()
    harness.expect("device_info", "hardware_name: TestFlipper\r\n>: ")
    harness.expect("info power", "Battery Charge: 88%\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(EmissionBlocked):
            await get("flipper_subghz_tx_from_file").invoke(
                flipper,
                {"path": "/ext/subghz/x.sub"},
                transport="test",
            )
        assert not any("tx_from_file" in w for w in harness.all_written())
    finally:
        await flipper.stop()


@pytest.mark.parametrize(
    "bad_path",
    [
        "relative/path.sub",          # not absolute
        "/ext/../etc/passwd",          # traversal
        "/ext/subghz/\x01evil.sub",   # control char
        "",                            # empty
    ],
)
async def test_tx_from_file_bad_path_rejected(fake_flipper, monkeypatch, bad_path):
    """Bad paths are rejected by the storage validator before any serial I/O."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    harness.expect("device_info", "hardware_name: TestFlipper\r\n>: ")
    harness.expect("info power", "Battery Charge: 88%\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionParamError):
            await get("flipper_subghz_tx_from_file").invoke(
                flipper,
                {"path": bad_path},
                transport="test",
            )
        assert not any("tx_from_file" in w for w in harness.all_written())
    finally:
        await flipper.stop()
