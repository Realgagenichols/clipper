"""clipper.hardware.subghz — Sub-GHz transmit action (emissive).

Verified against Momentum mntm-012 — the firmware's `subghz tx` command form is:

    subghz tx <key_hex_3bytes> <freq_hz> <te_us> <repeat_count> <device:0|1>

Where:
  - key_hex_3bytes: 3 bytes (6 hex chars) of the protocol key, e.g. "1AAAA0"
  - freq_hz: integer frequency in Hz, e.g. 433920000
  - te_us: timing element microseconds (protocol-specific, often 200-500)
  - repeat_count: number of times to repeat
  - device: 0 = internal CC1101, 1 = external CC1101

Frequency safety check runs BEFORE any serial I/O via
`clipper.safety.assert_frequency_allowed`. Disallowed frequencies become an
ActionParamError. (Note: stock-firmware docs show a different
`subghz tx <preset> <freq_hz> <payload_hex>` form. Momentum uses the form
above. We commit to Momentum's form; if a stock device is ever supported
we'll dispatch on the firmware string.)
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, field_validator

from clipper.actions import Action, ActionParamError, register
from clipper.hardware.storage import _validate_path
from clipper.safety import FrequencyNotAllowed, assert_frequency_allowed

if TYPE_CHECKING:
    from clipper.flipper import FlipperConnection

log = logging.getLogger(__name__)

# Plausibility bounds for Sub-GHz receive. Receiving is non-emissive so it does
# NOT consult the TX emission allow-list — but a frequency the CC1101 can't tune
# is still nonsense, so we range-check the radio's usable Sub-GHz span (~300-928
# MHz). Out-of-range raises ActionParamError before any serial I/O.
_RX_FREQ_MIN_MHZ = 300.0
_RX_FREQ_MAX_MHZ = 928.0

# Cap on the receive window. The handler clamps rather than rejects so a caller
# asking for "as long as possible" gets the max instead of an error.
_RX_MAX_DURATION_S = 60.0

# Parses a "Packets received: N"-style exit line from `subghz rx` output.
_RX_PACKETS_RE = re.compile(r"[Pp]ackets?\s+received:\s*(\d+)")


# ---------------------------------------------------------------------------
# Params model
# ---------------------------------------------------------------------------


_HEX_KEY_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


class SubGhzTxParams(BaseModel):
    """Parameters for flipper_subghz_tx (Momentum CLI shape)."""

    frequency_mhz: float
    key_hex: str  # 3-byte (6 hex char) protocol key
    te_us: int = 400  # timing element in microseconds, default fits many fixed-code remotes
    repeat: int = 5
    device: Literal[0, 1] = 0  # 0 = internal CC1101, 1 = external

    @field_validator("key_hex")
    @classmethod
    def validate_key_hex(cls, v: str) -> str:
        if not _HEX_KEY_RE.match(v):
            raise ValueError(
                f"key_hex must be exactly 6 hex chars (3 bytes), got {v!r}"
            )
        return v.upper()

    @field_validator("te_us")
    @classmethod
    def validate_te(cls, v: int) -> int:
        if v < 10 or v > 10000:
            raise ValueError(f"te_us must be between 10 and 10000, got {v}")
        return v

    @field_validator("repeat")
    @classmethod
    def validate_repeat(cls, v: int) -> int:
        if v < 1 or v > 100:
            raise ValueError(f"repeat must be between 1 and 100, got {v}")
        return v


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _subghz_tx_handler(
    flipper: FlipperConnection,
    params: SubGhzTxParams,
) -> dict:
    """Assert frequency allowed, then send Momentum's `subghz tx` form.

    Raises:
        ActionParamError: if the frequency is outside all allowed ISM bands.

    Returns:
        {"ok": True, "frequency_mhz": float, "key_hex": str, "te_us": int,
         "repeat": int, "device": int}
    """
    # Safety check before touching the serial port.
    try:
        assert_frequency_allowed(params.frequency_mhz)
    except FrequencyNotAllowed as exc:
        raise ActionParamError(
            "flipper_subghz_tx",
            [{"loc": ["frequency_mhz"], "msg": str(exc), "type": "frequency_not_allowed"}],
        ) from exc

    freq_hz = int(params.frequency_mhz * 1_000_000)
    cmd = (
        f"subghz tx {params.key_hex} {freq_hz} "
        f"{params.te_us} {params.repeat} {params.device}"
    )
    log.debug("subghz_tx: sending %r", cmd)
    response = await flipper.send_command(cmd, timeout=10.0)
    for line in response.splitlines():
        s = line.strip()
        if s.lower().startswith(("err:", "error:")):
            raise RuntimeError(f"flipper rejected subghz tx: {s}")
    log.info(
        "subghz_tx: transmitted freq=%.3fMHz key=%s te=%dus repeat=%d device=%d",
        params.frequency_mhz, params.key_hex, params.te_us, params.repeat, params.device,
    )
    return {
        "ok": True,
        "frequency_mhz": params.frequency_mhz,
        "key_hex": params.key_hex,
        "te_us": params.te_us,
        "repeat": params.repeat,
        "device": params.device,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


register(
    Action(
        name="flipper_subghz_tx",
        description=(
            "Transmit a Sub-GHz signal from the Flipper Zero (Momentum CLI form). "
            "Frequency must be in an allowed ISM band (315, 433.92, 868, 915 MHz ±0.5). "
            "key_hex is the 3-byte (6 hex char) protocol key, te_us is the timing "
            "element in microseconds, repeat is how many times to send, device is "
            "0 for internal CC1101 or 1 for external."
        ),
        params=SubGhzTxParams,
        handler=_subghz_tx_handler,
        emissive=True,
    )
)


# ---------------------------------------------------------------------------
# R9 — flipper_subghz_rx (capture, NON-emissive)
# ---------------------------------------------------------------------------


class SubGhzRxParams(BaseModel):
    """Parameters for flipper_subghz_rx (Momentum `subghz rx <freq_hz> <device>`)."""

    frequency_mhz: float
    duration_seconds: float = 10.0
    external: bool = False  # False → device 0 (internal CC1101), True → device 1

    @field_validator("frequency_mhz")
    @classmethod
    def validate_frequency(cls, v: float) -> float:
        # Plausibility range only — receiving never consults the TX allow-list.
        if not (_RX_FREQ_MIN_MHZ <= v <= _RX_FREQ_MAX_MHZ):
            raise ValueError(
                f"frequency_mhz must be between {_RX_FREQ_MIN_MHZ} and "
                f"{_RX_FREQ_MAX_MHZ} MHz (CC1101 Sub-GHz range), got {v}"
            )
        return v

    @field_validator("duration_seconds")
    @classmethod
    def validate_duration(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"duration_seconds must be positive, got {v}")
        return v


async def _subghz_rx_handler(
    flipper: FlipperConnection,
    params: SubGhzRxParams,
) -> dict:
    """Capture Sub-GHz packets via `subghz rx <freq_hz> <device>`.

    Non-emissive: receiving does not transmit, so this is ungated and does NOT
    consult the emission allow-list. Frequency plausibility is enforced by the
    param model. The receive window is capped at 60s.

    Returns:
        {"raw": str, "packets": int | None, "frequency_mhz": float,
         "duration_s": float}
    """
    duration_s = min(params.duration_seconds, _RX_MAX_DURATION_S)
    device = 1 if params.external else 0
    freq_hz = round(params.frequency_mhz * 1_000_000)
    cmd = f"subghz rx {freq_hz} {device}"
    log.debug("subghz_rx: %r for %.2fs", cmd, duration_s)

    raw = await flipper.run_bounded_command(cmd, duration_s)

    packets: int | None = None
    m = _RX_PACKETS_RE.search(raw)
    if m:
        packets = int(m.group(1))
    log.info(
        "subghz_rx: freq=%.3fMHz device=%d duration=%.2fs packets=%s",
        params.frequency_mhz, device, duration_s, packets,
    )
    return {
        "raw": raw,
        "packets": packets,
        "frequency_mhz": params.frequency_mhz,
        "duration_s": duration_s,
    }


# ---------------------------------------------------------------------------
# R10 — flipper_subghz_tx_from_file (replay a .sub, emissive + gated)
# ---------------------------------------------------------------------------


class SubGhzTxFromFileParams(BaseModel):
    """Parameters for flipper_subghz_tx_from_file.

    `subghz tx_from_file <path> <repeat> <device>`. The transmit frequency is
    read from the .sub file, so the Sub-GHz allow-list cannot pre-screen it —
    the emission safety gate is the control here.
    """

    path: str  # absolute Flipper filesystem path to a .sub file
    repeat: int = 1
    external: bool = False  # False → device 0 (internal CC1101), True → device 1

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        # Reuse the storage path validator: rejects relative paths, '..'
        # traversal, control characters, over-long paths, before any serial I/O.
        path = _validate_path(v)
        # The CLI command is built by space-delimited interpolation and the
        # Flipper CLI has no argument quoting, so a path containing whitespace
        # would shift argument parsing — e.g. smuggling a different repeat/device
        # or transmitting the wrong file. Reject whitespace outright (review W2).
        if any(ch.isspace() for ch in path):
            raise ValueError("path must not contain whitespace")
        # Replay only makes sense for a .sub capture file (review W3).
        if not path.lower().endswith(".sub"):
            raise ValueError("path must point to a .sub file")
        return path

    @field_validator("repeat")
    @classmethod
    def validate_repeat(cls, v: int) -> int:
        if v < 1 or v > 100:
            raise ValueError(f"repeat must be between 1 and 100, got {v}")
        return v


async def _subghz_tx_from_file_handler(
    flipper: FlipperConnection,
    params: SubGhzTxFromFileParams,
) -> dict:
    """Replay a saved .sub via `subghz tx_from_file <path> <repeat> <device>`.

    One-shot: the command returns after transmitting, so we use send_command
    (NOT run_bounded_command). Emissive — the safety gate in Action.invoke is
    the control, since the .sub file carries its own frequency that the
    allow-list cannot inspect ahead of time.

    Returns:
        {"path": str, "repeat": int, "ok": True, "raw": str}
    """
    device = 1 if params.external else 0
    cmd = f"subghz tx_from_file {params.path} {params.repeat} {device}"
    log.debug("subghz_tx_from_file: sending %r", cmd)
    response = await flipper.send_command(cmd, timeout=10.0)
    for line in response.splitlines():
        s = line.strip()
        if s.lower().startswith(("err:", "error:")):
            raise RuntimeError(f"flipper rejected subghz tx_from_file: {s}")
    log.info(
        "subghz_tx_from_file: replayed path=%r repeat=%d device=%d",
        params.path, params.repeat, device,
    )
    return {
        "path": params.path,
        "repeat": params.repeat,
        "ok": True,
        "raw": response,
    }


register(
    Action(
        name="flipper_subghz_rx",
        description=(
            "Capture Sub-GHz packets with the Flipper Zero by listening on a "
            "frequency for a bounded window. frequency_mhz must be plausible for "
            "the CC1101 radio (~300-928 MHz). duration_seconds defaults to 10 and "
            "is capped at 60. external=True uses the external CC1101 (device 1) "
            "instead of the internal one (device 0). Non-emissive (receive only) "
            "— this does NOT transmit and is not safety-gated. Returns the raw "
            "capture text and a parsed packet count when the device reports one."
        ),
        params=SubGhzRxParams,
        handler=_subghz_rx_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_subghz_tx_from_file",
        description=(
            "Replay a saved Sub-GHz capture (.sub file) from the Flipper Zero "
            "filesystem via `subghz tx_from_file`. path must be an absolute "
            "Flipper path (validated: no '..' traversal or control chars). repeat "
            "defaults to 1; external=True uses the external CC1101 (device 1). "
            "Emissive and safety-gated: requires CLIPPER_ALLOW_EMIT/CLIPPER_SAFETY "
            "to be on. The transmit frequency comes from the file itself, so the "
            "Sub-GHz allow-list cannot pre-screen it — the safety gate is the "
            "control."
        ),
        params=SubGhzTxFromFileParams,
        handler=_subghz_tx_from_file_handler,
        emissive=True,
    )
)
