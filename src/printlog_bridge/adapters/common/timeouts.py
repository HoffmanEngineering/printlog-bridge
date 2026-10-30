"""Waiting with a deadline, in code that must also stop when cancelled."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def bounded(awaitable: Awaitable[T], timeout: float) -> T:
    """`asyncio.wait_for` for code that must stop when cancelled. Before Python 3.12, wait_for
    returns the result and drops the cancel when both happen at once (bpo-42130).

    The caller's own cancel is told apart from the deadline by where it lands: `asyncio.wait`
    raises CancelledError only when the caller is cancelled (it never cancels what it waits on,
    and never drops a cancel), while the deadline cancels the task, which `wait` then returns
    done. So a cancel and the deadline in the same loop iteration stay a cancel, and the caller
    stops. Either way the task is cancelled and has finished before this returns or raises."""
    task = asyncio.ensure_future(awaitable)
    timed_out = False

    def time_out() -> None:
        nonlocal timed_out
        timed_out = True
        task.cancel()

    timer = asyncio.get_running_loop().call_later(timeout, time_out)
    try:
        await asyncio.wait({task})
    except asyncio.CancelledError:
        task.cancel()
        while not task.done():  # let it clean up, as awaiting it would
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()  # retrieved: the caller's cancel is what counts
        raise
    finally:
        timer.cancel()
    if task.cancelled():
        if timed_out:
            raise asyncio.TimeoutError
        raise asyncio.CancelledError
    return task.result()
