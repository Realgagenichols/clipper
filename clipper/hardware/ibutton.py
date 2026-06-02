"""clipper.hardware.ibutton — iButton / 1-Wire emulation action (emissive).

Command sent to Flipper CLI:
  ikey emulate <key_type> <key_data>  → EMISSIVE: drives the 1-Wire contact to
                 impersonate an iButton key until the ETX stop byte arrives.

Note the CLI verb is ``ikey`` (NOT ``ibutton``) on both stock and Momentum
firmware. Supported key types (case-insensitive at the param level here):

  - Dallas   — 8-byte key (DS1990A and family)
  - Cyfral   — 2-byte key
  - Metakom  — 4-byte key

key_data is the per-type hex payload; the device validates the exact byte
length, so we only enforce non-empty hex here and surface any device error
lines (``err:`` / ``error:``) as an ActionRuntimeError.

Emissive=True, so Action.invoke enforces the safety gate
(CLIPPER_ALLOW_EMIT / CLIPPER_SAFETY) and writes an audit entry. Runs via
run_bounded_command, which guarantees the ETX stop (N2).
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, field_validator

from clipper.actions import Action, ActionRuntimeError, register

if TYPE_CHECKING:
    from clipper.flipper import FlipperConnection

log = logging.getLogger(__name__)

# Cap on the emulation window. The handler clamps rather than rejects so a
# caller asking for "as long as possible" gets the max instead of an error.
_EMULATE_MAX_DURATION_S = 60.0

# key_data is per-type hex; we only enforce non-empty hex here and let the
# device validate the per-type byte length.
_HEX_DATA_RE = re.compile(r"^[0-9A-Fa-f]+$")


# ---------------------------------------------------------------------------
# Params model
# ---------------------------------------------------------------------------


class IButtonKeyType(str, Enum):
    """Supported iButton key types. Value is the CLI token the device expects."""

    dallas = "Dallas"
    cyfral = "Cyfral"
    metakom = "Metakom"


# Case-insensitive lookup from any-case input to the canonical enum member.
_KEY_TYPE_BY_LOWER = {kt.value.lower(): kt for kt in IButtonKeyType}


class IButtonEmulateParams(BaseModel):
    """Parameters for flipper_ibutton_emulate (`ikey emulate <type> <data>`)."""

    key_type: IButtonKeyType  # Dallas | Cyfral | Metakom (case-insensitive in)
    key_data: str  # per-type hex payload
    duration_seconds: float = 10.0

    @field_validator("key_type", mode="before")
    @classmethod
    def normalize_key_type(cls, v: object) -> object:
        # Accept any case (e.g. "dallas", "DALLAS") and map to the canonical
        # member; leave unknown values to the enum to reject with a clear error.
        if isinstance(v, str):
            return _KEY_TYPE_BY_LOWER.get(v.strip().lower(), v)
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


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _ibutton_emulate_handler(
    flipper: FlipperConnection,
    params: IButtonEmulateParams,
) -> dict:
    """Emulate an iButton key via `ikey emulate <key_type> <key_data>`.

    Emissive — the safety gate in Action.invoke is the control. Runs until the
    ETX stop byte through run_bounded_command (N2 guarantees the stop). The
    window is capped at 60s. Device error lines (``err:`` / ``error:``) are
    surfaced as an ActionRuntimeError rather than reported as a false success.

    Returns:
        {"key_type", "key_data", "duration_s", "emulated": True, "raw": str}
    """
    duration_s = min(params.duration_seconds, _EMULATE_MAX_DURATION_S)
    cmd = f"ikey emulate {params.key_type.value} {params.key_data}"
    # Do NOT log key_data at INFO (it is credential material). The type is fine
    # to surface; the full payload only goes to DEBUG.
    log.info(
        "ibutton_emulate: key_type=%s duration=%.2fs",
        params.key_type.value,
        duration_s,
    )
    log.debug("ibutton_emulate: sending %r", cmd)

    raw = await flipper.run_bounded_command(cmd, duration_s)

    for line in raw.splitlines():
        s = line.strip()
        if s.lower().startswith(("err:", "error:")):
            raise ActionRuntimeError("flipper_ibutton_emulate", s)

    return {
        "key_type": params.key_type.value,
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
        name="flipper_ibutton_emulate",
        description=(
            "Emulate an iButton / 1-Wire key with the Flipper Zero (via "
            "`ikey emulate <key_type> <key_data>`). key_type is one of Dallas "
            "(8 bytes), Cyfral (2 bytes), or Metakom (4 bytes) — case "
            "insensitive. key_data is the per-type hex payload. "
            "duration_seconds defaults to 10 and is capped at 60. Emissive and "
            "safety-gated: requires CLIPPER_ALLOW_EMIT/CLIPPER_SAFETY. Returns "
            "{key_type, key_data, duration_s, emulated, raw}."
        ),
        params=IButtonEmulateParams,
        handler=_ibutton_emulate_handler,
        emissive=True,
        redact_params=frozenset({"key_data"}),  # never persist credential to audit log
    )
)
