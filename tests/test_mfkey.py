"""Tests for the mfkey32 pure-Python Crypto1 key-recovery action.

- Recover a single sector's KeyA (single capture → known key).
- Recover keys for multiple sectors in one call.
- Mfkey-format parse error → ActionRuntimeError.
- Empty log returns no keys (NOT an error).
- Gated by safety toggle: gate OFF raises EmissionBlocked before any decode.
- Cryptographic correctness regression: every fixture in `tests/fixtures/mfkey/`
  recovers its `expected_keys.json` entries exactly.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import clipper.actions as actions_mod
from clipper.actions import ActionRuntimeError, EmissionBlocked, get
from clipper.hardware.mfkey import (
    MfkeyCapture,
    parse_mfkey32_log,
    recover_key_from_capture,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mfkey"
EXPECTED_KEYS: dict[str, list[dict]] = json.loads(
    (FIXTURE_DIR / "expected_keys.json").read_text(encoding="utf-8")
)


# ---------------------------------------------------------------------------
# Audit log isolation — mfkey is non-emissive so audit writes don't happen,
# but the safety gate path reuses the audit module's reset machinery.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test sees the same shared registry — we want flipper_mfkey_crack
    to be available, but we shallow-copy so other tests that register/unregister
    actions don't bleed into this file (mirrors the pattern in
    test_safety_gate.py)."""
    fresh: dict = dict(actions_mod.registry)
    monkeypatch.setattr(actions_mod, "registry", fresh)


def _b64(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return base64.b64encode(text).decode("ascii")


def _all_fixture_names() -> list[str]:
    """Discover every `.log` file the regression sweep should cover."""
    return sorted(EXPECTED_KEYS.keys())


# ---------------------------------------------------------------------------
# Parser smoke — fixtures must parse to the expected capture count
# ---------------------------------------------------------------------------


def test_parse_log_extracts_captures() -> None:
    """Every fixture parses into MfkeyCapture records with valid types."""
    for fixture_name in _all_fixture_names():
        text = (FIXTURE_DIR / fixture_name).read_text(encoding="utf-8")
        captures = parse_mfkey32_log(text)
        assert len(captures) >= 1, f"{fixture_name} parsed to zero captures"
        for c in captures:
            assert isinstance(c, MfkeyCapture)
            assert c.key_half in ("A", "B")
            assert 0 <= c.sector < 40
            assert 0 <= c.uid <= 0xFFFFFFFF
            for word in (c.nt0, c.nr0, c.ar0, c.nt1, c.nr1, c.ar1):
                assert 0 <= word <= 0xFFFFFFFF


def test_parse_log_empty_input_returns_empty_list() -> None:
    """Empty input is a valid no-op ('Empty log' scenario, parser side)."""
    assert parse_mfkey32_log("") == []
    assert parse_mfkey32_log("\n\n  \n") == []


def test_parse_log_malformed_line_raises_value_error() -> None:
    """A line missing a label or with non-hex values raises ValueError —
    Action.invoke wraps this as ActionRuntimeError."""
    bad = "Sec 0 key A cuid deadbeef nt0 12345678 nr0 deadbeef"  # truncated
    with pytest.raises(ValueError, match="expected 18"):
        parse_mfkey32_log(bad)


# ---------------------------------------------------------------------------
# Algorithm smoke — at least one fixture must recover its known key
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_recover_key_from_single_capture_transport_key() -> None:
    """`synthetic_transport_key.log` contains one capture; the recovered key
    must equal `a0a1a2a3a4a5` exactly."""
    text = (FIXTURE_DIR / "synthetic_transport_key.log").read_text(encoding="utf-8")
    captures = parse_mfkey32_log(text)
    assert len(captures) == 1
    key = recover_key_from_capture(captures[0])
    assert key is not None
    assert key.hex() == "a0a1a2a3a4a5"


# ---------------------------------------------------------------------------
# Scenario: Recover a single sector's KeyA from a known capture
# ---------------------------------------------------------------------------


@pytest.mark.regression
async def test_recover_single_sector_keya(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPPER_SAFETY", "1")
    log_text = (FIXTURE_DIR / "synthetic_transport_key.log").read_text(encoding="utf-8")
    action = get("flipper_mfkey_crack")
    result = await action.invoke(
        None,
        {"log_content_b64": _b64(log_text)},
        transport="test",
    )
    assert result["keys"] == [
        {"sector": 4, "key_a": "a0a1a2a3a4a5", "key_b": None}
    ]
    assert result["stats"]["captures_parsed"] == 1
    assert result["stats"]["sectors_recovered"] == 1


# ---------------------------------------------------------------------------
# Scenario: Recover keys for multiple sectors in one call
# ---------------------------------------------------------------------------


@pytest.mark.regression
async def test_recover_multi_sector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPPER_SAFETY", "1")
    log_text = (FIXTURE_DIR / "synthetic_multi_sector.log").read_text(encoding="utf-8")
    action = get("flipper_mfkey_crack")
    result = await action.invoke(
        None,
        {"log_content_b64": _b64(log_text)},
        transport="test",
    )
    expected = EXPECTED_KEYS["synthetic_multi_sector.log"]
    assert result["keys"] == expected
    assert result["stats"]["captures_parsed"] == 3
    assert result["stats"]["sectors_recovered"] == 3


# ---------------------------------------------------------------------------
# Dedup: duplicate (sector, key_half, cuid) captures are skipped after the
# first successful recovery. Real-world .mfkey32.log files can contain
# dozens of captures per (sector, half) because every reader tap appends a
# line; without dedup the handler runtime is N × 5s and the chat-side MCP
# transport times out before the response lands.
# ---------------------------------------------------------------------------


@pytest.mark.regression
async def test_dedup_skips_duplicate_sector_half_cuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A log with 5 captures for the same (sector, half, cuid) recovers ONCE."""
    monkeypatch.setenv("CLIPPER_SAFETY", "1")
    base = (FIXTURE_DIR / "synthetic_default_key.log").read_text(encoding="utf-8").strip()
    # Replicate the same single-capture line 5 times — same sector/half/cuid.
    log_text = (base + "\n") * 5
    action = get("flipper_mfkey_crack")
    result = await action.invoke(
        None,
        {"log_content_b64": _b64(log_text)},
        transport="test",
    )
    assert result["stats"]["captures_parsed"] == 5
    assert result["stats"]["captures_skipped"] == 4  # 4 dedup hits after the first
    assert result["stats"]["sectors_recovered"] == 1


# ---------------------------------------------------------------------------
# Scenario: Mfkey-format parse error → ActionRuntimeError
# ---------------------------------------------------------------------------


async def test_parse_error_raises_action_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIPPER_SAFETY", "1")
    bad = "this is not a valid mfkey32 line\n"
    action = get("flipper_mfkey_crack")
    with pytest.raises(ActionRuntimeError, match="not a valid mfkey32 line|tokens"):
        await action.invoke(
            None,
            {"log_content_b64": _b64(bad)},
            transport="test",
        )


# ---------------------------------------------------------------------------
# Scenario: Empty log returns no keys
# ---------------------------------------------------------------------------


async def test_empty_log_returns_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPPER_SAFETY", "1")
    action = get("flipper_mfkey_crack")
    result = await action.invoke(
        None,
        {"log_content_b64": _b64("")},
        transport="test",
    )
    assert result == {
        "keys": [],
        "stats": {
            "captures_parsed": 0,
            "captures_skipped": 0,
            "sectors_recovered": 0,
        },
    }


# ---------------------------------------------------------------------------
# Scenario: Gated by safety toggle
# ---------------------------------------------------------------------------


async def test_gated_by_safety(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the safety gate is OFF, the action must raise EmissionBlocked
    BEFORE any parsing or base64 decode — even on garbage input."""
    monkeypatch.delenv("CLIPPER_SAFETY", raising=False)
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)

    action = get("flipper_mfkey_crack")
    # Garbage base64 — would normally produce an ActionRuntimeError. But
    # because the gate is OFF, the EmissionBlocked path must fire first.
    with pytest.raises(EmissionBlocked) as exc_info:
        await action.invoke(
            None,
            {"log_content_b64": "not-valid-base64-at-all-$$$"},
            transport="test",
        )
    assert str(exc_info.value) == "flipper_mfkey_crack"


# ---------------------------------------------------------------------------
# Action description honours the spec (mentions safety-gated)
# ---------------------------------------------------------------------------


def test_action_is_registered_non_emissive_and_describes_safety_gate() -> None:
    action = get("flipper_mfkey_crack")
    assert action.emissive is False
    assert "safety" in action.description.lower()


# ---------------------------------------------------------------------------
# Fixture regression sweep — every fixture must recover its expected keys
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("fixture_name", _all_fixture_names())
async def test_fixture_keys_match_expected(
    fixture_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cryptographic regression net ('correctness' scenario)."""
    monkeypatch.setenv("CLIPPER_SAFETY", "1")
    log_content = (FIXTURE_DIR / fixture_name).read_bytes()
    expected = EXPECTED_KEYS[fixture_name]
    action = get("flipper_mfkey_crack")
    result = await action.invoke(
        None,
        {"log_content_b64": base64.b64encode(log_content).decode("ascii")},
        transport="test",
    )
    assert result["keys"] == expected, (
        f"fixture {fixture_name!r}: expected {expected}, got {result['keys']}"
    )
