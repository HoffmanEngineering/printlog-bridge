"""Runs the whole bridge in the test's event loop, as `printlog-bridge run` would."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Union

from fake_moonraker import eventually

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.core.bridge import run_bridge
from printlog_bridge.core.config import load_config

Condition = Callable[[], Union[bool, Awaitable[bool]]]


def write_config(tmp_path: Path, moonraker_url: str, api_url: str, printer_id: int = 42) -> Path:
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(
        f"[bridge]\napi_url = {api_url}\napi_key = key-123\nstate_dir = {tmp_path / 'state'}\n"
        f"[printer voron]\nprinter_id = {printer_id}\nurl = {moonraker_url}\n",
        encoding="utf-8",
    )
    return path


async def until(condition: Condition, timeout: float = 5.0) -> None:
    """`eventually`, for a condition that may be a coroutine (one that asks a real server)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = condition()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        if loop.time() > deadline:
            raise AssertionError("condition not met within the timeout")
        await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def bridge(
    config_path: Path,
    connected: Condition,
    disconnected: Condition,
    timeout: float = 5.0,
    **options,
):
    """The bridge, running until the block ends. Waits for it to connect, and to let go.

    `options` go to `run_bridge`."""
    config = load_config(config_path, adapters=ADAPTERS, env={}, home=config_path.parent)
    task = asyncio.ensure_future(run_bridge(config, **options))
    try:
        await until(connected, timeout)
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await until(disconnected, timeout)


__all__ = ["bridge", "eventually", "until", "write_config"]
