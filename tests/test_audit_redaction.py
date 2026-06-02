"""Regression test (review finding C1): credential params must never reach the
audit log in cleartext.

flipper_rfid_emulate / flipper_ibutton_emulate are emissive, so Action.invoke
audit-logs their params. key_data is a clonable access credential and MUST be
redacted before it is persisted to ~/.clipper/audit.log. Action.redact_params
masks it with "***".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import clipper.actions  # noqa: F401 — register the hardware actions
import clipper.audit as audit
from clipper.actions import EmissionBlocked, get

SECRET = "DEADBEEF12345678"


@pytest.fixture(autouse=True)
def isolated_audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    log_path = tmp_path / "audit.log"
    monkeypatch.setenv("CLIPPER_AUDIT_PATH", str(log_path))
    # Gate off so invoke takes the audited "denied" path without needing serial I/O.
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)
    monkeypatch.delenv("CLIPPER_SAFETY", raising=False)
    audit.reset_for_tests()
    yield log_path
    audit.reset_for_tests()


def _audit_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.mark.regression
@pytest.mark.parametrize(
    ("tool", "params"),
    [
        ("flipper_rfid_emulate", {"key_type": "EM4100", "key_data": SECRET}),
        ("flipper_ibutton_emulate", {"key_type": "Dallas", "key_data": SECRET}),
    ],
)
async def test_key_data_redacted_in_audit_log(isolated_audit, tool, params):
    """The audit entry records the attempt but NEVER the raw key_data value."""
    with pytest.raises(EmissionBlocked):
        await get(tool).invoke(None, params)  # gate off → audited denial

    text = _audit_text(isolated_audit)
    assert text, "expected a denied audit entry to be written"
    entry = json.loads(text.splitlines()[-1])

    assert entry["action"] == tool
    assert entry["outcome"] == "denied"
    # The secret must NOT appear anywhere in the persisted log.
    assert SECRET not in text
    # key_data is present but masked; key_type is fine to keep.
    assert entry["params"]["key_data"] == "***"
    assert entry["params"]["key_type"] == params["key_type"]
