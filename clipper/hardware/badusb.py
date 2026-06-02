"""clipper.hardware.badusb — Bad USB action (emissive).

**Momentum (and stock) firmware does NOT expose a `badusb` CLI command.**

Source review of `applications/main/bad_usb/` shows no `cli/` subdirectory and
no CLI-registration files — Bad USB is implemented as a GUI-only application
launched via the on-device Apps menu. There is no programmatic way to run a
DuckyScript via the text CLI.

We keep the action REGISTERED (so the surface stays stable for clients that
expect to see it) but every invocation fails fast with a clear unsupported-
firmware error. This is better than silently sending bytes that the device
ignores — the MCP client gets a meaningful error explaining the
limitation, not a generic timeout.

If a future Momentum release adds a `badusb` CLI, or if we want to support
script execution via the Loader (`loader open "Bad USB"`) plus separate
storage upload, this is the file to update.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel

from clipper.actions import Action, register

if TYPE_CHECKING:
    from clipper.flipper import FlipperConnection

log = logging.getLogger(__name__)


class BadUsbRunParams(BaseModel):
    """Parameters for flipper_badusb_run (currently unsupported — see module docstring)."""

    script: str


async def _badusb_run_handler(
    flipper: FlipperConnection,  # noqa: ARG001
    params: BadUsbRunParams,  # noqa: ARG001
) -> dict:
    """Always raises — Bad USB is GUI-only on Momentum/stock firmware."""
    raise RuntimeError(
        "Bad USB is not available via the Flipper CLI on this firmware. "
        "The bad_usb application is GUI-only — launch it from the Flipper's "
        "Apps menu and select a script file already on the device. "
        "Reading the firmware source (applications/main/bad_usb/) confirms "
        "no CLI command is registered. Track upstream changes if you need "
        "scripted Bad USB execution."
    )


register(
    Action(
        name="flipper_badusb_run",
        description=(
            "[UNSUPPORTED ON THIS FIRMWARE] Run a DuckyScript on the Flipper Zero. "
            "Momentum and stock firmware do not expose a Bad USB CLI command; "
            "calls return a clear unsupported-firmware error rather than silently "
            "failing. Use the Flipper's on-device Apps menu to run Bad USB scripts."
        ),
        params=BadUsbRunParams,
        handler=_badusb_run_handler,
        emissive=True,
    )
)
