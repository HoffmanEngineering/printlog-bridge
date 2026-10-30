"""`adapters/common/timeouts.py`'s `bounded`, which every adapter's waits go through."""

from __future__ import annotations

import asyncio

import pytest

from printlog_bridge.adapters.common.timeouts import bounded


def test_returns_the_result():
    async def answer():
        await asyncio.sleep(0)
        return 42

    assert asyncio.run(bounded(answer(), 1)) == 42


def test_times_out_and_cancels_what_it_waited_on():
    cancelled = []

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await bounded(slow(), 0.05)

    asyncio.run(scenario())
    assert cancelled == [True]


def test_a_cancel_is_a_cancel():
    async def scenario():
        outer = asyncio.ensure_future(bounded(asyncio.sleep(10), 5))
        await asyncio.sleep(0.01)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

    asyncio.run(scenario())


def test_a_cancel_at_the_moment_the_timer_fires_stays_a_cancel():
    """The timer and the cancel land in the same loop iteration: the caller is being stopped,
    so it must see CancelledError, not TimeoutError (or `run` would carry on)."""

    async def scenario():
        outer = asyncio.ensure_future(bounded(asyncio.sleep(10), 0))
        await asyncio.sleep(0)  # bounded has started: its timer runs in the next iteration
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

    asyncio.run(scenario())


def test_an_error_inside_comes_out():
    async def broken():
        await asyncio.sleep(0)
        raise ValueError("inside")

    with pytest.raises(ValueError, match="inside"):
        asyncio.run(bounded(broken(), 1))
