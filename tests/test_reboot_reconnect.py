"""Regression test: reboot / re-enumeration recovery.

After a Flipper reboots and its USB-CDC port re-enumerates (possibly under a new
device path), the server must:
  - surface a clean FlipperDisconnected to tool callers (never a raw OSError),
  - report connected:false (with no stale device info) while the link is down,
  - and the reconnect loop must re-detect the device via a fresh comports()
    scan, re-open the new path, re-handshake, and flip connected false→true.

The earlier bug left the server wedged on a dead FD with a stale connected:true.
"""

from __future__ import annotations

import pytest

import clipper.actions  # noqa: F401 — register hardware actions
from clipper.actions import get
from clipper.flipper import FlipperConnection, FlipperDisconnected

_DEVICE_INFO = "hardware_name: Pelima\r\nfirmware_commit: mntm-012\r\n>: "
_POWER = "Battery Charge: 80%\r\n>: "


def _script_handshake(harness) -> None:
    # FIFO harness — order matters, keys are decorative. start() sends
    # device_info then info power; both re-queued on every (re)open.
    harness.expect("device_info", _DEVICE_INFO)
    harness.expect("info power", _POWER)


async def test_reboot_reenumerate_reconnect_recovers(fake_flipper):
    fake_flipper.add_port("/dev/cu.usbmodemflip_Pelima1")
    _script_handshake(fake_flipper)
    flipper = FlipperConnection(
        port_factory=fake_flipper.port_factory, reconnect_interval=0.05
    )
    await flipper.start()
    assert flipper.connected is True

    # --- Device reboots: the current FD now raises OSError(6) on read. ---
    fake_flipper.simulate_oserror(6)

    # A tool call must surface FlipperDisconnected — NOT a raw OSError — and
    # mark the connection down.
    with pytest.raises(FlipperDisconnected):
        await flipper.send_command("uptime")
    assert flipper.connected is False

    # Via the action layer too: no raw OSError escapes to the caller.
    with pytest.raises(FlipperDisconnected):
        await get("flipper_uptime").invoke(flipper, {}, transport="test")

    # --- flipper_state reports disconnected with NO stale device info. ---
    state = await get("flipper_state").invoke(flipper, {}, transport="test")
    assert state == {"connected": False, "name": None, "firmware": None, "battery": None}

    # --- Re-enumeration: device returns on a NEW path; comports() reflects it. ---
    fake_flipper.remove_ports()
    fake_flipper.add_port("/dev/cu.usbmodemflip_Pelima2")  # fresh, no error flag

    # A reconnect tick (what the background loop runs) re-detects via the port
    # factory, opens the new path, re-handshakes, and connects.
    reconnected = await flipper._try_connect()
    assert reconnected is True
    assert flipper.connected is True
    # The reconnect opened the freshly-enumerated path (not the stale one).
    assert fake_flipper.fake_serial is not None
    assert fake_flipper.fake_serial.port == "/dev/cu.usbmodemflip_Pelima2"

    # Tools work again, and flipper_state reports the live device.
    state2 = await get("flipper_state").invoke(flipper, {}, transport="test")
    assert state2["connected"] is True
    assert state2["name"] == "Pelima"

    await flipper.stop()


async def test_try_connect_returns_false_when_handshake_port_is_dead(fake_flipper):
    """A reopened-but-dead port must NOT be declared connected (no false positive)."""
    fake_flipper.add_port("/dev/cu.usbmodemflip_Dead1")
    # Every opened FakeSerial raises OSError(6) on read → the device_info
    # handshake probe raises → _try_connect must refuse to declare connected.
    fake_flipper.fail_reads_on_open(6)

    flipper = FlipperConnection(
        port_factory=fake_flipper.port_factory, reconnect_interval=0.05
    )

    ok = await flipper._try_connect()
    assert ok is False
    assert flipper.connected is False
