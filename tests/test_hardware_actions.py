"""Tests for hardware actions: GPIO, IR, Sub-GHz, NFC, RFID, BadUSB.

TDD: these tests were written first, implementation follows.

Test pattern (per project spec):
- Script the Flipper's responses with harness.expect(cmd, response_bytes).
- Queue device_info + info power FIRST (consumed by _try_connect handshake).
- Then queue the action-specific response.
- Use monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1") for emissive actions.
"""

from __future__ import annotations

import pytest

from clipper.actions import ActionParamError, ActionRuntimeError, get
from clipper.flipper import FlipperConnection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HANDSHAKE_DEVICE = "hardware_name: TestFlipper\r\n>: "
HANDSHAKE_POWER = "Battery Charge: 88%\r\n>: "


def script_handshake(harness) -> None:
    """Queue the two handshake responses consumed by FlipperConnection.start()."""
    harness.expect("device_info", HANDSHAKE_DEVICE)
    harness.expect("info power", HANDSHAKE_POWER)


# ===========================================================================
# 6.1 / 6.2 — GPIO
# ===========================================================================


async def test_gpio_read_returns_level_1(fake_flipper):
    """gpio_read auto-sets mode 0 (input) and parses 'Pin <pin> <= <0|1>'."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("gpio mode PC0 0", "Pin PC0 is now an input\r\n>: ")
    harness.expect("gpio read PC0", "Pin PC0 <= 1\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_gpio_read").invoke(flipper, {"pin": "PC0"}, transport="test")
        assert result == {"pin": "PC0", "level": 1}
    finally:
        await flipper.stop()


async def test_gpio_read_returns_level_0(fake_flipper):
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("gpio mode PA7 0", "Pin PA7 is now an input\r\n>: ")
    harness.expect("gpio read PA7", "Pin PA7 <= 0\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_gpio_read").invoke(flipper, {"pin": "PA7"}, transport="test")
        assert result == {"pin": "PA7", "level": 0}
    finally:
        await flipper.stop()


async def test_gpio_read_invalid_pin_raises(fake_flipper):
    """Invalid pin names must raise ActionParamError."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionParamError):
            await get("flipper_gpio_read").invoke(flipper, {"pin": "INVALID"}, transport="test")
    finally:
        await flipper.stop()


async def test_gpio_write_sets_high(fake_flipper):
    """gpio_write auto-sets mode 1 (output) before driving the pin."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("gpio mode PC0 1", "Pin PC0 is now an output (low)\r\n>: ")
    harness.expect("gpio set PC0 1", "Pin PC0 => 1\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_gpio_write").invoke(
            flipper, {"pin": "PC0", "level": 1}, transport="test"
        )
        assert result == {"pin": "PC0", "level": 1, "ok": True}
    finally:
        await flipper.stop()


async def test_gpio_write_sets_low(fake_flipper):
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("gpio mode PB2 1", "Pin PB2 is now an output (low)\r\n>: ")
    harness.expect("gpio set PB2 0", "Pin PB2 => 0\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_gpio_write").invoke(
            flipper, {"pin": "PB2", "level": 0}, transport="test"
        )
        assert result == {"pin": "PB2", "level": 0, "ok": True}
    finally:
        await flipper.stop()


async def test_gpio_write_invalid_pin_raises(fake_flipper):
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionParamError):
            await get("flipper_gpio_write").invoke(
                flipper, {"pin": "PD0", "level": 1}, transport="test"
            )
    finally:
        await flipper.stop()


async def test_gpio_write_invalid_level_raises(fake_flipper):
    """Level must be 0 or 1; anything else raises ActionParamError."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionParamError):
            await get("flipper_gpio_write").invoke(
                flipper, {"pin": "PC0", "level": 2}, transport="test"
            )
    finally:
        await flipper.stop()


# ===========================================================================
# 6.3 / 6.4 — Infrared
# ===========================================================================


async def test_ir_tx_sends_command(fake_flipper, monkeypatch):
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("ir tx NEC 0x01 0x02", ">: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_ir_tx").invoke(
            flipper,
            {"protocol": "NEC", "address": "0x01", "command": "0x02"},
            transport="test",
        )
        assert result == {"ok": True, "protocol": "NEC", "address": "0x01", "command": "0x02"}
    finally:
        await flipper.stop()


async def test_ir_tx_blocked_without_allow_emit(fake_flipper):
    """ir_tx is emissive — must be blocked when CLIPPER_ALLOW_EMIT is not set."""
    from clipper.actions import EmissionBlocked

    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(EmissionBlocked):
            await get("flipper_ir_tx").invoke(
                flipper,
                {"protocol": "NEC", "address": "0x01", "command": "0x02"},
                transport="test",
            )
    finally:
        await flipper.stop()


async def test_ir_rx_captures_signal(fake_flipper):
    """ir_rx returns a list of captured signals."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    # Simulate Flipper responding with captured IR signal
    harness.expect(
        "ir rx",
        "NEC A:0x01 C:0x02 (42 samples)\r\n>: ",
    )

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_ir_rx").invoke(
            flipper, {"timeout_s": 1.0}, transport="test"
        )
        assert "signals" in result
        assert isinstance(result["signals"], list)
    finally:
        await flipper.stop()


async def test_ir_rx_empty_on_timeout(fake_flipper):
    """Note: timeout with no signal is a success, not an error."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    # Flipper returns nothing useful — just the prompt
    harness.expect("ir rx", ">: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_ir_rx").invoke(
            flipper, {"timeout_s": 1.0}, transport="test"
        )
        assert result == {"signals": []}
    finally:
        await flipper.stop()


async def test_ir_rx_timeout_out_of_range_raises(fake_flipper):
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionParamError):
            await get("flipper_ir_rx").invoke(
                flipper, {"timeout_s": 60.0}, transport="test"
            )
    finally:
        await flipper.stop()


# ===========================================================================
# 6.5 / 6.6 — Sub-GHz transmit
# ===========================================================================


async def test_subghz_tx_allowed_frequency(fake_flipper, monkeypatch):
    """Momentum CLI form: subghz tx <key_hex> <freq_hz> <te_us> <repeat> <device>."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    # 433.92 MHz → 433920000 Hz; key DEADBE (6 hex chars), te=400us, repeat=5, dev=0
    harness.expect("subghz tx DEADBE 433920000 400 5 0", ">: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_subghz_tx").invoke(
            flipper,
            {
                "frequency_mhz": 433.92,
                "key_hex": "DEADBE",
                "te_us": 400,
                "repeat": 5,
                "device": 0,
            },
            transport="test",
        )
        assert result["ok"] is True
        assert result["frequency_mhz"] == 433.92
        assert result["key_hex"] == "DEADBE"
    finally:
        await flipper.stop()


async def test_subghz_tx_disallowed_frequency_raises(fake_flipper, monkeypatch):
    """Disallowed frequency must raise before any serial bytes are written."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionParamError):
            await get("flipper_subghz_tx").invoke(
                flipper,
                {
                    "frequency_mhz": 100.0,  # Not an ISM band
                    "key_hex": "DEADBE",
                },
                transport="test",
            )
        # Verify no subghz command was written (only handshake bytes)
        written = harness.all_written()
        assert not any("subghz" in w for w in written)
    finally:
        await flipper.stop()


async def test_subghz_tx_invalid_key_hex_raises(fake_flipper, monkeypatch):
    """key_hex must be exactly 6 hex chars (3 bytes)."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionParamError):
            await get("flipper_subghz_tx").invoke(
                flipper,
                {"frequency_mhz": 433.92, "key_hex": "TOO_LONG_HEX"},
                transport="test",
            )
    finally:
        await flipper.stop()


async def test_subghz_tx_blocked_without_allow_emit(fake_flipper):
    from clipper.actions import EmissionBlocked

    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(EmissionBlocked):
            await get("flipper_subghz_tx").invoke(
                flipper,
                {"frequency_mhz": 433.92, "key_hex": "DEADBE"},
                transport="test",
            )
    finally:
        await flipper.stop()


# ===========================================================================
# 6.7 / 6.8 — NFC + RFID
# ===========================================================================


async def test_nfc_read_detects_tag(fake_flipper):
    """Hardware-verified Momentum flow: enter `nfc`, scan, parse, abort, exit.

    Real-firmware scanner output (with tag present):
        scanner
        Press Ctrl+C to abort

        Protocols detected: Mifare Classic
        [nfc]>:
    Scanner self-terminates and returns to the [nfc]>: prompt on detection.
    """
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect(
        "nfc",
        "Welcome to NFC Command Line Interface!\r\n[nfc]>: ",
    )
    harness.expect(
        "scanner",
        "Press Ctrl+C to abort\r\n\r\nProtocols detected: Mifare Classic\r\n[nfc]>: ",
    )
    # Defensive Ctrl+C in case scanner didn't auto-stop — harness still queues a response.
    harness.expect("\x03", "^C\r\n[nfc]>: ")
    harness.expect("exit", "\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_nfc_read").invoke(
            flipper, {"timeout_s": 1.0}, transport="test"
        )
        assert result == {"detected": True, "protocols": ["Mifare Classic"]}
    finally:
        await flipper.stop()


async def test_nfc_read_multiple_protocols(fake_flipper):
    """When the firmware reports multiple protocols on one line, parse them all."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("nfc", "[nfc]>: ")
    harness.expect(
        "scanner",
        "Press Ctrl+C to abort\r\nProtocols detected: ISO14443-4A, Mifare Classic\r\n[nfc]>: ",
    )
    harness.expect("\x03", "^C\r\n[nfc]>: ")
    harness.expect("exit", "\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_nfc_read").invoke(
            flipper, {"timeout_s": 1.0}, transport="test"
        )
        assert result == {
            "detected": True,
            "protocols": ["ISO14443-4A", "Mifare Classic"],
        }
    finally:
        await flipper.stop()


async def test_nfc_read_no_tag_is_success(fake_flipper):
    """No card detected within timeout returns detected=False."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("nfc", "[nfc]>: ")
    harness.expect("scanner", "Press Ctrl+C to abort\r\n")  # scanner kept running, no detection
    harness.expect("\x03", "^C\r\n[nfc]>: ")
    harness.expect("exit", "\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_nfc_read").invoke(
            flipper, {"timeout_s": 1.0}, transport="test"
        )
        assert result == {"detected": False, "protocols": []}
    finally:
        await flipper.stop()


async def test_rfid_read_detects_hid_card_with_fc_and_card_details(fake_flipper):
    """Hardware-verified output format from Momentum mntm-012.

    The protocol+hex line has no label; FC and Card are parsed into details.
    """
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect(
        "rfid read",
        "Reading RFID...\r\nPress Ctrl+C to abort\r\n"
        "H10301 0D01E2\r\nFC: 13\r\nCard: 482\r\nReading stopped\r\n>: ",
    )

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_rfid_read").invoke(
            flipper, {"timeout_s": 2.0}, transport="test"
        )
        assert result == {
            "detected": True,
            "protocol": "H10301",
            "id": "0D01E2",
            "details": {"FC": "13", "Card": "482"},
        }
    finally:
        await flipper.stop()


async def test_rfid_read_detects_em4100_card_without_details(fake_flipper):
    """EM4100 cards print the protocol+hex line but no FC/Card detail lines."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect(
        "rfid read",
        "Reading RFID...\r\nPress Ctrl+C to abort\r\n"
        "EM4100 0123456789\r\nReading stopped\r\n>: ",
    )

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_rfid_read").invoke(
            flipper, {"timeout_s": 2.0}, transport="test"
        )
        assert result == {
            "detected": True,
            "protocol": "EM4100",
            "id": "0123456789",
            "details": {},
        }
    finally:
        await flipper.stop()


async def test_rfid_read_no_card_is_success(fake_flipper):
    """Note: no card detected returns detected=False, not an error."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect(
        "rfid read",
        "Reading RFID...\r\nPress Ctrl+C to abort\r\nReading stopped\r\n>: ",
    )

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_rfid_read").invoke(
            flipper, {"timeout_s": 2.0}, transport="test"
        )
        assert result == {
            "detected": False,
            "protocol": None,
            "id": None,
            "details": {},
        }
    finally:
        await flipper.stop()


# ===========================================================================
# 6.9 / 6.10 — Bad USB
# Momentum mntm-012 (and stock) firmware does NOT register a `badusb` CLI
# command — verified by reading applications/main/bad_usb/ which has no
# cli/ subdir. The action is preserved in the registry so the surface
# stays stable but it fails fast with an unsupported-firmware error.
# ===========================================================================

_SIMPLE_SCRIPT = "DELAY 100\nSTRING Hello World\n"


async def test_badusb_run_is_unsupported_on_this_firmware(fake_flipper, monkeypatch):
    """All invocations raise a clear unsupported-firmware error."""
    monkeypatch.setenv("CLIPPER_ALLOW_EMIT", "1")
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionRuntimeError, match="not available via the Flipper CLI"):
            await get("flipper_badusb_run").invoke(
                flipper, {"script": _SIMPLE_SCRIPT}, transport="test"
            )
        # Crucial: no badusb / storage commands were sent to the device.
        written = harness.all_written()
        assert not any("badusb" in w for w in written)
        assert not any("storage write" in w for w in written)
    finally:
        await flipper.stop()


async def test_badusb_run_blocked_without_allow_emit(fake_flipper):
    """Safety gate still applies even though the action is unsupported."""
    from clipper.actions import EmissionBlocked

    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(EmissionBlocked):
            await get("flipper_badusb_run").invoke(
                flipper, {"script": _SIMPLE_SCRIPT}, transport="test"
            )
    finally:
        await flipper.stop()


# ===========================================================================
# Feedback actions — flipper_led_set, flipper_vibro_set, flipper_loader_open
# ===========================================================================


async def test_led_set_sends_correct_command(fake_flipper):
    """`led <channel> <level>` is the verified Momentum CLI form."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("led r 200", ">: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_led_set").invoke(
            flipper, {"channel": "r", "level": 200}, transport="test"
        )
        assert result == {"channel": "r", "level": 200, "ok": True}
    finally:
        await flipper.stop()


async def test_led_set_rejects_invalid_channel(fake_flipper):
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionParamError):
            await get("flipper_led_set").invoke(
                flipper, {"channel": "purple", "level": 100}, transport="test"
            )
    finally:
        await flipper.stop()


async def test_led_set_rejects_out_of_range_level(fake_flipper):
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionParamError):
            await get("flipper_led_set").invoke(
                flipper, {"channel": "r", "level": 300}, transport="test"
            )
    finally:
        await flipper.stop()


async def test_vibro_set_on(fake_flipper):
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("vibro 1", ">: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_vibro_set").invoke(
            flipper, {"on": True}, transport="test"
        )
        assert result == {"on": True, "ok": True}
    finally:
        await flipper.stop()


async def test_vibro_set_off(fake_flipper):
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("vibro 0", ">: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_vibro_set").invoke(
            flipper, {"on": False}, transport="test"
        )
        assert result == {"on": False, "ok": True}
    finally:
        await flipper.stop()


async def test_loader_open_launches_named_app(fake_flipper):
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("loader open NFC", "\r\n>: ")

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        result = await get("flipper_loader_open").invoke(
            flipper, {"app_name": "NFC"}, transport="test"
        )
        assert result == {"app_name": "NFC", "ok": True}
    finally:
        await flipper.stop()


async def test_loader_open_surfaces_firmware_error(fake_flipper):
    """If the firmware can't find the app, the handler must raise."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect(
        "loader open NoSuchApp",
        'could not find application "NoSuchApp"\r\n>: ',
    )

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionRuntimeError, match="could not find"):
            await get("flipper_loader_open").invoke(
                flipper, {"app_name": "NoSuchApp"}, transport="test"
            )
    finally:
        await flipper.stop()


async def test_loader_open_rejects_newline_injection(fake_flipper):
    """Block CR/LF in app_name so a malicious caller can't append commands."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(port_factory=harness.port_factory, reconnect_interval=60)
    await flipper.start()
    try:
        with pytest.raises(ActionParamError):
            await get("flipper_loader_open").invoke(
                flipper, {"app_name": "NFC\rgpio set PC0 1"}, transport="test"
            )
    finally:
        await flipper.stop()
