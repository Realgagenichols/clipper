"""Regression test for the serial-handoff race (empty/truncated reads under
back-to-back operations).

Bug: ``exclusive_serial()`` serialized callers with a lock but released the lock
as soon as an operation saw the CLI prompt, without draining the device's
trailing bytes. A late-arriving stale ``>: `` prompt from the previous
operation then satisfied the next operation's ``read_until()`` immediately,
yielding an empty / off-by-one response. Observed on hardware when an MCP client
issued several storage tools in one batch: exactly one (rotating) member failed
with a parse-of-empty/truncated response, while isolated retries succeeded.

Reproduced deterministically here with a fake serial that leaves a trailing
prompt (residue) after each command's response — the in-flight tail that
``reset_input_buffer()`` (a no-op for in-flight bytes, as on real hardware)
cannot clear; only draining (reading to quiescence) consumes it.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import clipper.flipper as flipper_mod
from clipper.flipper import FlipperConnection

PROMPT = b"\r\n>: "


@pytest.fixture(autouse=True)
def _fast_quiesce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the post-byte quiet window so drain-based tests stay fast.

    These tests assert correctness of the handoff, not the exact settle
    duration, so a tiny quiet window keeps them quick.
    """
    monkeypatch.setattr(flipper_mod, "_QUIESCE_QUIET", 0.01)


class _HandoffFakeSerial:
    """Fake serial that appends a stale trailing prompt after each response.

    A command write enqueues the command's response (terminated by a prompt)
    PLUS an extra lingering prompt — the residue. ``reset_input_buffer`` is a
    no-op, modeling that a host cannot clear bytes still in flight; only a
    read drains them.
    """

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.is_open = True
        self.written: list[bytes] = []

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.append(data)
            line = data.decode("utf-8", "replace").strip()
            if line in self._responses:
                self._buf.extend(self._responses[line].encode() + PROMPT)
                self._buf.extend(PROMPT)  # lingering stale prompt (in-flight tail)
        return len(data)

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        with self._lock:
            idx = self._buf.find(expected)
            if idx == -1:
                return b""
            end = idx + len(expected)
            out = bytes(self._buf[:end])
            del self._buf[:end]
            return out

    def read(self, n: int = 1) -> bytes:
        with self._lock:
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    def reset_input_buffer(self) -> None:
        # No-op: a host cannot clear bytes still in flight in the USB-CDC
        # pipeline — exactly the condition that triggered the bug.
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)


def _make_flipper(fake: _HandoffFakeSerial) -> FlipperConnection:
    flipper = FlipperConnection(port_factory=lambda: "/dev/fake", reconnect_interval=60)
    flipper._connected = True
    flipper._serial = fake  # type: ignore[assignment]
    return flipper


@pytest.mark.regression
async def test_back_to_back_commands_do_not_bleed_stale_prompt():
    """Sequential back-to-back commands each return their OWN response."""
    n = 8
    responses = {f"probe {i}": f"value-{i}" for i in range(n)}
    flipper = _make_flipper(_HandoffFakeSerial(responses))

    for _round in range(5):
        for i in range(n):
            got = await flipper.send_command(f"probe {i}")
            assert got == f"value-{i}", (
                f"probe {i} returned {got!r} — stale-prompt bleed under back-to-back ops"
            )


@pytest.mark.regression
async def test_batched_concurrent_commands_each_get_their_own_response():
    """A batch of concurrent calls (as an MCP client issues) each get their own
    response — they serialize on the lock and must not bleed across the handoff."""
    n = 8
    responses = {f"probe {i}": f"value-{i}" for i in range(n)}
    flipper = _make_flipper(_HandoffFakeSerial(responses))

    results = await asyncio.gather(
        *(flipper.send_command(f"probe {i}") for i in range(n))
    )
    for i, got in enumerate(results):
        assert got == f"value-{i}", (
            f"probe {i} returned {got!r} — stale-prompt bleed in concurrent batch"
        )


class _FlakyFirstReadSerial:
    """Serves a bare stale prompt on the FIRST read of a command, the real
    response on the second.

    Models a late trailing tail that drain-to-quiet can't always catch (it lands
    after the line briefly went quiet): the first read sees only a stale prompt
    → empty response. ``read()`` (the quiesce drain) serves a separate, empty
    residue buffer so the drain can't paper over it — only the command-level
    ``retry_if_empty`` recovers, which is the guarantee this test pins down.
    """

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self._attempts: dict[str, int] = {}
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.is_open = True
        self.written: list[bytes] = []

    def write(self, data: bytes) -> int:
        with self._lock:
            self.written.append(data)
            line = data.decode("utf-8", "replace").strip()
            if line in self._responses:
                self._attempts[line] = self._attempts.get(line, 0) + 1
                if self._attempts[line] == 1:
                    self._buf.extend(PROMPT)  # stale prompt only → empty response
                else:
                    self._buf.extend(self._responses[line].encode() + PROMPT)
        return len(data)

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        with self._lock:
            idx = self._buf.find(expected)
            if idx == -1:
                return b""
            end = idx + len(expected)
            out = bytes(self._buf[:end])
            del self._buf[:end]
            return out

    def read(self, n: int = 1) -> bytes:
        return b""  # separate (empty) residue stream — quiesce drain is a no-op

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


@pytest.mark.regression
async def test_retry_if_empty_recovers_from_a_bled_read():
    """A read-only command whose first read comes back empty (late-tail bleed)
    recovers on the in-lock retry; without the retry it would return empty."""
    # With retry (as read-only storage handlers use): recovers.
    flipper = _make_flipper(_FlakyFirstReadSerial({"storage md5 /ext/x": "deadbeef"}))
    got = await flipper.send_command("storage md5 /ext/x", retry_if_empty=True)
    assert got == "deadbeef"

    # Without retry: the bled read surfaces as an empty response (the bug).
    flipper2 = _make_flipper(_FlakyFirstReadSerial({"storage md5 /ext/x": "deadbeef"}))
    got2 = await flipper2.send_command("storage md5 /ext/x")
    assert got2 == "", "control: a bled read returns empty without retry_if_empty"
