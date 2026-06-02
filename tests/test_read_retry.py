"""Regression test (review finding W4 / lesson L1): the new read-only tools opt
into send_command(retry_if_empty=True), so an empty first response (a stale-
prompt bleed under back-to-back ops) is recovered by re-issuing the command.

Modeled with a fake whose first command write yields a bare prompt (empty
response) and whose second yields the real data.
"""

from __future__ import annotations

import clipper.actions  # noqa: F401 — register hardware actions
from clipper.actions import get
from clipper.flipper import FlipperConnection

PROMPT = b"\r\n>: "


class _EmptyThenDataSerial:
    """First command read returns a bare prompt (empty); the retry returns data."""

    def __init__(self, real_response: bytes) -> None:
        self._real = real_response
        self._buf = bytearray()
        self._writes = 0
        self.is_open = True

    def reset_input_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False

    def write(self, data: bytes) -> int:
        # Each command write queues the next response: empty first, real second.
        self._writes += 1
        self._buf.extend(PROMPT if self._writes == 1 else self._real)
        return len(data)

    def read(self, n: int = 1) -> bytes:
        return b""  # residue stream (quiesce drain) — empty, no-op

    def read_until(self, expected: bytes = b"\n", size: int | None = None) -> bytes:
        idx = self._buf.find(expected)
        if idx == -1:
            return b""
        end = idx + len(expected)
        out = bytes(self._buf[:end])
        del self._buf[:end]
        return out


def _make_flipper(serial) -> FlipperConnection:
    flipper = FlipperConnection(port_factory=lambda: "/dev/fake", reconnect_interval=60)
    flipper._connected = True
    flipper._serial = serial  # type: ignore[assignment]
    return flipper


async def test_loader_info_recovers_from_empty_first_response():
    """A bled (empty) first read is retried; the tool returns the real result."""
    real = b'Application "NFC" is running\r\n>: '
    flipper = _make_flipper(_EmptyThenDataSerial(real))

    result = await get("flipper_loader_info").invoke(flipper, {}, transport="test")

    assert result["running"] is True
    assert result["name"] == "NFC"
