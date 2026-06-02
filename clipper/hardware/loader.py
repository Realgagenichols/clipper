"""clipper.hardware.loader — loader management actions (list, info, close).

Three NON-emissive (ungated) actions for inspecting and controlling the app
loader on the Flipper Zero. None of these transmit RF/IR/USB — they query or
locally control the on-device app loader — so all register with
``emissive=False``.

  - ``flipper_loader_list``  — `loader list`  → available apps (raw + parsed)
  - ``flipper_loader_info``  — `loader info`  → {running, name}
  - ``flipper_loader_close`` — `loader close` → {closed, name, detail}

``flipper_loader_open`` (the emissive-launch sibling) lives in
clipper.hardware.feedback and is intentionally left there.

Source-confirmed CLI (Momentum loader_cli.c):
  loader list                              (no args)
  loader info   → 'Application "<name>" is running' | 'No application is running'
  loader close  → 'Application "<name>" was closed'
                | 'No application is running'
                | 'Application "<name>" has to be closed manually'

Parsing is deliberately lenient/firmware-variant tolerant: list parsing keeps
the raw text alongside a best-effort app list, and info/close match on the
recognizable phrase fragments rather than exact whole-line equality.

Registered automatically when this module is imported (which happens via
`from clipper.hardware import (... loader ...)` at the bottom of
clipper/actions.py).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel

from clipper.actions import Action, register

if TYPE_CHECKING:
    from clipper.flipper import FlipperConnection

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Match the quoted application name out of an info/close reply, e.g.
#   Application "NFC" is running
#   Application "Snake Game" was closed
_APP_NAME_RE = re.compile(r'Application\s+"([^"]+)"')


def _clean_lines(response: str) -> list[str]:
    """Split *response* into stripped, non-empty lines with the CLI prompt removed.

    The CLI host loop emits ``>: `` after each command; we drop the bare prompt
    token, blank lines, and CR artefacts so parsers see only meaningful content.
    """
    lines: list[str] = []
    for raw in response.splitlines():
        line = raw.rstrip("\r").strip()
        if not line or line == ">:":
            continue
        if line.endswith(">: "):
            line = line[: -len(">: ")].strip()
            if not line:
                continue
        lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Param models — all three actions take no inputs
# ---------------------------------------------------------------------------


class LoaderListParams(BaseModel):
    """No parameters required to list loader apps."""


class LoaderInfoParams(BaseModel):
    """No parameters required to query loader info."""


class LoaderCloseParams(BaseModel):
    """No parameters required to close the running app."""


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_list(response: str) -> list[str]:
    """Best-effort parse of ``loader list`` into a flat list of app names.

    The CLI groups apps by category. A category header is an un-indented line
    ending in ``:`` (e.g. ``Main:``); the apps under it are indented (a leading
    TAB on real hardware). We treat indented lines as app names and skip
    headers, the ``Apps:`` banner, and the prompt.

    Lenient by design: if the firmware variant doesn't use indentation we fall
    back to keeping any line that is neither a header nor an obvious banner, so
    an unfamiliar format yields a non-empty-where-possible list rather than an
    exception. The raw text is always returned alongside this for the caller.
    """
    apps: list[str] = []
    saw_indent = False
    for raw in response.splitlines():
        stripped = raw.rstrip("\r")
        body = stripped.strip()
        if not body or body == ">:" or body.endswith(">: "):
            # Tolerate a trailing-prompt suffix on the same line.
            body = body[: -len(">: ")].strip() if body.endswith(">: ") else body
            if not body or body == ">:":
                continue
        # Category headers are un-indented and end with ':'.
        if body.endswith(":"):
            continue
        # Skip the leading "Apps:" style banner already handled above; any
        # remaining indented line is an app entry.
        if stripped[:1] in ("\t", " "):
            saw_indent = True
            apps.append(body)
            continue
        if not saw_indent:
            # No indentation seen anywhere — keep non-header lines as a fallback
            # so firmware variants without leading TABs still surface names.
            apps.append(body)
    return apps


def _parse_info(response: str) -> dict:
    """Parse ``loader info`` → ``{"running": bool, "name": str | None}``."""
    for line in _clean_lines(response):
        m = _APP_NAME_RE.search(line)
        if m and "running" in line.lower():
            return {"running": True, "name": m.group(1)}
        if "no application is running" in line.lower():
            return {"running": False, "name": None}
    # Unrecognized output: report idle rather than raise (lenient).
    log.debug("loader_info: unrecognized response %r", response)
    return {"running": False, "name": None}


def _parse_close(response: str) -> dict:
    """Parse ``loader close`` → ``{"closed": bool, "name": str|None, "detail": str}``.

    Three documented replies:
      - 'Application "<name>" was closed'              → closed=True
      - 'No application is running'                    → closed=False, name=None
      - 'Application "<name>" has to be closed manually'→ closed=False, name set
    """
    for line in _clean_lines(response):
        lower = line.lower()
        m = _APP_NAME_RE.search(line)
        if m and "was closed" in lower:
            return {"closed": True, "name": m.group(1), "detail": line}
        if m and "closed manually" in lower:
            return {"closed": False, "name": m.group(1), "detail": line}
        if "no application is running" in lower:
            return {"closed": False, "name": None, "detail": line}
    # Unrecognized output: report not-closed with the raw text as detail.
    detail = " ".join(_clean_lines(response)) or response.strip()
    log.debug("loader_close: unrecognized response %r", response)
    return {"closed": False, "name": None, "detail": detail}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _loader_list_handler(
    flipper: FlipperConnection,
    params: LoaderListParams,  # noqa: ARG001 — no params; signature must match
) -> dict:
    """List the apps the loader knows about via ``loader list``."""
    cmd = "loader list"
    log.debug("loader_list: %r", cmd)
    response = await flipper.send_command(cmd, timeout=5.0, retry_if_empty=True)
    apps = _parse_list(response)
    log.debug("loader_list: %d app(s) parsed", len(apps))
    return {"raw": response, "apps": apps}


async def _loader_info_handler(
    flipper: FlipperConnection,
    params: LoaderInfoParams,  # noqa: ARG001 — no params; signature must match
) -> dict:
    """Report whether an app is running via ``loader info``."""
    cmd = "loader info"
    log.debug("loader_info: %r", cmd)
    response = await flipper.send_command(cmd, timeout=5.0, retry_if_empty=True)
    result = _parse_info(response)
    log.debug("loader_info: running=%s name=%r", result["running"], result["name"])
    return result


async def _loader_close_handler(
    flipper: FlipperConnection,
    params: LoaderCloseParams,  # noqa: ARG001 — no params; signature must match
) -> dict:
    """Close the running app via ``loader close``."""
    cmd = "loader close"
    log.debug("loader_close: %r", cmd)
    response = await flipper.send_command(cmd, timeout=5.0, retry_if_empty=True)
    result = _parse_close(response)
    log.debug("loader_close: closed=%s name=%r", result["closed"], result["name"])
    return result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    Action(
        name="flipper_loader_list",
        description=(
            "List the applications the Flipper Zero loader knows about "
            "(via 'loader list'). Returns both the raw CLI text and a "
            "best-effort parsed list of app names (the exact grouping is "
            "firmware-dependent). Use the names with flipper_loader_open."
        ),
        params=LoaderListParams,
        handler=_loader_list_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_loader_info",
        description=(
            "Report whether an app is currently running on the Flipper Zero "
            "(via 'loader info'). Returns {running: bool, name: str|None}."
        ),
        params=LoaderInfoParams,
        handler=_loader_info_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_loader_close",
        description=(
            "Close the app currently running on the Flipper Zero "
            "(via 'loader close'). Returns {closed, name, detail}. closed is "
            "False when nothing was running or when the app must be closed "
            "manually on the device."
        ),
        params=LoaderCloseParams,
        handler=_loader_close_handler,
        emissive=False,
    )
)
