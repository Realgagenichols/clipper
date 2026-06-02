"""clipper.hardware.gpio — GPIO read/write actions (non-emissive).

Flipper GPIO header pins: PA4, PA6, PA7, PB2, PB3, PC0, PC1, PC3.
Any pin outside this set is rejected with ActionParamError.

Commands sent to Flipper CLI (verified against Momentum mntm-012):
  gpio mode <pin> 0      → "Pin <pin> is now an input"           + prompt
  gpio mode <pin> 1      → "Pin <pin> is now an output (low)"    + prompt
  gpio read <pin>        → "Pin <pin> <= <0|1>"                  + prompt
  gpio set <pin> <level> → "Pin <pin> => <0|1>"                  + prompt

Without an explicit `gpio mode` first, the firmware returns
`Err: pin <pin> is not set as an input.` / `... as an output.` and refuses
the operation — so each handler auto-sets the mode before its primary
command. The auto-mode is a stateful side-effect that persists for the
remainder of the Flipper session.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, field_validator

from clipper.actions import Action, register

if TYPE_CHECKING:
    from clipper.flipper import FlipperConnection

log = logging.getLogger(__name__)

# GPIO pins the Momentum CLI exposes (verified against the firmware's
# "Wrong pin name. Available pins: ..." error message). Other STM32
# GPIO pins exist on the MCU but are reserved internally (SPI to the
# display, SD card, debug, etc.) and aren't accessible via `gpio`.
_VALID_PINS: frozenset[str] = frozenset(
    {"PA4", "PA6", "PA7", "PB2", "PB3", "PC0", "PC1", "PC3"}
)


# ---------------------------------------------------------------------------
# Params models
# ---------------------------------------------------------------------------


class GpioReadParams(BaseModel):
    """Parameters for flipper_gpio_read."""

    pin: str

    @field_validator("pin")
    @classmethod
    def validate_pin(cls, v: str) -> str:
        upper = v.upper()
        if upper not in _VALID_PINS:
            msg = (
                f"invalid GPIO pin {v!r}. "
                f"Valid pins: {', '.join(sorted(_VALID_PINS))}"
            )
            raise ValueError(msg)
        return upper


class GpioWriteParams(BaseModel):
    """Parameters for flipper_gpio_write."""

    pin: str
    level: Literal[0, 1]

    @field_validator("pin")
    @classmethod
    def validate_pin(cls, v: str) -> str:
        upper = v.upper()
        if upper not in _VALID_PINS:
            msg = (
                f"invalid GPIO pin {v!r}. "
                f"Valid pins: {', '.join(sorted(_VALID_PINS))}"
            )
            raise ValueError(msg)
        return upper


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _check_err(response: str, cmd: str) -> None:
    """Raise RuntimeError if the firmware reported an error for this command."""
    for line in response.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(("err:", "error:")):
            raise RuntimeError(f"flipper rejected {cmd!r}: {stripped}")


# Real-firmware output: "Pin PC0 <= 0" (read) or "Pin PC0 => 1" (set).
_READ_LEVEL_RE = re.compile(r"Pin\s+\w+\s*<=\s*([01])")


async def _gpio_read_handler(
    flipper: FlipperConnection,
    params: GpioReadParams,
) -> dict:
    """Auto-set pin as input, then send `gpio read <pin>`.

    Returns:
        {"pin": str, "level": int}  where level is 0 or 1.
    """
    # Step 1: ensure the pin is in input mode (0). Idempotent on the device.
    mode_cmd = f"gpio mode {params.pin} 0"
    log.debug("gpio_read: %r (set input)", mode_cmd)
    mode_resp = await flipper.send_command(mode_cmd)
    _check_err(mode_resp, mode_cmd)

    # Step 2: read the level. Momentum prints "Pin <pin> <= <0|1>".
    cmd = f"gpio read {params.pin}"
    log.debug("gpio_read: %r", cmd)
    response = await flipper.send_command(cmd)
    _check_err(response, cmd)
    m = _READ_LEVEL_RE.search(response)
    if not m:
        raise RuntimeError(
            f"could not parse GPIO level from response {response!r}"
        )
    level = int(m.group(1))
    log.debug("gpio_read: pin=%s level=%d", params.pin, level)
    return {"pin": params.pin, "level": level}


async def _gpio_write_handler(
    flipper: FlipperConnection,
    params: GpioWriteParams,
) -> dict:
    """Auto-set pin as output, then send `gpio set <pin> <level>`.

    Returns:
        {"pin": str, "level": int, "ok": True}
    """
    # Step 1: ensure the pin is in output mode (1).
    mode_cmd = f"gpio mode {params.pin} 1"
    log.debug("gpio_write: %r (set output)", mode_cmd)
    mode_resp = await flipper.send_command(mode_cmd)
    _check_err(mode_resp, mode_cmd)

    # Step 2: drive the level.
    cmd = f"gpio set {params.pin} {params.level}"
    log.debug("gpio_write: %r", cmd)
    response = await flipper.send_command(cmd)
    _check_err(response, cmd)
    log.debug("gpio_write: pin=%s level=%d ok", params.pin, params.level)
    return {"pin": params.pin, "level": params.level, "ok": True}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    Action(
        name="flipper_gpio_read",
        description=(
            "Read the current logic level (0 or 1) from a Flipper Zero GPIO pin. "
            "Valid pins: PA4, PA6, PA7, PB2, PB3, PC0, PC1, PC3."
        ),
        params=GpioReadParams,
        handler=_gpio_read_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_gpio_write",
        description=(
            "Set a Flipper Zero GPIO pin HIGH (1) or LOW (0). "
            "Valid pins: PA4, PA6, PA7, PB2, PB3, PC0, PC1, PC3."
        ),
        params=GpioWriteParams,
        handler=_gpio_write_handler,
        emissive=False,
    )
)
