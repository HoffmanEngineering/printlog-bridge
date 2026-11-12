"""A bare HTTP server for the adapters' HTTP clients: one that records every request it gets,
to show a client's request (and its credentials) never reached it, or that answers with a given
body."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping

from aiohttp import web


class Server:
    def __init__(self) -> None:
        self.url = ""
        self.requests: list[tuple[str, dict[str, str]]] = []
        """Each request's path and headers."""
        self.body = b"{}"
        self.headers: Mapping[str, str] = {"Content-Type": "application/json"}


@contextlib.asynccontextmanager
async def http_server() -> AsyncIterator[Server]:
    server = Server()

    async def handler(request: web.Request) -> web.Response:
        server.requests.append((request.path, dict(request.headers)))
        return web.Response(body=server.body, headers=dict(server.headers))

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
