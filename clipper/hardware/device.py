"""clipper.hardware.device — device read actions (state + diagnostics).

Registered automatically when this module is imported (which happens via
`from clipper.hardware import device` at the bottom of clipper/actions.py).

Actions (all NON-emissive / ungated — they only read device state):
  - ``flipper_state``       — cached connection/firmware/battery snapshot
  - ``flipper_uptime``      — `uptime`  → {raw, uptime}
  - ``flipper_datetime``    — `date`    → {raw, datetime}  (no args = read RTC)
  - ``flipper_diagnostics`` — `free` + `ps` → {free:{raw,...}, ps:{raw,tasks}}

The exact CLI output formats for uptime/date/free/ps are NOT confirmed for this
firmware build, so parsing is deliberately lenient/firmware-variant tolerant:
the RAW command text is always returned (it is the contract), and best-effort
parsed fields degrade to None / empty list / partial dict rather than raising
on unexpected output.
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


def _clean_lines(response: str) -> list[str]:
    """Split *response* into stripped, non-empty lines with the CLI prompt removed.

    The CLI host loop emits ``>: `` after each command; drop the bare prompt
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
# Params model — flipper_state takes no inputs
# ---------------------------------------------------------------------------


class StateParams(BaseModel):
    """No parameters required for reading device state."""


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _flipper_state_handler(
    flipper: FlipperConnection,
    params: StateParams,  # noqa: ARG001 — no params used but signature must match
) -> dict:
    """Return device name, firmware version, battery level, and connection status.

    Reads the cached connection state, which the rest of the layer keeps
    accurate: any failing serial I/O marks the connection disconnected, the
    power tools mark it on reboot/off, and the reconnect loop only sets it back
    to connected after a verified handshake. When not connected we return
    connected:false and NULL device info — never stale name/firmware from a
    previous session (a dead link must not look alive).

    Returns:
        {
            "connected": bool,
            "name": str | None,
            "firmware": str | None,
            "battery": int | None,
        }
    """
    if not flipper.connected:
        log.debug("flipper_state: not connected — reporting connected:false")
        return {"connected": False, "name": None, "firmware": None, "battery": None}

    connected = flipper.connected
    device_info = flipper.device_info

    name: str | None = device_info.get("hardware_name")
    # Different builds expose different firmware keys: stock uses
    # `firmware_version`, Momentum / Unleashed use `firmware_commit` or
    # `firmware_branch`. Prefer whatever's present in priority order.
    firmware: str | None = (
        device_info.get("firmware_version")
        or device_info.get("firmware_branch")
        or device_info.get("firmware_commit")
    )
    battery: int | None = flipper.battery

    log.debug(
        "flipper_state: connected=%s name=%r firmware=%r battery=%r",
        connected,
        name,
        firmware,
        battery,
    )

    return {
        "connected": connected,
        "name": name,
        "firmware": firmware,
        "battery": battery,
    }


# ---------------------------------------------------------------------------
# Diagnostics param models — all take no inputs
# ---------------------------------------------------------------------------


class UptimeParams(BaseModel):
    """No parameters required to read device uptime."""


class DateTimeParams(BaseModel):
    """No parameters required to read the device RTC."""


class DiagnosticsParams(BaseModel):
    """No parameters required to read heap/task diagnostics."""


# ---------------------------------------------------------------------------
# Lenient parsers (raw text is the contract; parsed fields are a bonus)
# ---------------------------------------------------------------------------

# Recognize an "uptime"-style value, e.g. "Uptime: 0d0h12m34s" or "12m34s".
_UPTIME_RE = re.compile(
    r"(?:uptime[:\s]+)?((?:\d+\s*[dhms]\s*)+|(?:\d+:){1,2}\d+)",
    re.IGNORECASE,
)

# Recognize a date/time value, e.g. "2026-06-03 14:22:07" or "14:22:07".
_DATETIME_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?|\d{1,2}:\d{2}(?::\d{2})?)"
)

# Heap-stat lines like "Free heap size: 102400" → ("Free heap size", 102400).
_HEAP_LINE_RE = re.compile(r"^(.*?)[:=]\s*(\d+)\b")

# Map a normalized heap label fragment to the field name we expose.
_HEAP_FIELDS: tuple[tuple[str, str], ...] = (
    ("minimum free", "min_free_heap"),
    ("min free", "min_free_heap"),
    ("free", "free_heap"),
    ("total", "total_heap"),
    ("max", "max_block"),
    ("largest", "max_block"),
)


def _parse_uptime(response: str) -> str | None:
    """Best-effort: pull a human-readable uptime value out of *response*.

    Returns None (not an exception) if nothing recognizable is present.
    """
    for line in _clean_lines(response):
        m = _UPTIME_RE.search(line)
        if m:
            return m.group(1).strip()
    log.debug("uptime: unrecognized response %r", response)
    return None


def _parse_datetime(response: str) -> str | None:
    """Best-effort: pull a date/time value out of *response*.

    Returns None (not an exception) if nothing recognizable is present.
    """
    for line in _clean_lines(response):
        m = _DATETIME_RE.search(line)
        if m:
            return m.group(1).strip()
    log.debug("datetime: unrecognized response %r", response)
    return None


def _parse_free(response: str) -> dict:
    """Best-effort parse of ``free`` heap stats → ``{raw, ...numeric fields...}``.

    Recognized labels map to free_heap / total_heap / min_free_heap / max_block.
    Unfamiliar output simply yields ``{"raw": ...}`` with no numeric fields.
    """
    result: dict = {"raw": response}
    for line in _clean_lines(response):
        m = _HEAP_LINE_RE.match(line)
        if not m:
            continue
        label = m.group(1).strip().lower()
        value = int(m.group(2))
        for fragment, field in _HEAP_FIELDS:
            if fragment in label and field not in result:
                result[field] = value
                break
    return result


def _parse_ps(response: str) -> dict:
    """Best-effort parse of ``ps`` task list → ``{raw, tasks:[...]}``.

    Each task is ``{"name": str, "raw": <line>}`` with any trailing integer
    columns captured as ``fields`` when present. A header line (no digits, or
    containing the word "name"/"stack") is skipped. Unfamiliar output yields an
    empty ``tasks`` list — never an exception.
    """
    result: dict = {"raw": response, "tasks": []}
    # Some firmware builds (e.g. Momentum mntm-012) have no `ps` command — the
    # CLI replies "could not find command 'ps', did you mean ...". Detect that
    # sentinel and report ps as unsupported instead of parsing the error as a
    # bogus task.
    if "could not find command" in response.lower():
        result["supported"] = False
        return result
    for line in _clean_lines(response):
        lower = line.lower()
        # Skip obvious header rows.
        if ("name" in lower and "stack" in lower) or lower.startswith("name"):
            continue
        parts = line.split()
        if not parts:
            continue
        task: dict = {"name": parts[0], "raw": line}
        fields = [int(p) for p in parts[1:] if p.lstrip("-").isdigit()]
        if fields:
            task["fields"] = fields
        result["tasks"].append(task)
    return result


# ---------------------------------------------------------------------------
# Diagnostics handlers
# ---------------------------------------------------------------------------


async def _flipper_uptime_handler(
    flipper: FlipperConnection,
    params: UptimeParams,  # noqa: ARG001 — no params; signature must match
) -> dict:
    """Read device uptime via ``uptime``. Returns ``{raw, uptime}``."""
    cmd = "uptime"
    log.debug("flipper_uptime: %r", cmd)
    response = await flipper.send_command(cmd, retry_if_empty=True)
    uptime = _parse_uptime(response)
    log.debug("flipper_uptime: parsed=%r", uptime)
    return {"raw": response, "uptime": uptime}


async def _flipper_datetime_handler(
    flipper: FlipperConnection,
    params: DateTimeParams,  # noqa: ARG001 — no params; signature must match
) -> dict:
    """Read the device RTC via bare ``date`` (no args = read). Returns ``{raw, datetime}``."""
    cmd = "date"
    log.debug("flipper_datetime: %r", cmd)
    response = await flipper.send_command(cmd, retry_if_empty=True)
    dt = _parse_datetime(response)
    log.debug("flipper_datetime: parsed=%r", dt)
    return {"raw": response, "datetime": dt}


async def _flipper_diagnostics_handler(
    flipper: FlipperConnection,
    params: DiagnosticsParams,  # noqa: ARG001 — no params; signature must match
) -> dict:
    """Read heap stats (``free``) and the task list (``ps``).

    Returns ``{"free": {raw, ...}, "ps": {raw, tasks:[...]}}``. Both commands are
    read-only, so each uses ``retry_if_empty=True`` (lesson L1).
    """
    log.debug("flipper_diagnostics: free + ps")
    free_resp = await flipper.send_command("free", retry_if_empty=True)
    ps_resp = await flipper.send_command("ps", retry_if_empty=True)
    free = _parse_free(free_resp)
    ps = _parse_ps(ps_resp)
    log.debug("flipper_diagnostics: %d task(s) parsed", len(ps["tasks"]))
    return {"free": free, "ps": ps}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    Action(
        name="flipper_state",
        description=(
            "Return the current Flipper Zero connection state, device name, "
            "firmware version, and battery level."
        ),
        params=StateParams,
        handler=_flipper_state_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_uptime",
        description=(
            "Read how long the Flipper Zero has been running since its last "
            "boot (via 'uptime'). Returns the raw CLI text plus a best-effort "
            "parsed uptime string (None if the format is unrecognized)."
        ),
        params=UptimeParams,
        handler=_flipper_uptime_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_datetime",
        description=(
            "Read the Flipper Zero's real-time clock (via a bare 'date' with no "
            "arguments — this reads, it does not set). Returns the raw CLI text "
            "plus a best-effort parsed datetime string (None if unrecognized)."
        ),
        params=DateTimeParams,
        handler=_flipper_datetime_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_diagnostics",
        description=(
            "Read low-level device diagnostics: heap statistics (via 'free') and "
            "the running task list (via 'ps'). Returns "
            "{free: {raw, ...heap fields...}, ps: {raw, tasks: [...]}}. Raw text "
            "is always included; parsed fields are best-effort and "
            "firmware-variant tolerant."
        ),
        params=DiagnosticsParams,
        handler=_flipper_diagnostics_handler,
        emissive=False,
    )
)
