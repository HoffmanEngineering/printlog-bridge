"""A webcam's snapshot endpoint, like crowsnest's ustreamer (`/?action=snapshot`) answers it."""

from __future__ import annotations

import asyncio
import base64

from aiohttp import web
from aiohttp.test_utils import TestServer

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class FakeWebcam:
    def __init__(
        self,
        *,
        body: bytes = JPEG,
        content_type: str = "image/jpeg",
        delay: float = 0.0,
        status: int = 200,
        credentials: tuple[str, str] | None = None,
    ) -> None:
        self.body = body
        self.content_type = content_type
        self.delay = delay
        """Seconds before answering, as a camera that hangs would."""
        self.status = status
        self.credentials = credentials
        """Basic auth the camera requires, as (user, password)."""
        self.requests: list[str] = []
        self._server: TestServer | None = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/{tail:.*}", self._snapshot)
        self._server = TestServer(app)
        await self._server.start_server()
        return str(self._server.make_url("")).rstrip("/")

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()

    async def _snapshot(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(str(request.rel_url))
        if self.credentials is not None:
            expected = "Basic " + base64.b64encode(":".join(self.credentials).encode()).decode()
            if request.headers.get("Authorization") != expected:
                return web.Response(status=401)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status != 200:
            return web.Response(status=self.status)
        return web.Response(body=self.body, content_type=self.content_type)
