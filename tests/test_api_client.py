from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from printlog_bridge import __version__
from printlog_bridge.core.api_client import ApiClient, ApiUnavailable, AuthRejected


def with_api(handler, call):
    """Serves GET /api/Users/me with `handler`, then awaits `call(client)`."""

    async def scenario():
        app = web.Application()
        app.router.add_get("/api/Users/me", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            base = str(server.make_url("")).rstrip("/")
            async with ApiClient(base, api_key="key-123") as client:
                return await call(client)
        finally:
            await server.close()

    return asyncio.run(scenario())


def test_current_user_sends_the_api_key_and_identifies_the_bridge():
    seen = {}

    async def me(request):
        seen.update(request.headers)
        return web.json_response({"id": 42, "displayName": "Chris", "bio": None})

    user = with_api(me, lambda client: client.current_user())

    assert user.id == 42
    assert user.display_name == "Chris"
    assert seen["X-Api-Key"] == "key-123"
    assert seen["User-Agent"] == f"printlog-bridge/{__version__}"
    assert seen["Accept"] == "application/json"


def test_a_rejected_key_raises_auth_rejected():
    async def me(_request):
        return web.Response(status=401)

    with pytest.raises(AuthRejected):
        with_api(me, lambda client: client.current_user())


def test_a_server_error_raises_api_unavailable():
    async def me(_request):
        return web.Response(status=503)

    with pytest.raises(ApiUnavailable, match="503"):
        with_api(me, lambda client: client.current_user())


def test_an_unreachable_api_raises_api_unavailable():
    async def scenario():
        async with ApiClient("http://127.0.0.1:9", api_key="k") as client:
            await client.current_user()

    with pytest.raises(ApiUnavailable):
        asyncio.run(scenario())


def test_the_key_never_appears_in_the_client_repr():
    client = ApiClient("https://api.example.test", api_key="secret-key")

    assert "secret-key" not in repr(client)
