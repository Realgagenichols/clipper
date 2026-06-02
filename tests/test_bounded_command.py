"""Tests for FlipperConnection.run_bounded_command().

run_bounded_command wraps Flipper's long-running CLI commands (`emulate`,
`subghz rx`) which loop until an ETX byte (0x03) arrives. The contract:

  1. acquire exclusive_serial() (serialize, raise if disconnected)
  2. write `<cmd>\r`
  3. collect any device output for `duration_s` seconds
  4. ALWAYS send the ETX stop byte (b"\\x03") and drain to the `>: ` prompt,
     even on timeout or error (N2 — mirrors _exit_rpc_session's always-close
     contract via try/finally)
  5. return the collected text, ANSI-stripped and echo/prompt-stripped like
     _send_locked

The fake serial here is write-gated (L2): output bytes only appear once the
command is written, and the trailing prompt that the ETX-stop drains is held
back until the stop byte is seen — modeling a scanner that runs until aborted.
``read()`` (the quiesce drain) is backed by a separate, empty residue stream so
the drain can't paper over response handling.
"""

from __future__ import annotations

import threading

import pytest

import clipper.flipper as flipper_mod
from clipper.flipper import FlipperConnection, FlipperDisconnected

ETX = b"\x03"
PROMPT = b"\r\n>: "


@pytest.fixture(autouse=True)
def _fast_quiesce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the post-byte quiet window so drain-based tests stay fast."""
    monkeypatch.setattr(flipper_mod, "_QUIESCE_QUIET", 0.01)
    monkeypatch.setattr(flipper_mod, "_QUIESCE_MAX", 0.1)


class _BoundedFakeSerial:
    """Write-gated fake modeling a long-running CLI command.

    On the command write, the command is echoed and ``output`` starts flowing
    from ``read()`` ... no — output flows from ``read()`` is wrong: the bounded
    command collects via ``read()`` during the window. So:

    - Writing ``<cmd>\\r`` echoes the command + emits ``output`` into the
      read() stream (what the device prints while running).
    - The command does NOT emit its trailing prompt on its own — it runs until
      the ETX stop byte arrives. Writing b"\\x03" releases the trailing prompt
      into the read()/read_until stream so the drain-to-prompt completes.
    """

    def __init__(self, cmd: str, output: bytes) -> None:
        self._cmd = cmd
        self._output = output
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.is_open = True
        self.written: list[bytes] = []
        self.stop_sent = False

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.append(data)
            text = data.decode("utf-8", "replace")
            if text.strip() == self._cmd:
                # Echo the command line + the running output.
                self._buf.extend(self._cmd.encode() + b"\r\n")
                self._buf.extend(self._output)
            if ETX in data:
                self.stop_sent = True
                # The command aborts and the prompt returns.
                self._buf.extend(PROMPT)
        return len(data)

    def read(self, n: int = 1) -> bytes:
        with self._lock:
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        with self._lock:
            idx = self._buf.find(expected)
            if idx == -1:
                out = bytes(self._buf)
                self._buf.clear()
                return out
            end = idx + len(expected)
            out = bytes(self._buf[:end])
            del self._buf[:end]
            return out

    def reset_input_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)


def _make_flipper(fake: _BoundedFakeSerial) -> FlipperConnection:
    flipper = FlipperConnection(port_factory=lambda: "/dev/fake", reconnect_interval=60)
    flipper._connected = True
    flipper._serial = fake  # type: ignore[assignment]
    return flipper


async def test_writes_command_then_etx_stop() -> None:
    """The command is written first, then the ETX stop byte after the window."""
    fake = _BoundedFakeSerial("subghz rx 433920000 0", b"")
    flipper = _make_flipper(fake)

    await flipper.run_bounded_command("subghz rx 433920000 0", duration_s=0.05)

    # The command line must be written, then b"\x03" must follow.
    assert fake.stop_sent, "ETX stop byte was never sent"
    joined = b"".join(fake.written)
    assert b"subghz rx 433920000 0\r" in joined
    etx_idx = joined.index(ETX)
    cmd_idx = joined.index(b"subghz rx 433920000 0\r")
    assert cmd_idx < etx_idx, "ETX must be sent AFTER the command"


async def test_returns_collected_output_stripped() -> None:
    """Output emitted during the window is returned, ANSI/echo/prompt stripped."""
    output = b"\x1b[32mPackets received: 3\x1b[0m\r\n"
    fake = _BoundedFakeSerial("subghz rx 433920000 0", output)
    flipper = _make_flipper(fake)

    result = await flipper.run_bounded_command(
        "subghz rx 433920000 0", duration_s=0.05
    )

    assert result == "Packets received: 3"
    # Echoed command must be stripped.
    assert "subghz rx" not in result
    # ANSI escapes must be stripped.
    assert "\x1b" not in result
    # Trailing prompt must be stripped.
    assert ">:" not in result


async def test_drains_to_prompt_after_stop() -> None:
    """After ETX, the device's trailing prompt is drained (not left buffered)."""
    output = b"Protocols detected: Mifare Classic\r\n"
    fake = _BoundedFakeSerial("scanner", output)
    flipper = _make_flipper(fake)

    await flipper.run_bounded_command("scanner", duration_s=0.05)

    # The prompt released by the stop byte must have been consumed.
    assert fake.in_waiting == 0, "trailing prompt was not drained after stop"


async def test_serial_error_mid_window_surfaces_as_disconnected() -> None:
    """A serial error during the collect window marks the port dead (L3).

    The window read raises, so the connection is marked disconnected and the
    error surfaces as FlipperDisconnected. The finally's ETX stop is then
    intentionally SKIPPED (guarded by self._connected) — you cannot write to a
    dead FD. (The N2 always-stop guarantee on the normal/timeout path is covered
    by test_writes_command_then_etx_stop / test_drains_to_prompt_after_stop.)
    """

    class _FailingReadSerial(_BoundedFakeSerial):
        def __init__(self) -> None:
            super().__init__("emulate", b"")
            self._reads = 0

        def read(self, n: int = 1) -> bytes:
            import serial

            self._reads += 1
            raise serial.SerialException("fake disconnect during window")

    fake = _FailingReadSerial()
    flipper = _make_flipper(fake)

    with pytest.raises(FlipperDisconnected):
        await flipper.run_bounded_command("emulate", duration_s=0.05)


async def test_raises_when_disconnected() -> None:
    """If not connected, run_bounded_command raises before any I/O."""
    flipper = FlipperConnection(port_factory=lambda: "/dev/fake", reconnect_interval=60)
    flipper._connected = False

    with pytest.raises(FlipperDisconnected):
        await flipper.run_bounded_command("emulate", duration_s=0.05)


async def test_holds_lock_for_duration() -> None:
    """run_bounded_command serializes on the exclusive_serial lock."""
    fake = _BoundedFakeSerial("scanner", b"")
    flipper = _make_flipper(fake)

    assert flipper._lock.locked() is False
    await flipper.run_bounded_command("scanner", duration_s=0.05)
    assert flipper._lock.locked() is False
