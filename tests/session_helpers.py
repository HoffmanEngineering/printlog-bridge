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
        self.console: list[str] = []
        self.connected: list[Any] = []
        """For each connection, what `server.history.list` answered through the given call."""

    async def on_connected(self, call) -> None:
        self.connected.append(await call("server.history.list", {"limit": 1}))

    async def on_klippy_state(self, state: str) -> None:
        self.klippy.append(state)

    async def on_status(self, status: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        self.status = {k: dict(v) for k, v in status.items()}
        self.changes.append(changed)

    async def on_history(self, action: str, job: Mapping[str, Any]) -> None:
        self.history.append((action, job))

    async def on_gcode_response(self, line: str) -> None:
        self.console.append(line)


class ReportRecorder:
    """A PrinterReport that keeps everything it's told, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.warnings: dict[str, str] = {}

    def connected(self) -> None:
        self.calls.append(("connected",))

    def disconnected(self, reason: str) -> None:
        self.calls.append(("disconnected", reason))

    def printer_state(self, state: str) -> None:
        self.calls.append(("printer_state", state))

    def firmware(self, name: str, version: str) -> None:
        self.calls.append(("firmware", name, version))

    def host_software(self, name: str, version: str) -> None:
        self.calls.append(("host_software", name, version))

    def printer_model(self, model: str) -> None:
        self.calls.append(("printer_model", model))

    def warn(self, topic: str, message: str | None) -> None:
        if message is None:
            self.warnings.pop(topic, None)
        else:
            self.warnings[topic] = message

    def named(self, name: str) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == name]


@contextlib.asynccontextmanager
async def running(session: MoonrakerSession):
    task = asyncio.ensure_future(session.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
