"""Tests for device diagnostics read actions: uptime, datetime, diagnostics.

TDD: these tests were written first, implementation follows.

CRITICAL: the exact CLI output formats for `uptime`, `date`, `free`, and `ps`
are NOT confirmed for this firmware build. The tests below are written against
PLAUSIBLE sample outputs. They assert primarily that:
  (a) the raw command text is always returned verbatim, and
  (b) the lenient best-effort parser never raises on unexpected output —
      degrading to None / empty list / partial dict instead.
Parse assertions are intentionally loose so they survive format variance.

All three actions are NON-emissive (read-only), so no CLIPPER_ALLOW_EMIT
toggle is needed.
"""

from __future__ import annotations

from clipper.actions import get
from clipper.flipper import FlipperConnection

# ---------------------------------------------------------------------------
# Handshake helper (mirrors tests/test_loader.py)
# ---------------------------------------------------------------------------

HANDSHAKE_DEVICE = "hardware_name: TestFlipper\r\n>: "
HANDSHAKE_POWER = "Battery Charge: 88%\r\n>: "


def script_handshake(harness) -> None:
    """Queue the two handshake responses consumed by FlipperConnection.start()."""
    harness.expect("device_info", HANDSHAKE_DEVICE)
    harness.expect("info power", HANDSHAKE_POWER)


# ===========================================================================
# R1 — flipper_uptime
# ===========================================================================


async def test_uptime_returns_raw_and_parsed(fake_flipper):
    """`uptime` returns the raw text plus a best-effort parsed uptime string."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("uptime", "Uptime: 0d0h12m34s\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_uptime").invoke(flipper, {}, transport="test")
        assert "raw" in result
        assert isinstance(result["raw"], str)
        assert "0d0h12m34s" in result["raw"]
        assert "uptime" in result
        # Best-effort: should recognize the value, but at minimum must not raise.
        assert result["uptime"] is None or isinstance(result["uptime"], str)
        if result["uptime"] is not None:
            assert "12m" in result["uptime"]
    finally:
        await flipper.stop()


async def test_uptime_tolerates_unexpected_format(fake_flipper):
    """Lenient parsing: an unfamiliar body must not raise — raw still returned."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("uptime", "some totally different firmware banner\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_uptime").invoke(flipper, {}, transport="test")
        assert "raw" in result
        assert "uptime" in result
        assert result["uptime"] is None or isinstance(result["uptime"], str)
    finally:
        await flipper.stop()


# ===========================================================================
# R2 — flipper_datetime
# ===========================================================================


async def test_datetime_returns_raw_and_parsed(fake_flipper):
    """`date` (no args = read RTC) returns raw text plus best-effort datetime."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("date", "2026-06-03 14:22:07\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_datetime").invoke(flipper, {}, transport="test")
        assert "raw" in result
        assert isinstance(result["raw"], str)
        assert "2026-06-03" in result["raw"]
        assert "datetime" in result
        assert result["datetime"] is None or isinstance(result["datetime"], str)
        if result["datetime"] is not None:
            assert "2026-06-03" in result["datetime"]
    finally:
        await flipper.stop()


async def test_datetime_sends_no_args(fake_flipper):
    """`flipper_datetime` reads the RTC — it must send a bare `date` (no args)."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("date", "2026-06-03 14:22:07\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        await get("flipper_datetime").invoke(flipper, {}, transport="test")
        written = harness.all_written()
        assert "date" in written
        # No write-style date (with a timestamp argument) was sent.
        assert not any(w.startswith("date ") for w in written)
    finally:
        await flipper.stop()


async def test_datetime_tolerates_unexpected_format(fake_flipper):
    """Lenient parsing: an unfamiliar body must not raise — raw still returned."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("date", "clock not set\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_datetime").invoke(flipper, {}, transport="test")
        assert "raw" in result
        assert "datetime" in result
        assert result["datetime"] is None or isinstance(result["datetime"], str)
    finally:
        await flipper.stop()


# ===========================================================================
# R3 — flipper_diagnostics
# ===========================================================================

_FREE_RESPONSE = (
    "Free heap size: 102400\r\n"
    "Total heap size: 196608\r\n"
    "Minimum free heap size: 81920\r\n"
    "Maximum heap block: 65536\r\n"
    ">: "
)

_PS_RESPONSE = (
    "Name             Stack min free\r\n"
    "MainApp          512\r\n"
    "FuriHal          256\r\n"
    "IDLE             128\r\n"
    ">: "
)


async def test_diagnostics_returns_free_and_ps(fake_flipper):
    """`flipper_diagnostics` runs both `free` and `ps`, returning raw for each."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("free", _FREE_RESPONSE)
    harness.expect("ps", _PS_RESPONSE)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_diagnostics").invoke(flipper, {}, transport="test")
        # Structure: top-level free + ps sub-dicts, each carrying raw text.
        assert "free" in result
        assert "ps" in result
        assert isinstance(result["free"], dict)
        assert isinstance(result["ps"], dict)
        assert "raw" in result["free"]
        assert "raw" in result["ps"]
        assert "Free heap size" in result["free"]["raw"]
        assert "MainApp" in result["ps"]["raw"]

        # Best-effort heap fields (loose — keys present only if recognized).
        free = result["free"]
        if "free_heap" in free:
            assert free["free_heap"] == 102400

        # Best-effort task list — must be a list, contents are a bonus.
        assert "tasks" in result["ps"]
        assert isinstance(result["ps"]["tasks"], list)
        if result["ps"]["tasks"]:
            assert any("MainApp" in str(t) for t in result["ps"]["tasks"])
    finally:
        await flipper.stop()


async def test_diagnostics_sends_both_commands(fake_flipper):
    """Both `free` and `ps` must be issued."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("free", _FREE_RESPONSE)
    harness.expect("ps", _PS_RESPONSE)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        await get("flipper_diagnostics").invoke(flipper, {}, transport="test")
        written = harness.all_written()
        assert "free" in written
        assert "ps" in written
    finally:
        await flipper.stop()


async def test_diagnostics_tolerates_unexpected_format(fake_flipper):
    """Lenient parsing: unfamiliar bodies must not raise — raw still returned."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("free", "weird unfamiliar heap dump\r\n>: ")
    harness.expect("ps", "weird unfamiliar task dump\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_diagnostics").invoke(flipper, {}, transport="test")
        assert "raw" in result["free"]
        assert "raw" in result["ps"]
        assert isinstance(result["ps"]["tasks"], list)
    finally:
        await flipper.stop()
