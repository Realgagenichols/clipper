"""clipper.hardware.storage — Read-only Flipper filesystem actions.

This module is the wire-format authority for the Storage actions. The
``_STORAGE_CLI_FORMAT`` comment block below is the source of truth for the
exact bytes the device emits — every printf is cited verbatim from Momentum
mntm-012 (SHA ``e1784e7418d8b074e971983ceb6fef0f37e52ae4``).

Four non-emissive actions live here:

  - ``flipper_storage_list``    — list directory entries
  - ``flipper_storage_stat``    — stat a path (file/dir/volume/root)
  - ``flipper_storage_md5sum``  — md5 of a file (wire command is ``storage md5``)
  - ``flipper_storage_info``    — filesystem info for ``/int`` or ``/ext``

Binary-safe ``flipper_storage_read`` lives in a later section and uses the
Storage RPC ``ReadRequest`` path, NOT the text CLI ``storage read`` command
which is not binary-safe.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, field_validator

from clipper.actions import Action, EmissionBlocked, register
from clipper.rpc import (
    _STORAGE_WRITE_MAX_DATA_PER_FRAME,
    STORAGE_ERROR_ALREADY_EXIST,
    TAG_STORAGE_MKDIR_REQUEST,
    TAG_STORAGE_READ_REQUEST,
    TAG_STORAGE_READ_RESPONSE,
    TAG_STORAGE_WRITE_REQUEST,
    _decode_varint,
    decode_main,
    encode_main,
    encode_storage_mkdir_request,
    encode_storage_read_request,
    encode_storage_write_request,
    parse_storage_read_response,
    try_read_delimited,
)

if TYPE_CHECKING:
    from clipper.flipper import FlipperConnection

log = logging.getLogger(__name__)

# Maximum length we accept for a Flipper filesystem path. FatFs supports
# longer, but in practice nothing on a Flipper Zero pushes past 100 chars;
# 256 is generous and matches the bound the spec commits to.
_MAX_PATH_LEN = 256


def _validate_path(v: str) -> str:
    """Validate a Flipper filesystem path at the action boundary.

    Rejects: empty string, non-absolute paths, ``..`` segments, control
    characters (anything < 0x20 except TAB), and paths > 256 characters.
    Returns the path unchanged on success.

    Used by the Pydantic param models of every flipper_storage_* action so
    bad input never reaches ``send_command`` or ``rpc_request``.
    """
    if not v:
        raise ValueError("path must be a non-empty string")
    if not v.startswith("/"):
        raise ValueError("path must be absolute (start with '/')")
    if len(v) > _MAX_PATH_LEN:
        raise ValueError(f"path must be <= {_MAX_PATH_LEN} characters, got {len(v)}")
    for segment in v.split("/"):
        if segment == "..":
            raise ValueError("path must not contain '..' segments")
    for ch in v:
        if ord(ch) < 0x20 and ch != "\t":
            raise ValueError(f"path must not contain control characters (found 0x{ord(ch):02x})")
    return v


# ---------------------------------------------------------------------------
# _STORAGE_CLI_FORMAT — verbatim upstream printf citations
# ---------------------------------------------------------------------------
# Source: Next-Flip/Momentum-Firmware tag mntm-012
#   tag SHA:         e1784e7418d8b074e971983ceb6fef0f37e52ae4
#   file:            applications/services/storage/storage_cli.c
#   raw URL:         https://raw.githubusercontent.com/Next-Flip/
#                    Momentum-Firmware/mntm-012/applications/services/
#                    storage/storage_cli.c
# Latest commit touching this file on mntm-012:
#                    49d7ce7349fa149bdc7c8cf71360f5aae8118846
#
# All printf strings below are copied byte-for-byte from the upstream C
# source. Every line that the device emits ends with `\r\n` (CRLF). The
# leading `\t` (TAB, 0x09) on list entries IS part of the wire format —
# downstream parsers MUST strip the tab, not assume spaces.
#
# ---------------------------------------------------------------------------
# `storage list <path>` — storage_cli_list, lines 103-139
# ---------------------------------------------------------------------------
# Root listing (path == "/"), lines 107-109:
#     printf("\t[D] int\r\n");
#     printf("\t[D] ext\r\n");
#     printf("\t[D] any\r\n");
#
# Per-entry, inside the storage_dir_read loop (lines 121-125):
#     if(file_info_is_dir(&fileinfo)) {
#         printf("\t[D] %s\r\n", name);
#     } else {
#         printf("\t[F] %s %lub\r\n", name, (uint32_t)(fileinfo.size));
#     }
#
# Empty directory (line 129):
#     printf("\tEmpty\r\n");
#
# Error path (storage_cli_print_error, line 22):
#     printf("Storage error: %s\r\n", storage_error_get_desc(error));
#
# Format summary:
#   - Directories:  "\t[D] <name>\r\n"
#   - Files:        "\t[F] <name> <size>b\r\n"
#     (size is uint32 decimal, immediately followed by a literal lowercase
#      'b' — there is NO space between the digits and the 'b'.)
#   - Empty dir:    "\tEmpty\r\n"
#   - Root (`/`):   three fixed lines "\t[D] int", "\t[D] ext", "\t[D] any"
#   - Errors:       "Storage error: <description>\r\n"  (no leading tab)
#
# Ambiguity note: `name` MAY contain spaces. A naive `split(' ')` parser will
# misread "[F] My File.txt 1234b" as name="My". The robust approach is to
# anchor on the trailing " <digits>b\r\n" via regex, or split with maxsplit
# bounded from the right.
#
# ---------------------------------------------------------------------------
# `storage stat <path>` — storage_cli_stat, lines 349-389
# ---------------------------------------------------------------------------
# Root (path == "/"), line 355:
#     printf("Storage\r\n");
#
# A storage volume prefix (`/int`, `/ext`, or `/any`), lines 369-371:
#     printf(
#         "Storage, %luKiB total, %luKiB free\r\n",
#         (uint32_t)(total_space / 1024),
#         (uint32_t)(free_space / 1024));
#
# Any other path (after storage_common_stat succeeds), lines 378-382:
#     if(file_info_is_dir(&fileinfo)) {
#         printf("Directory\r\n");
#     } else {
#         printf("File, size: %lub\r\n", (uint32_t)(fileinfo.size));
#     }
#
# Error path: "Storage error: <description>\r\n" (same as list).
#
# Format summary — FIVE distinct shapes depending on path:
#   - "Storage\r\n"                                  (path == "/")
#   - "Storage, <N>KiB total, <N>KiB free\r\n"       (volume prefix)
#   - "Directory\r\n"                                (regular dir)
#   - "File, size: <N>b\r\n"                         (regular file)
#   - "Storage error: <description>\r\n"             (any FSE_* != OK)
#
# Ambiguity note: there is NO single "stat" record format. A consumer must
# pattern-match on the first token (`Storage`, `Directory`, `File`, or
# `Storage error:`). Size again uses lowercase `b` with no space.
#
# ---------------------------------------------------------------------------
# `storage md5 <path>` — storage_cli_md5, lines 497-516
# ---------------------------------------------------------------------------
# NOTE the CLI command is registered as "md5" (not "md5sum") in the command
# table (line 584-587). The on-device executable name is `storage md5 <path>`.
#
# Success (line 506):
#     printf("%s\r\n", furi_string_get_cstr(md5));
#
# The md5 string is produced by `md5_string_calc_file` and is a 32-character
# lowercase hex digest with no spaces, prefix, or label — JUST the digest
# followed by CRLF.
#
# Error path: "Storage error: <description>\r\n".
#
# ---------------------------------------------------------------------------
# `storage info <path>` — storage_cli_info, lines 25-73
# ---------------------------------------------------------------------------
# Internal flash (path == STORAGE_INT_PATH_PREFIX, "/int"), lines 39-43:
#     printf(
#         "Label: %s\r\nType: Virtual\r\n%luKiB total\r\n%luKiB free\r\n",
#         furi_hal_version_get_name_ptr() ? ... : "Unknown",
#         (uint32_t)(total_space / 1024),
#         (uint32_t)(free_space / 1024));
#
# SD card (path == STORAGE_EXT_PATH_PREFIX, "/ext"), lines 52-66:
#     printf(
#         "Label: %s\r\nType: %s\r\n%luKiB total\r\n%luKiB free\r\n"
#         "%02x%s %s v%i.%i\r\nSN:%04lx %02i/%i\r\n",
#         sd_info.label,
#         sd_api_get_fs_type_text(sd_info.fs_type),
#         sd_info.kb_total,
#         sd_info.kb_free,
#         sd_info.manufacturer_id,
#         sd_info.oem_id,
#         sd_info.product_name,
#         sd_info.product_revision_major,
#         sd_info.product_revision_minor,
#         sd_info.product_serial_number,
#         sd_info.manufacturing_month,
#         sd_info.manufacturing_year);
#
# Any other path: falls through to storage_cli_print_usage() (line 69) which
# emits multi-line usage text — NOT a structured info response. Treat any
# non-`/int`/`/ext` argument as "unsupported".
#
# Format summary:
#   - /int: 4 lines, "Label: ...", "Type: Virtual", "<N>KiB total",
#           "<N>KiB free"
#   - /ext: 6 lines, the first 4 identical in shape to /int, plus
#           "<hex2><oem> <product> v<maj>.<min>" and
#           "SN:<hex4> <MM>/<YYYY>"
#   - any other path: print_usage banner (not parseable as info)
#
# Ambiguity note: `Label` MAY contain spaces or be empty. `Type` is the
# fs_type_text helper output ("FAT12", "FAT16", "FAT32", "exFAT", "UNKNOWN")
# for SD and the literal string `"Virtual"` for internal flash. Parsers
# should be tolerant of unknown Type strings.
#
# ---------------------------------------------------------------------------
# Cross-cutting CLI notes
# ---------------------------------------------------------------------------
# - The CLI prompt itself emits `>: ` after each command completes; this is
#   produced by the CLI host loop, NOT the storage handlers. Consumers
#   driving the text CLI must consume the prompt as a frame delimiter.
# - Numeric sizes are %lu (uint32). Files larger than 4 GiB will overflow —
#   currently a non-issue on F0 (max SD card practical limits) but worth
#   noting.
# - `storage read` (lines 183-213) emits "Size: %lu\r\n" then RAW file bytes,
#   then a trailing "\r\n". This is text-CLI's read path and is NOT binary
#   safe (CR / LF / 0x00 in the file body break framing). Binary-safe reads
#   MUST use the Storage RPC ReadRequest pathway instead — see the proto
#   field tags pinned in clipper/rpc.py.

# ---------------------------------------------------------------------------
# _STORAGE_RPC_FORMAT — proto field-number cross-reference
# ---------------------------------------------------------------------------
# The matching proto-level constants live in clipper/rpc.py (TAG_STORAGE_*,
# _STORAGE_FILE_*_TAG). They are pinned to flipperzero-protobuf commit
# ea4f185f5eaa265955c520eae2832887ee6aa5e4 (the submodule SHA that
# Momentum mntm-012 itself pins — verified via the GitHub Contents API on
# 2026-05-30). Refer to clipper/rpc.py for the authoritative tag definitions
# and to that file's comment block for the per-field citations.


# ---------------------------------------------------------------------------
# Param models — every action funnels through ``_validate_path``
# ---------------------------------------------------------------------------


# Filesystem volumes the ``storage info`` text-CLI command actually supports
# (other paths fall through to the upstream usage banner).
_INFO_SUPPORTED_VOLUMES: frozenset[str] = frozenset({"/int", "/ext"})


class StorageListParams(BaseModel):
    """Parameters for flipper_storage_list."""

    path: str

    @field_validator("path")
    @classmethod
    def _validate(cls, v: str) -> str:
        return _validate_path(v)


class StorageStatParams(BaseModel):
    """Parameters for flipper_storage_stat."""

    path: str

    @field_validator("path")
    @classmethod
    def _validate(cls, v: str) -> str:
        return _validate_path(v)


class StorageMd5sumParams(BaseModel):
    """Parameters for flipper_storage_md5sum.

    Note: the user-facing action name is ``flipper_storage_md5sum`` but the
    wire command we send is ``storage md5 <path>`` — the CLI registers the
    command as ``md5``, not ``md5sum``. Sending ``storage md5sum`` would get
    "command not found".
    """

    path: str

    @field_validator("path")
    @classmethod
    def _validate(cls, v: str) -> str:
        return _validate_path(v)


class StorageInfoParams(BaseModel):
    """Parameters for flipper_storage_info. Defaults to /ext (SD card)."""

    path: str = "/ext"

    @field_validator("path")
    @classmethod
    def _validate(cls, v: str) -> str:
        v = _validate_path(v)
        if v not in _INFO_SUPPORTED_VOLUMES:
            raise ValueError(
                "storage info is only supported for /int or /ext; "
                f"got {v!r}"
            )
        return v


# Upper bound on max_inline_bytes (16 MiB). Anything larger should use download.
_MAX_INLINE_CAP = 16 * 1024 * 1024


class StorageReadParams(BaseModel):
    """Parameters for flipper_storage_read (binary-safe via RPC, R8).

    - ``download=False`` (default): return bytes base64-encoded in
      ``content_b64`` when the file fits under ``max_inline_bytes``;
      otherwise the handler raises (caller should retry with ``download=True``).
    - ``download=True``: write the file to the host under
      ``~/.clipper/flipper-files/<flipper-path>`` (or ``CLIPPER_DOWNLOAD_DIR``
      override) and return the host path. ``content_b64`` is omitted.
    """

    path: str
    download: bool = False
    max_inline_bytes: int = 1_048_576  # 1 MiB

    @field_validator("path")
    @classmethod
    def _validate(cls, v: str) -> str:
        return _validate_path(v)

    @field_validator("max_inline_bytes")
    @classmethod
    def _validate_max(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_inline_bytes must be non-negative")
        if v > _MAX_INLINE_CAP:
            raise ValueError(
                f"max_inline_bytes must be <= {_MAX_INLINE_CAP} (16 MiB)"
            )
        return v


class StorageWriteParams(BaseModel):
    """Parameters for flipper_storage_write (binary-safe via RPC, R10).

    - ``path``: absolute Flipper path (validated through ``_validate_path``).
    - ``content_b64``: file bytes base64-encoded. Default ``""`` writes a
      0-byte file (intentional Pydantic default).
    - ``create_parents``: when True, mkdir's any missing intermediate
      directories of ``path`` before writing the file. Default False.
    """

    path: str
    content_b64: str = ""
    create_parents: bool = False

    @field_validator("path")
    @classmethod
    def _validate(cls, v: str) -> str:
        return _validate_path(v)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Anchor on the trailing " <digits>b\r\n" so filenames containing spaces
# parse correctly. Leading TAB ("\t") is part of the wire format and stripped
# upstream of this regex.
_LIST_FILE_RE = re.compile(r"^\[F\]\s(.+)\s(\d+)b$")
_LIST_DIR_RE = re.compile(r"^\[D\]\s(.+)$")

# stat shapes — dispatch on leading token, NOT a single regex.
_STAT_VOLUME_RE = re.compile(
    r"^Storage,\s*(\d+)KiB total,\s*(\d+)KiB free$"
)
_STAT_FILE_RE = re.compile(r"^File,\s*size:\s*(\d+)b?$")

# md5 digest line: 32 lowercase hex chars.
_MD5_RE = re.compile(r"^([0-9a-f]{32})$")

# info /ext extra lines (parsed best-effort; included under "sd" if present).
_SD_HW_RE = re.compile(
    r"^([0-9a-f]{2})(\S+)\s+(.+)\s+v(\d+)\.(\d+)$"
)
_SD_SN_RE = re.compile(r"^SN:([0-9a-f]+)\s+(\d{1,2})/(\d{2,4})$")


def _strip_prompt(response: str) -> list[str]:
    """Return the response body split into lines, with the trailing prompt and
    CR/LF artefacts removed.

    The CLI host loop emits ``>: `` after every command; some response lines
    end with ``\\r\\n``. We split on newlines and drop empties + the bare
    prompt token.
    """
    lines: list[str] = []
    for raw in response.splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        if line.strip() == ">:":
            continue
        # The prompt may also appear as a suffix on the last line.
        if line.endswith(">: "):
            line = line[: -len(">: ")].rstrip()
            if not line:
                continue
        lines.append(line)
    return lines


def _check_storage_error(response: str, path: str) -> None:
    """Raise RuntimeError if the response contains a ``Storage error:`` line.

    The Flipper text CLI prints ``Storage error: <description>\\r\\n`` on any
    FSE_* != OK. We raise so ``Action.invoke`` wraps to ``ActionRuntimeError``
    (surfaced as a tool error).
    """
    for line in response.splitlines():
        stripped = line.strip()
        if stripped.startswith("Storage error:"):
            msg = stripped[len("Storage error:") :].strip()
            raise RuntimeError(f"storage error for {path!r}: {msg}")


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_list(response: str) -> list[dict]:
    """Parse ``storage list`` output into a list of {name, type, size} dicts.

    Wire format (per _STORAGE_CLI_FORMAT):
        "\\t[D] <name>\\r\\n"             — directory
        "\\t[F] <name> <size>b\\r\\n"     — file (size has NO space before 'b')
        "\\tEmpty\\r\\n"                  — empty directory
    """
    entries: list[dict] = []
    for line in _strip_prompt(response):
        # Each row begins with a literal TAB on real hardware.
        body = line.lstrip("\t").strip()
        if not body:
            continue
        if body == "Empty":
            return []
        m = _LIST_FILE_RE.match(body)
        if m:
            entries.append({
                "name": m.group(1),
                "type": "file",
                "size": int(m.group(2)),
            })
            continue
        m = _LIST_DIR_RE.match(body)
        if m:
            entries.append({
                "name": m.group(1).strip(),
                "type": "dir",
                "size": 0,
            })
            continue
        # Unknown line — log at debug and skip (don't blow up on banner noise).
        log.debug("storage list: ignoring unrecognized line %r", body)
    entries.sort(key=lambda e: e["name"])
    return entries


def _parse_stat(response: str, path: str) -> dict:
    """Parse ``storage stat`` output. Dispatches on the leading token because
    there are five distinct output shapes (per _STORAGE_CLI_FORMAT)."""
    lines = _strip_prompt(response)
    if not lines:
        raise RuntimeError(f"empty response from storage stat {path!r}")
    head = lines[0].strip()

    # Order matters: "Storage, ..." prefix-matches "Storage" so the volume
    # shape MUST be checked before the bare root shape.
    if head == "Storage":
        return {"path": path, "type": "root", "size": 0}
    m = _STAT_VOLUME_RE.match(head)
    if m:
        total_kib = int(m.group(1))
        free_kib = int(m.group(2))
        return {
            "path": path,
            "type": "volume",
            "total_kib": total_kib,
            "free_kib": free_kib,
            "size": total_kib * 1024,
        }
    if head == "Directory":
        return {"path": path, "type": "dir", "size": 0}
    m = _STAT_FILE_RE.match(head)
    if m:
        return {"path": path, "type": "file", "size": int(m.group(1))}
    raise RuntimeError(
        f"could not parse storage stat response for {path!r}: {head!r}"
    )


def _parse_md5(response: str, path: str) -> str:
    """Parse a ``storage md5`` response — a single 32-char lowercase hex line."""
    for line in _strip_prompt(response):
        m = _MD5_RE.match(line.strip())
        if m:
            return m.group(1)
    raise RuntimeError(
        f"could not parse md5 digest from storage md5 response for {path!r}"
    )


def _parse_info(response: str, path: str) -> dict:
    """Parse ``storage info`` for /int or /ext.

    /int wire format (4 lines):
        Label: <name>
        Type: Virtual
        <N>KiB total
        <N>KiB free

    /ext wire format (6 lines): first 4 identical, then:
        <hex2><oem> <product> v<maj>.<min>
        SN:<hex4> <MM>/<YYYY>

    Any other path is rejected by the param model before reaching this
    function, so encountering the usage banner here is a hard error.
    """
    lines = _strip_prompt(response)
    if not lines:
        raise RuntimeError(f"empty response from storage info {path!r}")
    # The first line MUST be "Label: ...". Anything else is the usage banner
    # or some other unexpected output.
    if not lines[0].startswith("Label:"):
        raise RuntimeError(
            f"unexpected storage info response for {path!r}: {lines[0]!r}"
        )

    label = lines[0][len("Label:") :].strip()
    fs_type = ""
    total_kib = 0
    free_kib = 0
    sd_extra: dict | None = None

    for line in lines[1:]:
        if line.startswith("Type:"):
            fs_type = line[len("Type:") :].strip()
            continue
        if line.endswith("KiB total"):
            with contextlib.suppress(ValueError):
                total_kib = int(line.split("KiB", 1)[0].strip())
            continue
        if line.endswith("KiB free"):
            with contextlib.suppress(ValueError):
                free_kib = int(line.split("KiB", 1)[0].strip())
            continue
        # Optional SD-card hardware lines (only present for /ext). Best-effort
        # parse — if the format ever drifts we drop the extras rather than
        # failing the whole info call.
        m = _SD_HW_RE.match(line)
        if m:
            sd_extra = sd_extra or {}
            sd_extra.update({
                "manufacturer": m.group(1),
                "oem": m.group(2),
                "product": m.group(3).strip(),
                "revision": f"{m.group(4)}.{m.group(5)}",
            })
            continue
        m = _SD_SN_RE.match(line)
        if m:
            sd_extra = sd_extra or {}
            sd_extra.update({
                "serial": m.group(1),
                "mfg_date": f"{int(m.group(2)):02d}/{m.group(3)}",
            })
            continue

    result: dict = {
        "path": path,
        "label": label,
        "type": fs_type,
        "total_kib": total_kib,
        "free_kib": free_kib,
    }
    if sd_extra:
        result["sd"] = sd_extra
    return result


# ---------------------------------------------------------------------------
# Host download policy — used by flipper_storage_read when download=True
# ---------------------------------------------------------------------------


def _download_root() -> Path:
    """Return the host directory under which downloaded files are mirrored.

    Honors ``CLIPPER_DOWNLOAD_DIR``; default ``~/.clipper/flipper-files``.
    """
    override = os.environ.get("CLIPPER_DOWNLOAD_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".clipper" / "flipper-files"


def _save_atomic(flipper_path: str, content: bytes) -> Path:
    """Save *content* under ``_download_root()`` mirroring *flipper_path*.

    The write is atomic on POSIX: bytes go to ``<final>.partial`` first,
    then ``Path.replace`` swaps it into place. On any failure the partial
    file is cleaned up before the exception re-propagates.

    Returns the absolute final ``Path``.
    """
    rel = flipper_path.lstrip("/")
    final = _download_root() / rel
    final.parent.mkdir(parents=True, exist_ok=True)
    partial = final.with_suffix(final.suffix + ".partial")
    try:
        partial.write_bytes(content)
        partial.replace(final)  # atomic on POSIX
    except Exception:
        # Clean up any leftover partial on failure so a retry isn't confused
        # by a stale half-written file (auto-recovery > user effort).
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()
        raise
    return final


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _storage_list_handler(
    flipper: FlipperConnection,
    params: StorageListParams,
) -> dict:
    """List directory entries via ``storage list <path>``."""
    cmd = f"storage list {params.path}"
    log.debug("storage_list: %r", cmd)
    response = await flipper.send_command(cmd, timeout=5.0, retry_if_empty=True)
    _check_storage_error(response, params.path)
    entries = _parse_list(response)
    log.debug("storage_list: %d entries", len(entries))
    return {"path": params.path, "entries": entries}


async def _storage_stat_handler(
    flipper: FlipperConnection,
    params: StorageStatParams,
) -> dict:
    """Stat a path via ``storage stat <path>``."""
    cmd = f"storage stat {params.path}"
    log.debug("storage_stat: %r", cmd)
    response = await flipper.send_command(cmd, timeout=5.0, retry_if_empty=True)
    _check_storage_error(response, params.path)
    result = _parse_stat(response, params.path)
    log.debug("storage_stat: type=%s", result.get("type"))
    return result


async def _storage_md5sum_handler(
    flipper: FlipperConnection,
    params: StorageMd5sumParams,
) -> dict:
    """Compute md5 via ``storage md5 <path>`` (NOT ``md5sum``)."""
    cmd = f"storage md5 {params.path}"
    log.debug("storage_md5sum: %r", cmd)
    # md5 of a multi-MB file takes seconds on a Flipper; bump the timeout.
    response = await flipper.send_command(cmd, timeout=30.0, retry_if_empty=True)
    _check_storage_error(response, params.path)
    digest = _parse_md5(response, params.path)
    log.debug("storage_md5sum: ok")
    return {"path": params.path, "md5": digest}


async def _storage_info_handler(
    flipper: FlipperConnection,
    params: StorageInfoParams,
) -> dict:
    """Filesystem info via ``storage info <path>`` (only /int and /ext)."""
    cmd = f"storage info {params.path}"
    log.debug("storage_info: %r", cmd)
    response = await flipper.send_command(cmd, timeout=5.0, retry_if_empty=True)
    _check_storage_error(response, params.path)
    result = _parse_info(response, params.path)
    log.debug(
        "storage_info: type=%s total_kib=%d",
        result.get("type"),
        result.get("total_kib", 0),
    )
    return result


def _reassemble_streaming_read(payload: bytes) -> bytes:
    """Walk a sequence of concatenated Storage.ReadResponse messages and
    return the byte-for-byte concatenation of every ``File.data`` slice.

    ``FlipperConnection.rpc_request`` concatenates the content_payload of
    every accepted frame in order. For a streaming read, that's
    ``ReadResponse1 || ReadResponse2 || ...`` — each ReadResponse wraps a
    single File submessage whose ``data`` field carries one chunk of the
    file. ``parse_storage_read_response`` on its own only walks the OUTER
    once and overwrites on each field-1 hit, so it returns the LAST chunk.
    We need every chunk in order, hence this dedicated walker.

    Single-frame responses are a degenerate case (one ReadResponse) and
    fall out of this loop naturally.
    """
    out = bytearray()
    pos = 0
    while pos < len(payload):
        tag_value, pos = _decode_varint(payload, pos)
        wire_type = tag_value & 0x07
        field_num = tag_value >> 3
        if wire_type == 2:
            length, pos = _decode_varint(payload, pos)
            slice_end = pos + length
            sub = payload[pos:slice_end]
            pos = slice_end
            if field_num == 1:
                # One ReadResponse → one File submessage; pull its data field
                # using the existing single-frame parser by wrapping in a
                # synthetic ReadResponse envelope.
                out.extend(parse_storage_read_response(
                    _wrap_field_ld_1(sub)
                ))
            # other outer fields (none defined in ReadResponse): ignore
        elif wire_type == 0:
            _, pos = _decode_varint(payload, pos)  # skip unknown varint
        else:
            # Unknown wire type — stop rather than risk an unbounded read.
            log.warning(
                "_reassemble_streaming_read: unexpected wire type %d field %d",
                wire_type,
                field_num,
            )
            break
    return bytes(out)


def _wrap_field_ld_1(payload: bytes) -> bytes:
    """Wrap *payload* as a single length-delimited field-1 entry.

    Used to feed each per-frame File submessage back through
    ``parse_storage_read_response`` without duplicating its inner walker.
    """
    # tag byte = (1<<3)|2 = 0x0A
    return b"\x0a" + _encode_varint_local(len(payload)) + payload


def _encode_varint_local(value: int) -> bytes:
    """Local copy of varint encode so we don't import a private from clipper.rpc."""
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


async def _storage_read_handler(
    flipper: FlipperConnection,
    params: StorageReadParams,
) -> dict:
    """Read a file from the Flipper via Storage RPC (binary-safe, R8).

    Always returns ``path``, ``size``, ``sha256``. Either ``content_b64`` (inline,
    base64) OR ``host_path`` (downloaded) is added depending on
    ``params.download`` and the file size. Never both.

    Raises:
        RuntimeError: if the file exceeds ``max_inline_bytes`` and download
            is False (caller should retry with ``download=True``), or if any
            host-side write fails. ``Action.invoke`` surfaces it as a tool error.
    """
    log.debug("storage_read: path=%r download=%s", params.path, params.download)

    request_payload = encode_storage_read_request(params.path)

    async def _read_once() -> bytes:
        # 60s is generous: a multi-MB file streams in multiple has_next=True
        # frames over USB CDC at ~115k baud framing overhead.
        data = await flipper.rpc_request(
            request_field_num=TAG_STORAGE_READ_REQUEST,
            request_payload=request_payload,
            response_field_num=TAG_STORAGE_READ_RESPONSE,
            timeout=60.0,
        )
        return _reassemble_streaming_read(data)

    file_bytes = await _read_once()
    if not file_bytes:
        # An empty read can be a stale-prompt/handoff bleed under back-to-back
        # ops (an empty file is also legitimately empty). Re-read once — reading
        # is idempotent and the retry's pre-flight drain clears any late tail.
        log.warning("storage_read: empty read for %r — retrying once", params.path)
        file_bytes = await _read_once()
    sha256_hex = hashlib.sha256(file_bytes).hexdigest()
    size = len(file_bytes)

    # NEVER log raw file bytes — only size + digest are safe at DEBUG.
    log.debug("storage_read: %d bytes sha256=%s", size, sha256_hex)

    result: dict = {
        "path": params.path,
        "size": size,
        "sha256": sha256_hex,
    }

    if params.download:
        final = _save_atomic(params.path, file_bytes)
        result["host_path"] = str(final.resolve())
        log.debug("storage_read: downloaded to %s", result["host_path"])
        return result

    if size > params.max_inline_bytes:
        raise RuntimeError(
            f"file size {size} exceeds max_inline_bytes "
            f"({params.max_inline_bytes}); retry with download=True"
        )

    result["content_b64"] = base64.b64encode(file_bytes).decode("ascii")
    return result


# ---------------------------------------------------------------------------
# Storage write (binary-safe via Storage.WriteRequest RPC)
# ---------------------------------------------------------------------------
#
# Design choice:
#
# We send WriteRequest frames INLINE inside the handler using
# ``exclusive_serial`` rather than extending ``FlipperConnection.rpc_request``
# with an outbound ``has_next`` parameter. Two reasons:
#
#   1. ``rpc_request`` is the one-shot RPC pattern (open session → send ONE
#      request → read response → close). Storage write is genuinely
#      multi-shot (send N WriteRequest chunks → read ONE Empty acknowledgment
#      → close). Different shape; shoehorning into rpc_request would muddy
#      that abstraction's "one logical message in, one logical bytes out"
#      contract.
#
#   2. Future actions (delete, mkdir-as-standalone, rename) may want similar
#      multi-step RPC sessions. Establishing the inline-frames pattern here
#      gives those a clean precedent — they can reuse the same
#      ``_enter_rpc_session`` / ``_exit_rpc_session`` helpers we factored
#      out of rpc_request.
#
# Per-frame data cap is _STORAGE_WRITE_MAX_DATA_PER_FRAME (512 bytes; see
# the SHA-cited block in clipper/rpc.py). Payloads above this are split into
# multiple frames; intermediate frames carry PB_Main.has_next=True and the
# firmware only acknowledges the final has_next=False frame
# (rpc_storage.c line 459 citation in clipper/rpc.py).
#
# Atomicity: the firmware exposes no atomic-write API. A failure mid-chunk
# leaves a truncated file on disk. ``_exit_rpc_session`` still runs in
# ``finally`` so the device returns to text-CLI mode regardless.


def _chunk_data(
    data: bytes,
    max_per_chunk: int = _STORAGE_WRITE_MAX_DATA_PER_FRAME,
) -> list[bytes]:
    """Split *data* into chunks of at most *max_per_chunk* bytes.

    Empty input is a special case: returns ``[b""]`` (a single empty chunk)
    NOT ``[]`` — otherwise the caller would never send a WriteRequest at
    all and a 0-byte file would silently not be created. The firmware
    accepts an empty File.data on the final frame as a valid 0-byte write.
    """
    if not data:
        return [b""]
    return [data[i : i + max_per_chunk] for i in range(0, len(data), max_per_chunk)]


async def _read_response_until_status(
    ser,
    loop: asyncio.AbstractEventLoop,
    rx_buf: bytearray,
    timeout: float = 10.0,
) -> int:
    """Read one PB_Main response frame from *ser* and return its command_status.

    *rx_buf* is the shared receive buffer carried across multiple reads in
    the same RPC session — it may already contain bytes from the
    ``_enter_rpc_session`` carry-over OR from a previous response's
    leftover (e.g. when a downstream firmware coalesces multiple frames
    into a single USB packet). Bytes consumed by the parsed frame are
    removed from *rx_buf* in place.

    Returns the integer command_status (0 = OK). Caller decides what to do
    with non-OK codes (mkdir treats ERROR_STORAGE_EXIST=6 as success; write
    raises on anything non-zero).

    Raises asyncio.TimeoutError if no terminating frame arrives within
    *timeout* seconds.
    """
    deadline = time.monotonic() + timeout

    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"storage_write: timed out after {timeout}s waiting for "
                f"PB_Main ack frame"
            )

        try:
            result = try_read_delimited(bytes(rx_buf))
        except ValueError:
            # Implausible length → drop a byte and resync
            del rx_buf[0:1]
            continue

        if result is None:
            chunk = await loop.run_in_executor(None, lambda: ser.read(4096))
            if chunk:
                rx_buf.extend(chunk)
            else:
                await asyncio.sleep(0.005)
            continue

        pb_bytes, consumed = result
        del rx_buf[0:consumed]
        _cmd, command_status, _field_num, _payload, _has_next = decode_main(pb_bytes)
        return command_status


async def _write_chunks(
    ser,
    loop: asyncio.AbstractEventLoop,
    rx_buf: bytearray,
    path: str,
    data: bytes,
) -> None:
    """Stream *data* to *path* as N WriteRequest frames over an open RPC session.

    Intermediate frames carry has_next=True and elicit NO response from the
    firmware. The final frame has has_next=False and the firmware replies
    with one Empty (PB_Main oneof field 4) carrying command_status=0 on
    success. Any non-OK command_status raises RuntimeError.

    Caller is responsible for opening the RPC session
    (``_enter_rpc_session``) and tearing it down (``_exit_rpc_session``)
    around this call.
    """
    chunks = _chunk_data(data)
    log.debug(
        "storage_write: streaming %d bytes in %d chunk(s) to %r",
        len(data),
        len(chunks),
        path,
    )

    cmd_id = 1
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        payload = encode_storage_write_request(path, chunk)
        frame = encode_main(
            cmd_id,
            TAG_STORAGE_WRITE_REQUEST,
            payload,
            has_next=not is_last,
        )

        def _write(f=frame) -> None:
            ser.write(f)
            ser.flush()

        await loop.run_in_executor(None, _write)
        cmd_id += 1

    # Only the final (has_next=False) frame elicits a response.
    status = await _read_response_until_status(ser, loop, rx_buf, timeout=10.0)
    if status != 0:
        raise RuntimeError(
            f"storage_write: device returned command_status={status} for "
            f"WriteRequest path={path!r} (size={len(data)})"
        )


def _parent_paths(path: str) -> list[str]:
    """Return *path*'s parent directories from shallowest to deepest.

    Excludes the file path itself and the volume prefix (anything ``/x`` for
    a single-segment volume like ``/ext``, ``/int``) — those always exist on
    a working Flipper and asking the firmware to mkdir them returns
    ERROR_STORAGE_INVALID_NAME on some forks.

    Example:
        "/ext/aaa/bbb/foo.bin" → ["/ext/aaa", "/ext/aaa/bbb"]
        "/ext/foo.bin"         → []   (no intermediate dirs needed)
        "/ext/aaa/foo.bin"     → ["/ext/aaa"]
    """
    parts = [p for p in path.split("/") if p]
    # parts[0] is the volume (e.g. "ext"); we want directories AFTER it
    # but BEFORE the final filename. So slice [1:-1] of segments-after-root,
    # accumulating each as a full path.
    if len(parts) <= 2:
        return []  # only volume + file, no intermediate dirs

    parents: list[str] = []
    # parts = [volume, dir1, dir2, ..., filename]; we want /volume/dir1,
    # /volume/dir1/dir2, ..., up to but not including the filename.
    for end in range(2, len(parts)):
        parents.append("/" + "/".join(parts[:end]))
    return parents


async def _mkdir_parents(
    ser,
    loop: asyncio.AbstractEventLoop,
    rx_buf: bytearray,
    path: str,
) -> None:
    """Mkdir each intermediate directory of *path*, shallowest-first.

    The firmware returns command_status=STORAGE_ERROR_ALREADY_EXIST (=6)
    when the directory already exists; we treat that specific code as
    success (idempotent ensure-parents) and continue. Any other non-OK
    status raises RuntimeError.

    Caller holds the RPC session open (``_enter_rpc_session`` /
    ``_exit_rpc_session`` wrap this).
    """
    parents = _parent_paths(path)
    if not parents:
        return

    log.debug("storage_write: ensuring %d parent dir(s) for %r", len(parents), path)

    cmd_id = 100  # arbitrary, distinct from the write chunk IDs
    for parent in parents:
        payload = encode_storage_mkdir_request(parent)
        frame = encode_main(cmd_id, TAG_STORAGE_MKDIR_REQUEST, payload)

        def _write(f=frame) -> None:
            ser.write(f)
            ser.flush()

        await loop.run_in_executor(None, _write)
        cmd_id += 1

        status = await _read_response_until_status(ser, loop, rx_buf, timeout=10.0)
        if status == 0 or status == STORAGE_ERROR_ALREADY_EXIST:
            continue
        raise RuntimeError(
            f"storage_write: mkdir failed for parent {parent!r} "
            f"(command_status={status})"
        )


async def _storage_write_handler(
    flipper: FlipperConnection,
    params: StorageWriteParams,
) -> dict:
    """Write *params.content_b64* (base64-decoded) to *params.path* via RPC.

    Multi-step RPC session: opens ONE session, optionally mkdir's missing
    parents, streams the file bytes as N WriteRequest frames with
    PB_Main.has_next chaining, reads the single closing Empty acknowledgment,
    closes the session.

    Uses ``exclusive_serial`` directly (NOT ``rpc_request``) because
    rpc_request is single-shot — see the design note above.

    Atomicity caveat: on mid-chunk failure the file on the device
    filesystem may be partial. ``_exit_rpc_session`` still runs in the
    ``finally`` block so the device returns to text-CLI mode regardless.

    Returns:
        ``{"path": ..., "size": ..., "sha256": ...}`` (sha256 of the raw
        decoded bytes, for the caller to verify).
    """
    from clipper.safety import safety_allowed

    if not safety_allowed():
        raise EmissionBlocked("flipper_storage_write")

    try:
        raw = base64.b64decode(params.content_b64, validate=True)
    except binascii.Error as exc:
        raise RuntimeError(
            f"content_b64 is not valid base64: {exc}"
        ) from exc

    # NEVER log file contents at INFO; size + sha256 at DEBUG is fine.
    log.debug(
        "storage_write: path=%r size=%d create_parents=%s",
        params.path,
        len(raw),
        params.create_parents,
    )

    async with flipper.exclusive_serial():
        ser = flipper._serial
        if ser is None or not flipper._connected:
            from clipper.flipper import FlipperDisconnected
            raise FlipperDisconnected(
                "Flipper is not connected; storage_write rejected."
            )
        loop = asyncio.get_running_loop()

        carry_over = await flipper._enter_rpc_session(ser, loop)
        # Shared receive buffer: starts with any bytes that arrived after the
        # echo terminator (a single USB read can pull echo + first response
        # frame together) and persists across mkdir+write reads in this
        # session so no inbound bytes are dropped.
        rx_buf = bytearray(carry_over)
        try:
            if params.create_parents:
                await _mkdir_parents(ser, loop, rx_buf, params.path)
            await _write_chunks(ser, loop, rx_buf, params.path, raw)
        finally:
            await flipper._exit_rpc_session(ser, loop)

    sha256_hex = hashlib.sha256(raw).hexdigest()
    log.debug(
        "storage_write: %d bytes written sha256=%s",
        len(raw),
        sha256_hex,
    )
    return {"path": params.path, "size": len(raw), "sha256": sha256_hex}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    Action(
        name="flipper_storage_list",
        description=(
            "List the contents of a directory on the Flipper Zero filesystem. "
            "Path must be absolute (start with '/'), free of '..' segments and "
            "control bytes. Returns entries sorted alphabetically by name. "
            "Common roots: /ext (SD card), /int (internal flash)."
        ),
        params=StorageListParams,
        handler=_storage_list_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_storage_stat",
        description=(
            "Stat a path on the Flipper Zero filesystem and report its type "
            "(file, dir, volume, or root) and size. Path must be absolute, "
            "free of '..' segments and control bytes."
        ),
        params=StorageStatParams,
        handler=_storage_stat_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_storage_md5sum",
        description=(
            "Compute the md5 hash of a file on the Flipper Zero filesystem. "
            "Returns a 32-character lowercase hex digest. Path must be "
            "absolute, free of '..' segments and control bytes."
        ),
        params=StorageMd5sumParams,
        handler=_storage_md5sum_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_storage_info",
        description=(
            "Report filesystem info (label, fs type, total/free KiB) for a "
            "Flipper Zero volume. Only '/int' (internal flash) and '/ext' "
            "(SD card) are supported; other paths are rejected. Defaults to "
            "'/ext'."
        ),
        params=StorageInfoParams,
        handler=_storage_info_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_storage_read",
        description=(
            "Read a file from the Flipper Zero filesystem "
            "(binary-safe via RPC). Returns size and sha256 of the file. "
            "By default the file content is returned base64-encoded in "
            "content_b64 if it's <= max_inline_bytes (default 1 MiB); "
            "larger files raise an error suggesting download=True. "
            "When download=True, saves the file to the host under "
            "~/.clipper/flipper-files/<flipper-path> (override with "
            "CLIPPER_DOWNLOAD_DIR env var) and returns host_path instead "
            "of content_b64. Path must be absolute (starts with '/')."
        ),
        params=StorageReadParams,
        handler=_storage_read_handler,
        emissive=False,
    )
)

register(
    Action(
        name="flipper_storage_write",
        description=(
            "Write a file to the Flipper Zero filesystem (binary-safe via RPC). "
            "Path must be absolute (starts with '/'). content_b64 is the file's "
            "bytes base64-encoded (empty string writes a 0-byte file). "
            "create_parents=True mkdir's missing intermediate directories. "
            "Returns {path, size, sha256} for verification. "
            "Safety-gated: requires the SAFETY toggle to be ON (or CLIPPER_SAFETY=1)."
        ),
        params=StorageWriteParams,
        handler=_storage_write_handler,
        # emissive=False — the safety gate is enforced explicitly inside the
        # handler (mirrors the mfkey pattern). This keeps Action.invoke
        # from emitting a redundant denial audit log for a write that the
        # handler will block anyway.
        emissive=False,
    )
)
