"""clipper.hardware.rfid — 125 kHz RFID read + emulate actions.

Commands sent to Flipper CLI:
  rfid read                       → non-emissive scan (see below).
  rfid emulate <key_type> <data>  → EMISSIVE: drives the 125 kHz coil to
                 impersonate a card until the ETX stop byte. key_type is a
                 protocol token (EM4100, H10301, Indala26, ...); key_data is
                 per-protocol hex. An unknown protocol makes the device print
                 `Unknown protocol: X` then `Available protocols:` + a list;
                 we detect that and raise so the agent learns the valid set.

  rfid read    → reads in ASK/PSK mode for ~5s; prints protocol + hex ID on
                 a single line, optionally followed by parsed fields like
                 `FC: 13` (facility code) and `Card: 482` (card number).

Momentum-verified output format (stock firmware uses the same layout):

    Reading RFID...
    Press Ctrl+C to abort
    H10301 0D01E2
    FC: 13
    Card: 482
    Reading stopped

The protocol-and-id line has no label; it's an uppercase protocol token
(EM4100, H10301, Indala26, etc.) followed by whitespace and hex bytes.

Note: no card detected is a success with {"detected": False, ...}.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, field_validator

from clipper.actions import Action, ActionRuntimeError, register
from clipper.hardware.feedback import activity_indicator

if TYPE_CHECKING:
    from clipper.flipper import FlipperConnection

log = logging.getLogger(__name__)

# An unlabeled protocol+hex line. Examples:
#   "H10301 0D01E2"
#   "EM4100 0123456789"
#   "Indala26 1A2B3C"
# Protocol = uppercase letter followed by alphanumerics; hex ID = 2+ hex chars.
_PROTOCOL_ID_RE = re.compile(
    r"^\s*(?P<proto>[A-Z][A-Z0-9_]+)\s+(?P<id>[0-9A-Fa-f]{2,}(?:\s+[0-9A-Fa-f]{2,})*)\s*$"
)
# Parsed key:value lines that Momentum (and stock) firmware prints below the
# protocol line for specific card formats — facility code, card number, etc.
_DETAIL_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _-]+?)\s*:\s*(.+?)\s*$")

# Lines we always skip when scanning for detail fields.
_SKIP_LINES = {
    "Reading RFID...",
    "Press Ctrl+C to abort",
    "Reading stopped",
    "rfid read",
}


# ---------------------------------------------------------------------------
# Params model
# ---------------------------------------------------------------------------


class RfidReadParams(BaseModel):
    """Parameters for flipper_rfid_read."""

    timeout_s: float = 10.0


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _rfid_read_handler(
    flipper: FlipperConnection,
    params: RfidReadParams,
) -> dict:
    """Send `rfid read`, parse protocol/id/details from response.

    Note: no card → all-None result with empty details
    dict and detected=False — this is success, NOT an error.

    Returns:
        {
            "detected": bool,
            "protocol": str | None,    # e.g. "H10301" or "EM4100"
            "id": str | None,           # raw hex bytes, e.g. "0D01E2"
            "details": dict[str, str],  # e.g. {"FC": "13", "Card": "482"}
        }
    """
    # The Flipper CLI's `rfid read` runs for ~5s of active scanning before
    # auto-stopping. Pad the caller's timeout to cover the prompt round-trip.
    timeout = params.timeout_s + 1.0
    log.debug("rfid_read: waiting %.1fs for RFID card", params.timeout_s)
    # Visual feedback while scanning — blue working, green on detection.
    async with activity_indicator(flipper):
        response = await flipper.send_command("rfid read", timeout=timeout)

    protocol: str | None = None
    card_id: str | None = None
    details: dict[str, str] = {}

    for raw_line in response.splitlines():
        line = raw_line.strip()
        if not line or line in _SKIP_LINES:
            continue
        # Try the protocol+id line first (no label).
        m_pi = _PROTOCOL_ID_RE.match(line)
        if m_pi and protocol is None:
            protocol = m_pi.group("proto")
            card_id = m_pi.group("id").replace(" ", "")
            continue
        # Otherwise treat it as a parsed detail field.
        m_kv = _DETAIL_RE.match(line)
        if m_kv:
            details[m_kv.group(1).strip()] = m_kv.group(2).strip()

    detected = protocol is not None or card_id is not None or bool(details)
    log.debug(
        "rfid_read: detected=%s protocol=%r id=%r details=%r",
        detected, protocol, card_id, details,
    )
    return {
        "detected": detected,
        "protocol": protocol,
        "id": card_id,
        "details": details,
    }


# ---------------------------------------------------------------------------
# R11 — flipper_rfid_emulate (emissive + gated)
# ---------------------------------------------------------------------------

# Cap on the emulation window. The handler clamps rather than rejects so a
# caller asking for "as long as possible" gets the max instead of an error.
_EMULATE_MAX_DURATION_S = 60.0

# The device prints this when it doesn't recognise the protocol token; it is
# followed by an `Available protocols:` list. Detecting either line means the
# emulation never actually started, so we surface it as an error.
_UNKNOWN_PROTO_RE = re.compile(
    r"(Unknown protocol:|Available protocols:)", re.IGNORECASE
)

# key_data is per-protocol hex; we only enforce it is non-empty hex here and
# let the device validate the per-protocol byte length.
_HEX_DATA_RE = re.compile(r"^[0-9A-Fa-f]+$")


class RfidEmulateParams(BaseModel):
    """Parameters for flipper_rfid_emulate (`rfid emulate <key_type> <data>`)."""

    key_type: str  # protocol token, e.g. "EM4100" / "H10301" / "Indala26"
    key_data: str  # per-protocol hex payload
    duration_seconds: float = 10.0

    @field_validator("key_type")
    @classmethod
    def validate_key_type(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("key_type must not be empty")
        return v

    @field_validator("key_data")
    @classmethod
    def validate_key_data(cls, v: str) -> str:
        v = v.strip()
        if not _HEX_DATA_RE.match(v):
            raise ValueError(f"key_data must be non-empty hex, got {v!r}")
        return v.upper()

    @field_validator("duration_seconds")
    @classmethod
    def validate_duration(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"duration_seconds must be positive, got {v}")
        return v


async def _rfid_emulate_handler(
    flipper: FlipperConnection,
    params: RfidEmulateParams,
) -> dict:
    """Emulate a 125 kHz card via `rfid emulate <key_type> <key_data>`.

    Emissive — the safety gate in Action.invoke is the control. Runs until the
    ETX stop byte through run_bounded_command (N2 guarantees the stop). The
    window is capped at 60s. If the device rejects the protocol it prints
    `Unknown protocol:` / `Available protocols:` instead of emulating — we
    detect that and raise ActionRuntimeError surfacing the device message so the
    agent learns the valid protocol set, rather than reporting a false success.

    Returns:
        {"key_type", "key_data", "duration_s", "emulated": True, "raw": str}
    """
    duration_s = min(params.duration_seconds, _EMULATE_MAX_DURATION_S)
    cmd = f"rfid emulate {params.key_type} {params.key_data}"
    # Do NOT log key_data at INFO (it is card credential material). The type is
    # fine to surface; the full payload only goes to DEBUG.
    log.info(
        "rfid_emulate: key_type=%s duration=%.2fs", params.key_type, duration_s
    )
    log.debug("rfid_emulate: sending %r", cmd)

    raw = await flipper.run_bounded_command(cmd, duration_s)

    if _UNKNOWN_PROTO_RE.search(raw):
        # The device rejected the protocol — surface its message verbatim so the
        # caller sees the `Available protocols:` list and can retry correctly.
        raise ActionRuntimeError("flipper_rfid_emulate", raw.strip())

    return {
        "key_type": params.key_type,
        "key_data": params.key_data,
        "duration_s": duration_s,
        "emulated": True,
        "raw": raw,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    Action(
        name="flipper_rfid_read",
        description=(
            "Read a 125 kHz RFID card with the Flipper Zero. "
            "Returns protocol and ID if a card is found; "
            "detected=False on timeout is a success, not an error."
        ),
        params=RfidReadParams,
        handler=_rfid_read_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_rfid_emulate",
        description=(
            "Emulate a 125 kHz RFID card with the Flipper Zero (via "
            "`rfid emulate <key_type> <key_data>`). key_type is a protocol "
            "token (e.g. EM4100, H10301, Indala26); key_data is the "
            "per-protocol hex payload. duration_seconds defaults to 10 and is "
            "capped at 60. Emissive and safety-gated: requires "
            "CLIPPER_ALLOW_EMIT/CLIPPER_SAFETY. If the protocol is unknown the "
            "device returns its list of available protocols and this raises an "
            "error carrying that list (so you can retry with a valid one). "
            "Returns {key_type, key_data, duration_s, emulated, raw}."
        ),
        params=RfidEmulateParams,
        handler=_rfid_emulate_handler,
        emissive=True,
        redact_params=frozenset({"key_data"}),  # never persist credential to audit log
    )
)
