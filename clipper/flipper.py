"""clipper.flipper — Flipper Zero USB serial connection layer.

Responsibilities:
- Port discovery (VID/PID scan + name pattern, env-var override)
- FlipperConnection lifecycle (connect, send_command, reconnect)
- Background reconnect task

Serial I/O rules:
- All bytes decoded as UTF-8 with errors="replace"
- send_command protected by asyncio.Lock
- Multiple matching ports raises FlipperConfigError
- PermissionError on open raises FlipperPortBusy
- ANSI escapes stripped from responses              (firmware colorizes output)
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import os
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

import serial
import serial.tools.list_ports

log = logging.getLogger(__name__)

# Flipper Zero USB CDC identifiers (STM32 virtual COM port)
FLIPPER_VID = 0x0483
FLIPPER_PID = 0x5740
# macOS assigns names like /dev/tty.usbmodemflip_Flipper1 — match the prefix
FLIPPER_PORT_PATTERN = "usbmodemflip*"

# The Flipper CLI prompt that marks the end of a response
FLIPPER_PROMPT = b">: "

# ANSI CSI escape sequences emitted by the firmware (color, cursor, etc.).
# Matches things like \x1b[31m, \x1b[38;2;255;130;0m, \x1b[0m, \x1b[2J.
_ANSI_CSI_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")

# Serial port settings
BAUD_RATE = 115_200
READ_CHUNK = 4096

# RPC-mode tunables.
_RPC_READ_CHUNK = 4096
_RPC_ENTRY_MAX = 1.0   # max seconds to wait for the `start_rpc_session\r` echo
_STOP_DRAIN_TIMEOUT = 3.0  # max seconds to wait for the `>: ` prompt after teardown
_TEXT_PROMPT = b">: "

# Draining trailing bytes so the next operation starts from a quiet line.
# A single empty read is NOT proof the device is idle — its trailing prompt can
# land after one read-timeout window. So we drain until the line has stayed
# silent for a sustained interval (_QUIESCE_QUIET) AFTER the last byte seen,
# bounded by _QUIESCE_MAX. If the line is already quiet (no bytes at all), we
# return immediately.
_QUIESCE_QUIET = 0.25
_QUIESCE_MAX = 1.5


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FlipperError(Exception):
    """Base class for all Flipper-layer errors."""


class FlipperConfigError(FlipperError):
    """Configuration or discovery problem (multiple ports, bad env, etc.)."""


class FlipperDisconnected(FlipperError):
    """Operation attempted while Flipper is not connected."""


class FlipperPortBusy(FlipperError):
    """Serial port is held by another process."""


# ---------------------------------------------------------------------------
# Port discovery
# ---------------------------------------------------------------------------


def find_flipper_port(env_override: str | None = None) -> str | None:
    """Return the serial port path for the attached Flipper Zero, or None.

    Resolution order:
    1. ``env_override`` (or ``CLIPPER_FLIPPER_PORT`` env var) — used verbatim,
       skips scanning entirely.
    2. Scan ``serial.tools.list_ports.comports()`` for VID/PID match.
    3. Fall back to port-name pattern ``usbmodemflip*``.

    Raises:
        FlipperConfigError: exactly when two or more ports match and no
            override was provided — fail-fast, never silently pick one.
    """
    override = env_override or os.environ.get("CLIPPER_FLIPPER_PORT")
    if override:
        log.debug("flipper port override: %s", override)
        return override

    ports = serial.tools.list_ports.comports()
    matches: list[str] = []

    for p in ports:
        vid_pid_match = (p.vid == FLIPPER_VID and p.pid == FLIPPER_PID)
        name_match = fnmatch.fnmatch(os.path.basename(p.device), FLIPPER_PORT_PATTERN)
        if vid_pid_match or name_match:
            matches.append(p.device)
            log.debug("flipper port candidate: %s (vid=%s pid=%s)", p.device, p.vid, p.pid)

    if len(matches) == 0:
        log.debug("no Flipper Zero port found")
        return None

    if len(matches) > 1:
        raise FlipperConfigError(
            f"Multiple Flipper Zero ports detected: {', '.join(sorted(matches))}. "
            "Set CLIPPER_FLIPPER_PORT to select one explicitly."
        )

    log.info("discovered Flipper Zero on %s", matches[0])
    return matches[0]


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_DEFAULT_RECONNECT_INTERVAL = 1.0  # seconds


def _open_serial(port: str) -> serial.Serial:
    """Open a serial port, mapping OS-level permission errors to FlipperPortBusy."""
    try:
        s = serial.Serial(
            port=port,
            baudrate=BAUD_RATE,
            timeout=0.1,
        )
        return s
    except serial.SerialException as exc:
        msg = str(exc).lower()
        if "busy" in msg or "permission" in msg or "access denied" in msg:
            raise FlipperPortBusy(
                f"Serial port {port!r} is held by another process. "
                "Close any other serial terminals (e.g. screen, minicom, Arduino IDE) "
                f"and retry. Original error: {exc}"
            ) from exc
        raise


def _parse_device_info(output: str) -> dict[str, str]:
    """Parse ``device info`` CLI output into a flat dict."""
    info: dict[str, str] = {}
    for line in output.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            info[key.strip().lower().replace(" ", "_")] = value.strip()
    return info


def _parse_power_info(output: str) -> int | None:
    """Extract battery charge percentage from ``power info`` output."""
    for line in output.splitlines():
        line_lower = line.lower()
        if "charge" in line_lower or "battery" in line_lower or "soc" in line_lower:
            # Look for something like "Battery Charge: 73%" or "charge: 73"
            parts = line.split(":")
            if len(parts) >= 2:
                val = parts[-1].strip().rstrip("%")
                try:
                    pct = int(float(val))
                    if 0 <= pct <= 100:
                        return pct
                except ValueError:
                    pass
    return None


class FlipperConnection:
    """Manages a single, shared, lock-protected serial connection to a Flipper Zero.

    Usage::

        conn = FlipperConnection()
        await conn.start()
        response = await conn.send_command("device info")
        await conn.stop()

    Design notes:
    - ``send_command`` is protected by an ``asyncio.Lock`` so concurrent
      callers are serialized, never interleaved.
    - Bytes are decoded UTF-8 with ``errors="replace"``.
    - Background task polls for reconnect every ``_reconnect_interval`` seconds.
    """

    def __init__(
        self,
        port_factory: Callable[[], str | None] | None = None,
        reconnect_interval: float = _DEFAULT_RECONNECT_INTERVAL,
        drain_timeout: float = 1.5,
    ) -> None:
        self._port_factory: Callable[[], str | None] = port_factory or find_flipper_port
        self._reconnect_interval = reconnect_interval
        self._drain_timeout = drain_timeout

        self._serial: serial.Serial | None = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._reconnect_task: asyncio.Task | None = None  # type: ignore[type-arg]

        self.device_info: dict[str, str] = {}
        self.battery: int | None = None

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open the port, handshake with the device, start the reconnect loop."""
        await self._try_connect()
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def stop(self) -> None:
        """Cancel the background task and close the serial port."""
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None
        self._close_port()

    # ------------------------------------------------------------------
    # Command I/O
    # ------------------------------------------------------------------

    async def send_command(
        self, cmd: str, timeout: float = 2.0, *, retry_if_empty: bool = False
    ) -> str:
        """Write *cmd* to the Flipper CLI and return the response (up to the prompt).

        Routes through ``exclusive_serial()`` so concurrent callers are
        serialized onto the serial port for the duration.

        ``retry_if_empty`` (read-only/idempotent callers only): if the response
        comes back empty — the signature of a stale-prompt bleed under back-to-
        back operations, where a late trailing prompt from the previous command
        satisfied this read before the real output arrived — re-issue the
        command once, still holding the lock. The retry's pre-flight drain has a
        second chance to clear the late tail. NEVER enable this for emissive or
        state-changing commands: a retry would send them twice.

        Raises:
            FlipperDisconnected: if the connection is not currently open.
        """
        async with self.exclusive_serial():
            result = await self._send_locked(cmd, timeout)
            if retry_if_empty and not result.strip():
                log.warning(
                    "empty response to %r — retrying once (stale-prompt bleed?)", cmd
                )
                result = await self._send_locked(cmd, timeout)
            return result

    # ------------------------------------------------------------------
    # Timeout-bounded long-running CLI commands (emulate, subghz rx, ...)
    # ------------------------------------------------------------------

    async def run_bounded_command(self, cmd: str, duration_s: float) -> str:
        """Run a long-running CLI command for ``duration_s`` then stop it.

        Flipper's long-running CLI commands (``emulate``, ``subghz rx``, the
        NFC ``scanner``) loop until an ETX byte (``0x03``) arrives on the pipe.
        This drives that contract: write ``cmd\\r``, collect whatever the device
        prints for ``duration_s`` seconds, then ALWAYS send the ETX stop byte
        and drain to the ``>: `` prompt — even on timeout or error — so the
        device is never left stuck in the command for the next caller (N2,
        mirroring ``_exit_rpc_session``'s always-close contract via try/finally).

        Routes through ``exclusive_serial()`` so concurrent callers serialize
        onto the single serial port for the whole window, and so the line is
        drained to sustained quiescence on exit (L1).

        Returns the collected output, ANSI-stripped and echo/prompt-stripped
        the same way ``_send_locked`` cleans a normal command response.

        Raises:
            FlipperDisconnected: if not connected, or if a serial error during
                the collect window marks the port dead (L3).
        """
        async with self.exclusive_serial():
            if self._serial is None or not self._connected:
                raise FlipperDisconnected(
                    "Flipper is not connected; run_bounded_command rejected."
                )
            ser = self._serial
            loop = asyncio.get_running_loop()
            buf = bytearray()

            try:
                # Pre-flight drain so a stale prompt from the previous op can't
                # leak into this command's collected output, then write cmd\r.
                # The Flipper CLI uses CR (\r) as the line terminator (LF would
                # be processed as a phantom empty line).
                await self._quiesce_serial()
                payload = (cmd + "\r").encode("utf-8")

                def _write_cmd() -> None:
                    ser.reset_input_buffer()
                    ser.write(payload)
                    ser.flush()

                await loop.run_in_executor(None, _write_cmd)

                # Collect output for the bounded window. The command keeps
                # running (no terminating prompt yet), so we just accumulate
                # whatever bytes arrive until the window elapses.
                deadline = time.monotonic() + duration_s
                while time.monotonic() < deadline:
                    chunk = await loop.run_in_executor(
                        None, lambda: ser.read(READ_CHUNK)
                    )
                    if chunk:
                        buf.extend(chunk)
                    else:
                        await asyncio.sleep(0.02)
            except (serial.SerialException, OSError) as exc:
                # A rebooted / re-enumerated CDC port surfaces a bare OSError
                # (errno 6, ENXIO) or SerialException from the collect-window
                # I/O — mark dead so the reconnect loop re-opens it, instead of
                # leaking a raw OSError. The finally still attempts the ETX stop.
                log.error("serial error during run_bounded_command(%r): %s", cmd, exc)
                self._mark_disconnected()
                raise FlipperDisconnected(f"Serial error: {exc}") from exc
            finally:
                # ALWAYS send ETX + drain to the prompt — even on timeout or
                # error — so the device exits the long-running command (N2).
                # Best-effort: serial errors here are swallowed so they can't
                # mask a primary exception we may be unwinding.
                if self._serial is not None and self._connected:
                    try:
                        def _write_stop() -> None:
                            ser.write(b"\x03")
                            ser.flush()

                        await loop.run_in_executor(None, _write_stop)
                        await self._drain_to_text_prompt(
                            ser, loop, _STOP_DRAIN_TIMEOUT
                        )
                    except (serial.SerialException, OSError) as exc:
                        log.warning(
                            "run_bounded_command stop: ignoring serial error: %s",
                            exc,
                            exc_info=True,
                        )

            log.debug(
                "run_bounded_command(%r) collected %d bytes over %.2fs",
                cmd,
                len(buf),
                duration_s,
            )
            cleaned = _ANSI_CSI_RE.sub(b"", bytes(buf))
            text = cleaned.decode("utf-8", errors="replace")
            return _strip_echo_and_prompt(text, cmd)

    # ------------------------------------------------------------------
    # RPC roundtrip (binary protobuf, one-shot)
    # ------------------------------------------------------------------

    async def rpc_request(
        self,
        *,
        request_field_num: int,
        request_payload: bytes,
        response_field_num: int,
        timeout: float = 5.0,
    ) -> bytes:
        """Open a fresh RPC session, send one PB_Main request, read streaming
        responses (concatenating payloads while has_next=True), close the
        session cleanly, and return the assembled response payload.

        Uses ``exclusive_serial()`` to hold the port for the duration. The RPC
        session is always closed (StopSession + escape bytes + drain to text
        prompt) on the way out — even on timeout or command_status error, so the
        device is never left stuck in RPC mode.

        Args:
            request_field_num:  PB_Main oneof tag for the request message.
            request_payload:    Pre-encoded inner-message bytes (may be empty
                                for requests like StopSession/Empty).
            response_field_num: Expected PB_Main oneof tag for response frames.
                                Frames carrying a different tag are logged at
                                DEBUG and skipped (e.g. unsolicited Gui events).
            timeout:            Seconds to wait for the full has_next=False
                                terminating frame. Bigger files may need >5s.

        Returns:
            The assembled response payload — concatenation of every accepted
            frame's content_payload in arrival order.

        Raises:
            FlipperDisconnected: if not currently connected.
            asyncio.TimeoutError: if the receive loop doesn't complete within
                                  ``timeout`` seconds.
            RuntimeError:        if the device returns a frame with
                                  command_status != 0 (non-OK).
        """
        # Local imports to avoid a circular import at module-load time.
        from clipper.rpc import (
            decode_main,
            encode_main,
            try_read_delimited,
        )

        async with self.exclusive_serial():
            ser = self._serial
            if ser is None or not self._connected:
                raise FlipperDisconnected(
                    "Flipper is not connected; rpc_request rejected."
                )
            loop = asyncio.get_running_loop()

            assembled = bytearray()
            command_status_err: int | None = None
            request_cmd_id = 1
            try:
                # --- Step 1: enter RPC mode ------------------------------
                carry_over = await self._enter_rpc_session(ser, loop)

                # --- Step 2: send the request frame ----------------------
                request_frame = encode_main(
                    request_cmd_id, request_field_num, request_payload
                )

                def _write_request() -> None:
                    ser.write(request_frame)
                    ser.flush()

                await loop.run_in_executor(None, _write_request)
                log.debug(
                    "rpc_request: sent request field=%d cmd_id=%d (%d bytes)",
                    request_field_num,
                    request_cmd_id,
                    len(request_frame),
                )

                # --- Step 3: receive loop, bounded by timeout ------------
                async def _receive() -> None:
                    nonlocal command_status_err
                    buf = bytearray(carry_over)
                    while True:
                        try:
                            result = try_read_delimited(bytes(buf))
                        except ValueError as exc:
                            # Implausible frame length — drop a byte and resync.
                            log.warning(
                                "rpc_request: %s — dropping byte to resync", exc
                            )
                            buf = buf[1:]
                            continue

                        if result is None:
                            # Need more bytes
                            try:
                                chunk = await loop.run_in_executor(
                                    None, lambda: ser.read(_RPC_READ_CHUNK)
                                )
                            except (serial.SerialException, OSError) as exc:
                                log.warning(
                                    "rpc_request: serial error: %s",
                                    exc,
                                    exc_info=True,
                                )
                                raise
                            if chunk:
                                buf.extend(chunk)
                            else:
                                # No bytes available — yield so timeout can fire
                                await asyncio.sleep(0.005)
                            continue

                        pb_bytes, consumed = result
                        buf = buf[consumed:]

                        try:
                            (
                                _cmd_id,
                                command_status,
                                content_field_num,
                                content_payload,
                                has_next,
                            ) = decode_main(pb_bytes)
                        except ValueError as exc:
                            log.warning(
                                "rpc_request: failed to decode PB_Main "
                                "(%d bytes): %s",
                                len(pb_bytes),
                                exc,
                                exc_info=True,
                            )
                            continue

                        if command_status != 0:
                            # Fail-fast: record the
                            # error and break so the StopSession cleanup runs.
                            command_status_err = command_status
                            return

                        if content_field_num != response_field_num:
                            log.debug(
                                "rpc_request: skipping unsolicited frame "
                                "field=%d (expected %d)",
                                content_field_num,
                                response_field_num,
                            )
                            if not has_next:
                                return
                            continue

                        assembled.extend(content_payload)

                        if not has_next:
                            return

                await asyncio.wait_for(_receive(), timeout=timeout)
            except TimeoutError:
                # A timeout is NOT a disconnect (the device may just be slow).
                # Re-raise it unchanged — note TimeoutError is an OSError
                # subclass, so it must be handled before the broad clause below.
                # (asyncio.wait_for raises the builtin TimeoutError on 3.11+.)
                raise
            except (serial.SerialException, OSError) as exc:
                # A rebooted/re-enumerated port surfaces OSError (errno 6) or
                # SerialException from the enter/write/receive I/O — mark the
                # connection dead so the reconnect loop re-opens it, instead of
                # leaking a raw OSError to the caller.
                log.error("serial error during rpc_request: %s", exc)
                self._mark_disconnected()
                raise FlipperDisconnected(f"Serial error: {exc}") from exc
            finally:
                # --- Step 4: ALWAYS close the session ---------------------
                # Even on timeout or command_status error: we never
                # leave the device stuck in RPC mode for the next caller.
                await self._exit_rpc_session(ser, loop, cmd_id=request_cmd_id + 1)

            if command_status_err is not None:
                raise RuntimeError(
                    f"Flipper RPC returned command_status={command_status_err} "
                    f"(non-OK) for field={request_field_num}"
                )
            return bytes(assembled)

    # ------------------------------------------------------------------
    # RPC session helpers (used by rpc_request AND multi-step callers like
    # _storage_write_handler that send N WriteRequest frames in one session)
    # ------------------------------------------------------------------

    async def _enter_rpc_session(
        self,
        ser: serial.Serial,
        loop: asyncio.AbstractEventLoop,
    ) -> bytes:
        """Write ``start_rpc_session\\r`` and drain the CLI echo.

        Extracted from ``rpc_request`` so multi-shot callers (storage write,
        which streams N WriteRequest frames in one session) can open the same
        kind of session without duplicating the entry sequence.

        Caller is responsible for holding ``exclusive_serial()`` around this.

        Returns the bytes that arrived AFTER the terminating ``\\n`` in the
        echo — those are the first bytes of the device's RPC byte stream and
        the caller's receive loop must prepend them to its parse buffer.
        """
        # The previous operation's exclusive_serial() exit already drained the
        # line to quiet; reset_input_buffer here clears anything still buffered
        # before we write, so leftover bytes can't pollute the entry echo drain.
        def _enter_rpc() -> None:
            ser.reset_input_buffer()
            ser.write(b"start_rpc_session\r")
            ser.flush()

        await loop.run_in_executor(None, _enter_rpc)
        return await self._drain_rpc_echo(ser, loop)

    async def _exit_rpc_session(
        self,
        ser: serial.Serial,
        loop: asyncio.AbstractEventLoop,
        cmd_id: int = 2,
    ) -> None:
        """Send StopSession + escape bytes + drain to the text CLI prompt.

        Extracted from ``rpc_request`` so multi-shot callers can guarantee
        the same teardown semantics in their own ``finally`` blocks — never
        leaving the device stuck in RPC mode for the next caller.

        Serial errors during teardown are caught and logged but never
        re-raised: the caller may already be unwinding a primary exception
        and we mustn't mask it.
        """
        # Local imports to avoid circular import at module-load time.
        from clipper.rpc import TAG_STOP_SESSION, encode_empty, encode_main

        try:
            stop_frame = encode_main(cmd_id, TAG_STOP_SESSION, encode_empty())

            def _write_stop() -> None:
                ser.write(stop_frame)
                ser.flush()
                ser.write(b"\xff\xff\xff\r\r")
                ser.flush()

            await loop.run_in_executor(None, _write_stop)
            await self._drain_to_text_prompt(ser, loop, _STOP_DRAIN_TIMEOUT)
        except (serial.SerialException, OSError) as exc:
            log.warning(
                "rpc session teardown: ignoring serial error: %s",
                exc,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Exclusive serial access (public)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def exclusive_serial(self):  # type: ignore[no-untyped-def]
        """Hold the serial port exclusively for a multi-step operation.

        Raises FlipperDisconnected if not connected; otherwise acquires
        ``self._lock`` for the duration of the ``with`` body so concurrent
        callers (text CLI commands and raw RPC roundtrips) are serialized
        onto the single serial port.

        On exit — before the lock is released — the line is drained to
        quiescence (``_quiesce_serial``). A command/RPC read returns the moment
        it sees the terminating prompt, but the device may still be flushing a
        trailing prompt or echo. Without this drain those in-flight tail bytes
        bleed into the *next* operation's read (its ``reset_input_buffer`` can't
        clear bytes that haven't arrived yet), so a stale ``>: `` satisfies the
        next ``read_until`` immediately and yields an empty/truncated response.
        Draining here guarantees the next caller starts from a quiet line.
        """
        if not self._connected:
            raise FlipperDisconnected(
                "Flipper is not connected; exclusive_serial rejected."
            )

        async with self._lock:
            try:
                yield
            finally:
                await self._quiesce_serial()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _quiesce_serial(self) -> None:
        """Read and discard trailing bytes until the line is *sustained* quiet.

        Called from ``exclusive_serial`` (after each op) and as a pre-flight in
        ``_send_locked`` / ``_enter_rpc_session`` (before writing), so a slow
        trailing prompt from the previous operation can't bleed into the next
        read and get matched as a stale prompt (yielding an empty/truncated
        response).

        A single empty read is not enough: the device may pause mid-flush and
        emit a trailing prompt slightly later. So once we've seen any bytes we
        keep reading until the line has been silent for ``_QUIESCE_QUIET`` after
        the last byte. If the line is already quiet (no bytes at all), we return
        immediately so clean handoffs add no latency. Bounded by
        ``_QUIESCE_MAX``. Best-effort: never raises.
        """
        ser = self._serial
        if ser is None or not self._connected:
            return
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + _QUIESCE_MAX
        last_byte = 0.0
        saw_bytes = False
        try:
            while time.monotonic() < deadline:
                chunk = await loop.run_in_executor(
                    None, lambda: ser.read(READ_CHUNK)
                )
                now = time.monotonic()
                if chunk:
                    saw_bytes = True
                    last_byte = now
                    continue
                if not saw_bytes:
                    return  # line was already quiet — nothing to drain
                if now - last_byte >= _QUIESCE_QUIET:
                    return  # silent for the full settle window after last byte
                await asyncio.sleep(0.02)  # pace the poll while waiting for quiet
        except (serial.SerialException, OSError) as exc:
            log.debug("quiesce drain ignored serial error: %s", exc)

    async def _send_locked(self, cmd: str, timeout: float) -> str:
        """Inner send — must be called while holding ``self._lock``."""
        if self._serial is None or not self._connected:
            raise FlipperDisconnected("Flipper is not connected; command rejected.")

        loop = asyncio.get_running_loop()
        try:
            # The Flipper CLI uses CR (\r) as the line terminator. Sending
            # CR+LF causes the firmware to process the LF as a phantom empty
            # line, which shifts every subsequent read by one slot. Matches
            # the pyflipper protocol.
            payload = (cmd + "\r").encode("utf-8")
            deadline = time.monotonic() + timeout

            # Pre-flight: drain any trailing bytes from a previous operation to
            # sustained quiet before we write, so a stale prompt can't be
            # matched as this command's response. reset_input_buffer() then
            # clears anything still buffered. Together these prevent the
            # off-by-one bleed between back-to-back commands.
            await self._quiesce_serial()

            def _write_and_flush() -> None:
                self._serial.reset_input_buffer()  # type: ignore[union-attr]
                self._serial.write(payload)  # type: ignore[union-attr]
                self._serial.flush()  # type: ignore[union-attr]

            await loop.run_in_executor(None, _write_and_flush)

            # Read until we see the prompt or time out
            buf = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning("timeout waiting for Flipper prompt after cmd=%r", cmd)
                    break
                # read_until is blocking — run in executor
                chunk = await loop.run_in_executor(
                    None,
                    lambda: self._serial.read_until(FLIPPER_PROMPT, READ_CHUNK),  # type: ignore[union-attr]
                )
                if chunk:
                    buf.extend(chunk)
                if buf.endswith(FLIPPER_PROMPT):
                    break
                if not chunk:
                    # No data yet — small yield to avoid spinning
                    await asyncio.sleep(0.01)

            log.debug("raw response to cmd=%r (%d bytes): %r", cmd, len(buf), bytes(buf)[:500])
            # Strip ANSI escapes before decode so split/parse sees clean text.
            cleaned = _ANSI_CSI_RE.sub(b"", bytes(buf))
            # Decode: UTF-8, replace unmappable bytes
            text = cleaned.decode("utf-8", errors="replace")
            # Strip the echoed command and the trailing prompt
            text = _strip_echo_and_prompt(text, cmd)
            return text

        except (serial.SerialException, OSError) as exc:
            # A rebooted / re-enumerated CDC port surfaces a bare OSError
            # (errno 6, ENXIO) rather than serial.SerialException — both mean
            # the port is dead, so mark disconnected and let the reconnect loop
            # re-open the freshly enumerated device.
            log.error("serial error during send_command(%r): %s", cmd, exc)
            self._mark_disconnected()
            raise FlipperDisconnected(f"Serial error: {exc}") from exc

    def _mark_disconnected(self) -> None:
        if self._connected:
            log.warning("Flipper marked as disconnected")
        self._connected = False
        self._close_port()

    def _close_port(self) -> None:
        if self._serial is not None:
            with contextlib.suppress(Exception):
                self._serial.close()
            self._serial = None

    async def _try_connect(self) -> bool:
        """Attempt to open the port and handshake. Returns True on success."""
        try:
            port = self._port_factory()
        except FlipperConfigError:
            raise

        if port is None:
            log.debug("no Flipper port available; remaining disconnected")
            return False

        log.info("connecting to Flipper on %s", port)
        try:
            ser = await asyncio.get_running_loop().run_in_executor(None, _open_serial, port)
        except FlipperPortBusy as exc:
            # Don't crash the server — the user may launch qFlipper, the
            # official Web Serial tool (lab.flipper.net) in Chrome, a serial
            # terminal, etc., alongside clipper. Log clearly and let the
            # reconnect loop retry once the other process releases the port.
            log.warning(
                "Flipper port busy — clipper will retry. To find the offender: "
                "`lsof %s`. Common culprits on macOS: qFlipper (quit via "
                "Cmd+Q), a Chrome tab using Web Serial (lab.flipper.net / "
                "app.flipperzero.one — visit chrome://serial-internals to find "
                "active connections), or a lingering `screen` session. "
                "Underlying error: %s",
                port,
                exc,
            )
            return False
        except (serial.SerialException, OSError) as exc:
            log.warning("failed to open %s: %s", port, exc)
            return False

        self._serial = ser
        self._connected = True

        # Drain the welcome banner: the Flipper prints a banner + initial `>: `
        # prompt as soon as USB CDC opens. Send a CR to nudge a fresh prompt
        # and consume everything up through that prompt before the first real
        # command.
        await self._drain_welcome_banner()

        # Fetch device info to populate the cache. The stock Flipper firmware
        # registers this as a single-token command with an underscore
        # (`device_info`), not two tokens — `device info` is parsed as
        # command=`device` (which doesn't exist) and silently errors.
        # The device_info round-trip is the connectivity probe: if it RAISES,
        # the port we just opened isn't actually a live Flipper (a stale/dead
        # node lingering after a reboot/re-enumeration, or the device dropped
        # again). Declaring connected here would wedge the server on a dead FD
        # and make flipper_state report a false connected:true. So treat a raised
        # handshake as "not connected" — mark disconnected and return False so the
        # reconnect loop retries with a FRESH comports() scan until the real
        # device answers. (An empty-but-non-raising reply is the RPC-stuck case;
        # that stays connected-but-degraded, as before.)
        try:
            raw_info = await self.send_command("device_info", timeout=3.0)
        except Exception as exc:
            log.warning(
                "handshake failed on %s (%s) — connection not established", port, exc
            )
            self._mark_disconnected()
            return False

        self.device_info = _parse_device_info(raw_info)
        if not self.device_info:
            # Empty parse (no exception) usually means the device is stuck in
            # RPC mode (qFlipper / lab.flipper.net leaves it there after exit).
            log.warning(
                "device_info returned no parseable lines. The Flipper may "
                "be stuck in RPC binary mode from a previous qFlipper / "
                "Web Serial session — reboot the device (Settings → Power "
                "→ Reboot, or hold BACK+LEFT for 5s) and reconnect."
            )
        else:
            log.info(
                "connected: name=%r firmware=%r",
                self.device_info.get("hardware_name"),
                self.device_info.get("firmware_commit"),
            )

        # Battery info lives under `info power` on stock Flipper firmware and
        # Momentum forks. `power_info` does NOT exist; `power` is a separate
        # command for shutdown/reboot/5v control.
        try:
            raw_power = await self.send_command("info power", timeout=3.0)
            self.battery = _parse_power_info(raw_power)
            log.info("battery=%r", self.battery)
        except Exception:
            log.info("info power failed (non-fatal)", exc_info=True)

        return True

    async def _drain_rpc_echo(
        self, ser: serial.Serial, loop: asyncio.AbstractEventLoop
    ) -> bytes:
        """Consume the CLI echo of ``start_rpc_session\\r`` up through its \\n.

        Returns the bytes that arrived AFTER the terminating ``\\n``. Those
        bytes are the start of the first RPC frame and must be prepended to
        the receive loop's buffer.
        """
        deadline = time.monotonic() + _RPC_ENTRY_MAX
        buf = bytearray()
        while time.monotonic() < deadline:
            try:
                chunk = await loop.run_in_executor(
                    None, lambda: ser.read(_RPC_READ_CHUNK)
                )
            except (serial.SerialException, OSError) as exc:
                log.warning(
                    "rpc_request: error draining RPC entry echo: %s",
                    exc,
                    exc_info=True,
                )
                raise
            if chunk:
                buf.extend(chunk)
                nl_idx = buf.find(b"\n")
                if nl_idx != -1:
                    carry_over = bytes(buf[nl_idx + 1 :])
                    log.debug(
                        "rpc_request: RPC entry echo drained "
                        "(%d bytes; %d carry-over)",
                        nl_idx + 1,
                        len(carry_over),
                    )
                    return carry_over
            else:
                await asyncio.sleep(0.02)
        log.warning(
            "rpc_request: timed out waiting for RPC entry echo terminator "
            "(%d bytes buffered) — continuing, receive loop will resync",
            len(buf),
        )
        return bytes(buf)

    async def _drain_to_text_prompt(
        self,
        ser: serial.Serial,
        loop: asyncio.AbstractEventLoop,
        timeout: float,
    ) -> None:
        """Consume bytes from serial until `>: ` appears (text CLI is back)."""
        buf = bytearray()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            try:
                chunk = await loop.run_in_executor(
                    None, lambda: ser.read(_RPC_READ_CHUNK)
                )
            except (serial.SerialException, OSError) as exc:
                log.warning(
                    "rpc_request: error draining to text prompt: %s",
                    exc,
                    exc_info=True,
                )
                return

            if chunk:
                buf.extend(chunk)
                if _TEXT_PROMPT in buf:
                    log.debug(
                        "rpc_request: text CLI prompt detected; drain complete"
                    )
                    return

            await asyncio.sleep(0.02)

        log.warning(
            "rpc_request: timed out waiting for text CLI prompt after stop "
            "(drained %d bytes); continuing anyway",
            len(buf),
        )

    async def _drain_welcome_banner(self) -> None:
        """Synchronize with the CLI after CDC open AND escape any stuck RPC mode.

        Three things to handle at port open on a real Flipper:

        1. The firmware auto-prints a welcome banner (ASCII-art dolphin +
           version line) ending in a `>: ` prompt.
        2. The first host->device byte is often consumed as a "wake" by the
           USB CDC pipeline and never reaches the CLI.
        3. If a previous client (qFlipper, Chrome `lab.flipper.net`, etc.)
           left the device in RPC binary mode, text commands are silently
           dropped. Reading the Momentum source (`applications/services/rpc/
           rpc.c`) shows that invalid protobuf input triggers session
           closure: the firmware sends an `ERROR_DECODE` response and the
           closed-callback fires, which returns the device to text CLI mode.
           So we proactively send a short sequence of intentionally invalid
           bytes — a no-op in text mode (just gets echoed as garbage and
           printed back as `could not find command`), but in RPC mode it
           closes the session and gets us back to a working CLI.

        Strategy: write the wake+escape bytes, then read EVERY byte the
        device sends until the line goes quiet for ``quiet_period``.

        If ``drain_timeout`` is <= 0 (test fixtures), the drain is fully
        skipped — no bytes written, no bytes consumed.
        """
        if self._serial is None or self._drain_timeout <= 0:
            return
        loop = asyncio.get_running_loop()
        quiet_period = 0.3
        try:
            # Wake + RPC-escape sequence. The 0xff 0xff 0xff bytes can't be a
            # valid protobuf message header (high bits set on a length varint
            # that decodes to an impossible value), so if the firmware was
            # parsing protobuf it will hit ERROR_DECODE and exit RPC mode.
            # The trailing CR pair gives us blank-line prompts in text mode.
            escape = b"\xff\xff\xff\r\r"
            await loop.run_in_executor(None, self._serial.write, escape)
            await loop.run_in_executor(None, self._serial.flush)

            deadline = time.monotonic() + self._drain_timeout
            buf = bytearray()
            last_byte_at = time.monotonic()
            while time.monotonic() < deadline:
                chunk = await loop.run_in_executor(
                    None,
                    lambda: self._serial.read(READ_CHUNK),  # type: ignore[union-attr]
                )
                now = time.monotonic()
                if chunk:
                    buf.extend(chunk)
                    last_byte_at = now
                elif now - last_byte_at >= quiet_period and buf:
                    # Line went quiet after receiving data — we're synced.
                    break
                else:
                    await asyncio.sleep(0.02)
            tail = bytes(buf)[-80:]
            log.debug(
                "synced with Flipper: drained %d bytes (head=%r tail=%r)",
                len(buf),
                bytes(buf)[:120],
                tail,
            )
        except (serial.SerialException, OSError):
            log.warning("serial error draining welcome banner", exc_info=True)
            self._mark_disconnected()

    async def _reconnect_loop(self) -> None:
        """Background task: polls while disconnected, reconnects on plug-in."""
        while True:
            try:
                await asyncio.sleep(self._reconnect_interval)
                if not self._connected:
                    log.debug("reconnect loop: attempting reconnect")
                    await self._try_connect()
            except asyncio.CancelledError:
                return
            except FlipperConfigError:
                # Multiple matching ports / bad CLIPPER_FLIPPER_PORT — fatal config
                # error; bubble it so the user has to fix the env explicitly.
                raise
            except Exception:
                # log the actual exception with traceback so we can see
                # what's going wrong, instead of silently retrying forever.
                log.warning("reconnect loop caught exception (will retry)", exc_info=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_echo_and_prompt(text: str, cmd: str) -> str:
    """Remove the echoed command at the start and the CLI prompt at the end."""
    # The Flipper echoes the command back; strip that first line if it matches
    lines = text.splitlines()
    if lines and cmd.strip() in lines[0]:
        lines = lines[1:]
    # Strip trailing prompt line(s)
    while lines and lines[-1].strip() in (">:", ">: ", ""):
        lines.pop()
    return "\n".join(lines).strip()
