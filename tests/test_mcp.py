"""Tests for clipper.mcp — MCP server (scenarios).

Tests call the handler functions registered on the Server instance directly
(no transport layer needed) by extracting them from server.request_handlers.

Pattern for invoking handlers:
    server.request_handlers[types.ListToolsRequest]  → async (req) → ServerResult
    server.request_handlers[types.CallToolRequest]   → async (req) → ServerResult

We build fake requests using the appropriate types.* models and assert on the
unwrapped result payloads.

FlipperConnection lifecycle:
    For most tests a disconnected FlipperConnection is fine — the action
    handlers will surface FlipperDisconnected only if they actually try to
    send a command.  flipper_state reads from the cached device_info which is
    set during start(), so we use a connected fake_flipper for that test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp import types

from clipper.actions import Action, register, registry
from clipper.flipper import FlipperConnection
from clipper.mcp import build_server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HANDSHAKE_DEVICE = "hardware_name: TestFlipper\r\nFirmware Version: 1.0.0\r\n>: "
HANDSHAKE_POWER = "Battery Charge: 88%\r\n>: "


def script_handshake(harness: Any) -> None:
    """Queue the two handshake responses consumed by FlipperConnection.start()."""
    harness.expect("device_info", HANDSHAKE_DEVICE)
    harness.expect("info power", HANDSHAKE_POWER)


async def _list_tools(server) -> list[types.Tool]:
    """Call the list_tools handler and return the tool list."""
    handler = server.request_handlers[types.ListToolsRequest]
    result = await handler(None)
    # result is a ServerResult wrapping a ListToolsResult
    return result.root.tools


async def _call_tool(server, name: str, arguments: dict) -> list[types.TextContent]:
    """Call the call_tool handler and return the content list.

    Raises ValueError if the tool returns isError=True (mirrors the SDK
    behaviour of converting ValueError from handlers to error results).
    """
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments),
    )
    handler = server.request_handlers[types.CallToolRequest]
    result = await handler(req)
    call_result = result.root
    if call_result.isError:
        # Surface the error text as ValueError so tests can assert on it
        text = call_result.content[0].text if call_result.content else ""
        raise ValueError(text)
    return call_result.content  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 7.1 — list_tools returns every registered action with schemas
# ---------------------------------------------------------------------------


async def test_list_tools_includes_flipper_state():
    """GIVEN a built server, WHEN list_tools is called, THEN flipper_state is present."""
    flipper = FlipperConnection()
    server = build_server(flipper)

    tools = await _list_tools(server)
    names = [t.name for t in tools]
    assert "flipper_state" in names

    # The tool should have a non-empty inputSchema
    state_tool = next(t for t in tools if t.name == "flipper_state")
    assert isinstance(state_tool.inputSchema, dict)
    assert state_tool.description


async def test_list_tools_includes_every_registered_action():
    """WHEN list_tools is called, THEN every registry entry appears exactly once."""
    flipper = FlipperConnection()
    server = build_server(flipper)

    tools = await _list_tools(server)
    tool_names = {t.name for t in tools}
    registry_names = set(registry.keys())

    assert tool_names == registry_names
    assert len(tools) == len(registry)


# ---------------------------------------------------------------------------
# 7.1 — call_tool invokes action and returns TextContent
# ---------------------------------------------------------------------------


async def test_call_tool_invokes_action(fake_flipper):
    """WHEN call_tool('flipper_gpio_read', ...) is called with valid params,
    THEN the action runs and returns JSON TextContent."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("gpio mode PC0 0", "Pin PC0 is now an input\r\n>: ")
    harness.expect("gpio read PC0", "Pin PC0 <= 1\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        server = build_server(flipper)
        content = await _call_tool(server, "flipper_gpio_read", {"pin": "PC0"})

        assert len(content) == 1
        assert content[0].type == "text"
        payload = json.loads(content[0].text)
        assert payload == {"pin": "PC0", "level": 1}
    finally:
        await flipper.stop()


# ---------------------------------------------------------------------------
# 7.1 — unknown tool returns error
# ---------------------------------------------------------------------------


async def test_call_tool_unknown_returns_error():
    """WHEN call_tool is called with an unknown name, THEN ValueError with
    'action_not_found' is raised (MCP SDK converts this to an isError result)."""
    flipper = FlipperConnection()
    server = build_server(flipper)

    with pytest.raises(ValueError, match="action_not_found"):
        await _call_tool(server, "does_not_exist", {})


# ---------------------------------------------------------------------------
# 7.1 — invalid params returns error
# ---------------------------------------------------------------------------


async def test_call_tool_invalid_params_returns_error():
    """WHEN call_tool is called with params that fail schema validation, THEN
    ValueError with 'invalid_params' is raised."""
    flipper = FlipperConnection()
    server = build_server(flipper)

    # flipper_gpio_write requires pin (must be a valid GPIO pin enum) and level (0|1).
    # Passing an invalid pin value triggers ActionParamError.
    with pytest.raises(ValueError, match="invalid_params"):
        await _call_tool(server, "flipper_gpio_write", {"pin": "INVALID_PIN", "level": 1})


# ---------------------------------------------------------------------------
# 7.5 — action surface stays in sync (registry_sync scenario)
# ---------------------------------------------------------------------------


async def test_registry_sync():
    """WHEN a new action is registered, THEN it appears in MCP list_tools
    without extra wiring — registry sync."""
    from pydantic import BaseModel

    class _SyncParams(BaseModel):
        pass

    async def _sync_handler(flipper: Any, params: _SyncParams) -> dict:
        return {"ok": True}

    test_action_name = "test_sync_action_7_5"

    # Guard: clean up if a previous test run left this action registered
    # (shouldn't happen with isolated test registry, but defensive)
    registry.pop(test_action_name, None)

    new_action = Action(
        name=test_action_name,
        description="Registry sync test action",
        params=_SyncParams,
        handler=_sync_handler,
    )
    register(new_action)

    try:
        flipper = FlipperConnection()
        server = build_server(flipper)
        tools = await _list_tools(server)
        mcp_names = {t.name for t in tools}
        assert test_action_name in mcp_names, "new action not in MCP list_tools"
    finally:
        # Clean up: remove test action so it doesn't pollute other tests
        registry.pop(test_action_name, None)
