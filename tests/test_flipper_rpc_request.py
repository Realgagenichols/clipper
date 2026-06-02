"""Tests for FlipperConnection.rpc_request().

The rpc_request one-shot method must:
  - acquire exclusive_serial()
  - send the request frame after start_rpc_session\\r + echo drain
  - accept streaming responses, concatenating payloads while has_next=True
  - raise RuntimeError on non-OK command_status, after still closing the session
  - raise asyncio.TimeoutError when no terminating frame arrives in time,
    after still closing the session
  - always send StopSession + the 0xff 0xff 0xff escape bytes on the way out
    (never leave the device in RPC mode for the next caller).
"""

from __future__ import annotations

import asyncio

import pytest

from clipper.flipper import FlipperConnection
from clipper.rpc import (
    TAG_STOP_SESSION,
    _encode_field_ld,
    _encode_field_varint,
    _encode_varint,
    encode_empty,
    encode_main,
)

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

# Pick arbitrary "request"/"response" oneof tags that don't clash with the
# real Gui/Storage tags so the tests don't accidentally validate against any
# specific RPC contract.
_REQ_FIELD = 50
_RESP_FIELD = 51

# Echo the Flipper CLI emits after `start_rpc_session\r`. _drain_rpc_echo
# returns once it sees the terminating \n.
_RPC_ENTRY_ECHO = b"start_rpc_session\r\n"
# Text prompt that lets _drain_to_text_prompt complete promptly.
_TEXT_PROMPT = b">: "


# ---------------------------------------------------------------------------
# Hand-built PB_Main response frames (full delimited framing)
# ---------------------------------------------------------------------------


def _build_main_payload(
    *,
    command_id: int = 0,
    command_status: int = 0,
    has_next: bool = False,
    content_field_num: int,
    content_payload: bytes,
) -> bytes:
    """Build a raw PB_Main payload (no framing varint)."""
    parts = bytearray()
    if command_id:
        parts += _encode_field_varint((1 << 3) | 0, command_id)
    if command_status:
        parts += _encode_field_varint((2 << 3) | 0, command_status)
    if has_next:
        parts += _encode_field_varint((3 << 3) | 0, 1)
    parts += _encode_field_ld(content_field_num, content_payload)
    return bytes(parts)


def _delimit(pb: bytes) -> bytes:
    """Wrap a raw PB_Main payload in nanopb PB_ENCODE_DELIMITED framing."""
    return _encode_varint(len(pb)) + pb


def _build_response_frame(
    *,
    command_id: int = 0,
    command_status: int = 0,
    has_next: bool = False,
    content_field_num: int = _RESP_FIELD,
    content_payload: bytes = b"",
) -> bytes:
    """Build a fully framed PB_Main response ready to queue into the fake serial."""
    return _delimit(
        _build_main_payload(
            command_id=command_id,
            command_status=command_status,
            has_next=has_next,
            content_field_num=content_field_num,
            content_payload=content_payload,
        )
    )


# ---------------------------------------------------------------------------
# Fake serial (inline, to keep this test file self-contained).
# ---------------------------------------------------------------------------


class RPCFakeSerial:
    """write/read/flush/close fake suitable for RPC byte exchanges."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self._buf = bytearray()
        self._closed = False

    def queue(self, data: bytes) -> None:
        self._buf.extend(data)

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        pass

    def read(self, n: int = 1) -> bytes:
        if not self._buf:
            return b""
        chunk = bytes(self._buf[:n])
        self._buf = self._buf[n:]
        return chunk

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rpc_flipper():
    """A FlipperConnection backed by an RPCFakeSerial and marked connected."""
    flipper = FlipperConnection(
        port_factory=lambda: "/dev/fake", reconnect_interval=60
    )
    ser = RPCFakeSerial()
    flipper._serial = ser
    flipper._connected = True
    return flipper, ser


# ---------------------------------------------------------------------------
# single-chunk response
# ---------------------------------------------------------------------------


async def test_rpc_request_single_chunk_returns_payload(rpc_flipper):
    """One PB_Main with has_next=False — returns its payload bytes."""
    flipper, ser = rpc_flipper
    ser.queue(_RPC_ENTRY_ECHO)
    ser.queue(
        _build_response_frame(
            command_id=1,
            command_status=0,
            has_next=False,
            content_payload=b"the-payload",
        )
    )
    ser.queue(_TEXT_PROMPT)

    result = await flipper.rpc_request(
        request_field_num=_REQ_FIELD,
        request_payload=encode_empty(),
        response_field_num=_RESP_FIELD,
        timeout=2.0,
    )
    assert result == b"the-payload"


async def test_rpc_request_single_chunk_binary_safe(rpc_flipper):
    """A response payload containing every byte 0x00-0xFF returns intact."""
    flipper, ser = rpc_flipper
    body = bytes(range(256))

    ser.queue(_RPC_ENTRY_ECHO)
    ser.queue(
        _build_response_frame(
            command_id=1, command_status=0, has_next=False, content_payload=body
        )
    )
    ser.queue(_TEXT_PROMPT)

    result = await flipper.rpc_request(
        request_field_num=_REQ_FIELD,
        request_payload=encode_empty(),
        response_field_num=_RESP_FIELD,
        timeout=2.0,
    )
    assert result == body


# ---------------------------------------------------------------------------
# multi-chunk streaming response
# ---------------------------------------------------------------------------


async def test_rpc_request_multi_chunk_concatenates_in_order(rpc_flipper):
    """Three frames (True, True, False) — payloads concatenate in arrival order."""
    flipper, ser = rpc_flipper
    chunks = [b"AAAA", b"BBBB", b"CCCC"]

    ser.queue(_RPC_ENTRY_ECHO)
    for i, chunk in enumerate(chunks):
        ser.queue(
            _build_response_frame(
                command_id=1,
                command_status=0,
                has_next=(i < len(chunks) - 1),
                content_payload=chunk,
            )
        )
    ser.queue(_TEXT_PROMPT)

    result = await flipper.rpc_request(
        request_field_num=_REQ_FIELD,
        request_payload=encode_empty(),
        response_field_num=_RESP_FIELD,
        timeout=2.0,
    )
    assert result == b"AAAABBBBCCCC"


async def test_rpc_request_ignores_unsolicited_other_field_frames(rpc_flipper):
    """Frames whose content_field_num != response_field_num are dropped."""
    flipper, ser = rpc_flipper

    ser.queue(_RPC_ENTRY_ECHO)
    # An unrelated frame (e.g. a stray Gui event) sneaks into the stream.
    ser.queue(
        _build_response_frame(
            command_id=0,
            command_status=0,
            has_next=True,
            content_field_num=99,  # not our response_field_num
            content_payload=b"ignored",
        )
    )
    # The real terminating frame
    ser.queue(
        _build_response_frame(
            command_id=1,
            command_status=0,
            has_next=False,
            content_payload=b"real",
        )
    )
    ser.queue(_TEXT_PROMPT)

    result = await flipper.rpc_request(
        request_field_num=_REQ_FIELD,
        request_payload=encode_empty(),
        response_field_num=_RESP_FIELD,
        timeout=2.0,
    )
    assert result == b"real"


# ---------------------------------------------------------------------------
# command_status != OK raises (after cleanup)
# ---------------------------------------------------------------------------


async def test_rpc_request_raises_on_non_ok_command_status(rpc_flipper):
    """A frame with command_status != 0 raises RuntimeError."""
    flipper, ser = rpc_flipper
    ser.queue(_RPC_ENTRY_ECHO)
    ser.queue(
        _build_response_frame(
            command_id=1,
            command_status=7,  # arbitrary non-OK
            has_next=False,
            content_payload=b"",
        )
    )
    ser.queue(_TEXT_PROMPT)

    with pytest.raises(RuntimeError, match="command_status=7"):
        await flipper.rpc_request(
            request_field_num=_REQ_FIELD,
            request_payload=encode_empty(),
            response_field_num=_RESP_FIELD,
            timeout=2.0,
        )

    # Session must still have been closed: escape bytes present in written stream
    all_written = b"".join(ser.written)
    assert b"\xff\xff\xff\r\r" in all_written


# ---------------------------------------------------------------------------
# timeout raises (after cleanup)
# ---------------------------------------------------------------------------


async def test_rpc_request_raises_timeout_when_no_response(rpc_flipper):
    """No bytes arrive after entry echo — receive loop times out."""
    flipper, ser = rpc_flipper
    ser.queue(_RPC_ENTRY_ECHO)
    # Pre-queue the prompt so the cleanup drain completes quickly. The
    # receive loop will exhaust the bytes queue before this drains.
    ser.queue(_TEXT_PROMPT)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await flipper.rpc_request(
            request_field_num=_REQ_FIELD,
            request_payload=encode_empty(),
            response_field_num=_RESP_FIELD,
            timeout=0.2,  # very small — fake serial returns no bytes
        )

    # Session teardown still ran: escape bytes are in the written stream.
    all_written = b"".join(ser.written)
    assert b"\xff\xff\xff\r\r" in all_written


# ---------------------------------------------------------------------------
# session lifecycle bytes
# ---------------------------------------------------------------------------


async def test_rpc_request_session_lifecycle_bytes(rpc_flipper):
    """Written bytes must start with start_rpc_session and end with escape bytes."""
    flipper, ser = rpc_flipper
    ser.queue(_RPC_ENTRY_ECHO)
    ser.queue(
        _build_response_frame(
            command_id=1, command_status=0, has_next=False, content_payload=b"x"
        )
    )
    ser.queue(_TEXT_PROMPT)

    await flipper.rpc_request(
        request_field_num=_REQ_FIELD,
        request_payload=encode_empty(),
        response_field_num=_RESP_FIELD,
        timeout=2.0,
    )

    # First written chunk is the text command to enter RPC mode.
    assert ser.written[0] == b"start_rpc_session\r"

    all_written = b"".join(ser.written)

    # A PB_Main carrying the request field tag was written. The request
    # oneof field number is _REQ_FIELD=50, wire type 2: tag = (50<<3)|2 = 402
    # → varint = 0x92 0x03.
    req_tag = _encode_varint((_REQ_FIELD << 3) | 2)
    assert req_tag in all_written

    # A StopSession frame was written.
    stop_tag = _encode_varint((TAG_STOP_SESSION << 3) | 2)
    assert stop_tag in all_written

    # And the very last bytes written are the RPC-escape sequence.
    assert ser.written[-1] == b"\xff\xff\xff\r\r"


# ---------------------------------------------------------------------------
# Disconnected guard
# ---------------------------------------------------------------------------


async def test_rpc_request_raises_when_disconnected(rpc_flipper):
    """If the flipper is not connected, rpc_request raises FlipperDisconnected."""
    from clipper.flipper import FlipperDisconnected

    flipper, _ser = rpc_flipper
    flipper._connected = False
    with pytest.raises(FlipperDisconnected):
        await flipper.rpc_request(
            request_field_num=_REQ_FIELD,
            request_payload=encode_empty(),
            response_field_num=_RESP_FIELD,
            timeout=1.0,
        )


# ---------------------------------------------------------------------------
# Request frame uses the supplied field tag + payload
# ---------------------------------------------------------------------------


async def test_rpc_request_emits_supplied_request_field_and_payload(rpc_flipper):
    """The request PB_Main carries the supplied field number and payload bytes."""
    flipper, ser = rpc_flipper
    ser.queue(_RPC_ENTRY_ECHO)
    ser.queue(
        _build_response_frame(
            command_id=1, command_status=0, has_next=False, content_payload=b""
        )
    )
    ser.queue(_TEXT_PROMPT)

    # Match what storage_read will use — a non-empty inner payload
    request_payload = _encode_field_ld(1, b"/ext/foo.nfc")

    await flipper.rpc_request(
        request_field_num=_REQ_FIELD,
        request_payload=request_payload,
        response_field_num=_RESP_FIELD,
        timeout=2.0,
    )

    # The full request PB_Main frame should appear verbatim somewhere in the
    # written stream.
    expected_frame = encode_main(1, _REQ_FIELD, request_payload)
    all_written = b"".join(ser.written)
    assert expected_frame in all_written
