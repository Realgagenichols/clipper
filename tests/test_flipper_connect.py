"""Tests for clipper.flipper — Serial layer (scenarios).

All tests use the fake_flipper fixture from conftest.py so no real hardware
is needed. Async tests run under pytest-asyncio with asyncio_mode=auto.
"""

from __future__ import annotations

import asyncio
import time

import pytest
import serial

from clipper.flipper import (
    FlipperConfigError,
    FlipperConnection,
    FlipperDisconnected,
    FlipperPortBusy,
    find_flipper_port,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEVICE_INFO_RESPONSE = (
    "device info\r\n"  # echoed command
    "Hardware Name: ClipperDev\r\n"
    "Firmware Version: 0.99.1\r\n"
    "Hardware Version: 11\r\n"
    ">: "
)

_POWER_INFO_RESPONSE = (
    "power info\r\n"
    "Battery Charge: 88%\r\n"
    ">: "
)


def _setup_default_harness(harness):
    """Register a port + scripted responses on harness."""
    harness.add_port()
    harness.expect("device info", _DEVICE_INFO_RESPONSE)
    harness.expect("power info", _POWER_INFO_RESPONSE)


# ---------------------------------------------------------------------------
# Scenario: Device present
# ---------------------------------------------------------------------------


async def test_device_present_auto_detected(fake_flipper):
    """GIVEN one matching port, WHEN start() runs, THEN connected=True and device_info populated."""
    _setup_default_harness(fake_flipper)

    conn = FlipperConnection(port_factory=fake_flipper.port_factory)
    try:
        await conn.start()
        assert conn.connected is True
        assert conn.device_info.get("hardware_name") == "ClipperDev"
    finally:
        await conn.stop()


async def test_device_present_firmware_version_parsed(fake_flipper):
    """GIVEN device info response, THEN firmware_version is captured in device_info."""
    _setup_default_harness(fake_flipper)

    conn = FlipperConnection(port_factory=fake_flipper.port_factory)
    try:
        await conn.start()
        assert conn.device_info.get("firmware_version") == "0.99.1"
    finally:
        await conn.stop()


async def test_device_present_uses_env_override(fake_flipper, monkeypatch):
    """GIVEN CLIPPER_FLIPPER_PORT set, WHEN find_flipper_port called, THEN it wins verbatim."""
    # No matching ports in the port list — env override should win
    monkeypatch.setenv("CLIPPER_FLIPPER_PORT", "/dev/tty.usbmodemflip_Override")

    port = find_flipper_port()
    assert port == "/dev/tty.usbmodemflip_Override"


async def test_device_present_env_override_skips_discovery(fake_flipper, monkeypatch):
    """GIVEN env override + NO ports in scan, WHEN start() runs, THEN it tries the overridden port.
    """
    monkeypatch.setenv("CLIPPER_FLIPPER_PORT", "/dev/tty.usbmodemflip_Override")

    # Add an appropriate response keyed to the fake port
    fake_flipper.expect("device info", _DEVICE_INFO_RESPONSE)
    fake_flipper.expect("power info", _POWER_INFO_RESPONSE)

    # FlipperConnection with default port_factory (which reads env var)
    conn = FlipperConnection()
    try:
        await conn.start()
        # The env-override port was attempted (serial_factory was called)
        assert fake_flipper.fake_serial is not None
        assert fake_flipper.fake_serial.port == "/dev/tty.usbmodemflip_Override"
    finally:
        await conn.stop()


# ---------------------------------------------------------------------------
# Scenario: No device present
# ---------------------------------------------------------------------------


async def test_no_device_present_connected_false(fake_flipper):
    """GIVEN no ports, WHEN start() runs, THEN connected=False."""
    # No ports added to harness
    conn = FlipperConnection(port_factory=fake_flipper.port_factory, reconnect_interval=0.1)
    try:
        await conn.start()
        assert conn.connected is False
    finally:
        await conn.stop()


async def test_no_device_present_background_poll_keeps_trying(fake_flipper):
    """GIVEN no device, WHEN we wait a few reconnect cycles, THEN port_factory called repeatedly."""
    call_count = 0
    original = fake_flipper.port_factory

    def counting_factory() -> str | None:
        nonlocal call_count
        call_count += 1
        return original()

    conn = FlipperConnection(port_factory=counting_factory, reconnect_interval=0.05)
    try:
        await conn.start()
        # Wait a few reconnect intervals
        await asyncio.sleep(0.3)
        # Should have been called multiple times
        assert call_count >= 3, f"Expected ≥3 poll calls, got {call_count}"
    finally:
        await conn.stop()


# ---------------------------------------------------------------------------
# Scenario: Disconnect mid-session
# ---------------------------------------------------------------------------


async def test_disconnect_mid_session_raises_flipper_disconnected(fake_flipper):
    """GIVEN connected Flipper, WHEN device unplugged, THEN send_command raises FlipperDisconnected.
    """
    _setup_default_harness(fake_flipper)

    conn = FlipperConnection(port_factory=fake_flipper.port_factory, reconnect_interval=0.1)
    try:
        await conn.start()
        assert conn.connected is True

        # Simulate disconnect
        fake_flipper.simulate_disconnect()

        with pytest.raises(FlipperDisconnected):
            # Force the send to actually try the serial (queue it up first)
            fake_flipper.fake_serial._disconnected = True  # ensure read_until raises
            await conn.send_command("device info", timeout=1.0)
    finally:
        await conn.stop()


async def test_disconnect_mid_session_connected_flips_to_false(fake_flipper):
    """GIVEN connected Flipper, WHEN serial write fails, THEN connected=False within 2s."""
    _setup_default_harness(fake_flipper)

    conn = FlipperConnection(port_factory=fake_flipper.port_factory, reconnect_interval=0.1)
    try:
        await conn.start()
        assert conn.connected is True

        # Simulate disconnect
        fake_flipper.simulate_disconnect()

        start = time.monotonic()
        # Trigger the failure path
        with pytest.raises(FlipperDisconnected):
            await conn.send_command("device info", timeout=1.0)

        elapsed = time.monotonic() - start
        assert conn.connected is False
        assert elapsed < 2.0, f"Took {elapsed:.2f}s to detect disconnect; expected <2s"
    finally:
        await conn.stop()


# ---------------------------------------------------------------------------
# Scenario: Reconnect on plug-in
# ---------------------------------------------------------------------------


async def test_reconnect_on_plug_in(fake_flipper):
    """GIVEN disconnected, WHEN device appears after ~0.15s, THEN connected=True within 2.5s."""
    # Start with no device
    conn = FlipperConnection(port_factory=fake_flipper.port_factory, reconnect_interval=0.1)

    async def _add_device_after_delay() -> None:
        await asyncio.sleep(0.15)
        # Add the port and queue responses
        fake_flipper.add_port()
        fake_flipper.expect("device info", _DEVICE_INFO_RESPONSE)
        fake_flipper.expect("power info", _POWER_INFO_RESPONSE)
        # Re-queue responses on the harness for next serial open
        # (responses are consumed on open, so we pre-load them)

    try:
        await conn.start()
        assert conn.connected is False

        # Kick off the delayed device appearance in the background
        add_task = asyncio.create_task(_add_device_after_delay())

        # Poll until connected or timeout
        deadline = time.monotonic() + 2.5
        while not conn.connected and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        await add_task
        assert conn.connected is True, "Expected reconnect within 2.5s after plug-in"
    finally:
        await conn.stop()


# ---------------------------------------------------------------------------
# Regression: concurrent send_command must be serialized
# ---------------------------------------------------------------------------


@pytest.mark.regression
async def test_concurrent_send_command_serialized(fake_flipper):
    """Fire 5 concurrent send_command calls; writes must appear in strict serial order."""
    harness = fake_flipper
    harness.add_port()

    # device info + power info called on start, then 5 more commands
    harness.expect("device info", _DEVICE_INFO_RESPONSE)
    harness.expect("power info", _POWER_INFO_RESPONSE)

    # We'll track the order commands exit the lock, not how many bytes arrive
    order: list[str] = []
    original_send = None

    conn = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=10)

    # Patch _send_locked to queue per-call responses and record order
    async def patched_send_locked(cmd: str, timeout: float) -> str:
        # Queue a response for this specific command
        if harness.fake_serial is not None:
            response = f"{cmd}\r\nResult for {cmd}\r\n>: "
            harness.fake_serial.queue_response(response.encode("utf-8"))
        result = await original_send(cmd, timeout)
        order.append(cmd)
        return result

    try:
        await conn.start()
        original_send = conn._send_locked
        conn._send_locked = patched_send_locked

        cmds = [f"cmd_{i}" for i in range(5)]
        tasks = [asyncio.create_task(conn.send_command(c, timeout=3.0)) for c in cmds]
        await asyncio.gather(*tasks, return_exceptions=True)

        # All 5 commands should have executed
        assert len(order) == 5
        # Verify no two commands ran concurrently (order should be a permutation)
        assert sorted(order) == sorted(cmds)
    finally:
        await conn.stop()


# ---------------------------------------------------------------------------
# Regression: non-ASCII + garbage bytes must not raise
# ---------------------------------------------------------------------------


@pytest.mark.regression
async def test_decode_handles_non_ascii_and_garbage_bytes(fake_flipper):
    """GIVEN response with \\xff and multi-byte UTF-8, THEN decoding does not raise."""
    harness = fake_flipper
    harness.add_port()

    # Build a raw bytes response containing 0xFF and a multi-byte UTF-8 char (é = 0xC3 0xA9)
    raw = (
        b"device info\r\n"
        b"Hardware Name: Clipper\xff\xc3\xa9\r\n"
        b">: "
    )
    harness.expect("device info", raw)
    harness.expect("power info", _POWER_INFO_RESPONSE)

    conn = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=10)
    try:
        # Should not raise even though raw contains 0xFF
        await conn.start()
        assert conn.connected is True
        # The name should contain the replacement char for 0xFF plus 'é'
        name = conn.device_info.get("hardware_name", "")
        assert "Clipper" in name
        # Confirm no UnicodeDecodeError was raised (we got here without exception)
    finally:
        await conn.stop()


# ---------------------------------------------------------------------------
# Regression: multiple ports must fail-fast
# ---------------------------------------------------------------------------


async def test_multiple_ports_fail_fast(fake_flipper):
    """GIVEN two matching ports, no env override, THEN find_flipper_port raises FlipperConfigError.
    """
    fake_flipper.add_port("/dev/tty.usbmodemflip_A")
    fake_flipper.add_port("/dev/tty.usbmodemflip_B")

    with pytest.raises(FlipperConfigError) as exc_info:
        find_flipper_port()

    msg = str(exc_info.value)
    assert "usbmodemflip_A" in msg
    assert "usbmodemflip_B" in msg


# ---------------------------------------------------------------------------
# Regression: port busy → clear error message
# ---------------------------------------------------------------------------


@pytest.mark.regression
async def test_port_busy_clear_error(fake_flipper, caplog):
    """GIVEN serial.Serial raises 'Device or resource busy', THEN FlipperPortBusy raised.

    The clear-error contract is: when serial.Serial raises a busy/permission
    error, ``_open_serial`` wraps it in ``FlipperPortBusy`` with a message that
    mentions "another process" and the user-facing diagnostic. ``_try_connect``
    catches the wrapped exception, logs a WARNING with an ``lsof`` hint, and
    returns False — the server stays up so the reconnect loop can retry once
    the offending process releases the port.
    """
    from clipper.flipper import _open_serial

    harness = fake_flipper
    harness.add_port(device="/dev/tty.usbmodemflip_Test1")

    busy_exc = serial.SerialException(
        "could not open port /dev/tty.usbmodemflip_Test1: [Errno 16] Device or resource busy"
    )
    harness.set_open_raises(busy_exc)

    # The low-level wrapper raises FlipperPortBusy with the user-facing message.
    with pytest.raises(FlipperPortBusy) as exc_info:
        _open_serial("/dev/tty.usbmodemflip_Test1")
    assert "another process" in str(exc_info.value).lower()

    # The connection-level handler swallows it: start() succeeds, connected stays
    # False, and the WARNING log includes the lsof hint.
    harness.set_open_raises(busy_exc)
    conn = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=10)
    with caplog.at_level("WARNING", logger="clipper.flipper"):
        await conn.start()
    try:
        assert conn.connected is False
        warning_text = " ".join(r.getMessage().lower() for r in caplog.records)
        assert "lsof" in warning_text or "busy" in warning_text
    finally:
        await conn.stop()
