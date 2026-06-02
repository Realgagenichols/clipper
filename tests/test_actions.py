"""Tests for clipper.actions — Action Registry.

All tests use the fake_flipper fixture from conftest.py so no real hardware
is needed. Async tests run under pytest-asyncio with asyncio_mode=auto.

Registry isolation: each test that registers actions uses monkeypatch to swap
in a fresh dict so registration state never leaks between tests.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

import clipper.actions as actions_mod
from clipper.actions import Action, ActionNotFound, ActionParamError, get, register
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


class _DoubleParams(BaseModel):
    x: int


async def _double_handler(flipper: FlipperConnection, params: _DoubleParams) -> dict:
    return {"doubled": params.x * 2}


def _make_double_action(name: str = "test_double") -> Action:
    return Action(
        name=name,
        description="Doubles the input",
        params=_DoubleParams,
        handler=_double_handler,
    )


# ---------------------------------------------------------------------------
# 3.1 + 3.2: register exposes schema
# ---------------------------------------------------------------------------


def test_register_exposes_schema(monkeypatch):
    """GIVEN a registered Action with a typed param, THEN json_schema() has that property."""
    monkeypatch.setattr(actions_mod, "registry", {})

    action = _make_double_action()
    register(action)

    schema = actions_mod.registry["test_double"].json_schema()
    assert "properties" in schema
    assert "x" in schema["properties"]


# ---------------------------------------------------------------------------
# 3.3: invoke with valid params runs handler
# ---------------------------------------------------------------------------


async def test_invoke_with_valid_params(monkeypatch):
    """GIVEN an Action and valid params dict, WHEN invoke called, THEN handler result returned."""
    monkeypatch.setattr(actions_mod, "registry", {})

    action = _make_double_action()
    register(action)

    # invoke needs a flipper instance — pass None; our handler ignores it
    result = await action.invoke(None, {"x": 5})  # type: ignore[arg-type]
    assert result == {"doubled": 10}


# ---------------------------------------------------------------------------
# 3.3: invoke with invalid params raises ActionParamError
# ---------------------------------------------------------------------------


async def test_invoke_with_invalid_params_raises(monkeypatch):
    """GIVEN invalid params, WHEN invoke called, THEN ActionParamError raised with details."""
    monkeypatch.setattr(actions_mod, "registry", {})

    action = _make_double_action()
    register(action)

    with pytest.raises(ActionParamError) as exc_info:
        await action.invoke(None, {"x": "not_an_int"})  # type: ignore[arg-type]

    err = exc_info.value
    assert err.action == "test_double"
    # pydantic v2 errors have 'loc' tuples
    locs = [tuple(e["loc"]) for e in err.errors]
    assert ("x",) in locs


# ---------------------------------------------------------------------------
# 3.1: duplicate registration is rejected
# ---------------------------------------------------------------------------


def test_duplicate_registration_rejected(monkeypatch):
    """GIVEN an action already registered, WHEN same name registered again, THEN ValueError."""
    monkeypatch.setattr(actions_mod, "registry", {})

    register(_make_double_action("dup_test"))
    with pytest.raises(ValueError, match="already registered"):
        register(_make_double_action("dup_test"))


# ---------------------------------------------------------------------------
# 3.1: get unknown action raises ActionNotFound
# ---------------------------------------------------------------------------


def test_get_unknown_action_raises(monkeypatch):
    """GIVEN empty registry, WHEN get('nope') called, THEN ActionNotFound raised."""
    monkeypatch.setattr(actions_mod, "registry", {})

    with pytest.raises(ActionNotFound):
        get("nope")


# ---------------------------------------------------------------------------
# 3.2: emissive flag defaults to False
# ---------------------------------------------------------------------------


def test_emissive_defaults_false():
    """GIVEN Action with no explicit emissive flag, THEN emissive is False."""
    action = _make_double_action()
    assert action.emissive is False


def test_emissive_can_be_set_true():
    """GIVEN Action with emissive=True, THEN emissive is True."""
    action = Action(
        name="emissive_test",
        description="Emissive action",
        params=_DoubleParams,
        handler=_double_handler,
        emissive=True,
    )
    assert action.emissive is True


# ---------------------------------------------------------------------------
# 3.4: flipper_state end-to-end against fake_flipper
# ---------------------------------------------------------------------------


async def test_flipper_state_against_fake(fake_flipper):
    """GIVEN a connected fake Flipper, WHEN flipper_state invoked, THEN correct state returned."""
    fake_flipper.add_port()
    fake_flipper.expect("device info", _DEVICE_INFO_RESPONSE)
    fake_flipper.expect("power info", _POWER_INFO_RESPONSE)

    conn = FlipperConnection(port_factory=fake_flipper.port_factory, reconnect_interval=10)
    try:
        await conn.start()
        assert conn.connected is True

        # Import and exercise flipper_state action (registered at module load)
        from clipper.actions import get as action_get
        from clipper.hardware import device  # noqa: F401 — registers flipper_state

        flipper_state_action = action_get("flipper_state")
        result = await flipper_state_action.invoke(conn, {})

        assert result["connected"] is True
        assert result["name"] == "ClipperDev"
        assert result["firmware"] == "0.103.1"
        assert result["battery"] == 87
    finally:
        await conn.stop()
