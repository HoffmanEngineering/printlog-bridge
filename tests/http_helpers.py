"""A bare HTTP server for the adapters' HTTP clients: one that records every request it gets,
to show a client's request (and its credentials) never reached it, or that answers with a given
body, whole or in pieces."""

from __future__ import annotations

import asyncio
import contextlib
import zlib
from collections.abc import AsyncIterator, Iterable, Mapping

from aiohttp import web


class Server:
    def __init__(self) -> None:
        self.url = ""
        self.requests: list[tuple[str, dict[str, str]]] = []
        """Each request's path and headers."""
        self.body = b"{}"
        self.headers: Mapping[str, str] = {"Content-Type": "application/json"}
        self.pieces: Iterable[bytes] | None = None
        """When set, the body is sent as these, a moment apart, instead of `body`."""


@contextlib.asynccontextmanager
async def http_server() -> AsyncIterator[Server]:
    server = Server()

    async def handler(request: web.Request) -> web.StreamResponse:
        server.requests.append((request.path, dict(request.headers)))
        if server.pieces is None:
            return web.Response(body=server.body, headers=dict(server.headers))
        response = web.StreamResponse(headers=dict(server.headers))
        await response.prepare(request)
        for piece in server.pieces:
            await response.write(piece)
            await asyncio.sleep(0.005)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    server.url = f"http://127.0.0.1:{port}"
    try:
        yield server
    finally:
        await runner.cleanup()


def host_of(server: Server) -> str:
    """`127.0.0.1:<port>`, for a path that tries to reach the server from another's URL."""
    return server.url[len("http://") :]


def gzip_bomb(server: Server, inflated: int = 32 * 1024 * 1024) -> None:
    """Has `server` answer with a JSON body of `inflated` bytes, gzipped to about a thousandth
    of that: built a MiB at a time, so the test never holds it whole."""
    packer = zlib.compressobj(9, zlib.DEFLATED, 31)
    mib = b"x" * (1024 * 1024)
    packed = packer.compress(b'{"a":"')
    for _ in range(inflated // len(mib)):
        packed += packer.compress(mib)
    server.body = packed + packer.compress(b'"}') + packer.flush()
    server.headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
