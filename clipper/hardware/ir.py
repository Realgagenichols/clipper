"""clipper.hardware.ir — Infrared TX/RX actions.

ir_tx is emissive (transmits IR signals).
ir_rx is non-emissive (receives and decodes IR signals).

Commands sent to Flipper CLI:
  ir tx <protocol> <address> <command>  → transmits an IR signal
  ir rx                                 → receive mode; returns captured signals or empty

Design note for ir_rx:
  The Flipper `ir rx` command runs until interrupted. We send it with a short
  timeout equal to timeout_s + 0.5s so send_command reads until the prompt.
  In practice the fake harness returns immediately with whatever is queued;
  on real hardware the timeout drives how long we wait before the read loop
  breaks (see FlipperConnection._send_locked — it stops at the deadline).
  A clean empty result on timeout is a success.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, field_validator

from clipper.actions import Action, register

if TYPE_CHECKING:
    from clipper.flipper import FlipperConnection

log = logging.getLogger(__name__)

# Pattern to parse captured IR signal lines from `ir rx` output.
# Example line: "NEC A:0x01 C:0x02 (42 samples)"
# Also handles: "NEC A:0x20 C:0x40"
_IR_SIGNAL_RE = re.compile(
    r"(?P<protocol>\w+)\s+A:(?P<address>[^\s]+)\s+C:(?P<command>[^\s(]+)(?:\s+\((?P<raw>[^)]+)\))?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Params models
# ---------------------------------------------------------------------------


class IrTxParams(BaseModel):
    """Parameters for flipper_ir_tx."""

    protocol: str
    address: str
    command: str


class IrRxParams(BaseModel):
    """Parameters for flipper_ir_rx."""

    timeout_s: float = 10.0

    @field_validator("timeout_s")
    @classmethod
    def validate_timeout(cls, v: float) -> float:
        if not (1.0 <= v <= 30.0):
            raise ValueError(f"timeout_s must be between 1.0 and 30.0, got {v}")
        return v


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _ir_tx_handler(
    flipper: FlipperConnection,
    params: IrTxParams,
) -> dict:
    """Send `ir tx <protocol> <address> <command>` and confirm.

    Returns:
        {"ok": True, "protocol": str, "address": str, "command": str}
    """
    cmd = f"ir tx {params.protocol} {params.address} {params.command}"
    log.debug("ir_tx: sending %r", cmd)
    await flipper.send_command(cmd)
    log.info(
        "ir_tx: sent protocol=%r address=%r command=%r",
        params.protocol,
        params.address,
        params.command,
    )
    return {
        "ok": True,
        "protocol": params.protocol,
        "address": params.address,
        "command": params.command,
    }


async def _ir_rx_handler(
    flipper: FlipperConnection,
    params: IrRxParams,
) -> dict:
    """Send `ir rx`, wait up to timeout_s + 0.5, parse captured signals.

    Note: a clean empty list on timeout is a success,
    NOT an error.

    Returns:
        {"signals": [{"protocol": str, "address": str, "command": str, "raw": str | None}]}
    """
    from clipper.hardware.feedback import activity_indicator

    timeout = params.timeout_s + 0.5
    log.debug("ir_rx: waiting %.1fs for IR signals", params.timeout_s)
    async with activity_indicator(flipper):
        response = await flipper.send_command("ir rx", timeout=timeout)

    signals = []
    for line in response.splitlines():
        m = _IR_SIGNAL_RE.search(line)
        if m:
            signals.append(
                {
                    "protocol": m.group("protocol"),
                    "address": m.group("address"),
                    "command": m.group("command"),
                    "raw": m.group("raw"),
                }
            )

    log.debug("ir_rx: captured %d signal(s)", len(signals))
    return {"signals": signals}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    Action(
        name="flipper_ir_tx",
        description=(
            "Transmit an infrared signal from the Flipper Zero. "
            "Specify the IR protocol (e.g. NEC, Samsung, RC6), address (hex), "
            "and command (hex)."
        ),
        params=IrTxParams,
        handler=_ir_tx_handler,
        emissive=True,
    )
)

register(
    Action(
        name="flipper_ir_rx",
        description=(
            "Put the Flipper Zero into IR receive mode and capture signals. "
            "Returns a (possibly empty) list of decoded signals — an empty list "
            "on timeout is a success, not an error."
        ),
        params=IrRxParams,
        handler=_ir_rx_handler,
        emissive=False,
    )
)
