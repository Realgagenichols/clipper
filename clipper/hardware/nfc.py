"""clipper.hardware.nfc — NFC scan action (non-emissive).

Verified against Momentum mntm-012 by reading `applications/main/nfc/cli/`:

- `nfc` is a CLI command that enters a sub-shell. cli_shell_set_prompt is
  called with NFC_PROMPT = "[\\x1b[32mnfc\\x1b[0m]" so the prompt becomes
  `[nfc]>: `.
- Inside the sub-shell, the read-cards subcommand is `scanner` (NOT
  `detect`, `read`, or anything else — `applications/main/nfc/cli/commands/
  nfc_cli_command_scanner.c` defines `.name = "scanner"`).
- The `scanner` command SELF-TERMINATES on tag detection (returns to the
  `[nfc]>: ` prompt automatically). On hardware its output looks like:
      Press Ctrl+C to abort
      Protocols detected: Mifare Classic
  Only if NO tag is detected within `timeout_s` does the host need to send
  Ctrl+C (0x03) to abort. The firmware-source comment about "no auto-stop"
  was misleading; in practice the scanner stops once it identifies a tag.
- To return to the root prompt from any sub-shell, we send `exit\\r` — the
  shell-level `exit` command stops the *inner* event loop (the same
  mechanism stock CLI uses for sub-shell exit).

Flow:
  1. Send `nfc\\r`        → enter the sub-shell; consume `[nfc]>: ` prompt
  2. Send `scanner\\r`    → start scanning; reads accumulate
  3. After `timeout_s`,
     send `\\x03` (Ctrl+C) → abort the scanner; consume the response
  4. Send `exit\\r`       → leave the sub-shell; restore `>: ` prompt

Note: no tag detected within the timeout is a SUCCESS,
not an error. Returns detected=False with empty protocol list.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel

from clipper.actions import Action, register
from clipper.hardware.feedback import activity_indicator

if TYPE_CHECKING:
    from clipper.flipper import FlipperConnection

log = logging.getLogger(__name__)

# Hardware-verified output line: "Protocols detected: <name>[, <name>...]"
# (plural "Protocols", no brackets, comma-separated if multiple).
_PROTOCOL_LINE_RE = re.compile(r"Protocols?\s+detected:\s*(.+)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Params model
# ---------------------------------------------------------------------------


class NfcReadParams(BaseModel):
    """Parameters for flipper_nfc_read."""

    timeout_s: float = 10.0


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def _nfc_read_handler(
    flipper: FlipperConnection,
    params: NfcReadParams,
) -> dict:
    """Run the NFC `scanner` subcommand inside its sub-shell.

    Returns:
        {
            "detected": bool,
            "protocols": list[str],   # e.g. ["Mifare Classic"] — empty on no card
        }

    Always exits the sub-shell on the way out, even on error, so future
    serial operations land on the root prompt.
    """
    # Visual feedback wraps the ENTIRE shell entry → scan → exit cycle so the
    # `led` commands run from the root prompt where they're actually valid
    # (the [nfc] sub-shell only knows nfc-specific subcommands and silently
    # rejects `led`).
    async with activity_indicator(flipper):
        # Step 1: enter the nfc sub-shell. The response is the welcome banner +
        # `[nfc]>: ` prompt. send_command's read_until(">: ") matches the inner
        # prompt's `>: ` suffix correctly.
        await flipper.send_command("nfc", timeout=3.0)

        try:
            # Step 2: kick off the scanner. It runs indefinitely, so we don't
            # wait for a prompt — we issue the bytes and then let scanned cards
            # accumulate in the serial buffer for `timeout_s` seconds.
            scanner_buf = await _scan_for(flipper, params.timeout_s)
        finally:
            # Step 3: abort the scanner (Ctrl+C) — even on exception above so
            # the device isn't left scanning.
            try:
                await flipper.send_command("\x03", timeout=2.0)
            except Exception:
                log.warning("failed to send Ctrl+C to nfc scanner", exc_info=True)
            # Step 4: leave the sub-shell so the LED commands in the
            # activity_indicator exit path run from the root prompt.
            try:
                await flipper.send_command("exit", timeout=2.0)
            except Exception:
                log.warning("failed to exit nfc sub-shell", exc_info=True)

    protocols: list[str] = []
    for line in scanner_buf.splitlines():
        m = _PROTOCOL_LINE_RE.search(line)
        if m:
            # Multiple protocols on one line are comma-separated
            for name in m.group(1).split(","):
                name = name.strip()
                if name:
                    protocols.append(name)

    detected = bool(protocols)
    log.debug("nfc_read: detected=%s protocols=%r", detected, protocols)
    return {"detected": detected, "protocols": protocols}


async def _scan_for(flipper: FlipperConnection, seconds: float) -> str:
    """Send `scanner` and accumulate bytes for `seconds`, then return decoded.

    Sends the command directly through send_command, which times out after
    `seconds + 1`. Because the scanner never prints its own prompt, the
    send_command read loop will hit its timeout warning — that's expected
    and not an error here.
    """
    try:
        # send_command will time out (no prompt arrives during scanning),
        # but it returns whatever it accumulated. That's exactly what we want.
        return await flipper.send_command("scanner", timeout=seconds + 1.0)
    except asyncio.CancelledError:
        raise


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


register(
    Action(
        name="flipper_nfc_read",
        description=(
            "Scan for NFC tags with the Flipper Zero. Enters the `nfc` sub-shell, "
            "runs `scanner` for `timeout_s` seconds, aborts, and returns the list "
            "of detected protocols. detected=False with empty protocols is a "
            "success (no card present), not an error."
        ),
        params=NfcReadParams,
        handler=_nfc_read_handler,
        emissive=False,
    )
)
