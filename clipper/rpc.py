"""clipper.rpc — Minimal hand-rolled Flipper RPC protobuf encode/decode.

Implements just the handful of message types the server needs to frame
binary-safe Storage operations over RPC:
  PB_Main (wrapper), Storage Read/Write/Mkdir requests + responses, Empty.

Field tag numbers are sourced from the flipperzero-protobuf submodule used
by Momentum firmware (commit ea4f185f5eaa265955c520eae2832887ee6aa5e4):
  - flipper.proto  — PB_Main.oneof content field numbers
  - storage.proto  — Storage request/response inner field numbers
Source URL: https://github.com/Next-Flip/flipperzero-protobuf/tree/ea4f185f

Protobuf wire format summary (proto3):
  Each field is encoded as (tag << 3) | wire_type followed by the value.
  Wire types used here:
    0 = varint  (uint32, bool, enum)
    2 = length-delimited (bytes, embedded messages)
  Varints are encoded little-endian 7 bits per byte, MSB=1 means more follows.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PB_Main.oneof content field numbers (from flipper.proto)
# ---------------------------------------------------------------------------
# Field 4 — empty response  (when there is no real payload to return)
# Field 19 — StopSession request

TAG_EMPTY_RESPONSE: int = 4
TAG_STOP_SESSION: int = 19

# ---------------------------------------------------------------------------
# Storage RPC PB_Main.oneof content field numbers
# ---------------------------------------------------------------------------
# Source: flipperzero-protobuf commit ea4f185f5eaa265955c520eae2832887ee6aa5e4
#   file: flipper.proto
#   URL:  https://github.com/Next-Flip/flipperzero-protobuf/blob/ea4f185f/flipper.proto
#
#   Line 92:  .PB_Storage.ReadRequest  storage_read_request  = 9;
#   Line 93:  .PB_Storage.ReadResponse storage_read_response = 10;
#
# Context (other Storage oneof entries from the same file, NOT yet wired up):
#   Line 84-85:  storage_info_request = 28 / storage_info_response = 29
#   Line 88-89:  storage_stat_request = 24 / storage_stat_response = 25
#   Line 90-91:  storage_list_request = 7  / storage_list_response = 8
#   Line 97-98:  storage_md5sum_request = 14 / storage_md5sum_response = 15
# These are recorded here for traceability but intentionally NOT exported as
# constants — only the field numbers needed for binary-safe reads ship now.

TAG_STORAGE_READ_REQUEST: int = 9
TAG_STORAGE_READ_RESPONSE: int = 10

# Storage.WriteRequest — added in the mfkey-and-write change.
#
# Source: flipperzero-protobuf commit ea4f185f5eaa265955c520eae2832887ee6aa5e4
#   file: flipper.proto
#   URL:  https://github.com/Next-Flip/flipperzero-protobuf/blob/ea4f185f/flipper.proto
#
#   Line 94:  .PB_Storage.WriteRequest storage_write_request = 11;
#
# Inner-message shape — verified against the same SHA's storage.proto:
#   Line 61-64:  message WriteRequest {
#                    string path = 1;
#                    File   file = 2;
#                }
# The nested File submessage carries the actual payload bytes at field 4
# (`bytes data = 4;`), already documented above via _STORAGE_FILE_DATA_TAG.
#
# Response shape: the firmware acknowledges a (final) WriteRequest with a
# PB_Main carrying an Empty inner message (oneof field 4). See rpc_storage.c
# `rpc_send_and_release_empty(...)` at line 473 of the firmware citation
# documented below in _STORAGE_WRITE_MAX_DATA_PER_FRAME.

TAG_STORAGE_WRITE_REQUEST: int = 11

# Storage.MkdirRequest — create_parents support walks parent paths via this RPC.
#
# Source: flipperzero-protobuf commit ea4f185f5eaa265955c520eae2832887ee6aa5e4
#   file: flipper.proto
#   URL:  https://github.com/Next-Flip/flipperzero-protobuf/blob/ea4f185f/flipper.proto
#
#   Line 96:  .PB_Storage.MkdirRequest storage_mkdir_request = 13;
#
# Inner-message shape — verified against the same SHA's storage.proto:
#   Line 73-75:  message MkdirRequest {
#                    string path = 1;
#                }
# The path is encoded inline at field 1 (length-delimited UTF-8 string), NOT
# wrapped in a Storage.File submessage — unlike WriteRequest. The firmware
# acknowledges with an Empty (PB_Main oneof field 4), or returns
# command_status=ERROR_STORAGE_EXIST (=6) if the directory already exists,
# which _mkdir_parents in storage.py treats as success.

TAG_STORAGE_MKDIR_REQUEST: int = 13

# Storage error: directory already exists.
#
# Source: flipperzero-protobuf commit ea4f185f5eaa265955c520eae2832887ee6aa5e4
#   file: flipper.proto (CommandStatus enum)
#   URL:  https://github.com/Next-Flip/flipperzero-protobuf/blob/ea4f185f/flipper.proto
#
#   Line 24:  ERROR_STORAGE_EXIST = 6; /**< File/Dir already exist */
#
# The flipper.proto here is the same enum the Momentum firmware emits via
# rpc_send_and_release_empty(status=...) on a MkdirRequest whose target
# already exists. _mkdir_parents in clipper/hardware/storage.py treats this
# specific code as success so an idempotent "ensure parents" walk can run
# without first stat'ing every level.

STORAGE_ERROR_ALREADY_EXIST: int = 6

# Per-frame Storage.WriteRequest `File.data` size cap.
#
# Source: Next-Flip/Momentum-Firmware tag mntm-012
#   tag SHA: e1784e7418d8b074e971983ceb6fef0f37e52ae4
#   file:    applications/services/rpc/rpc_storage.c
#   URL:     https://github.com/Next-Flip/Momentum-Firmware/blob/
#            e1784e7418d8b074e971983ceb6fef0f37e52ae4/applications/services/
#            rpc/rpc_storage.c
#
# The firmware defines a single shared cap used by both the READ chunker
# and (by symmetry) the write path:
#
#   Line 21:  static const size_t MAX_DATA_SIZE = 512;
#   Line 370: size_t read_size = MIN(size_left, MAX_DATA_SIZE);
#
# The write-side handler (`rpc_system_storage_write_process`, lines 411–476)
# does NOT explicitly bounds-check incoming `File.data` against MAX_DATA_SIZE
# — it just passes whatever buffer arrives straight to `storage_file_write`
# (line 455). However:
#   * The outer PB_Main frame is already varint-delimited and capped by
#     `try_read_delimited` in this file at 1500 bytes (a Flipper RPC ceiling).
#   * The reference chunk size used on the symmetric read path is 512.
#   * Line 459 of the write handler shows multi-frame WriteRequest is
#     explicitly supported via `has_next` — intermediate frames carry
#     has_next=True, only the final frame triggers a response (the
#     streaming-RPC pattern).
#
# We adopt the firmware's own 512-byte chunk size as the outbound cap so
# the host's WriteRequest frames mirror the firmware's ReadResponse
# chunking exactly. Comfortably below the 1500-byte PB_Main ceiling even
# after path + oneof + File + nested-data field tags + length prefixes
# (~10–20 bytes of metadata).

_STORAGE_WRITE_MAX_DATA_PER_FRAME: int = 512

# ---------------------------------------------------------------------------
# Storage.File inner-message field numbers (used inside ReadResponse.file)
# ---------------------------------------------------------------------------
# Source: flipperzero-protobuf commit ea4f185f5eaa265955c520eae2832887ee6aa5e4
#   file: storage.proto
#   URL:  https://github.com/Next-Flip/flipperzero-protobuf/blob/ea4f185f/storage.proto
#
#   Line  6-16:  message File { ... }
#   Line 11:     FileType type   = 1;   // enum: 0=FILE, 1=DIR  (varint)
#   Line 12:     string   name   = 2;   // length-delimited (UTF-8)
#   Line 13:     uint32   size   = 3;   // varint
#   Line 14:     bytes    data   = 4;   // length-delimited (raw bytes — binary safe)
#   Line 15:     string   md5sum = 5;   // length-delimited
#
# Also at line 57-59 — ReadResponse:
#   message ReadResponse { File file = 1; }
# i.e. ReadResponse wraps a single Storage.File at field 1 (length-delimited).
#
# Wire-type encoding reminder: tag byte = (field_num << 3) | wire_type
#   wire_type 0 = varint, wire_type 2 = length-delimited

_STORAGE_FILE_TYPE_TAG = (1 << 3) | 0  # 0x08 (varint enum)
_STORAGE_FILE_NAME_TAG = (2 << 3) | 2  # 0x12 (length-delimited string)
_STORAGE_FILE_SIZE_TAG = (3 << 3) | 0  # 0x18 (varint uint32)
_STORAGE_FILE_DATA_TAG = (4 << 3) | 2  # 0x22 (length-delimited bytes)
_STORAGE_FILE_MD5SUM_TAG = (5 << 3) | 2  # 0x2A (length-delimited string)

# ---------------------------------------------------------------------------
# PB_Main outer field numbers
# ---------------------------------------------------------------------------
# Verified against flipperzero-protobuf @ ea4f185f, flipper.proto `message Main`:
#   command_id:     field 1, varint (uint32)
#   command_status: field 2, varint (CommandStatus enum; 0 = COMMAND_STATUS_OK)
#   has_next:       field 3, varint (bool)
#   oneof content:  fields 4–100 — the specific message type occupies one slot
#
# NOTE: an earlier version of this file's comment block labeled these
# fields as `has_next=2, command_status=3` and decode_main implemented
# that ordering. The proto says the opposite, which is what this code now
# follows: command_status=2, has_next=3.

_MAIN_COMMAND_ID_TAG = (1 << 3) | 0  # 0x08
_MAIN_STATUS_TAG = (2 << 3) | 0  # 0x10  (command_status varint)
_MAIN_HAS_NEXT_TAG = (3 << 3) | 0  # 0x18  (has_next varint)


# ---------------------------------------------------------------------------
# Varint helpers
# ---------------------------------------------------------------------------


def _encode_varint(value: int) -> bytes:
    """Encode *value* as a protobuf varint (little-endian, 7 bits/byte)."""
    if value < 0:
        raise ValueError(f"varint must be non-negative, got {value}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def _decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Decode a varint from *buf* starting at *pos*.

    Returns (value, new_pos).
    Raises ValueError on truncated input.
    """
    value = 0
    shift = 0
    while pos < len(buf):
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, pos
        shift += 7
        if shift >= 64:
            raise ValueError("varint too long (>64-bit)")
    raise ValueError("buffer truncated while decoding varint")


# ---------------------------------------------------------------------------
# Length-delimited field helpers
# ---------------------------------------------------------------------------


def _encode_field_ld(field_num: int, payload: bytes) -> bytes:
    """Encode a length-delimited field (wire type 2)."""
    tag = (field_num << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(payload)) + payload


def _encode_field_varint(tag_byte: int, value: int) -> bytes:
    """Encode a varint field given the already-computed tag byte."""
    return _encode_varint(tag_byte) + _encode_varint(value)


# ---------------------------------------------------------------------------
# Public encode helpers
# ---------------------------------------------------------------------------


def encode_empty() -> bytes:
    """Encode an Empty inner message (zero-length bytes)."""
    return b""


def encode_main(
    command_id: int,
    content_field_num: int,
    content_payload: bytes,
    *,
    has_next: bool = False,
) -> bytes:
    """Encode a PB_Main wrapper.

    Args:
        command_id:        Monotonically increasing command ID (uint32).
        content_field_num: The oneof field number for the inner message
                           (one of the TAG_* constants above).
        content_payload:   Pre-encoded inner message bytes.
        has_next:          Outbound multi-frame chaining flag (PB_Main field 3,
                           varint bool). When True, includes the explicit
                           ``has_next=1`` varint; when False (the default), the
                           field is omitted under proto3 default-elision so the
                           wire bytes are byte-identical to the pre-mfkey-and-
                           write encoder. Used by Storage.WriteRequest chunking
                          : every chunk except the last sets has_next=True
                           and the firmware only acknowledges the final frame.

    Returns:
        The fully-encoded PB_Main bytes, length-prefixed for framing.
        The Flipper RPC stream uses nanopb's PB_ENCODE_DELIMITED framing:
        a protobuf varint encoding the payload length, followed by the
        PB_Main payload bytes. NOT a 4-byte big-endian prefix.
    """
    parts = bytearray()

    # Field 1: command_id
    if command_id:
        parts += _encode_field_varint(_MAIN_COMMAND_ID_TAG, command_id)

    # Field 3: has_next (only when True — proto3 default-elision means
    # legacy callers that pass nothing get byte-identical output).
    if has_next:
        parts += _encode_field_varint(_MAIN_HAS_NEXT_TAG, 1)

    # oneof content: field N (length-delimited, wire type 2)
    # An Empty message (zero bytes) still needs the field tag + length=0.
    parts += _encode_field_ld(content_field_num, content_payload)

    pb_bytes = bytes(parts)
    return _encode_varint(len(pb_bytes)) + pb_bytes


def try_read_delimited(buf: bytes) -> tuple[bytes, int] | None:
    """Try to slice one length-delimited PB_Main payload from the front of *buf*.

    Returns (payload, consumed) on success, or None if *buf* is incomplete.
    Raises ValueError on a varint length that exceeds the Flipper RPC max
    message size (1500 bytes).
    """
    try:
        msg_len, after_varint = _decode_varint(buf, 0)
    except ValueError:
        # Truncated varint — wait for more bytes
        return None
    if msg_len > 1500:
        raise ValueError(f"varint length {msg_len} exceeds RPC max — desync")
    end = after_varint + msg_len
    if end > len(buf):
        return None
    return bytes(buf[after_varint:end]), end


def decode_main(buf: bytes) -> tuple[int, int, int, bytes, bool]:
    """Decode a PB_Main from *buf* (the raw protobuf bytes, WITHOUT the framing varint prefix).

    Returns:
        (command_id, command_status, content_field_num, content_payload, has_next)

    Field-number mapping (from flipper.proto @ ea4f185f `message Main`):
        field 1 → command_id     (varint uint32)
        field 2 → command_status (varint enum; 0 = COMMAND_STATUS_OK)
        field 3 → has_next       (varint bool)
        field >=4 → oneof content (length-delimited)

    content_payload is the raw bytes of the embedded message; it may be empty
    for content messages that carry no data (e.g. an Empty response).

    Raises:
        ValueError on malformed protobuf.
    """
    command_id = 0
    command_status = 0
    has_next = False
    content_field_num = 0
    content_payload = b""

    pos = 0
    while pos < len(buf):
        tag_value, pos = _decode_varint(buf, pos)
        wire_type = tag_value & 0x07
        field_num = tag_value >> 3

        if wire_type == 0:  # varint
            value, pos = _decode_varint(buf, pos)
            if field_num == 1:
                command_id = value
            elif field_num == 2:
                command_status = value
            elif field_num == 3:
                has_next = bool(value)
            # other varint fields are tolerated and ignored
        elif wire_type == 2:  # length-delimited
            length, pos = _decode_varint(buf, pos)
            payload = buf[pos : pos + length]
            pos += length
            if field_num >= 4:  # content oneof
                content_field_num = field_num
                content_payload = payload
        else:
            # Skip unknown wire types (future-proofing)
            log.warning("rpc.decode_main: unknown wire type %d for field %d", wire_type, field_num)
            break

    return command_id, command_status, content_field_num, content_payload, has_next


# ---------------------------------------------------------------------------
# Storage.ReadRequest / Storage.ReadResponse helpers
# ---------------------------------------------------------------------------
# Source: flipperzero-protobuf @ ea4f185f (see file header), storage.proto:
#   message ReadRequest  { string path = 1; }                # field 1, length-delimited string
#   message ReadResponse { File file   = 1; }                # field 1, length-delimited submessage
#   message File         { ... bytes data = 4; ... }         # field 4, length-delimited bytes
#
# These two functions wrap exactly enough of that schema to carry binary file
# bytes through a Storage.ReadRequest / Storage.ReadResponse roundtrip — the
# other File fields (type, name, size, md5sum) are tolerated on decode but not
# emitted on encode. This is the binary-safe transport for file reads, so files
# containing CR/LF/NUL/0xFF survive verbatim — unlike the text-CLI
# `storage read`.


def encode_storage_read_request(path: str) -> bytes:
    """Encode a Storage.ReadRequest inner-message payload.

    The wire layout is just one length-delimited field at tag 1 carrying the
    UTF-8 path bytes:  ``(1 << 3 | 2) varint(len(path)) path_bytes``.

    An empty path encodes to ``\\x0a\\x00`` (tag byte + length=0) — the proto3
    default-string-elision rule for the OUTER PB_Main does NOT apply here
    because the request frame's presence is signalled by the oneof tag in
    PB_Main; we always emit the path field even if empty so the device sees an
    explicit Storage.ReadRequest (with whatever path policy it has for "").
    """
    path_bytes = path.encode("utf-8")
    return _encode_field_ld(1, path_bytes)


def encode_storage_write_request(path: str, data: bytes) -> bytes:
    """Encode a Storage.WriteRequest inner-message payload.

    Wire layout — verified against flipperzero-protobuf @ ea4f185f
    (see this file's TAG_STORAGE_WRITE_REQUEST comment block above for SHA
    + line citations):

        WriteRequest {
            string path = 1;    // field 1, length-delimited UTF-8
            File   file = 2;    // field 2, length-delimited submessage
        }
        File { ... bytes data = 4; ... }

    Only the nested ``File.data`` is set; ``type``, ``name``, ``size``,
    ``md5sum`` are all elided under proto3 default-elision. The firmware's
    write handler only reads ``data`` off the inbound File (see the
    rpc_storage.c citation in this file's _STORAGE_WRITE_MAX_DATA_PER_FRAME
    block).

    For payloads larger than _STORAGE_WRITE_MAX_DATA_PER_FRAME (512 bytes),
    the host MUST chunk and send multiple WriteRequest frames with
    PB_Main.has_next=True on every frame except the last. That chunking is
    NOT performed here — this function encodes a single chunk's worth of
    inner payload. The chaining lives in clipper/hardware/storage.py's
    ``_write_chunks`` helper.

    Args:
        path: Absolute Flipper filesystem path (e.g. "/ext/foo.bin"). UTF-8.
        data: Raw bytes for this chunk's File.data. May be empty (yields a
              minimally valid WriteRequest with an empty inner File).

    Returns:
        The encoded WriteRequest payload bytes (suitable as the
        content_payload argument to encode_main).
    """
    # Inner File: only the `data` field (tag 4, length-delimited bytes).
    # _STORAGE_FILE_DATA_TAG = (4 << 3) | 2 = 0x22 (the precomputed tag byte);
    # _encode_field_ld takes the field NUMBER directly.
    file_payload = _encode_field_ld(4, data)
    # Outer WriteRequest: path at field 1, then File submessage at field 2.
    return (
        _encode_field_ld(1, path.encode("utf-8"))
        + _encode_field_ld(2, file_payload)
    )


def encode_storage_mkdir_request(path: str) -> bytes:
    """Encode a Storage.MkdirRequest inner-message payload (create_parents).

    Wire layout — verified against flipperzero-protobuf @ ea4f185f
    (see this file's TAG_STORAGE_MKDIR_REQUEST comment block above for SHA
    + line citations):

        MkdirRequest { string path = 1; }

    Unlike WriteRequest, the path lives at field 1 DIRECTLY — there is no
    nested Storage.File wrapper.

    Args:
        path: Absolute Flipper filesystem path of the directory to create
              (e.g. "/ext/foo/bar"). UTF-8.

    Returns:
        The encoded MkdirRequest payload bytes (suitable as the
        content_payload argument to encode_main).
    """
    return _encode_field_ld(1, path.encode("utf-8"))


def parse_storage_read_response(payload: bytes) -> bytes:
    """Extract the file ``data`` field from a Storage.ReadResponse payload.

    Walks two nested length-delimited submessages:
      ReadResponse { file = 1 } → File { ... data = 4 ... }

    Returns the raw bytes of ``File.data`` — verbatim, binary-safe (every byte
    0x00–0xFF is preserved). Returns ``b""`` if the data field is absent
    (e.g. a zero-byte file would simply omit field 4 under proto3 default-elision
    rules; we treat that as "empty file" rather than malformed).

    Raises:
        ValueError on malformed protobuf framing.
    """
    file_payload = b""

    pos = 0
    while pos < len(payload):
        tag_value, pos = _decode_varint(payload, pos)
        wire_type = tag_value & 0x07
        field_num = tag_value >> 3

        if wire_type == 2 and field_num == 1:  # File submessage
            length, pos = _decode_varint(payload, pos)
            file_payload = payload[pos : pos + length]
            pos += length
        elif wire_type == 2:
            length, pos = _decode_varint(payload, pos)
            pos += length  # skip unknown length-delimited fields
        elif wire_type == 0:
            _, pos = _decode_varint(payload, pos)  # skip unknown varint
        else:
            log.warning(
                "parse_storage_read_response: unexpected wire type %d field %d",
                wire_type,
                field_num,
            )
            break

    # Now walk the inner File message for tag 4 (data bytes).
    data = b""
    pos = 0
    while pos < len(file_payload):
        tag_value, pos = _decode_varint(file_payload, pos)
        wire_type = tag_value & 0x07
        field_num = tag_value >> 3

        if wire_type == 2 and field_num == 4:  # data
            length, pos = _decode_varint(file_payload, pos)
            data = file_payload[pos : pos + length]
            pos += length
        elif wire_type == 2:
            length, pos = _decode_varint(file_payload, pos)
            pos += length
        elif wire_type == 0:
            _, pos = _decode_varint(file_payload, pos)
        else:
            log.warning(
                "parse_storage_read_response: unexpected inner wire type %d field %d",
                wire_type,
                field_num,
            )
            break

    return data


