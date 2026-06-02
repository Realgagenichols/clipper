"""clipper.audit — Structured audit log for emissive actions.

Writes JSON-line entries to ~/.clipper/audit.log (or CLIPPER_AUDIT_PATH override).
Each entry contains: ts (ms epoch), transport, action, params, outcome, and
optionally detail.

SECURITY NOTE: The params field is logged verbatim. This is safe for all
current actions (frequencies, IR codes, GPIO pins — none are sensitive).
If a future action adds sensitive params (tokens, passphrases, etc.), that
action's audit call MUST redact before passing params here.

Thread-safety: Python's logging.FileHandler is thread-safe (internal lock),
so concurrent async requests are handled correctly.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger("clipper.audit")
_configured = False


def _default_path() -> Path:
    override = os.environ.get("CLIPPER_AUDIT_PATH")
    if override:
        return Path(override)
    return Path.home() / ".clipper" / "audit.log"


def configure(path: Path | None = None) -> None:
    """Attach a FileHandler to the audit logger.

    Idempotent — safe to call repeatedly; subsequent calls are no-ops.
    """
    global _configured
    if _configured:
        return
    if path is None:
        path = _default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False
    _configured = True


def log(
    *,
    transport: str,
    action: str,
    params: dict[str, Any],
    outcome: str,
    detail: str | None = None,
) -> None:
    """Write one JSON-line audit entry.

    Args:
        transport: Caller transport — "http", "mcp", or "ui".
        action:    Action name.
        params:    Raw (unredacted) params dict. See module-level SECURITY NOTE.
        outcome:   One of "ok", "denied", "error".
        detail:    Optional free-text detail (e.g. reason for denial, error str).
    """
    configure()
    entry: dict[str, Any] = {
        "ts": int(time.time() * 1000),
        "transport": transport,
        "action": action,
        "params": params,
        "outcome": outcome,
    }
    if detail is not None:
        entry["detail"] = detail
    _logger.info(json.dumps(entry, sort_keys=True))


def reset_for_tests() -> None:
    """Remove all handlers and reset the configured flag.

    Call this in test teardown (or a fixture) so each test can configure
    the logger with a fresh tmp_path rather than accumulating handlers.
    """
    global _configured
    for h in list(_logger.handlers):
        _logger.removeHandler(h)
        h.close()
    _configured = False
