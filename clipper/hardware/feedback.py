"""clipper.hardware.feedback — on-device feedback actions + auto-feedback helper.

Three user-invocable actions:
  - flipper_led_set    — control a notification LED channel or backlight
  - flipper_vibro_set  — toggle the vibration motor
  - flipper_loader_open — launch an app on the Flipper screen

Plus an ``activity_indicator`` async context manager that long-running scans
(NFC, RFID, IR rx) wrap themselves in: blue LED at start, green flash on
success, red flash on error, cleared at end. So when Claude triggers a scan
you can SEE the device is doing something.

Quick actions (flipper_state, gpio_read) deliberately do NOT use the
indicator — they complete in <100ms and the LED pulse would dominate
latency without being perceptible.

All commands verified via Momentum source (cli_main_commands.c):
  led <r|g|b|bl> <0-255>
  vibro <0|1>
  loader open <app_name>
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, field_validator, model_validator

from clipper.actions import Action, register

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from clipper.flipper import FlipperConnection

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level helpers (used by both the action handlers and the auto-feedback CM)
# ---------------------------------------------------------------------------


async def _led(flipper: FlipperConnection, channel: str, level: int) -> None:
    """Set a single LED channel. Silent on failure — feedback is best-effort."""
    try:
        await flipper.send_command(f"led {channel} {level}", timeout=1.5)
    except Exception:
        log.debug("led %s %d failed", channel, level, exc_info=True)


async def _all_off(flipper: FlipperConnection) -> None:
    """Turn off r, g, b channels. Backlight is left alone (it's user-controlled)."""
    for ch in ("r", "g", "b"):
        await _led(flipper, ch, 0)


async def _set_pure(flipper: FlipperConnection, channel: str, level: int) -> None:
    """Set ONE channel and explicitly clear the others.

    The Flipper notification system runs a charging-status sequence that
    asserts green periodically while on USB power. Setting `led b 64`
    without clearing green produces cyan, not blue. So we clear r/g/b
    (except the channel we're setting) before/during the set.

    Note: this can't fully defeat the firmware's periodic charging
    notification — green may reassert within a few seconds. The brief
    window between our set and the next firmware reassertion is usually
    long enough for a human to perceive.
    """
    other_channels = [c for c in ("r", "g", "b") if c != channel]
    for ch in other_channels:
        await _led(flipper, ch, 0)
    await _led(flipper, channel, level)


@asynccontextmanager
async def activity_indicator(
    flipper: FlipperConnection,
    *,
    working_color: str = "b",
    working_level: int = 128,
    success_color: str = "g",
    success_level: int = 200,
    error_color: str = "r",
    error_level: int = 200,
    flash_ms: int = 350,
) -> AsyncIterator[None]:
    """Visual indicator that wraps a long-running operation.

    Sequence:
      enter         → clear g/r + LED <working_color> at <working_level>
      success exit  → clear r/b + LED <success_color> pulse, then all off
      error exit    → clear g/b + LED <error_color> pulse, then all off, re-raise

    All LED calls are best-effort. A failed LED command will NOT mask the
    underlying operation's exception. The firmware's charging notification
    may reassert green within a few seconds; we use brighter levels (>= 128)
    and a flash window (350ms) to maximize the visibility window.
    """
    await _set_pure(flipper, working_color, working_level)
    try:
        yield
    except Exception:
        await _set_pure(flipper, error_color, error_level)
        await asyncio.sleep(flash_ms / 1000.0)
        await _all_off(flipper)
        raise
    else:
        await _set_pure(flipper, success_color, success_level)
        await asyncio.sleep(flash_ms / 1000.0)
        await _all_off(flipper)


# ---------------------------------------------------------------------------
# flipper_led_set
# ---------------------------------------------------------------------------


class LedSetParams(BaseModel):
    """Parameters for flipper_led_set."""

    channel: Literal["r", "g", "b", "bl"]
    level: int

    @field_validator("level")
    @classmethod
    def validate_level(cls, v: int) -> int:
        if not 0 <= v <= 255:
            raise ValueError(f"level must be 0-255, got {v}")
        return v


async def _led_set_handler(flipper: FlipperConnection, params: LedSetParams) -> dict:
    cmd = f"led {params.channel} {params.level}"
    log.debug("led_set: %r", cmd)
    await flipper.send_command(cmd, timeout=2.0)
    return {"channel": params.channel, "level": params.level, "ok": True}


register(
    Action(
        name="flipper_led_set",
        description=(
            "Set ONE channel of the Flipper Zero LED to a specific intensity, "
            "leaving the other channels at their current values. "
            "channel: r/g/b (RGB notification LED) or bl (display backlight). "
            "level: 0-255 (0 = off, 255 = max). "
            "For changing the LED to a pure color, prefer flipper_led_color "
            "(which clears the other channels first)."
        ),
        params=LedSetParams,
        handler=_led_set_handler,
        emissive=False,
    )
)


# ---------------------------------------------------------------------------
# flipper_led_color — "make it red" without color-mixing surprises
# ---------------------------------------------------------------------------


class LedColorParams(BaseModel):
    """Parameters for flipper_led_color.

    Two interchangeable input forms:
      - Named color: ``color="red"`` (one of the 8 presets), optional ``level`` (0-255).
      - Direct RGB: ``r``, ``g``, ``b`` (each 0-255), optional ``brightness`` (0-255
        master scale applied to all three channels).
    Exactly one form must be provided. RGB takes precedence if both are present.
    """

    color: Literal["red", "green", "blue", "yellow", "cyan", "magenta", "white", "off"] | None = (
        None
    )
    level: int = 200
    r: int | None = None
    g: int | None = None
    b: int | None = None
    brightness: int = 255

    @field_validator("level", "brightness")
    @classmethod
    def validate_0_255(cls, v: int) -> int:
        if not 0 <= v <= 255:
            raise ValueError(f"value must be 0-255, got {v}")
        return v

    @field_validator("r", "g", "b")
    @classmethod
    def validate_channel(cls, v: int | None) -> int | None:
        if v is not None and not 0 <= v <= 255:
            raise ValueError(f"channel value must be 0-255, got {v}")
        return v

    @model_validator(mode="after")
    def validate_one_form(self) -> LedColorParams:
        rgb_provided = self.r is not None or self.g is not None or self.b is not None
        rgb_complete = self.r is not None and self.g is not None and self.b is not None
        if rgb_provided and not rgb_complete:
            raise ValueError("must provide all of r, g, b together (or none)")
        if self.color is None and not rgb_complete:
            raise ValueError("must provide either 'color' or all of r, g, b")
        return self


async def _led_color_handler(flipper: FlipperConnection, params: LedColorParams) -> dict:
    """Set the RGB LED, clearing channels that should be off.

    The Flipper's notification system asserts green periodically while
    USB-charging — without clearing other channels first, "red" produces
    yellow. This handler always sets every r/g/b channel explicitly so
    the displayed color matches the request.
    """
    if params.r is not None and params.g is not None and params.b is not None:
        # Direct RGB form: scale each channel by brightness/255 master fader.
        scale = params.brightness / 255.0
        levels = {
            "r": int(round(params.r * scale)),
            "g": int(round(params.g * scale)),
            "b": int(round(params.b * scale)),
        }
        log.debug("led_color: rgb=(%d,%d,%d) brightness=%d → %r",
                  params.r, params.g, params.b, params.brightness, levels)
    else:
        # Named color form: each active channel at `level`, others off.
        channel_map: dict[str, tuple[str, ...]] = {
            "off": (),
            "red": ("r",),
            "green": ("g",),
            "blue": ("b",),
            "yellow": ("r", "g"),
            "cyan": ("g", "b"),
            "magenta": ("r", "b"),
            "white": ("r", "g", "b"),
        }
        active = channel_map[params.color]  # type: ignore[index]
        log.debug("led_color: color=%s level=%d active=%s", params.color, params.level, active)
        levels = {ch: (params.level if ch in active else 0) for ch in ("r", "g", "b")}

    for ch in ("r", "g", "b"):
        await _led(flipper, ch, levels[ch])
    return {"r": levels["r"], "g": levels["g"], "b": levels["b"], "ok": True}


register(
    Action(
        name="flipper_led_color",
        description=(
            "Set the Flipper Zero notification LED to a color, clearing channels "
            "that aren't part of that color. Use this for \"make the LED red\" "
            "requests — flipper_led_set leaves other channels alone, which mixes "
            "with the firmware's green charging indicator. "
            "Two input forms (exactly one required): "
            "(1) NAMED COLOR — color: red/green/blue/yellow/cyan/magenta/white/off, "
            "optional level (0-255, default 200). "
            "(2) DIRECT RGB — r/g/b (each 0-255), optional brightness (0-255 "
            "master scale, default 255). Use this for arbitrary colors like "
            "orange or pink. "
            "NOTE: the firmware reasserts green periodically on USB power, "
            "so a chosen color may revert to green within ~1-3 seconds."
        ),
        params=LedColorParams,
        handler=_led_color_handler,
        emissive=False,
    )
)


# ---------------------------------------------------------------------------
# flipper_vibro_set
# ---------------------------------------------------------------------------


class VibroSetParams(BaseModel):
    """Parameters for flipper_vibro_set."""

    on: bool


async def _vibro_set_handler(flipper: FlipperConnection, params: VibroSetParams) -> dict:
    cmd = f"vibro {1 if params.on else 0}"
    log.debug("vibro_set: %r", cmd)
    await flipper.send_command(cmd, timeout=2.0)
    return {"on": params.on, "ok": True}


register(
    Action(
        name="flipper_vibro_set",
        description=(
            "Turn the Flipper Zero vibration motor on or off. "
            "State persists until changed — don't forget to turn it off."
        ),
        params=VibroSetParams,
        handler=_vibro_set_handler,
        emissive=False,
    )
)


# ---------------------------------------------------------------------------
# flipper_loader_open
# ---------------------------------------------------------------------------


class LoaderOpenParams(BaseModel):
    """Parameters for flipper_loader_open."""

    app_name: str

    @field_validator("app_name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("app_name must not be empty")
        # Block command-injection: app names shouldn't contain shell metacharacters
        # the CLI uses for parsing. The CLI takes the rest-of-line as the name,
        # but a CR (which would split into a new command) must not be present.
        if "\r" in v or "\n" in v:
            raise ValueError("app_name must not contain newlines")
        return v


async def _loader_open_handler(flipper: FlipperConnection, params: LoaderOpenParams) -> dict:
    cmd = f"loader open {params.app_name}"
    log.debug("loader_open: %r", cmd)
    response = await flipper.send_command(cmd, timeout=3.0)
    for line in response.splitlines():
        s = line.strip()
        if s.lower().startswith(("err:", "error:", "could not find")):
            raise RuntimeError(f"flipper rejected loader open {params.app_name!r}: {s}")
    return {"app_name": params.app_name, "ok": True}


register(
    Action(
        name="flipper_loader_open",
        description=(
            "Open an app on the Flipper Zero screen. Useful for visible "
            "confirmation that clipper is interacting with the device. "
            'Examples of app names: "NFC", "Sub-GHz", "Infrared", "GPIO", '
            '"Settings". Names are firmware-dependent — case and spacing matter.'
        ),
        params=LoaderOpenParams,
        handler=_loader_open_handler,
        emissive=False,
    )
)
