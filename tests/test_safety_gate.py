"""Tests for the emission safety gate and audit log.

Covers:
- Emissive actions blocked when CLIPPER_ALLOW_EMIT is unset (EmissionBlocked raised)
- Emissive actions allowed when CLIPPER_ALLOW_EMIT=1
- Audit log written on success AND on gated denial
- Transport label propagated to audit log
- Non-emissive actions skip audit entirely
- Handler errors logged with outcome="error"
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import clipper.actions as actions_mod
import clipper.audit as audit
from clipper.actions import Action, ActionRuntimeError, EmissionBlocked, register
from clipper.flipper import FlipperConnection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEVICE_INFO_RESPONSE = (
    "device info\r\n"
    "Hardware Name: ClipperDev\r\n"
    "Firmware Version: 0.103.1\r\n"
    "Hardware Version: 11\r\n"
    ">: "
)

_POWER_INFO_RESPONSE = (
    "power info\r\n"
    "Battery Charge: 87%\r\n"
    ">: "
)


class _PingParams(BaseModel):
    message: str = "ping"


async def _ping_handler(_flipper: Any, params: _PingParams) -> dict:
    return {"echo": params.message}


class _BoomParams(BaseModel):
    message: str = "boom"


async def _boom_handler(_flipper: Any, params: _BoomParams) -> dict:
    raise ValueError("boom from handler")


def _make_emissive_action(name: str = "test_emit") -> Action:
    return Action(
        name=name,
        description="Fake emissive action for tests",
        params=_PingParams,
        handler=_ping_handler,
        emissive=True,
    )


def _make_failing_emissive_action(name: str = "test_emit_fail") -> Action:
    return Action(
        name=name,
        description="Fake emissive action that raises in the handler",
        params=_BoomParams,
        handler=_boom_handler,
        emissive=True,
    )


def _read_audit_entries(audit_path: Path) -> list[dict]:
    """Read all JSON-line entries from the audit log file."""
    if not audit_path.exists():
        return []
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test gets a fresh action registry so registrations don't bleed."""
    # Keep built-in actions (flipper_state etc.) by shallow-copying the live registry
    fresh: dict = dict(actions_mod.registry)
    monkeypatch.setattr(actions_mod, "registry", fresh)


@pytest.fixture(autouse=True)
def isolated_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Each test gets a fresh audit log in a temp dir.

    Sets CLIPPER_AUDIT_PATH env var and calls audit.reset_for_tests() before
    and after so handler state never leaks between tests.
    """
    log_path = tmp_path / "audit.log"
    monkeypatch.setenv("CLIPPER_AUDIT_PATH", str(log_path))
    audit.reset_for_tests()
    yield log_path
    audit.reset_for_tests()


# ---------------------------------------------------------------------------
# 5.1 — Blocked when env unset
# ---------------------------------------------------------------------------


async def test_emissive_action_blocked_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
    isolated_audit: Path,
) -> None:
    """GIVEN CLIPPER_ALLOW_EMIT unset, WHEN emissive action invoked directly,
    THEN EmissionBlocked raised and audit log records 'denied'."""
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)

    action = _make_emissive_action("blocked_test")
    register(action)

    with pytest.raises(EmissionBlocked) as exc_info:
        await action.invoke(None, {"message": "hello"})

    assert str(exc_info.value) == "blocked_test"

    entries = _read_audit_entries(isolated_audit)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "denied"
    assert entries[0]["action"] == "blocked_test"
    assert "emit gate" in entries[0].get("detail", "")


# ---------------------------------------------------------------------------
# 5.1 — Allowed when env set
# ---------------------------------------------------------------------------


async def test_emissive_action_allowed_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
    isolated_audit: Path,
) -> None:
    """GIVEN CLIPPER_ALLOW_EMIT=1, WHEN emissive action invoked directly,
    THEN handler runs and audit log records 'ok'."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")

    action = _make_emissive_action("allowed_test")
    register(action)

    result = await action.invoke(None, {"message": "hello"})
    assert result == {"echo": "hello"}

    entries = _read_audit_entries(isolated_audit)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "ok"
    assert entries[0]["action"] == "allowed_test"


# ---------------------------------------------------------------------------
# 5.1 — Audit log records transport
# ---------------------------------------------------------------------------


async def test_audit_log_records_transport(
    monkeypatch: pytest.MonkeyPatch,
    isolated_audit: Path,
) -> None:
    """WHEN an emissive action is invoked via invoke(transport='mcp'),
    THEN the audit log records the 'mcp' transport label."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")

    action = _make_emissive_action("transport_test")
    register(action)

    await action.invoke(None, {"message": "via-mcp"}, transport="mcp")

    entries = _read_audit_entries(isolated_audit)
    transports = [e["transport"] for e in entries]
    assert "mcp" in transports


# ---------------------------------------------------------------------------
# 5.1 — Non-emissive actions skip audit
# ---------------------------------------------------------------------------


async def test_non_emissive_action_skips_audit(
    monkeypatch: pytest.MonkeyPatch,
    isolated_audit: Path,
    fake_flipper: Any,
) -> None:
    """WHEN flipper_state (non-emissive) is invoked, THEN audit log stays empty."""
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)

    fake_flipper.add_port()
    fake_flipper.expect("device info", _DEVICE_INFO_RESPONSE)
    fake_flipper.expect("power info", _POWER_INFO_RESPONSE)

    flipper = FlipperConnection(port_factory=fake_flipper.port_factory, reconnect_interval=60)
    await flipper.start()

    try:
        from clipper.actions import get as action_get

        state_action = action_get("flipper_state")
        await state_action.invoke(flipper, {}, transport="http")
    finally:
        await flipper.stop()

    entries = _read_audit_entries(isolated_audit)
    assert entries == [], f"expected empty audit log, got: {entries}"


# ---------------------------------------------------------------------------
# 5.1 — Handler error logged with outcome="error"
# ---------------------------------------------------------------------------


async def test_handler_error_logged_with_outcome_error(
    monkeypatch: pytest.MonkeyPatch,
    isolated_audit: Path,
) -> None:
    """GIVEN CLIPPER_ALLOW_EMIT=1 and a handler that raises ValueError,
    WHEN emissive action invoked, THEN audit log has outcome='error' and detail."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")

    action = _make_failing_emissive_action("error_test")
    register(action)

    with pytest.raises(ActionRuntimeError, match="boom from handler"):
        await action.invoke(None, {}, transport="http")

    entries = _read_audit_entries(isolated_audit)
    assert len(entries) == 1
    assert entries[0]["outcome"] == "error"
    assert "boom from handler" in entries[0].get("detail", "")
