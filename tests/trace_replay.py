"""Replays a trace (`trace = true`, core/trace.py) as Moonraker, so a recorded print becomes a
regression test.

A trace holds what the bridge sent and what Moonraker answered. `ReplayMoonraker` answers each
method with what Moonraker answered to it in the trace, in the order it did (the last answer
again once they run out), and `play()` pushes every notification Moonraker sent (status
updates, history changes, console lines) in the order it sent them. So the bridge sees the
recorded session again, and the test checks what reached the fake 3D Print Log:

    moonraker = ReplayMoonraker.from_file(trace_path)
    ... run the bridge against it, then: await moonraker.play()

`replay()` does all of that. The connection itself is the fake's, so identifying and the
methods that change the printer (`printer.gcode.script`) are answered by the fake. A trace that
spans reconnects is replayed as one connection: answers and notifications from all of them, in
order.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import bridge_helpers as helpers
from fake_moonraker import FakeMoonraker, _RpcError, eventually
from fake_printlog_api import FakePrintLogApi

FROM_THE_FAKE = frozenset(
    {"server.connection.identify", "connection.register_remote_method", "printer.gcode.script"}
)
"""Answered by the fake, not the trace: the connection's own bookkeeping, and side effects."""


def read_trace(path: Path) -> list[dict[str, Any]]:
    """A trace file's lines, in order."""
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class ReplayMoonraker(FakeMoonraker):
    def __init__(self, lines: Iterable[Mapping[str, Any]], **options: Any) -> None:
        super().__init__(**options)
        self.answers: dict[str, deque[Mapping[str, Any]]] = defaultdict(deque)
        """What Moonraker answered, by method, in order."""
        self.notifications: list[dict[str, Any]] = []
        """What Moonraker sent unasked, in order."""
        self._read(lines)
        if "printer.objects.list" in self.answers:
            self.objects = list(self.answers["printer.objects.list"][0]["result"]["objects"])

    @classmethod
    def from_file(cls, path: Path, **options: Any) -> ReplayMoonraker:
        return cls(read_trace(path), **options)

    def _read(self, lines: Iterable[Mapping[str, Any]]) -> None:
        asked: dict[Any, str] = {}  # request id -> method, on the current connection
        for line in lines:
            direction, message = line.get("dir"), line.get("msg")
            if direction == "open":
                asked = {}
            elif direction == "send" and isinstance(message, dict) and "method" in message:
                asked[message.get("id")] = message["method"]
            elif direction == "recv" and isinstance(message, dict):
                if "method" in message and "id" not in message:
                    self.notifications.append(message)
                elif message.get("id") in asked and ("result" in message or "error" in message):
                    self.answers[asked.pop(message["id"])].append(message)

    async def play(self, pause: float = 0.01) -> None:
        """Sends every recorded notification to the bridge, in order."""
        for message in self.notifications:
            for send in list(self._clients):
                await send(dict(message))
            await asyncio.sleep(pause)

    def _dispatch(self, method: str, params: dict, conn: dict) -> Any:
        if method in FROM_THE_FAKE or method not in self.answers or not conn["authorized"]:
            return super()._dispatch(method, params, conn)
        self.calls.append(method)
        if method == "printer.objects.subscribe":
            self.subscriptions.append(params["objects"])
        recorded = self.answers[method]
        answer = recorded.popleft() if len(recorded) > 1 else recorded[0]
        if "error" in answer:
            error = answer["error"] or {}
            raise _RpcError(error.get("code", 0), error.get("message", ""))
        return answer["result"]


async def replay(
    trace: Path,
    tmp_path: Path,
    api: FakePrintLogApi,
    until: Callable[[], bool],
    *,
    ready: Callable[[], bool] = lambda: True,
    timeout: float = 10.0,
) -> ReplayMoonraker:
    """Runs the bridge against `trace`, logging to `api`, until `until()` holds.

    The notifications are played once the bridge has subscribed and `ready()` holds, such as
    once the spools a reader saw at the start are assigned, as they were when it was recorded.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    moonraker = ReplayMoonraker.from_file(trace)
    moonraker_url, api_url = await moonraker.start(), await api.start()
    try:
        async with helpers.bridge(
            helpers.write_config(tmp_path, moonraker_url, api_url),
            connected=lambda: moonraker.client_count == 1,
            disconnected=lambda: moonraker.client_count == 0,
        ):
            await eventually(lambda: moonraker.subscriptions, timeout)
            await eventually(ready, timeout)
            await moonraker.play()
            await eventually(until, timeout)
    finally:
        await moonraker.close()
        await api.close()
    return moonraker
