"""Tests for clipper.hardware.storage read-only actions.

Covers ``flipper_storage_list``, ``flipper_storage_stat``,
``flipper_storage_md5sum``, and ``flipper_storage_info``.

These tests script the Flipper's responses with ``harness.expect`` (see
``tests/test_hardware_actions.py`` for the canonical pattern).

Storage actions are registered via ``clipper.actions``'s hardware import
block. Importing ``clipper.actions`` here is enough to trigger them;
no direct ``import clipper.hardware.storage`` is needed.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from clipper.actions import ActionParamError, ActionRuntimeError, get
from clipper.flipper import FlipperConnection
from clipper.rpc import (
    _STORAGE_FILE_DATA_TAG,
    TAG_EMPTY_RESPONSE,
    TAG_STORAGE_MKDIR_REQUEST,
    TAG_STORAGE_READ_RESPONSE,
    TAG_STORAGE_WRITE_REQUEST,
    _encode_field_ld,
    _encode_field_varint,
    _encode_varint,
    encode_main,
    encode_storage_write_request,
)

# ---------------------------------------------------------------------------
# Shared helpers (copied from tests/test_hardware_actions.py for isolation)
# ---------------------------------------------------------------------------

HANDSHAKE_DEVICE = "hardware_name: TestFlipper\r\n>: "
HANDSHAKE_POWER = "Battery Charge: 88%\r\n>: "


def script_handshake(harness) -> None:
    """Queue the two responses consumed by FlipperConnection.start()."""
    harness.expect("device_info", HANDSHAKE_DEVICE)
    harness.expect("info power", HANDSHAKE_POWER)


# ===========================================================================
# flipper_storage_list (List a directory)
# ===========================================================================


async def test_list_directory_with_files_and_dirs(fake_flipper):
    """Mixed file+dir listing — entries sorted alphabetically by name."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect(
        "storage list /ext",
        "\t[D] foo\r\n\t[F] bar.txt 1234b\r\n\t[F] baz.nfc 196b\r\n>: ",
    )

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        result = await get("flipper_storage_list").invoke(
            flipper, {"path": "/ext"}, transport="test"
        )
        assert result == {
            "path": "/ext",
            "entries": [
                {"name": "bar.txt", "type": "file", "size": 1234},
                {"name": "baz.nfc", "type": "file", "size": 196},
                {"name": "foo", "type": "dir", "size": 0},
            ],
        }
    finally:
        await flipper.stop()


async def test_list_empty_directory(fake_flipper):
    """Empty directory returns entries=[] (per `\\tEmpty\\r\\n` wire format)."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("storage list /ext/empty", "\tEmpty\r\n>: ")

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        result = await get("flipper_storage_list").invoke(
            flipper, {"path": "/ext/empty"}, transport="test"
        )
        assert result == {"path": "/ext/empty", "entries": []}
    finally:
        await flipper.stop()


async def test_list_filename_with_space(fake_flipper):
    """CRITICAL: filenames may contain spaces — anchor on trailing ' <N>b\\r\\n',
    NOT a naive split(' ')."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect(
        "storage list /ext",
        "\t[F] My File.txt 99b\r\n>: ",
    )

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        result = await get("flipper_storage_list").invoke(
            flipper, {"path": "/ext"}, transport="test"
        )
        assert result == {
            "path": "/ext",
            "entries": [
                {"name": "My File.txt", "type": "file", "size": 99},
            ],
        }
    finally:
        await flipper.stop()


# ===========================================================================
# flipper_storage_stat (Stat a file / missing file)
# ===========================================================================


async def test_stat_file(fake_flipper):
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("storage stat /ext/nfc/foo.nfc", "File, size: 196\r\n>: ")

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        result = await get("flipper_storage_stat").invoke(
            flipper, {"path": "/ext/nfc/foo.nfc"}, transport="test"
        )
        assert result == {
            "path": "/ext/nfc/foo.nfc",
            "type": "file",
            "size": 196,
        }
    finally:
        await flipper.stop()


async def test_stat_directory(fake_flipper):
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("storage stat /ext/nfc", "Directory\r\n>: ")

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        result = await get("flipper_storage_stat").invoke(
            flipper, {"path": "/ext/nfc"}, transport="test"
        )
        assert result == {"path": "/ext/nfc", "type": "dir", "size": 0}
    finally:
        await flipper.stop()


async def test_stat_volume_ext(fake_flipper):
    """Volume prefix shape: 'Storage, <N>KiB total, <N>KiB free'."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect(
        "storage stat /ext",
        "Storage, 30412KiB total, 28811KiB free\r\n>: ",
    )

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        result = await get("flipper_storage_stat").invoke(
            flipper, {"path": "/ext"}, transport="test"
        )
        assert result == {
            "path": "/ext",
            "type": "volume",
            "total_kib": 30412,
            "free_kib": 28811,
            "size": 30412 * 1024,
        }
    finally:
        await flipper.stop()


async def test_stat_root(fake_flipper):
    """Root (path == '/') emits the bare 'Storage' shape."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect("storage stat /", "Storage\r\n>: ")

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        result = await get("flipper_storage_stat").invoke(
            flipper, {"path": "/"}, transport="test"
        )
        assert result == {"path": "/", "type": "root", "size": 0}
    finally:
        await flipper.stop()


async def test_stat_missing_file_raises_runtime_error(fake_flipper):
    """`Storage error: ...` must surface as ActionRuntimeError (surfaced as a tool error)."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect(
        "storage stat /ext/nfc/missing.nfc",
        "Storage error: file/dir not exist\r\n>: ",
    )

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        with pytest.raises(ActionRuntimeError) as excinfo:
            await get("flipper_storage_stat").invoke(
                flipper, {"path": "/ext/nfc/missing.nfc"}, transport="test"
            )
        # Detail must mention the missing path so callers can debug.
        assert "/ext/nfc/missing.nfc" in str(excinfo.value)
    finally:
        await flipper.stop()


# ===========================================================================
# flipper_storage_md5sum (Md5sum a file)
# ===========================================================================


async def test_md5sum_returns_lowercase_hex(fake_flipper):
    """md5 field is exactly 32 lowercase hex chars (empty-string digest here)."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    # d41d8cd98f00b204e9800998ecf8427e is md5 of the empty string.
    harness.expect(
        "storage md5 /ext/empty.bin",
        "d41d8cd98f00b204e9800998ecf8427e\r\n>: ",
    )

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        result = await get("flipper_storage_md5sum").invoke(
            flipper, {"path": "/ext/empty.bin"}, transport="test"
        )
        assert result == {
            "path": "/ext/empty.bin",
            "md5": "d41d8cd98f00b204e9800998ecf8427e",
        }
    finally:
        await flipper.stop()


async def test_md5_sends_storage_md5_not_md5sum(fake_flipper):
    """CRITICAL: the wire command is `storage md5`, NOT `storage md5sum`.

    Sending `storage md5sum` would get "command not found" from the firmware.
    This catches the spelling trap.
    """
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect(
        "storage md5 /ext/foo.nfc",
        "0123456789abcdef0123456789abcdef\r\n>: ",
    )

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        await get("flipper_storage_md5sum").invoke(
            flipper, {"path": "/ext/foo.nfc"}, transport="test"
        )
        written = harness.all_written()
        assert any("storage md5 /ext/foo.nfc" in w for w in written), (
            f"expected 'storage md5 /ext/foo.nfc' in writes, got {written!r}"
        )
        assert not any("storage md5sum" in w for w in written), (
            f"must NOT send 'storage md5sum', got {written!r}"
        )
    finally:
        await flipper.stop()


# ===========================================================================
# flipper_storage_info (Filesystem info)
# ===========================================================================


async def test_info_int(fake_flipper):
    """/int wire format: 4 lines, Type is the literal 'Virtual'."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    harness.expect(
        "storage info /int",
        "Label: clipper_test\r\nType: Virtual\r\n1024KiB total\r\n512KiB free\r\n>: ",
    )

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        result = await get("flipper_storage_info").invoke(
            flipper, {"path": "/int"}, transport="test"
        )
        assert result["path"] == "/int"
        assert result["label"] == "clipper_test"
        assert result["type"] == "Virtual"
        assert result["total_kib"] == 1024
        assert result["free_kib"] == 512
        # /int has no SD hardware lines, so no "sd" key.
        assert "sd" not in result
    finally:
        await flipper.stop()


async def test_info_ext(fake_flipper):
    """/ext wire format: 6 lines, Type is the SD fs_type string (FAT32 etc.)."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)
    # Shape pulled directly from _STORAGE_CLI_FORMAT (storage_cli.c lines 52-66).
    harness.expect(
        "storage info /ext",
        "Label: SDCARD\r\n"
        "Type: FAT32\r\n"
        "30412KiB total\r\n"
        "28811KiB free\r\n"
        "1cBLUE SDXC v3.0\r\n"
        "SN:0abc 05/2024\r\n"
        ">: ",
    )

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        result = await get("flipper_storage_info").invoke(
            flipper, {"path": "/ext"}, transport="test"
        )
        assert result["path"] == "/ext"
        assert result["label"] == "SDCARD"
        assert result["type"] == "FAT32"
        assert result["total_kib"] == 30412
        assert result["free_kib"] == 28811
    finally:
        await flipper.stop()


async def test_info_rejects_unsupported_path(fake_flipper):
    """`path='/any'` is rejected at the param-validation boundary — NEVER
    sent to the device (would hit the multi-line usage banner, surprise #5)."""
    harness = fake_flipper
    harness.add_port()
    script_handshake(harness)

    flipper = FlipperConnection(
        port_factory=harness.port_factory, reconnect_interval=60
    )
    await flipper.start()
    try:
        with pytest.raises(ActionParamError):
            await get("flipper_storage_info").invoke(
                flipper, {"path": "/any"}, transport="test"
            )
        # Verify NO storage info command was written.
        written = harness.all_written()
        assert not any("storage info" in w for w in written), (
            f"must not send storage info for /any, got {written!r}"
        )
    finally:
        await flipper.stop()


# ===========================================================================
# flipper_storage_read (inline / R8 binary-safe RPC)
# ===========================================================================
#
# These tests bypass FlipperConnection.start() and the FakeFlipperHarness's
# text-CLI serial fake. The Storage RPC ReadResponse is a binary protobuf
# stream, NOT text CLI; we use an RPCFakeSerial mirroring the §4 test pattern
# (see tests/test_flipper_rpc_request.py).
#
# Bytes the fake must produce for one rpc_request call:
#   1. ``start_rpc_session\r\n`` — the CLI echo that _drain_rpc_echo consumes
#   2. One or more length-delimited PB_Main frames carrying a ReadResponse
#   3. ``>: `` — the text-CLI prompt that _drain_to_text_prompt consumes
# ---------------------------------------------------------------------------


# Mirrors RPCFakeSerial from tests/test_flipper_rpc_request.py; copied here so
# this test file is self-contained. (Future refactor: pull into conftest.py.)
class _RPCFakeSerial:
    """Minimal write/read/flush/close fake for binary RPC exchanges."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self._buf = bytearray()

    def queue(self, data: bytes) -> None:
        self._buf.extend(data)

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def flush(self) -> None:  # noqa: D401
        pass

    def reset_input_buffer(self) -> None:  # noqa: D401
        pass

    def read(self, n: int = 1) -> bytes:
        if not self._buf:
            return b""
        chunk = bytes(self._buf[:n])
        self._buf = self._buf[n:]
        return chunk

    def close(self) -> None:  # noqa: D401
        pass


_RPC_ENTRY_ECHO = b"start_rpc_session\r\n"
_TEXT_PROMPT = b">: "


def _build_read_response_payload(file_bytes: bytes) -> bytes:
    """Build a Storage.ReadResponse content payload carrying *file_bytes*.

    Wire layout (per ``_STORAGE_RPC_FORMAT`` in clipper/rpc.py):
        ReadResponse { file = 1 } → File { data = 4 }

    Both nested submessages are length-delimited.
    """
    # Inner File: just field 4 (data bytes).
    file_msg = _encode_field_ld(4, file_bytes)
    # Outer ReadResponse: field 1 wraps the File submessage.
    return _encode_field_ld(1, file_msg)


def _build_main_frame(
    *,
    command_id: int = 1,
    command_status: int = 0,
    has_next: bool = False,
    content_field_num: int = TAG_STORAGE_READ_RESPONSE,
    content_payload: bytes = b"",
) -> bytes:
    """Build a fully framed PB_Main response (varint length prefix + body)."""
    parts = bytearray()
    if command_id:
        parts += _encode_field_varint((1 << 3) | 0, command_id)
    if command_status:
        parts += _encode_field_varint((2 << 3) | 0, command_status)
    if has_next:
        parts += _encode_field_varint((3 << 3) | 0, 1)
    parts += _encode_field_ld(content_field_num, content_payload)
    pb = bytes(parts)
    return _encode_varint(len(pb)) + pb


def _script_storage_read_response(
    file_bytes: bytes, chunks: int = 1, cmd_id: int = 1
) -> list[bytes]:
    """Build framed PB_Main frames for a Storage.ReadResponse split into *chunks*.

    All but the last frame carry ``has_next=True``. Each frame wraps a single
    slice of *file_bytes* inside a fresh ``ReadResponse { File { data = ... } }``.
    Returns the list of complete framed bytes; the caller queues them in order.
    """
    if chunks < 1:
        raise ValueError("chunks must be >= 1")
    # Split as evenly as possible; final chunk soaks up any remainder so the
    # total length is preserved exactly.
    n = len(file_bytes)
    base = n // chunks
    frames: list[bytes] = []
    for i in range(chunks):
        start = i * base
        end = n if i == chunks - 1 else (i + 1) * base
        slice_bytes = file_bytes[start:end]
        payload = _build_read_response_payload(slice_bytes)
        frames.append(
            _build_main_frame(
                command_id=cmd_id,
                command_status=0,
                has_next=(i < chunks - 1),
                content_field_num=TAG_STORAGE_READ_RESPONSE,
                content_payload=payload,
            )
        )
    return frames


def _make_rpc_flipper() -> tuple[FlipperConnection, _RPCFakeSerial]:
    """Connected FlipperConnection backed by an in-memory _RPCFakeSerial."""
    flipper = FlipperConnection(
        port_factory=lambda: "/dev/fake", reconnect_interval=60
    )
    ser = _RPCFakeSerial()
    flipper._serial = ser
    flipper._connected = True
    return flipper, ser


def _queue_full_read(ser: _RPCFakeSerial, file_bytes: bytes, chunks: int = 1) -> None:
    """Queue echo + scripted frames + trailing prompt onto *ser*."""
    ser.queue(_RPC_ENTRY_ECHO)
    for frame in _script_storage_read_response(file_bytes, chunks=chunks):
        ser.queue(frame)
    ser.queue(_TEXT_PROMPT)


# ---------------------------------------------------------------------------
# binary-safe RPC read
# ---------------------------------------------------------------------------


async def test_read_binary_file_roundtrip_integrity():
    """All 256 byte values survive the encode→serial→parse→base64 roundtrip."""
    flipper, ser = _make_rpc_flipper()
    source = bytes(range(256))
    _queue_full_read(ser, source, chunks=1)

    result = await get("flipper_storage_read").invoke(
        flipper, {"path": "/ext/binary.bin"}, transport="test"
    )

    assert result["path"] == "/ext/binary.bin"
    assert result["size"] == len(source)
    assert result["sha256"] == hashlib.sha256(source).hexdigest()
    assert "host_path" not in result
    assert base64.b64decode(result["content_b64"]) == source


async def test_read_streaming_multi_chunk():
    """A multi-frame response with has_next=True between frames concatenates.

    NOTE: ``try_read_delimited`` in clipper/rpc.py caps a single PB_Main frame
    at 1500 bytes (the real Flipper RPC max). Each frame here carries ~1 KiB
    of file data; 4 frames covers a 4 KiB file end-to-end. This exercises the
    has_next=True → has_next=False handoff that the protocol specifies.
    """
    flipper, ser = _make_rpc_flipper()
    source = bytes((i * 7 + 3) & 0xFF for i in range(4 * 1024))  # deterministic
    _queue_full_read(ser, source, chunks=4)

    result = await get("flipper_storage_read").invoke(
        flipper, {"path": "/ext/big.bin"}, transport="test"
    )

    assert result["size"] == len(source)
    assert result["sha256"] == hashlib.sha256(source).hexdigest()
    assert base64.b64decode(result["content_b64"]) == source


# ---------------------------------------------------------------------------
# inline vs download policy
# ---------------------------------------------------------------------------


async def test_read_small_file_inline():
    """A small (~200 byte) file returns inline base64 with no host_path."""
    flipper, ser = _make_rpc_flipper()
    source = bytes(range(200))
    _queue_full_read(ser, source, chunks=1)

    result = await get("flipper_storage_read").invoke(
        flipper, {"path": "/ext/small.bin"}, transport="test"
    )

    assert "host_path" not in result
    assert "content_b64" in result
    assert base64.b64decode(result["content_b64"]) == source
    assert result["sha256"] == hashlib.sha256(source).hexdigest()
    assert result["size"] == len(source)


async def test_read_file_exceeds_inline_cap_raises():
    """A file larger than max_inline_bytes raises ActionRuntimeError suggesting download=True.

    We use a small ``max_inline_bytes=16`` and a ~200-byte file so the RPC
    transport stays well under the per-frame cap (1500 bytes; see
    ``try_read_delimited``); the *handler* size check is what we're exercising.
    A 2 MB on-wire file would require ~2000 frames of <=1 KiB each and is
    impractical to script — the cap-enforcement logic doesn't care about the
    transport details, only the byte count returned.
    """
    flipper, ser = _make_rpc_flipper()
    source = bytes(range(200))  # 200 bytes
    _queue_full_read(ser, source, chunks=1)

    with pytest.raises(ActionRuntimeError) as excinfo:
        await get("flipper_storage_read").invoke(
            flipper,
            {"path": "/ext/big.bin", "max_inline_bytes": 16},
            transport="test",
        )
    assert "download=True" in str(excinfo.value)


async def test_read_with_download_flag_writes_to_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """download=True saves under CLIPPER_DOWNLOAD_DIR and returns host_path."""
    monkeypatch.setenv("CLIPPER_DOWNLOAD_DIR", str(tmp_path))
    flipper, ser = _make_rpc_flipper()
    source = b"\x00\x01\x02 NFC payload \xff\xfe"
    _queue_full_read(ser, source, chunks=1)

    result = await get("flipper_storage_read").invoke(
        flipper,
        {"path": "/ext/nfc/foo.nfc", "download": True},
        transport="test",
    )

    expected = tmp_path / "ext" / "nfc" / "foo.nfc"
    assert expected.exists(), f"expected file at {expected}"
    assert expected.read_bytes() == source
    assert result["host_path"] == str(expected.resolve())
    assert "content_b64" not in result
    assert result["sha256"] == hashlib.sha256(source).hexdigest()


async def test_read_download_respects_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A different CLIPPER_DOWNLOAD_DIR target lands in the new location."""
    custom = tmp_path / "elsewhere"
    monkeypatch.setenv("CLIPPER_DOWNLOAD_DIR", str(custom))
    flipper, ser = _make_rpc_flipper()
    source = b"redirected-bytes"
    _queue_full_read(ser, source, chunks=1)

    result = await get("flipper_storage_read").invoke(
        flipper,
        {"path": "/ext/sub/dir/file.bin", "download": True},
        transport="test",
    )

    expected = custom / "ext" / "sub" / "dir" / "file.bin"
    assert expected.exists()
    assert expected.read_bytes() == source
    assert result["host_path"] == str(expected.resolve())


async def test_read_download_atomic_no_partial_on_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If Path.replace raises mid-save, the final path stays absent.

    The .partial file is cleaned up by _save_atomic so a retry isn't confused
    by a stale half-written file.
    """
    monkeypatch.setenv("CLIPPER_DOWNLOAD_DIR", str(tmp_path))
    flipper, ser = _make_rpc_flipper()
    source = b"some-bytes-that-will-not-land"
    _queue_full_read(ser, source, chunks=1)

    # Patch Path.replace globally to raise — _save_atomic's try/finally must
    # then unlink the .partial and let the OSError propagate, which Action.invoke
    # wraps in ActionRuntimeError.
    real_replace = Path.replace

    def _boom(self: Path, target):  # noqa: ANN001
        raise OSError("simulated mid-write failure")

    monkeypatch.setattr(Path, "replace", _boom)

    with pytest.raises(ActionRuntimeError):
        await get("flipper_storage_read").invoke(
            flipper,
            {"path": "/ext/interrupted.bin", "download": True},
            transport="test",
        )

    # Restore for any subsequent test (monkeypatch will also restore at teardown).
    monkeypatch.setattr(Path, "replace", real_replace)

    final = tmp_path / "ext" / "interrupted.bin"
    partial = final.with_suffix(final.suffix + ".partial")
    assert not final.exists(), "final path must not exist after interrupted write"
    assert not partial.exists(), ".partial must be cleaned up on failure"


async def test_read_validates_path():
    """Bad paths raise ActionParamError BEFORE any RPC I/O happens."""
    flipper, ser = _make_rpc_flipper()
    # Don't queue any bytes — if the action reached the wire, rpc_request would
    # block or time out instead of failing fast on validation.

    with pytest.raises(ActionParamError):
        await get("flipper_storage_read").invoke(
            flipper, {"path": ".."}, transport="test"
        )
    with pytest.raises(ActionParamError):
        await get("flipper_storage_read").invoke(
            flipper, {"path": "relative"}, transport="test"
        )

    # Nothing was written to the fake serial.
    assert ser.written == [], (
        f"path validation must short-circuit before RPC; got writes {ser.written!r}"
    )


# Silence "unused import" linter for the inner-tag constant referenced only
# for documentation / future ad-hoc construction.
_ = _STORAGE_FILE_DATA_TAG


# ===========================================================================
# flipper_storage_write (binary-safe write via Storage.WriteRequest RPC)
# ===========================================================================
# These tests share the _RPCFakeSerial harness defined above. Unlike read,
# write is multi-shot RPC: the handler sends N WriteRequest frames inside
# ONE exclusive_serial session and only the last (has_next=False) chunk
# elicits a response.


def _build_ack_frame(*, command_id: int = 1, command_status: int = 0) -> bytes:
    """Build a framed PB_Main carrying an Empty acknowledgment.

    The firmware sends this exactly once after the final has_next=False
    WriteRequest chunk (and once per MkdirRequest). content_field_num is
    TAG_EMPTY_RESPONSE (=4).
    """
    return _build_main_frame(
        command_id=command_id,
        command_status=command_status,
        has_next=False,
        content_field_num=TAG_EMPTY_RESPONSE,
        content_payload=b"",
    )


def _enable_safety(monkeypatch) -> None:
    """Flip the safety gate ON via env var for the test."""
    monkeypatch.setenv("CLIPPER_SAFETY", "1")
    # Clear the legacy var so set_safety_allowed's "canonical wins" rule
    # doesn't get confused by env state leaked from another test.
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)


def _disable_safety(monkeypatch) -> None:
    """Ensure the safety gate is OFF."""
    monkeypatch.delenv("CLIPPER_SAFETY", raising=False)
    monkeypatch.delenv("CLIPPER_ALLOW_EMIT", raising=False)


async def test_storage_write_small_file(monkeypatch):
    """Gate ON, ~200-byte file. Verify the exact WriteRequest bytes hit the wire."""
    _enable_safety(monkeypatch)
    flipper, ser = _make_rpc_flipper()

    source = bytes(range(200))
    path = "/ext/small.bin"

    # One chunk only — single ack at the end.
    ser.queue(_RPC_ENTRY_ECHO)
    ser.queue(_build_ack_frame(command_id=1, command_status=0))
    ser.queue(_TEXT_PROMPT)

    result = await get("flipper_storage_write").invoke(
        flipper,
        {"path": path, "content_b64": base64.b64encode(source).decode("ascii")},
        transport="test",
    )

    assert result == {
        "path": path,
        "size": len(source),
        "sha256": hashlib.sha256(source).hexdigest(),
    }

    # The exact PB_Main frame must appear in the written byte stream. cmd_id=1
    # is the first chunk; has_next=False because it's the only one.
    expected_frame = encode_main(
        1,
        TAG_STORAGE_WRITE_REQUEST,
        encode_storage_write_request(path, source),
        has_next=False,
    )
    all_written = b"".join(ser.written)
    assert expected_frame in all_written, (
        "the exact framed WriteRequest bytes must appear on the wire verbatim"
    )


async def test_storage_write_binary_integrity_all_byte_values(monkeypatch):
    """content_b64 decoding all 256 byte values — bytes verbatim on the wire."""
    _enable_safety(monkeypatch)
    flipper, ser = _make_rpc_flipper()
    source = bytes(range(256))

    ser.queue(_RPC_ENTRY_ECHO)
    ser.queue(_build_ack_frame(command_id=1, command_status=0))
    ser.queue(_TEXT_PROMPT)

    result = await get("flipper_storage_write").invoke(
        flipper,
        {
            "path": "/ext/all256.bin",
            "content_b64": base64.b64encode(source).decode("ascii"),
        },
        transport="test",
    )

    assert result["size"] == 256
    assert result["sha256"] == hashlib.sha256(source).hexdigest()

    # The full 256-byte source must appear verbatim inside the wire bytes
    # (inside the File.data field of the WriteRequest). Catches any
    # null-truncation, sign-extension, or escaping regression.
    all_written = b"".join(ser.written)
    assert source in all_written, (
        "every byte 0x00-0xFF must survive into the wire payload"
    )


async def test_storage_write_chunked_for_large_payload(monkeypatch):
    """1500-byte payload → 3 chunks (512 + 512 + 476) with has_next=True/True/False."""
    _enable_safety(monkeypatch)
    flipper, ser = _make_rpc_flipper()

    # Deterministic source slightly larger than 2 chunks worth of the 512-byte cap.
    source = bytes(((i * 13 + 7) & 0xFF) for i in range(1500))
    path = "/ext/big.bin"

    # Only the final (has_next=False) frame elicits an ack — script ONE.
    ser.queue(_RPC_ENTRY_ECHO)
    ser.queue(_build_ack_frame(command_id=1, command_status=0))
    ser.queue(_TEXT_PROMPT)

    result = await get("flipper_storage_write").invoke(
        flipper,
        {"path": path, "content_b64": base64.b64encode(source).decode("ascii")},
        transport="test",
    )

    assert result["size"] == 1500
    assert result["sha256"] == hashlib.sha256(source).hexdigest()

    # Build the three expected frames and verify each appears.
    chunks = [source[0:512], source[512:1024], source[1024:1500]]
    assert len(chunks[0]) == 512
    assert len(chunks[1]) == 512
    assert len(chunks[2]) == 476

    expected_frames = []
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        expected_frames.append(
            encode_main(
                i + 1,
                TAG_STORAGE_WRITE_REQUEST,
                encode_storage_write_request(path, chunk),
                has_next=not is_last,
            )
        )

    all_written = b"".join(ser.written)
    for i, frame in enumerate(expected_frames):
        assert frame in all_written, (
            f"chunk {i} (has_next={i < 2}) must appear on the wire verbatim"
        )

    # Belt-and-suspenders: 3 chunks → 3 occurrences of the WriteRequest oneof
    # tag varint in the written stream. Tag = (11 << 3) | 2 = 90 = 0x5a, but
    # we count by full-frame uniqueness instead via the chunk contents to
    # avoid false positives from the tag byte appearing inside file data.
    for chunk in chunks:
        assert chunk in all_written, "every chunk's raw bytes appear on the wire"


async def test_storage_write_empty_file(monkeypatch):
    """content_b64='' → 0 bytes. ONE WriteRequest frame with empty data, has_next=False."""
    _enable_safety(monkeypatch)
    flipper, ser = _make_rpc_flipper()

    path = "/ext/empty.bin"

    ser.queue(_RPC_ENTRY_ECHO)
    ser.queue(_build_ack_frame(command_id=1, command_status=0))
    ser.queue(_TEXT_PROMPT)

    result = await get("flipper_storage_write").invoke(
        flipper, {"path": path}, transport="test"
    )

    assert result == {
        "path": path,
        "size": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }

    # Exactly ONE WriteRequest frame on the wire, has_next=False, empty data.
    expected_frame = encode_main(
        1,
        TAG_STORAGE_WRITE_REQUEST,
        encode_storage_write_request(path, b""),
        has_next=False,
    )
    all_written = b"".join(ser.written)
    assert expected_frame in all_written


async def test_storage_write_missing_parent_default_raises(monkeypatch):
    """Device returns command_status != 0 → ActionRuntimeError wraps the failure."""
    _enable_safety(monkeypatch)
    flipper, ser = _make_rpc_flipper()

    ser.queue(_RPC_ENTRY_ECHO)
    # Firmware rejects: e.g. ERROR_STORAGE_NOT_EXIST (=7) for missing parent.
    ser.queue(_build_ack_frame(command_id=1, command_status=7))
    ser.queue(_TEXT_PROMPT)

    with pytest.raises(ActionRuntimeError) as excinfo:
        await get("flipper_storage_write").invoke(
            flipper,
            {"path": "/ext/missing/foo.bin", "content_b64": base64.b64encode(b"x").decode("ascii")},
            transport="test",
        )
    # Detail must mention the failure (specific code or the action name).
    assert "command_status=7" in str(excinfo.value) or "WriteRequest" in str(excinfo.value)


async def test_storage_write_create_parents_walks_path(monkeypatch):
    """create_parents=True mkdir's each intermediate dir BEFORE the WriteRequest.

    Path /ext/aaa/bbb/foo.bin → two parents: /ext/aaa, /ext/aaa/bbb.
    Each parent gets one MkdirRequest frame; firmware acks each.
    Then the final WriteRequest frame is sent.
    """
    _enable_safety(monkeypatch)
    flipper, ser = _make_rpc_flipper()

    path = "/ext/aaa/bbb/foo.bin"
    data = b"hello"

    ser.queue(_RPC_ENTRY_ECHO)
    # Two MkdirRequest acks: first parent OK, second parent already-exists.
    # Both must be tolerated as success.
    ser.queue(_build_ack_frame(command_id=100, command_status=0))
    ser.queue(_build_ack_frame(command_id=101, command_status=6))  # STORAGE_ERROR_ALREADY_EXIST
    # Then the WriteRequest final-chunk ack.
    ser.queue(_build_ack_frame(command_id=1, command_status=0))
    ser.queue(_TEXT_PROMPT)

    result = await get("flipper_storage_write").invoke(
        flipper,
        {
            "path": path,
            "content_b64": base64.b64encode(data).decode("ascii"),
            "create_parents": True,
        },
        transport="test",
    )

    assert result["size"] == 5
    assert result["sha256"] == hashlib.sha256(data).hexdigest()

    # The mkdir frames precede the write frame on the wire.
    from clipper.rpc import encode_storage_mkdir_request
    mkdir1 = encode_main(
        100, TAG_STORAGE_MKDIR_REQUEST, encode_storage_mkdir_request("/ext/aaa")
    )
    mkdir2 = encode_main(
        101,
        TAG_STORAGE_MKDIR_REQUEST,
        encode_storage_mkdir_request("/ext/aaa/bbb"),
    )
    write = encode_main(
        1,
        TAG_STORAGE_WRITE_REQUEST,
        encode_storage_write_request(path, data),
        has_next=False,
    )

    all_written = b"".join(ser.written)
    idx_mkdir1 = all_written.find(mkdir1)
    idx_mkdir2 = all_written.find(mkdir2)
    idx_write = all_written.find(write)
    assert idx_mkdir1 != -1, "first MkdirRequest frame must appear on the wire"
    assert idx_mkdir2 != -1, "second MkdirRequest frame must appear on the wire"
    assert idx_write != -1, "WriteRequest frame must appear on the wire"
    # Ordering: shallowest mkdir first, then deeper mkdir, then write.
    assert idx_mkdir1 < idx_mkdir2 < idx_write, (
        f"mkdir frames must precede write: "
        f"mkdir1@{idx_mkdir1} mkdir2@{idx_mkdir2} write@{idx_write}"
    )


@pytest.mark.regression
async def test_storage_write_path_validation_rejects_bad_paths(monkeypatch):
    """Bad paths raise ActionParamError BEFORE any byte is written to the wire.

    Regression-marked because path validation is a security-sensitive boundary
    (prevents '..' escapes, control-byte injection, non-absolute writes).
    """
    _enable_safety(monkeypatch)
    flipper, ser = _make_rpc_flipper()

    bad_paths = [
        "",                       # empty
        "relative",               # non-absolute
        "/ext/../etc/passwd",     # .. segment
        "/ext/foo\x00bar",        # control byte (NUL)
        "/ext/foo\x01bar",        # control byte
    ]
    for bad in bad_paths:
        with pytest.raises(ActionParamError):
            await get("flipper_storage_write").invoke(
                flipper,
                {"path": bad, "content_b64": ""},
                transport="test",
            )

    # NOT a single byte was written to the fake serial.
    assert ser.written == [], (
        f"path validation must short-circuit before any RPC I/O; "
        f"got writes {ser.written!r}"
    )


@pytest.mark.regression
async def test_storage_write_gated_by_safety(monkeypatch):
    """Gate OFF → EmissionBlocked, NO RPC roundtrip.

    Regression-marked because the safety gate is the user-facing safety
    boundary for state-affecting actions.
    """
    from clipper.actions import EmissionBlocked

    _disable_safety(monkeypatch)
    flipper, ser = _make_rpc_flipper()

    with pytest.raises(EmissionBlocked) as excinfo:
        await get("flipper_storage_write").invoke(
            flipper,
            {"path": "/ext/blocked.bin", "content_b64": "aGVsbG8="},
            transport="test",
        )
    assert str(excinfo.value) == "flipper_storage_write"

    # Zero bytes written.
    assert ser.written == [], (
        f"gate OFF must short-circuit before any RPC I/O; "
        f"got writes {ser.written!r}"
    )
