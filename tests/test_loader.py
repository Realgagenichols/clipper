"""Tests for loader management actions: list, info, close.

TDD: these tests were written first, implementation follows.

Source-confirmed CLI (Momentum loader_cli.c):
  - `loader list`  — lists installed apps grouped by category.
  - `loader info`  — `Application "<name>" is running` / `No application is running`.
  - `loader close` — `Application "<name>" was closed` /
                     `No application is running` /
                     `Application "<name>" has to be closed manually`.

All three are NON-emissive (read/local control only), so no
CLIPPER_ALLOW_EMIT toggle is needed.
"""

from __future__ import annotations

from clipper.actions import get
from clipper.flipper import FlipperConnection

# ---------------------------------------------------------------------------
# Handshake helper (mirrors tests/test_hardware_actions.py)
# ---------------------------------------------------------------------------

HANDSHAKE_DEVICE = "hardware_name: TestFlipper\r\n>: "
HANDSHAKE_POWER = "Battery Charge: 88%\r\n>: "


def script_handshake(harness) -> None:
    """Queue the two handshake responses consumed by FlipperConnection.start()."""
    harness.expect("device_info", HANDSHAKE_DEVICE)
    harness.expect("info power", HANDSHAKE_POWER)


# A representative `loader list` body grouped by category, as Momentum emits it.
_LOADER_LIST_RESPONSE = (
    "Apps:\r\n"
    "Main:\r\n"
    "\tNFC\r\n"
    "\tSub-GHz\r\n"
    "\tInfrared\r\n"
    "Settings:\r\n"
    "\tBluetooth\r\n"
    "\tStorage\r\n"
    ">: "
)


# ===========================================================================
# R4 — flipper_loader_list
# ===========================================================================


async def test_loader_list_returns_raw_and_parsed_apps(fake_flipper):
    """`loader list` returns both the raw text and a best-effort app list."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("loader list", _LOADER_LIST_RESPONSE)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_loader_list").invoke(flipper, {}, transport="test")
        assert "raw" in result
        assert isinstance(result["raw"], str)
        assert "NFC" in result["raw"]
        # Parsed app names should include the indented entries, not the
        # category headers or the prompt.
        assert "apps" in result
        assert isinstance(result["apps"], list)
        assert "NFC" in result["apps"]
        assert "Sub-GHz" in result["apps"]
        assert "Bluetooth" in result["apps"]
        # Category header / prompt noise must not appear as apps.
        assert "Main:" not in result["apps"]
        assert ">:" not in result["apps"]
    finally:
        await flipper.stop()


async def test_loader_list_tolerates_unexpected_format(fake_flipper):
    """Lenient parsing: an unfamiliar body must not raise — raw still returned."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("loader list", "some totally different firmware banner\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_loader_list").invoke(flipper, {}, transport="test")
        assert "raw" in result
        assert isinstance(result["apps"], list)
    finally:
        await flipper.stop()


# ===========================================================================
# R5 — flipper_loader_info
# ===========================================================================


async def test_loader_info_reports_running_app(fake_flipper):
    """`loader info` → running app name when one is running."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("loader info", 'Application "NFC" is running\r\n>: ')

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_loader_info").invoke(flipper, {}, transport="test")
        assert result == {"running": True, "name": "NFC"}
    finally:
        await flipper.stop()


async def test_loader_info_reports_no_app(fake_flipper):
    """`loader info` → running=False, name=None when idle."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("loader info", "No application is running\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_loader_info").invoke(flipper, {}, transport="test")
        assert result == {"running": False, "name": None}
    finally:
        await flipper.stop()


# ===========================================================================
# R6 — flipper_loader_close
# ===========================================================================


async def test_loader_close_closes_running_app(fake_flipper):
    """`loader close` → closed=True with the app name."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("loader close", 'Application "NFC" was closed\r\n>: ')

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_loader_close").invoke(flipper, {}, transport="test")
        assert result["closed"] is True
        assert result["name"] == "NFC"
        assert "was closed" in result["detail"]
    finally:
        await flipper.stop()


async def test_loader_close_no_app_running(fake_flipper):
    """`loader close` with nothing running → closed=False, name=None."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("loader close", "No application is running\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_loader_close").invoke(flipper, {}, transport="test")
        assert result["closed"] is False
        assert result["name"] is None
    finally:
        await flipper.stop()


async def test_loader_close_manual_close_required(fake_flipper):
    """`loader close` on an app that can't be closed remotely → closed=False, name set."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect(
        "loader close",
        'Application "Snake Game" has to be closed manually\r\n>: ',
    )

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_loader_close").invoke(flipper, {}, transport="test")
        assert result["closed"] is False
        assert result["name"] == "Snake Game"
        assert "manually" in result["detail"]
    finally:
        await flipper.stop()
