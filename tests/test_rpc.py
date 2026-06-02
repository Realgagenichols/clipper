"""Tests for clipper.rpc — protobuf encode/decode helpers.

Field tag constants are verified against the flipperzero-protobuf submodule
used by Momentum firmware (commit ea4f185f5eaa265955c520eae2832887ee6aa5e4):
  - flipper.proto PB_Main.oneof content: empty=4, stop_session=19
Source: https://github.com/Next-Flip/flipperzero-protobuf/tree/ea4f185f
"""

from __future__ import annotations

import pytest

from clipper.rpc import (
    TAG_EMPTY_RESPONSE,
    TAG_STOP_SESSION,
    _decode_varint,
    _encode_field_ld,
    _encode_field_varint,
    _encode_varint,
    decode_main,
    encode_empty,
    encode_main,
    encode_storage_mkdir_request,
    encode_storage_read_request,
    encode_storage_write_request,
    parse_storage_read_response,
    try_read_delimited,
)

# An arbitrary PB_Main.oneof content field number (any value >= 4) used to
# exercise the generic framing/round-trip helpers below.
_CONTENT_FIELD = 20

# ---------------------------------------------------------------------------
# Varint encoding/decoding
# ---------------------------------------------------------------------------


def test_varint_single_byte():
    """Values 0-127 encode to a single byte."""
    assert _encode_varint(0) == b"\x00"
    assert _encode_varint(1) == b"\x01"
    assert _encode_varint(127) == b"\x7f"


def test_varint_multi_byte():
    """Values 128+ use continuation bits."""
    assert _encode_varint(128) == b"\x80\x01"
    assert _encode_varint(300) == b"\xac\x02"
    assert _encode_varint(16383) == b"\xff\x7f"
    assert _encode_varint(16384) == b"\x80\x80\x01"


def test_varint_round_trip():
    """Decode reverses encode for a range of values."""
    for value in [0, 1, 127, 128, 255, 300, 1000, 65535, 2**21, 2**28]:
        encoded = _encode_varint(value)
        decoded, pos = _decode_varint(encoded, 0)
        assert decoded == value, f"round-trip failed for {value}"
        assert pos == len(encoded), f"pos mismatch for {value}"


def test_varint_negative_raises():
    with pytest.raises(ValueError, match="non-negative"):
        _encode_varint(-1)


def test_varint_truncated_raises():
    with pytest.raises(ValueError, match="truncated"):
        _decode_varint(b"\x80", 0)  # continuation bit set but no more bytes


# ---------------------------------------------------------------------------
# Field-tag constant values (cited against firmware source)
# ---------------------------------------------------------------------------


def test_tag_constants_match_firmware_source():
    """Field numbers from flipper.proto PB_Main.oneof content.

    Source: github.com/Next-Flip/flipperzero-protobuf @ ea4f185f
    File: flipper.proto, message Main, oneof content.
    """
    assert TAG_EMPTY_RESPONSE == 4
    assert TAG_STOP_SESSION == 19


# ---------------------------------------------------------------------------
# encode_main / decode_main round-trips
# ---------------------------------------------------------------------------


def _split_delimited(frame: bytes) -> bytes:
    """Strip nanopb PB_ENCODE_DELIMITED varint length prefix → payload bytes."""
    msg_len, after = _decode_varint(frame, 0)
    assert len(frame) - after == msg_len
    return frame[after : after + msg_len]


def test_encode_main_uses_varint_length_prefix():
    """encode_main uses nanopb PB_ENCODE_DELIMITED framing (varint prefix)."""
    frame = encode_main(1, _CONTENT_FIELD, encode_empty())
    msg_len, after = _decode_varint(frame, 0)
    assert msg_len == len(frame) - after


def test_encode_decode_round_trip_empty_payload():
    """A message with empty content can be encoded and decoded."""
    frame = encode_main(42, _CONTENT_FIELD, encode_empty())
    pb_bytes = _split_delimited(frame)

    cmd_id, command_status, field_num, payload, has_next = decode_main(pb_bytes)
    assert cmd_id == 42
    assert command_status == 0
    assert field_num == _CONTENT_FIELD
    assert payload == b""
    assert has_next is False


def test_encode_decode_round_trip_with_payload():
    """A message with non-empty content survives the round-trip."""
    inner_payload = b"\x01\x02\x03\x04\x05"
    frame = encode_main(7, _CONTENT_FIELD, inner_payload)
    pb_bytes = _split_delimited(frame)

    cmd_id, _status, field_num, payload, _has_next = decode_main(pb_bytes)
    assert cmd_id == 7
    assert field_num == _CONTENT_FIELD
    assert payload == inner_payload


def test_encode_decode_stop_session():
    frame = encode_main(99, TAG_STOP_SESSION, encode_empty())
    pb_bytes = _split_delimited(frame)

    _cmd_id, _status, field_num, payload, _has_next = decode_main(pb_bytes)
    assert field_num == TAG_STOP_SESSION
    assert payload == b""


def test_decode_command_id_zero():
    """command_id=0 is valid (field is omitted in proto3 when zero)."""
    frame = encode_main(0, TAG_EMPTY_RESPONSE, encode_empty())
    pb_bytes = _split_delimited(frame)

    cmd_id, _status, field_num, _payload, _has_next = decode_main(pb_bytes)
    assert cmd_id == 0
    assert field_num == TAG_EMPTY_RESPONSE


# ---------------------------------------------------------------------------
# try_read_delimited — slice a single varint-prefixed PB_Main from a buffer
# ---------------------------------------------------------------------------


def test_try_read_delimited_returns_none_when_incomplete():
    """An empty buffer or partial varint returns None (need more bytes)."""
    assert try_read_delimited(b"") is None
    assert try_read_delimited(b"\x80") is None  # varint continuation, no follow-up


def test_try_read_delimited_returns_none_when_payload_short():
    """A complete varint but truncated payload returns None."""
    frame = encode_main(1, _CONTENT_FIELD, encode_empty())
    # Take the varint prefix but only half the payload
    assert try_read_delimited(frame[:-1]) is None


def test_try_read_delimited_single_frame():
    """A complete frame is sliced cleanly."""
    frame = encode_main(7, _CONTENT_FIELD, b"\x01\x02\x03")
    result = try_read_delimited(frame)
    assert result is not None
    payload, consumed = result
    assert consumed == len(frame)
    cmd_id, _status, field_num, content, _has_next = decode_main(payload)
    assert cmd_id == 7
    assert field_num == _CONTENT_FIELD
    assert content == b"\x01\x02\x03"


def test_try_read_delimited_back_to_back_frames():
    """Two concatenated frames: read first, leave second."""
    a = encode_main(1, TAG_EMPTY_RESPONSE, encode_empty())
    b = encode_main(2, _CONTENT_FIELD, b"\xff")
    result = try_read_delimited(a + b)
    assert result is not None
    payload, consumed = result
    assert consumed == len(a)
    cmd_id, _status, _field, _payload, _has_next = decode_main(payload)
    assert cmd_id == 1
    # Remaining buffer still parses as the second frame
    result2 = try_read_delimited((a + b)[consumed:])
    assert result2 is not None
    _, c2 = result2
    assert c2 == len(b)


def test_try_read_delimited_implausible_length_raises():
    """A varint length above the RPC max raises ValueError."""
    huge = _encode_varint(50_000) + b"\x00" * 10
    with pytest.raises(ValueError, match="exceeds RPC max"):
        try_read_delimited(huge)


# ---------------------------------------------------------------------------
# encode_empty
# ---------------------------------------------------------------------------


def test_encode_empty_returns_empty_bytes():
    assert encode_empty() == b""


# ---------------------------------------------------------------------------
# decode_main: command_status (field 2) vs has_next (field 3)
# ---------------------------------------------------------------------------
# Verified against flipperzero-protobuf @ ea4f185f, flipper.proto `message Main`:
#   field 2 = command_status (CommandStatus enum, varint)
#   field 3 = has_next (bool, varint)
# An earlier version of decode_main read field 2 as has_next — these tests pin
# the proto-correct mapping so the bug can't silently regress.


def _hand_build_main(
    command_id: int,
    command_status: int,
    has_next: bool,
    content_field_num: int,
    content_payload: bytes,
) -> bytes:
    """Build a PB_Main payload (no framing varint) field-by-field.

    Lets tests exercise decode_main against bytes that don't come from
    encode_main, so a future bug in encode_main can't mask a decode bug.
    """
    parts = bytearray()
    if command_id:
        parts += _encode_field_varint((1 << 3) | 0, command_id)
    if command_status:
        parts += _encode_field_varint((2 << 3) | 0, command_status)
    if has_next:
        parts += _encode_field_varint((3 << 3) | 0, 1)
    parts += _encode_field_ld(content_field_num, content_payload)
    return bytes(parts)


def test_decode_main_reads_field_2_as_command_status():
    """Field 2 in PB_Main is command_status, not has_next."""
    pb = _hand_build_main(
        command_id=1,
        command_status=5,  # arbitrary non-OK code
        has_next=False,
        content_field_num=TAG_EMPTY_RESPONSE,
        content_payload=b"",
    )
    cmd_id, command_status, field_num, _payload, has_next = decode_main(pb)
    assert cmd_id == 1
    assert command_status == 5
    assert has_next is False
    assert field_num == TAG_EMPTY_RESPONSE


def test_decode_main_reads_field_3_as_has_next():
    """Field 3 in PB_Main is has_next, not command_status."""
    pb = _hand_build_main(
        command_id=2,
        command_status=0,
        has_next=True,
        content_field_num=TAG_EMPTY_RESPONSE,
        content_payload=b"",
    )
    _cmd, command_status, _field, _payload, has_next = decode_main(pb)
    assert command_status == 0
    assert has_next is True


def test_decode_main_regression_error_status_not_misread_as_has_next():
    """Regression: a device frame with command_status=NOT_OK and
    has_next=False must NOT be misread as a streaming-continuation frame.

    Before the fix, decode_main read field 2 as has_next — so any error
    response (command_status != 0) would surface as has_next=truthy and the
    receive loop would block waiting for more frames that never arrive.
    """
    # Hand-build: command_id=42, command_status=3 (some FS error),
    # NO has_next field at all (proto3 default-elision).
    pb = _hand_build_main(
        command_id=42,
        command_status=3,
        has_next=False,
        content_field_num=TAG_EMPTY_RESPONSE,
        content_payload=b"",
    )
    _cmd, command_status, _field, _payload, has_next = decode_main(pb)
    assert command_status == 3
    assert has_next is False  # must NOT be truthy — that was the bug


def test_decode_main_status_ok_and_has_next_both_zero_default():
    """When both command_status and has_next are zero (the proto3 default),
    they should decode to 0 / False respectively even though their tags are
    absent from the wire bytes."""
    pb = _hand_build_main(
        command_id=0,
        command_status=0,
        has_next=False,
        content_field_num=TAG_EMPTY_RESPONSE,
        content_payload=b"",
    )
    cmd_id, command_status, _field, _payload, has_next = decode_main(pb)
    assert cmd_id == 0
    assert command_status == 0
    assert has_next is False


# ---------------------------------------------------------------------------
# encode_storage_read_request / parse_storage_read_response
# ---------------------------------------------------------------------------


def _hand_build_read_request_payload(path: str) -> bytes:
    """Reference encoding for Storage.ReadRequest { string path = 1; }.

    Built independently of encode_storage_read_request so the test catches
    encoder bugs.
    """
    path_bytes = path.encode("utf-8")
    tag = (1 << 3) | 2  # field 1, wire type 2 (length-delimited)
    return _encode_varint(tag) + _encode_varint(len(path_bytes)) + path_bytes


def _hand_build_read_response_payload(file_data: bytes) -> bytes:
    """Reference encoding for ReadResponse { File file = 1; }.

    File { bytes data = 4; } — only the data field is set.
    """
    # Inner File payload: just `data` field (tag 4, length-delimited).
    file_tag = (4 << 3) | 2
    file_payload = (
        _encode_varint(file_tag) + _encode_varint(len(file_data)) + file_data
    )
    # Outer ReadResponse: `file` field (tag 1, length-delimited submessage).
    file_field_tag = (1 << 3) | 2
    return (
        _encode_varint(file_field_tag)
        + _encode_varint(len(file_payload))
        + file_payload
    )


def test_encode_storage_read_request_matches_reference_payload():
    """encode_storage_read_request bytes match a hand-built reference exactly."""
    path = "/ext/foo.nfc"
    reference = _hand_build_read_request_payload(path)
    assert encode_storage_read_request(path) == reference


def test_encode_storage_read_request_utf8_path():
    """Non-ASCII paths are encoded as UTF-8."""
    path = "/ext/тест.bin"  # Cyrillic
    encoded = encode_storage_read_request(path)
    assert encoded == _hand_build_read_request_payload(path)
    # And the path bytes should be present verbatim
    assert path.encode("utf-8") in encoded


def test_encode_storage_read_request_empty_path_round_trip():
    """Empty path encodes to (tag, len=0) and survives the round trip."""
    encoded = encode_storage_read_request("")
    # Tag byte 0x0a + length varint 0x00
    assert encoded == b"\x0a\x00"


def test_parse_storage_read_response_binary_safe():
    """parse_storage_read_response returns ALL 256 byte values verbatim."""
    data = bytes(range(256))
    payload = _hand_build_read_response_payload(data)
    assert parse_storage_read_response(payload) == data


def test_parse_storage_read_response_with_zero_bytes():
    """Payloads containing 0x00 are returned without truncation."""
    data = b"\x00" * 16 + b"after-zeros" + b"\x00\x00"
    payload = _hand_build_read_response_payload(data)
    assert parse_storage_read_response(payload) == data


def test_parse_storage_read_response_with_high_bit_bytes():
    """High-bit bytes (0x80-0xFF) survive — these would be mis-parsed as varint
    continuations by a buggy walker."""
    data = bytes([0x80, 0xFF, 0xAA, 0x55] * 64)
    payload = _hand_build_read_response_payload(data)
    assert parse_storage_read_response(payload) == data


def test_parse_storage_read_response_empty_data_field():
    """An empty File.data field round-trips as b''."""
    payload = _hand_build_read_response_payload(b"")
    assert parse_storage_read_response(payload) == b""


def test_parse_storage_read_response_missing_file_field_returns_empty():
    """A ReadResponse with no file field at all returns b'' (proto3 default
    elision for the inner submessage)."""
    assert parse_storage_read_response(b"") == b""


def test_parse_storage_read_response_skips_unknown_inner_fields():
    """File fields type/name/size/md5sum are tolerated, only `data` is returned."""
    # Build an inner File payload with type=0, name="x", size=3, data="abc"
    file_parts = bytearray()
    file_parts += _encode_field_varint((1 << 3) | 0, 0)  # type
    file_parts += _encode_field_ld(2, b"x")  # name
    file_parts += _encode_field_varint((3 << 3) | 0, 3)  # size
    file_parts += _encode_field_ld(4, b"abc")  # data
    file_payload = bytes(file_parts)
    payload = _encode_field_ld(1, file_payload)
    assert parse_storage_read_response(payload) == b"abc"


# ---------------------------------------------------------------------------
# encode_storage_write_request (round-trip tests)
# ---------------------------------------------------------------------------
# Reference encoding hand-built independently of the encoder so a bug in the
# encoder can't mask itself by also being present in the test fixture. Wire
# layout cited at clipper/rpc.py (TAG_STORAGE_WRITE_REQUEST comment block):
#   WriteRequest { string path = 1; File file = 2; }
#   File { ... bytes data = 4; ... }


def _hand_build_write_request_payload(path: str, data: bytes) -> bytes:
    """Reference encoding for Storage.WriteRequest.

    Built byte-by-byte from the proto definition so the test is independent
    of the production encoder.
    """
    # Inner File: just `data` at field 4.
    file_data_tag = (4 << 3) | 2
    file_payload = (
        _encode_varint(file_data_tag)
        + _encode_varint(len(data))
        + data
    )
    # Outer WriteRequest: path at field 1 (length-delimited UTF-8), File at field 2.
    path_bytes = path.encode("utf-8")
    path_tag = (1 << 3) | 2
    file_field_tag = (2 << 3) | 2
    return (
        _encode_varint(path_tag)
        + _encode_varint(len(path_bytes))
        + path_bytes
        + _encode_varint(file_field_tag)
        + _encode_varint(len(file_payload))
        + file_payload
    )


def test_encode_storage_write_request_empty_path_empty_data():
    """Minimal valid payload: empty path + empty data still emits both fields."""
    encoded = encode_storage_write_request("", b"")
    reference = _hand_build_write_request_payload("", b"")
    assert encoded == reference
    # Sanity: contains the path tag (0x0a), zero-length path, the file tag
    # (0x12), the File-payload length (2 bytes: tag 0x22 + len 0), then 0x22 0x00.
    assert encoded == b"\x0a\x00\x12\x02\x22\x00"


def test_encode_storage_write_request_utf8_path():
    """UTF-8 paths with non-ASCII chars are encoded byte-for-byte verbatim."""
    path = "/ext/тест/файл.bin"  # Cyrillic
    data = b"payload"
    encoded = encode_storage_write_request(path, data)
    assert encoded == _hand_build_write_request_payload(path, data)
    # Path UTF-8 bytes appear verbatim in the wire payload (no escaping).
    assert path.encode("utf-8") in encoded


def test_encode_storage_write_request_all_256_byte_values():
    """Binary safety: every byte 0x00-0xFF survives round-trip into the
    wire payload. Catches any premature null truncation, varint
    mis-tagging, or sign-extension bugs.
    """
    data = bytes(range(256))
    encoded = encode_storage_write_request("/ext/binary.bin", data)
    assert encoded == _hand_build_write_request_payload("/ext/binary.bin", data)
    # The full payload bytes appear contiguously inside the encoded frame.
    assert data in encoded


def test_encode_storage_write_request_zero_bytes_mid_stream():
    """Data containing 0x00 in the middle does NOT get truncated — the
    wire is length-delimited, so embedded nulls are fine.
    """
    data = b"abc" + b"\x00" * 100 + b"def" + b"\x00\xff\x00\xff"
    encoded = encode_storage_write_request("/ext/nullmid.bin", data)
    assert encoded == _hand_build_write_request_payload("/ext/nullmid.bin", data)
    # All raw payload bytes survived verbatim.
    assert data in encoded


# ---------------------------------------------------------------------------
# encode_storage_mkdir_request (used by create_parents=True)
# ---------------------------------------------------------------------------


def test_encode_storage_mkdir_request_matches_reference_payload():
    """encode_storage_mkdir_request bytes match a hand-built reference exactly.

    Per storage.proto @ ea4f185f: MkdirRequest { string path = 1; }
    — same shape as ReadRequest, so the reference encoder is identical.
    """
    path = "/ext/foo/bar"
    path_bytes = path.encode("utf-8")
    reference = (
        _encode_varint((1 << 3) | 2)
        + _encode_varint(len(path_bytes))
        + path_bytes
    )
    assert encode_storage_mkdir_request(path) == reference


# ---------------------------------------------------------------------------
# encode_main has_next backwards-compat + new field 3 emission
# ---------------------------------------------------------------------------


def test_encode_main_has_next_default_omits_field_3():
    """Default has_next=False produces bytes byte-identical to the earlier encoder.

    Regression guard: the new `has_next` keyword must NOT change the wire
    format for any existing caller that doesn't pass it.
    """
    # Build with the new keyword absent → should match.
    frame_legacy = encode_main(1, _CONTENT_FIELD, b"abc")
    frame_explicit_false = encode_main(1, _CONTENT_FIELD, b"abc", has_next=False)
    assert frame_legacy == frame_explicit_false
    # Decode to verify has_next is False either way.
    pb = _split_delimited(frame_legacy)
    _cmd, _status, _field, _payload, has_next = decode_main(pb)
    assert has_next is False


def test_encode_main_has_next_true_round_trips():
    """has_next=True emits PB_Main field 3 = varint(1) and decodes back to True."""
    frame = encode_main(1, _CONTENT_FIELD, b"xyz", has_next=True)
    pb = _split_delimited(frame)
    _cmd, _status, _field, payload, has_next = decode_main(pb)
    assert has_next is True
    assert payload == b"xyz"
    # Sanity: the explicit field-3 tag byte (0x18) must appear in the wire bytes.
    assert b"\x18\x01" in pb
