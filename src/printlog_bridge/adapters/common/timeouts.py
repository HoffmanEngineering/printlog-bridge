"""Waiting with a deadline, in code that must also stop when cancelled."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def bounded(awaitable: Awaitable[T], timeout: float) -> T:
    """`asyncio.wait_for` for code that must stop when cancelled. Before Python 3.12, wait_for
    returns the result and drops the cancel when both happen at once (bpo-42130). Awaiting the
    task itself always honours a cancel."""
    task = asyncio.ensure_future(awaitable)
    timed_out = False

    def time_out() -> None:
        nonlocal timed_out
        timed_out = True
        task.cancel()

    timer = asyncio.get_running_loop().call_later(timeout, time_out)
    try:
        return await task
    except asyncio.CancelledError:
        if timed_out:
            raise asyncio.TimeoutError from None
        raise
    finally:
        timer.cancel()
