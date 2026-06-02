"""clipper.safety — Sub-GHz frequency allow-list.

Default allowed bands are ITU Region 2 ISM frequencies:
  315 MHz, 433.92 MHz, 868 MHz, 915 MHz — each with a ±0.5 MHz window.

Override via CLIPPER_SGHZ_ALLOWED_MHZ (comma-separated float values, MHz):
  CLIPPER_SGHZ_ALLOWED_MHZ=100.0,200.0

Design notes:
- Fail-fast: FrequencyNotAllowed is a typed exception that propagates
  cleanly to the MCP layer. Never swallow it here.
- The allow-list is re-read from the env var on every call so runtime env changes
  are picked up without a restart (relevant for tests).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_DEFAULT_ALLOWED_MHZ: tuple[float, ...] = (315.0, 433.92, 868.0, 915.0)
_WINDOW_MHZ = 0.5


# ---------------------------------------------------------------------------
# Safety gate — runtime toggle
# ---------------------------------------------------------------------------
#
# State-affecting actions (RF/IR/BadUSB TX, filesystem writes, key recovery)
# are blocked unless this gate is open. Canonical env var: ``CLIPPER_SAFETY``.
# Legacy alias ``CLIPPER_ALLOW_EMIT`` is still honored for one release cycle so
# existing deployments keep working — a one-shot deprecation warning is emitted
# at module import time when only the legacy var is set.
#
# ``safety_allowed()`` re-reads env on every call so the MCP-stdio server
# observes the env-var contract. ``set_safety_allowed()`` mutates the in-process
# env var, letting a host flip the gate at runtime without a restart.


def safety_allowed() -> bool:
    """True if any state-affecting action is currently permitted."""
    return (
        os.environ.get("CLIPPER_SAFETY") == "1"
        or os.environ.get("CLIPPER_ALLOW_EMIT") == "1"
    )


def set_safety_allowed(value: bool) -> bool:
    """Mutate the safety gate in-process.

    When ``value`` is True: sets ``CLIPPER_SAFETY=1`` and clears the legacy
    ``CLIPPER_ALLOW_EMIT`` so the canonical var becomes the sole source of
    truth after a flip.
    When ``value`` is False: clears both ``CLIPPER_SAFETY`` and the legacy
    ``CLIPPER_ALLOW_EMIT`` so the runtime state is unambiguously off.

    Returns the new effective value. Logs a warning when the state actually
    changes so audit reviewers can correlate UI toggles with subsequent
    gated-action attempts.
    """
    prev = safety_allowed()
    if value:
        os.environ["CLIPPER_SAFETY"] = "1"
        os.environ.pop("CLIPPER_ALLOW_EMIT", None)  # canonicalize
    else:
        os.environ.pop("CLIPPER_SAFETY", None)
        os.environ.pop("CLIPPER_ALLOW_EMIT", None)
    new = safety_allowed()
    if prev != new:
        log.warning("safety gate toggled: %s → %s", prev, new)
    return new


# Deprecated aliases — kept for one release cycle. Use safety_allowed /
# set_safety_allowed in new code. These are name bindings (not separate
# function bodies) so behavior stays identical and there's no drift risk.
emit_allowed = safety_allowed
set_emit_allowed = set_safety_allowed


# One-shot deprecation warning: fires at most once per process if the legacy
# env var is the only thing enabling the gate. The flag is checked by the
# ``test_gate_enabled_via_legacy_env_with_deprecation_log`` regression test.
_LEGACY_ENV_WARNED = False


def _warn_legacy_env_once() -> None:
    """Emit a deprecation warning if only the legacy env var is set.

    Idempotent — guarded by the module-level ``_LEGACY_ENV_WARNED`` flag.
    Tests that want to re-trigger the warning can reset the flag to False.
    """
    global _LEGACY_ENV_WARNED
    if _LEGACY_ENV_WARNED:
        return
    if (
        os.environ.get("CLIPPER_ALLOW_EMIT") == "1"
        and os.environ.get("CLIPPER_SAFETY") != "1"
    ):
        log.warning(
            "CLIPPER_ALLOW_EMIT is deprecated; use CLIPPER_SAFETY=1 instead. "
            "Both currently enable the safety gate."
        )
        _LEGACY_ENV_WARNED = True


_warn_legacy_env_once()


class FrequencyNotAllowed(Exception):
    """Raised when a requested Sub-GHz frequency is outside all allowed bands."""


def _allowed() -> tuple[float, ...]:
    """Return the current set of allowed center frequencies in MHz.

    Reads CLIPPER_SGHZ_ALLOWED_MHZ from the environment on every call.
    Raises ValueError if the override contains non-numeric values.
    """
    override = os.environ.get("CLIPPER_SGHZ_ALLOWED_MHZ")
    if not override:
        return _DEFAULT_ALLOWED_MHZ
    try:
        return tuple(float(x.strip()) for x in override.split(",") if x.strip())
    except ValueError as exc:
        raise ValueError(
            f"CLIPPER_SGHZ_ALLOWED_MHZ has invalid float values: {override!r}"
        ) from exc


def assert_frequency_allowed(mhz: float) -> None:
    """Assert that *mhz* falls within an allowed ISM band.

    Args:
        mhz: Requested frequency in MHz.

    Raises:
        FrequencyNotAllowed: if the frequency is outside every allowed band
            window (center ± _WINDOW_MHZ).
        ValueError: if CLIPPER_SGHZ_ALLOWED_MHZ env var contains invalid floats.
    """
    centers = _allowed()
    for c in centers:
        if abs(mhz - c) <= _WINDOW_MHZ:
            return
    raise FrequencyNotAllowed(
        f"frequency {mhz} MHz not in allowed bands {centers} (±{_WINDOW_MHZ} MHz)"
    )
