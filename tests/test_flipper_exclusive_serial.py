"""Tests for FlipperConnection.exclusive_serial().

The exclusive_serial async CM is the seam that lets multi-step operations
(e.g. a raw RPC roundtrip) hold the serial port for the duration of the
exchange. It MUST:
  - raise FlipperDisconnected if flipper.connected is False
  - acquire FlipperConnection._lock for the duration of the with body
  - release the lock on exit, including when the body raises
  - serialize concurrent send_command callers (they queue on the lock)
"""

from __future__ import annotations

import asyncio

import pytest

from clipper.flipper import FlipperConnection, FlipperDisconnected


def _make_connected_flipper() -> FlipperConnection:
    flipper = FlipperConnection(port_factory=lambda: "/dev/fake", reconnect_interval=60)
    flipper._connected = True
    return flipper


async def test_exclusive_serial_is_async_context_manager() -> None:
    """exclusive_serial() returns an async CM usable with `async with`."""
    flipper = _make_connected_flipper()
    cm = flipper.exclusive_serial()
    assert hasattr(cm, "__aenter__")
    assert hasattr(cm, "__aexit__")
    async with cm:
        pass


async def test_exclusive_serial_holds_lock_for_duration() -> None:
    """The body of the with block runs while ._lock is held; released on exit."""
    flipper = _make_connected_flipper()

    async with flipper.exclusive_serial():
        assert flipper._lock.locked() is True
    assert flipper._lock.locked() is False


async def test_exclusive_serial_releases_lock_on_body_exception() -> None:
    """If the body raises, the lock is still released (cleanup in finally)."""
    flipper = _make_connected_flipper()

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        async with flipper.exclusive_serial():
            raise _Boom("body failure")

    assert flipper._lock.locked() is False


async def test_exclusive_serial_raises_when_disconnected() -> None:
    """If flipper.connected is False, the CM raises before acquiring the lock."""
    flipper = FlipperConnection(port_factory=lambda: "/dev/fake", reconnect_interval=60)
    flipper._connected = False

    with pytest.raises(FlipperDisconnected):
        async with flipper.exclusive_serial():
            pytest.fail("body must not execute when disconnected")

    # Lock must not have been left held
    assert flipper._lock.locked() is False


async def test_send_command_serializes_against_exclusive_serial(monkeypatch) -> None:
    """A concurrent send_command waits until the exclusive_serial body exits.

    Launches two tasks: one that holds exclusive_serial across an event, and
    one that calls send_command. The send_command's inner _send_locked must
    not run until after the exclusive holder exits.
    """
    flipper = _make_connected_flipper()

    send_locked_started = asyncio.Event()
    release_holder = asyncio.Event()
    exclusive_released = asyncio.Event()

    # Stub _send_locked so we don't need a real serial port; track ordering.
    observed_locked = []

    async def fake_send_locked(self, cmd: str, timeout: float) -> str:  # type: ignore[no-untyped-def]
        # Lock must be held when _send_locked is invoked.
        observed_locked.append(self._lock.locked())
        # Signal that we got here (and assert exclusive holder has released).
        send_locked_started.set()
        return f"ok:{cmd}"

    monkeypatch.setattr(FlipperConnection, "_send_locked", fake_send_locked)

    async def holder() -> None:
        async with flipper.exclusive_serial():
            # Body holds the lock; let the other task try to acquire.
            # Give the scheduler a chance to start the send_command task.
            for _ in range(10):
                await asyncio.sleep(0)
            # send_command should NOT have proceeded into _send_locked yet
            assert not send_locked_started.is_set(), (
                "send_command's _send_locked ran while exclusive_serial held the lock"
            )
            await release_holder.wait()
        exclusive_released.set()

    async def sender() -> str:
        return await flipper.send_command("hello")

    holder_task = asyncio.create_task(holder())
    # Tiny yield so the holder runs first and enters the CM.
    await asyncio.sleep(0)
    sender_task = asyncio.create_task(sender())

    # Let both tasks reach their wait points.
    for _ in range(10):
        await asyncio.sleep(0)
    # Sender is blocked on the lock.
    assert not send_locked_started.is_set()

    # Release the holder; sender should then proceed.
    release_holder.set()
    result = await asyncio.wait_for(sender_task, timeout=1.0)
    await asyncio.wait_for(holder_task, timeout=1.0)

    assert result == "ok:hello"
    assert exclusive_released.is_set()
    assert observed_locked == [True]
