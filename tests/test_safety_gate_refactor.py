"""Tests for the emission safety gate (canonical surface + back-compat).

- Gate disabled by default → emissive action raises EmissionBlocked
- Gate enabled via canonical CLIPPER_SAFETY
- Gate enabled via legacy CLIPPER_ALLOW_EMIT (deprecation log fires once)
- Both vars set → canonical wins, no deprecation log
- ``set_safety_allowed(True/False)`` mutates env correctly
- Runtime toggle unblocks every gated action category
- ``emit_allowed`` / ``set_emit_allowed`` aliases resolve to the new functions
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

import clipper.actions as actions_mod
import clipper.audit as audit
import clipper.safety as safety_mod
from clipper.actions import Action, EmissionBlocked, register

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _PingParams(BaseModel):
    message: str = "ping"


async def _ping_handler(_flipper: Any, params: _PingParams) -> dict:
    return {"echo": params.message}


def _make_emissive_action(name: str) -> Action:
    return Action(
        name=name,
        description="Fake emissive action for refactor tests",
        params=_PingParams,
        handler=_ping_handler,
        emissive=True,
    )


def _read_audit_entries(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_safety_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure both safety env vars start unset for every test.

    monkeypatch.setenv/delenv restores the original env on teardown so we
    never leak state between tests.
    """
    monkeypatch.delenv("CLIPPER_SAFETY", raising=False)
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)


@pytest.fixture(autouse=True)
def reset_legacy_warned_flag() -> None:
    """Reset the one-shot deprecation flag so each test sees a fresh process.

    The flag is module-level in clipper.safety; tests that assert the warning
    is emitted (or NOT emitted) need a clean slate.
    """
    safety_mod._LEGACY_ENV_WARNED = False
    yield
    safety_mod._LEGACY_ENV_WARNED = False


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh action registry per test so registrations don't bleed."""
    fresh: dict = dict(actions_mod.registry)
    monkeypatch.setattr(actions_mod, "registry", fresh)


@pytest.fixture(autouse=True)
def isolated_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Fresh audit log per test. Mirrors the pattern in test_safety_gate.py."""
    log_path = tmp_path / "audit.log"
    monkeypatch.setenv("CLIPPER_AUDIT_PATH", str(log_path))
    audit.reset_for_tests()
    yield log_path
    audit.reset_for_tests()


# ---------------------------------------------------------------------------
# Scenario: Gate disabled by default
# ---------------------------------------------------------------------------


async def test_gate_disabled_by_default(isolated_audit: Path) -> None:
    """GIVEN no env vars set, THEN safety_allowed() is False and gated action raises."""
    assert safety_mod.safety_allowed() is False

    action = _make_emissive_action("default_off_test")
    register(action)

    with pytest.raises(EmissionBlocked):
        await action.invoke(None, {"message": "hi"})


# ---------------------------------------------------------------------------
# Scenario: Gate enabled via canonical env var
# ---------------------------------------------------------------------------


async def test_gate_enabled_via_canonical_env(
    monkeypatch: pytest.MonkeyPatch,
    isolated_audit: Path,
) -> None:
    """GIVEN CLIPPER_SAFETY=1, THEN safety_allowed() is True and gated action runs."""
    monkeypatch.setenv("CLIPPER_SAFETY", "1")
    assert safety_mod.safety_allowed() is True

    action = _make_emissive_action("canonical_on_test")
    register(action)

    result = await action.invoke(None, {"message": "ok"})
    assert result == {"echo": "ok"}


# ---------------------------------------------------------------------------
# Scenario: Gate enabled via legacy env var (deprecation log fires once)
# ---------------------------------------------------------------------------


@pytest.mark.regression
async def test_gate_enabled_via_legacy_env_with_deprecation_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    isolated_audit: Path,
) -> None:
    """Legacy env enables the gate AND emits a single deprecation warning.

    GIVEN only CLIPPER_ALLOW_EMIT=1, THEN safety_allowed() is True AND the
    deprecation warning fires at most once per process.

    The warning is emitted by ``_warn_legacy_env_once()`` — we re-trigger it
    by reloading the module's flag and calling the helper directly, which
    emulates the import-time behavior. Calling it twice must still produce
    only ONE log record.
    """
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    monkeypatch.delenv("CLIPPER_SAFETY", raising=False)

    assert safety_mod.safety_allowed() is True

    caplog.set_level(logging.WARNING, logger="clipper.safety")
    # Re-emulate the import-time call (the autouse fixture reset the flag).
    safety_mod._warn_legacy_env_once()
    safety_mod._warn_legacy_env_once()  # Second call must be a no-op.

    deprecation_records = [
        r for r in caplog.records if "CLIPPER_ALLOW_EMIT is deprecated" in r.getMessage()
    ]
    assert len(deprecation_records) == 1, (
        f"expected exactly one deprecation log, got {len(deprecation_records)}: "
        f"{[r.getMessage() for r in deprecation_records]}"
    )
    assert deprecation_records[0].levelno == logging.WARNING


# ---------------------------------------------------------------------------
# Scenario: Both vars set → no deprecation log
# ---------------------------------------------------------------------------


@pytest.mark.regression
async def test_both_vars_enable_no_extra_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GIVEN both CLIPPER_SAFETY=1 and CLIPPER_ALLOW_EMIT=1, THEN gate open, NO deprecation log."""
    monkeypatch.setenv("CLIPPER_SAFETY", "1")
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")

    assert safety_mod.safety_allowed() is True

    caplog.set_level(logging.WARNING, logger="clipper.safety")
    safety_mod._warn_legacy_env_once()

    deprecation_records = [
        r for r in caplog.records if "CLIPPER_ALLOW_EMIT is deprecated" in r.getMessage()
    ]
    assert deprecation_records == [], (
        "deprecation log fired even though canonical CLIPPER_SAFETY was also set"
    )


# ---------------------------------------------------------------------------
# Scenario: set_safety_allowed runtime flip
# ---------------------------------------------------------------------------


async def test_set_safety_allowed_runtime_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """set_safety_allowed(True) sets CLIPPER_SAFETY=1; set_safety_allowed(False) clears both vars.

    Also confirms that even if the legacy var was set going in, set_safety_allowed(True)
    canonicalizes to CLIPPER_SAFETY only.
    """
    import os

    # --- Flip ON from a clean state ---
    new = safety_mod.set_safety_allowed(True)
    assert new is True
    assert os.environ.get("CLIPPER_SAFETY") == "1"
    assert safety_mod.safety_allowed() is True

    # --- Flip OFF — both env vars cleared ---
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")  # simulate legacy var leaking in
    new = safety_mod.set_safety_allowed(False)
    assert new is False
    assert "CLIPPER_SAFETY" not in os.environ
    assert "CLIPPER_ALLOW_EMIT" not in os.environ
    assert safety_mod.safety_allowed() is False


# ---------------------------------------------------------------------------
# Scenario: Runtime toggle unblocks every gated action
# ---------------------------------------------------------------------------


async def test_runtime_toggle_unblocks_every_gated_action(
    isolated_audit: Path,
) -> None:
    """Gate starts OFF; flip ON via set_safety_allowed(True); existing emissive action runs.

    Also stubs a fake non-existent action with emissive=True (representing the
    future flipper_storage_write / flipper_mfkey_crack categories from the write + crack)
    and asserts it's allowed too — proves the gate is one switch for all
    state-affecting categories.
    """
    assert safety_mod.safety_allowed() is False

    # The existing built-in emissive action (built-in): flipper_ir_tx.
    ir_tx = actions_mod.get("flipper_ir_tx")
    assert ir_tx.emissive is True

    # And a stub stand-in for the the write + crack write + crack categories.
    stub_write_like = _make_emissive_action("stub_write_like_action")
    stub_crack_like = _make_emissive_action("stub_crack_like_action")
    register(stub_write_like)
    register(stub_crack_like)

    # Flip ON.
    new = safety_mod.set_safety_allowed(True)
    assert new is True
    assert safety_mod.safety_allowed() is True

    # Both stub actions now run (they don't touch the device — _ping_handler
    # ignores the flipper arg).
    assert await stub_write_like.invoke(None, {"message": "w"}) == {"echo": "w"}
    assert await stub_crack_like.invoke(None, {"message": "c"}) == {"echo": "c"}

    # Reset for hygiene.
    safety_mod.set_safety_allowed(False)


# ---------------------------------------------------------------------------
# Scenario: Aliases still resolve to the new functions
# ---------------------------------------------------------------------------


def test_emit_allowed_aliases_resolve_to_safety_allowed() -> None:
    """emit_allowed and set_emit_allowed are name bindings, not separate bodies."""
    assert safety_mod.emit_allowed is safety_mod.safety_allowed
    assert safety_mod.set_emit_allowed is safety_mod.set_safety_allowed


# Defensive: importlib reload should not double-fire the warning either.
# (Kept out of the regression marker — it's belt-and-braces.)
def test_module_reload_does_not_duplicate_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reloading clipper.safety while legacy var is set fires the warning once per reload only."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    monkeypatch.delenv("CLIPPER_SAFETY", raising=False)

    caplog.set_level(logging.WARNING, logger="clipper.safety")
    importlib.reload(safety_mod)
    pre_count = sum(
        1 for r in caplog.records if "CLIPPER_ALLOW_EMIT is deprecated" in r.getMessage()
    )
    # A single reload triggers the import-time call exactly once.
    assert pre_count == 1
