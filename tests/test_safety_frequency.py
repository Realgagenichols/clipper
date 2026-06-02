"""Tests for clipper.safety — Sub-GHz frequency allow-list .

Covers:
- Allowed ISM frequencies pass (433.92, edge of window)
- Disallowed frequencies raise FrequencyNotAllowed
- CLIPPER_SGHZ_ALLOWED_MHZ env override replaces defaults
- Invalid env value raises ValueError
"""

from __future__ import annotations

import pytest

from clipper.safety import FrequencyNotAllowed, _allowed, assert_frequency_allowed

# ---------------------------------------------------------------------------
# 5.4 — Allowed frequency passes
# ---------------------------------------------------------------------------


def test_allowed_frequency_passes() -> None:
    """GIVEN default allow-list, WHEN 433.92 MHz requested, THEN no exception."""
    result = assert_frequency_allowed(433.92)
    assert result is None  # function returns None on success


def test_allowed_frequency_within_window() -> None:
    """GIVEN default allow-list, WHEN frequency within ±0.5 MHz of a center, THEN passes."""
    assert_frequency_allowed(433.92 + 0.5)  # exact edge — should pass
    assert_frequency_allowed(315.0 - 0.5)   # lower edge of 315 MHz band


def test_all_default_centers_pass() -> None:
    """GIVEN default allow-list, WHEN each center frequency requested, THEN all pass."""
    for mhz in (315.0, 433.92, 868.0, 915.0):
        assert_frequency_allowed(mhz)


# ---------------------------------------------------------------------------
# 5.4 — Disallowed frequency raises
# ---------------------------------------------------------------------------


def test_disallowed_frequency_raises() -> None:
    """GIVEN default allow-list, WHEN 100.0 MHz requested, THEN FrequencyNotAllowed raised."""
    with pytest.raises(FrequencyNotAllowed) as exc_info:
        assert_frequency_allowed(100.0)

    assert "100.0" in str(exc_info.value)


def test_frequency_just_outside_window_raises() -> None:
    """GIVEN default allow-list, WHEN frequency just beyond ±0.5 MHz window, THEN rejected."""
    with pytest.raises(FrequencyNotAllowed):
        assert_frequency_allowed(433.92 + 0.51)


# ---------------------------------------------------------------------------
# 5.4 — Env var override
# ---------------------------------------------------------------------------


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN CLIPPER_SGHZ_ALLOWED_MHZ=100.0,200.0, WHEN 100.0 checked, THEN passes;
    WHEN 433.92 (default) checked, THEN FrequencyNotAllowed."""
    monkeypatch.setenv("CLIPPER_SGHZ_ALLOWED_MHZ", "100.0,200.0")

    assert_frequency_allowed(100.0)  # should pass now

    with pytest.raises(FrequencyNotAllowed):
        assert_frequency_allowed(433.92)  # no longer in override list


def test_env_override_single_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN CLIPPER_SGHZ_ALLOWED_MHZ=500.0 (single value), WHEN 500.0 MHz checked, THEN passes."""
    monkeypatch.setenv("CLIPPER_SGHZ_ALLOWED_MHZ", "500.0")
    assert_frequency_allowed(500.0)


# ---------------------------------------------------------------------------
# 5.4 — Invalid env value raises ValueError
# ---------------------------------------------------------------------------


def test_env_override_invalid_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN CLIPPER_SGHZ_ALLOWED_MHZ contains a non-numeric value,
    WHEN _allowed() or assert_frequency_allowed called, THEN ValueError raised."""
    monkeypatch.setenv("CLIPPER_SGHZ_ALLOWED_MHZ", "not a number")

    with pytest.raises(ValueError, match="CLIPPER_SGHZ_ALLOWED_MHZ"):
        _allowed()

    with pytest.raises(ValueError):
        assert_frequency_allowed(433.92)


def test_env_override_mixed_valid_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN CLIPPER_SGHZ_ALLOWED_MHZ has one valid and one invalid value,
    THEN ValueError raised."""
    monkeypatch.setenv("CLIPPER_SGHZ_ALLOWED_MHZ", "433.92,oops")

    with pytest.raises(ValueError):
        _allowed()
