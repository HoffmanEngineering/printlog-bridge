"""Helpers for tests that run a MoonrakerSession, against the fake or a real Moonraker."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from typing import Any

from printlog_bridge.adapters.moonraker.session import MoonrakerSession


class Recorder:
    """A listener that keeps everything it's told."""

    def __init__(self) -> None:
        self.klippy: list[str] = []
        self.status: dict[str, dict[str, Any]] = {}
        self.changes: list[Mapping[str, Any]] = []
        self.history: list[tuple[str, Mapping[str, Any]]] = []

    async def on_klippy_state(self, state: str) -> None:
        self.klippy.append(state)

    async def on_status(self, status: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        self.status = {k: dict(v) for k, v in status.items()}
        self.changes.append(changed)

    async def on_history(self, action: str, job: Mapping[str, Any]) -> None:
        self.history.append((action, job))


@contextlib.asynccontextmanager
async def running(session: MoonrakerSession):
    task = asyncio.ensure_future(session.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
