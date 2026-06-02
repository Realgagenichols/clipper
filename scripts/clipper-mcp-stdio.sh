#!/usr/bin/env bash
# Wrapper for `claude mcp add` so the registration line stays short.
#
# All log output goes to stderr (the MCP server uses stdout for JSON-RPC).
# We can't use `make` here because make's recipe-echo writes to stdout and
# would corrupt the JSON-RPC stream. We invoke `python -m clipper.main` rather
# than the `clipper` console script because `-m` inserts the project root into
# sys.path explicitly, sidestepping editable-install path quirks on macOS.

set -eu

CLIPPER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export UV_NO_EDITABLE=0

exec uv run --directory "$CLIPPER_ROOT" python -m clipper.main mcp-stdio
