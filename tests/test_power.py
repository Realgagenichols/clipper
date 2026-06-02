"""Tests for power control actions: flipper_power and flipper_power_otg.

TDD: these tests are written first; implementation follows in
clipper/hardware/power.py.

Key behaviors covered:
- flipper_power sends `power off` / `power reboot` / `power reboot2dfu`.
- power off/reboot DISCONNECT the device mid-command — send_command raises
  FlipperDisconnected (or returns empty). The handler MUST treat that as
  SUCCESS (the reboot/off was triggered), never let it propagate.
- flipper_power_otg sends `power 5v 1` / `power 5v 0` as a normal command;
  a disconnect there IS a real error and may propagate.
- Both actions are emissive: gate OFF -> EmissionBlocked + audit 'denied';
  gate ON -> command sent + handler returns success + audit 'ok'.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import clipper.audit as audit
from clipper.actions import ActionParamError, EmissionBlocked, get
from clipper.flipper import FlipperConnection, FlipperDisconnected


@pytest.fixture
def isolated_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Each test gets a fresh audit log in a temp dir (mirrors test_safety_gate)."""
    log_path = tmp_path / "audit.log"
    monkeypatch.setenv("CLIPPER_AUDIT_PATH", str(log_path))
    audit.reset_for_tests()
    yield log_path
    audit.reset_for_tests()

# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_hardware_actions.py)
# ---------------------------------------------------------------------------

HANDSHAKE_DEVICE = "hardware_name: TestFlipper\r\n>: "
HANDSHAKE_POWER = "Battery Charge: 88%\r\n>: "


def script_handshake(harness) -> None:
    """Queue the two handshake responses consumed by FlipperConnection.start()."""
    harness.expect("device_info", HANDSHAKE_DEVICE)
    harness.expect("info power", HANDSHAKE_POWER)


def _read_audit_entries(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ===========================================================================
# R7 — flipper_power (off / reboot / reboot2dfu)
# ===========================================================================


@pytest.mark.parametrize(
    ("mode", "expected_cmd"),
    [
        ("off", "power off"),
        ("reboot", "power reboot"),
        ("reboot2dfu", "power reboot2dfu"),
    ],
)
async def test_power_sends_correct_command(fake_flipper, monkeypatch, mode, expected_cmd):
    """Each mode maps to its CLI command; an empty response is success."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    # Device acknowledges then the connection effectively goes quiet.
    harness.expect(expected_cmd, ">: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_power").invoke(
            flipper, {"mode": mode}, transport="test"
        )
        assert result["mode"] == mode
        assert result["ok"] is True
        assert "note" in result and result["note"]
        assert any(expected_cmd in w for w in harness.all_written())
    finally:
        await flipper.stop()


@pytest.mark.parametrize("mode", ["off", "reboot", "reboot2dfu"])
async def test_power_disconnect_is_success(fake_flipper, monkeypatch, mode):
    """CRITICAL: the reboot/off drops the serial link mid-command.

    send_command raises FlipperDisconnected — the handler MUST catch it and
    return success (the command was issued; reconnect will follow).
    """
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        # Device drops the link the instant the power command is processed.
        harness.simulate_disconnect()
        result = await get("flipper_power").invoke(
            flipper, {"mode": mode}, transport="test"
        )
        assert result["mode"] == mode
        assert result["ok"] is True
    finally:
        await flipper.stop()


@pytest.mark.parametrize("mode", ["off", "reboot", "reboot2dfu"])
async def test_power_oserror_disconnect_is_success(fake_flipper, monkeypatch, mode):
    """A re-enumerated CDC port surfaces OSError(errno 6) (L3) — still success."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        harness.simulate_oserror(6)  # ENXIO from the rebooting device
        result = await get("flipper_power").invoke(
            flipper, {"mode": mode}, transport="test"
        )
        assert result == {"mode": mode, "ok": True, "note": result["note"]}
        assert result["ok"] is True
    finally:
        await flipper.stop()


async def test_power_invalid_mode_raises(fake_flipper, monkeypatch):
    """Modes outside the enum must raise ActionParamError."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionParamError):
            await get("flipper_power").invoke(
                flipper, {"mode": "sleep"}, transport="test"
            )
    finally:
        await flipper.stop()


async def test_power_blocked_without_allow_emit(fake_flipper, isolated_audit):
    """Gate OFF -> EmissionBlocked raised + audit 'denied'; no command sent."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(EmissionBlocked):
            await get("flipper_power").invoke(
                flipper, {"mode": "reboot"}, transport="test"
            )
        assert not any("power" in w and "info" not in w for w in harness.all_written())
    finally:
        await flipper.stop()

    entries = [e for e in _read_audit_entries(isolated_audit) if e["action"] == "flipper_power"]
    assert len(entries) == 1
    assert entries[0]["outcome"] == "denied"


async def test_power_audit_ok_when_allowed(fake_flipper, monkeypatch, isolated_audit):
    """Gate ON -> handler runs, audit records 'ok'."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("power reboot", ">: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        await get("flipper_power").invoke(flipper, {"mode": "reboot"}, transport="test")
    finally:
        await flipper.stop()

    entries = [e for e in _read_audit_entries(isolated_audit) if e["action"] == "flipper_power"]
    assert len(entries) == 1
    assert entries[0]["outcome"] == "ok"


# ===========================================================================
# R8 — flipper_power_otg (external 5V OTG output)
# ===========================================================================


async def test_power_otg_enable_sends_5v_1(fake_flipper, monkeypatch):
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("power 5v 1", ">: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_power_otg").invoke(
            flipper, {"enable": True}, transport="test"
        )
        assert result["enabled"] is True
        assert result["ok"] is True
        assert any("power 5v 1" in w for w in harness.all_written())
    finally:
        await flipper.stop()


async def test_power_otg_disable_sends_5v_0(fake_flipper, monkeypatch):
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("power 5v 0", ">: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_power_otg").invoke(
            flipper, {"enable": False}, transport="test"
        )
        assert result["enabled"] is False
        assert result["ok"] is True
        assert any("power 5v 0" in w for w in harness.all_written())
    finally:
        await flipper.stop()


async def test_power_otg_disconnect_propagates(fake_flipper, monkeypatch):
    """OTG does NOT reboot the device — a disconnect here is a REAL error."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        harness.simulate_disconnect()
        with pytest.raises(FlipperDisconnected):
            await get("flipper_power_otg").invoke(
                flipper, {"enable": True}, transport="test"
            )
    finally:
        await flipper.stop()


async def test_power_otg_blocked_without_allow_emit(fake_flipper, isolated_audit):
    """Gate OFF -> EmissionBlocked + audit 'denied'."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(EmissionBlocked):
            await get("flipper_power_otg").invoke(
                flipper, {"enable": True}, transport="test"
            )
    finally:
        await flipper.stop()

    entries = [
        e for e in _read_audit_entries(isolated_audit) if e["action"] == "flipper_power_otg"
    ]
    assert len(entries) == 1
    assert entries[0]["outcome"] == "denied"
