"""The fake API and the fake Auth0 on a loop of their own, for tests of commands that run
`asyncio.run` themselves (`printlog-bridge pair`, `run`)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from fake_auth0 import FakeAuth0
from fake_printlog_api import FakePrintLogApi


class ThreadedFakes:
    def __init__(self, *, printers: tuple[int, ...] = (42,), pairing_on: bool = True) -> None:
        # Polls at once: the device flow's interval is real time here.
        self.auth0 = FakeAuth0(interval=0.01)
        self.api = FakePrintLogApi(printers=printers)
        self.pairing_on = pairing_on
        self.api_url = ""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)

    def __enter__(self) -> ThreadedFakes:
        self._thread.start()
        self._run(self._start())
        return self

    def __exit__(self, *exc: object) -> None:
        self._run(self.api.close())
        self._run(self.auth0.close())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop.close()

    async def _start(self) -> None:
        await self.auth0.start()
        self.api_url = await self.api.start()
        self.api.auth0 = self.auth0
        self.api.pairing = self.auth0.discovery() if self.pairing_on else None

    def _run(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(10)

    def config(self, folder: Path, printers: str) -> Path:
        path = folder / "printlog-bridge.cfg"
        path.write_text(
            f"[bridge]\napi_url = {self.api_url}\nstate_dir = {folder / 'state'}\n\n{printers}",
            encoding="utf-8",
        )
        return path
