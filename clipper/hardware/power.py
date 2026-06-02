"""clipper.hardware.power — power control actions (emissive / gated).

Two EMISSIVE actions wrapping the Flipper Zero `power` CLI command:

  - ``flipper_power``     — `power off` | `power reboot` | `power reboot2dfu`
  - ``flipper_power_otg`` — `power 5v 1` | `power 5v 0` (external 5V OTG output)

Both are ``emissive=True`` so ``Action.invoke`` enforces the safety gate
(CLIPPER_ALLOW_EMIT / CLIPPER_SAFETY) and writes an audit entry. They change
device state — off/reboot are obviously destructive to the running session,
and toggling 5V OTG drives current to an attached peripheral.

CRITICAL — flipper_power DISCONNECTS the device:
  `power off`, `power reboot`, and `power reboot2dfu` power down / reboot the
  Flipper *immediately*, so the USB CDC serial link drops mid-command. The
  read either times out empty or — more commonly — the rebooted/re-enumerated
  port surfaces a bare OSError(errno 6, ENXIO) or serial.SerialException, which
  ``FlipperConnection._send_locked`` converts to ``FlipperDisconnected`` (and
  marks the connection dead). For these commands that is EXPECTED SUCCESS: the
  power action was triggered and the background reconnect loop will re-establish
  the link once the device is back. So the handler catches FlipperDisconnected
  and treats it as success — it must NOT propagate (which would make
  Action.invoke wrap it as a failure / audit 'error').

flipper_power_otg does NOT disconnect: it stays online while toggling the 5V
rail, so a FlipperDisconnected there is a genuine error and is allowed to
propagate to the caller.

Source: Momentum power_cli.c registers `power off|reboot|reboot2dfu` and
`power 5v <0|1>`.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel

from clipper.actions import Action, register
from clipper.flipper import FlipperDisconnected

if TYPE_CHECKING:
    from clipper.flipper import FlipperConnection

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Params models
# ---------------------------------------------------------------------------


class PowerMode(str, Enum):
    """Supported `power` subcommands that change device power state."""

    off = "off"
    reboot = "reboot"
    reboot2dfu = "reboot2dfu"


# Per-mode CLI command + the note returned to the caller.
_MODE_COMMANDS: dict[PowerMode, tuple[str, str]] = {
    PowerMode.off: (
        "power off",
        "device is powering off; the serial link will drop until it is turned back on",
    ),
    PowerMode.reboot: (
        "power reboot",
        "device is rebooting; the serial link will drop and reconnect will follow",
    ),
    PowerMode.reboot2dfu: (
        "power reboot2dfu",
        "device is rebooting into DFU mode; the CLI link will drop (no auto-reconnect in DFU)",
    ),
}


class PowerParams(BaseModel):
    """Parameters for flipper_power."""

    mode: PowerMode


class PowerOtgParams(BaseModel):
    """Parameters for flipper_power_otg."""

    enable: bool


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _power_handler(flipper: FlipperConnection, params: PowerParams) -> dict:
    """Send `power off|reboot|reboot2dfu`, tolerating the resulting disconnect.

    The command powers down / reboots the device immediately, so send_command
    typically raises FlipperDisconnected (errno-6 / SerialException -> marked
    disconnected) or returns empty. Both are SUCCESS here: the action fired and
    the reconnect loop re-establishes the link afterward. FlipperDisconnected is
    caught so it never propagates out of the handler.

    Returns:
        {"mode": str, "ok": True, "note": str}
    """
    cmd, note = _MODE_COMMANDS[params.mode]
    log.info("power: %s (expect disconnect)", cmd)
    try:
        await flipper.send_command(cmd, timeout=2.0)
    except FlipperDisconnected:
        # Expected: the device dropped the link as it rebooted / powered off.
        log.info("power: %s triggered (link dropped as expected)", cmd)
    # The device is powering off / rebooting regardless of whether the command
    # read returned cleanly or the link already dropped — proactively mark the
    # connection dead so the reconnect loop engages immediately and flipper_state
    # reports disconnected (instead of a stale connected:true until the next
    # failing tool call). Idempotent; reconnect re-establishes the link after boot.
    flipper._mark_disconnected()
    return {"mode": params.mode.value, "ok": True, "note": note}


async def _power_otg_handler(flipper: FlipperConnection, params: PowerOtgParams) -> dict:
    """Toggle the external 5V OTG output via `power 5v <0|1>`.

    Unlike off/reboot this does NOT disconnect the device, so a
    FlipperDisconnected here is a real error and is allowed to propagate.

    Returns:
        {"enabled": bool, "ok": True, "raw": str}
    """
    cmd = f"power 5v {1 if params.enable else 0}"
    log.info("power_otg: %s", cmd)
    response = await flipper.send_command(cmd, timeout=2.0)
    return {"enabled": params.enable, "ok": True, "raw": response}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    Action(
        name="flipper_power",
        description=(
            "Power off or reboot the Flipper Zero (via 'power off|reboot|"
            "reboot2dfu'). This DROPS the USB serial link immediately; the "
            "background reconnect loop re-establishes it after reboot (DFU mode "
            "does not auto-reconnect to the CLI). Returns {mode, ok, note}."
        ),
        params=PowerParams,
        handler=_power_handler,
        emissive=True,
    )
)

register(
    Action(
        name="flipper_power_otg",
        description=(
            "Enable or disable the Flipper Zero external 5V OTG output (via "
            "'power 5v 1' / 'power 5v 0'). Drives 5V to a connected peripheral. "
            "Does not reboot the device. Returns {enabled, ok, raw}."
        ),
        params=PowerOtgParams,
        handler=_power_otg_handler,
        emissive=True,
    )
)
