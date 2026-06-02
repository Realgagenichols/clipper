"""Tests for clipper.hardware.storage._validate_path.

Malformed paths MUST be rejected before any command is sent to the device.

Marked ``regression`` because path validation is a security boundary —
silent regressions here could let path traversal through to the device.
"""

from __future__ import annotations

import pytest

from clipper.hardware.storage import _validate_path

pytestmark = pytest.mark.regression


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_validate_path_accepts_absolute_path() -> None:
    """A well-formed absolute path is returned unchanged."""
    assert _validate_path("/ext/nfc/foo.nfc") == "/ext/nfc/foo.nfc"
    assert _validate_path("/int") == "/int"
    assert _validate_path("/") == "/"


def test_validate_path_accepts_path_at_max_length() -> None:
    """A path of exactly 256 characters is accepted."""
    path = "/" + "x" * 255
    assert len(path) == 256
    assert _validate_path(path) == path


# ---------------------------------------------------------------------------
# Scenario: reject empty path
# ---------------------------------------------------------------------------


def test_validate_path_rejects_empty_string() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _validate_path("")


# ---------------------------------------------------------------------------
# Scenario: reject non-absolute path
# ---------------------------------------------------------------------------


def test_validate_path_rejects_non_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _validate_path("nfc/foo.nfc")


def test_validate_path_rejects_relative_with_dot() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _validate_path("./foo")


# ---------------------------------------------------------------------------
# Scenario: reject paths containing `..` segments
# ---------------------------------------------------------------------------


def test_validate_path_rejects_parent_traversal() -> None:
    with pytest.raises(ValueError, match=r"\.\."):
        _validate_path("/ext/../int/leak.bin")


def test_validate_path_rejects_parent_traversal_at_root() -> None:
    with pytest.raises(ValueError, match=r"\.\."):
        _validate_path("/..")


def test_validate_path_rejects_parent_traversal_trailing() -> None:
    with pytest.raises(ValueError, match=r"\.\."):
        _validate_path("/ext/nfc/..")


def test_validate_path_accepts_double_dot_inside_filename() -> None:
    """`..` is only a traversal segment when it's a whole path component."""
    # A filename containing '..' is fine — the segment is "foo..bar", not ".."
    assert _validate_path("/ext/foo..bar") == "/ext/foo..bar"


# ---------------------------------------------------------------------------
# Scenario: reject paths with null bytes or control characters
# ---------------------------------------------------------------------------


def test_validate_path_rejects_null_byte() -> None:
    with pytest.raises(ValueError, match="control"):
        _validate_path("/ext/foo\x00.nfc")


def test_validate_path_rejects_low_control_chars() -> None:
    """Every byte < 0x20 except \\t is rejected."""
    for ch_ord in (0x00, 0x01, 0x07, 0x0A, 0x0D, 0x1F):
        with pytest.raises(ValueError, match="control"):
            _validate_path(f"/ext/foo{chr(ch_ord)}bar")


def test_validate_path_accepts_tab() -> None:
    """Tab is the one allowed control character (Flipper paths rarely use it,
    but it's permitted by the spec; null/CR/LF are the dangerous ones)."""
    assert _validate_path("/ext/has\ttab") == "/ext/has\ttab"


# ---------------------------------------------------------------------------
# Length cap (implementation detail, exposed via design)
# ---------------------------------------------------------------------------


def test_validate_path_rejects_path_over_256_chars() -> None:
    path = "/" + "x" * 256
    assert len(path) == 257
    with pytest.raises(ValueError, match=r"256"):
        _validate_path(path)
