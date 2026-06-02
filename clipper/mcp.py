"""clipper.mcp — MCP server for exposing the action registry as tools.

Every registered action becomes a tool with the same name, description, and
JSON-Schema parameters.  No separate wiring is needed when a new action is
added to clipper.actions.registry.

Transport:
- stdio: ``run_stdio()`` — used by ``clipper mcp-stdio``
         (``claude mcp add clipper -- uv run clipper mcp-stdio``)

Design notes:
- Fail fast: ActionParamError / EmissionBlocked / ActionNotFound are re-raised
  as ``ValueError`` so the MCP SDK catches them and returns an isError=True
  CallToolResult to the client.  They are NOT silently swallowed.
- Concurrency: handlers delegate to ``Action.invoke``, which acquires the
  serial lock inside FlipperConnection — no additional locking needed here.
- Broad exceptions in transport-level code are logged at WARNING with
  ``exc_info=True`` rather than suppressed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

import clipper.hardware  # noqa: F401  ensure all actions register at import time
from clipper.actions import (
    ActionNotFound,
    ActionParamError,
    ActionRuntimeError,
    EmissionBlocked,
    registry,
)
from clipper.actions import (
    get as get_action,
)
from clipper.flipper import FlipperConnection, FlipperDisconnected

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def build_server(flipper: FlipperConnection) -> Server:
    """Build and return an MCP ``Server`` with tools wired to the action registry.

    The server is built once per process lifetime.  Tool registration happens
    at build time by iterating
    ``clipper.actions.registry``; new entries added after ``build_server`` was
    called will not appear until the server is rebuilt.  In practice the
    registry is populated at import time and does not change after startup.
    """
    server: Server = Server("clipper")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        """Return one MCP Tool per registered action."""
        return [
            types.Tool(
                name=a.name,
                description=a.description,
                inputSchema=a.json_schema(),
            )
            for a in registry.values()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        """Dispatch an MCP tool call to the matching registered action.

        Raises ``ValueError`` (which the SDK converts to an isError result)
        for unknown tools, bad params, and blocked emissions.
        """
        log.debug("mcp call_tool: name=%r arguments=%r", name, arguments)
        try:
            action = get_action(name)
            result = await action.invoke(flipper, arguments or {}, transport="mcp")
        except ActionNotFound as exc:
            log.warning("mcp tool not found: %r", name, exc_info=True)
            raise ValueError(f"action_not_found: {exc}") from exc
        except ActionParamError as exc:
            log.warning("mcp invalid params for tool %r: %s", name, exc.errors, exc_info=True)
            raise ValueError(f"invalid_params: {exc.errors}") from exc
        except EmissionBlocked as exc:
            log.warning("mcp emission blocked for tool %r", name, exc_info=True)
            raise ValueError(f"emission_not_enabled: {exc}") from exc
        except FlipperDisconnected as exc:
            # The device is down (unplugged / rebooting / re-enumerating). Surface
            # a clean tool error — never let a raw serial OSError reach the client.
            # The background reconnect loop will re-establish the link.
            log.warning("mcp tool %r: flipper disconnected: %s", name, exc)
            raise ValueError(f"device_disconnected: {exc}") from exc
        except ActionRuntimeError as exc:
            log.warning("mcp tool %r runtime error: %s", name, exc.detail, exc_info=True)
            raise ValueError(f"action_failed: {exc.detail}") from exc
        return [types.TextContent(type="text", text=json.dumps(result))]

    return server


# ---------------------------------------------------------------------------
# stdio transport entry point
# ---------------------------------------------------------------------------


def _git_commit_short() -> str:
    """Return short git commit hash this code was loaded from, or 'unknown'."""
    import subprocess
    from pathlib import Path

    try:
        root = Path(__file__).resolve().parent.parent
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def run_stdio() -> None:
    """Block running an MCP stdio server.

    Creates a ``FlipperConnection``, starts it (tolerates no-device), runs the
    MCP server over stdio until stdin is closed, then stops the connection.

    Called by ``clipper mcp-stdio`` via ``clipper.main.cmd_mcp_stdio``.
    If no Flipper is connected, the server still starts — only actions that
    touch the serial port will surface a disconnected error.
    """

    async def _run() -> None:
        log.info("mcp-stdio: clipper build %s", _git_commit_short())
        flipper = FlipperConnection()
        log.info("mcp-stdio: starting FlipperConnection")
        await flipper.start()
        try:
            server = build_server(flipper)
            async with stdio_server() as (read_stream, write_stream):
                log.info("mcp-stdio: server running")
                await server.run(
                    read_stream,
                    write_stream,
                    server.create_initialization_options(),
                )
        except Exception:
            log.warning("mcp-stdio: unexpected error in server loop", exc_info=True)
            raise
        finally:
            log.info("mcp-stdio: stopping FlipperConnection")
            await flipper.stop()

    asyncio.run(_run())
