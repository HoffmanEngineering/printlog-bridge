from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from printlog_bridge import __version__
from printlog_bridge.core.api_client import (
    ApiClient,
    ApiRejected,
    ApiUnavailable,
    AuthRejected,
)


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


def serve(routes, call):
    """Serves `routes` (method, path, handler), then awaits `call(client)`."""

    async def scenario():
        app = web.Application()
        for method, path, handler in routes:
            app.router.add_route(method, path, handler)
        server = TestServer(app)
        await server.start_server()
        try:
            base = str(server.make_url("")).rstrip("/")
            async with ApiClient(base, api_key="key-123") as client:
                return await call(client)
        finally:
            await server.close()

    return asyncio.run(scenario())


class TestPrints:
    def test_create_posts_the_body_and_says_whether_it_was_new(self):
        seen = []

        async def create(request):
            seen.append(await request.json())
            return web.json_response({"id": 9, "status": 2}, status=201 if len(seen) == 1 else 200)

        async def twice(client):
            return [await client.create_print({"title": "Benchy"}) for _ in range(2)]

        [(first, created), (again, created_again)] = serve([("POST", "/api/Prints", create)], twice)

        assert seen == [{"title": "Benchy"}, {"title": "Benchy"}]
        assert (first["id"], created) == (9, True)
        assert (again["id"], created_again) == (9, False)

    @pytest.mark.parametrize(
        ("method", "path", "call"),
        [
            ("GET", "/api/Prints/9", lambda c: c.get_print(9)),
            ("POST", "/api/Prints/9/complete", lambda c: c.complete_print(9, {"status": 3})),
            ("PATCH", "/api/Prints/9", lambda c: c.patch_print(9, {"notes": "n"})),
            ("GET", "/api/Printers/9", lambda c: c.get_printer(9)),
        ],
    )
    def test_a_print_or_printer_that_is_gone_is_none(self, method, path, call):
        async def gone(_request):
            return web.Response(status=404)

        assert serve([(method, path, gone)], call) is None

    def test_complete_and_patch_send_their_bodies(self):
        seen = []

        async def record(request):
            seen.append((request.method, request.path, await request.json()))
            return web.json_response({"id": 9})

        async def both(client):
            await client.complete_print(9, {"status": 3})
            await client.patch_print(9, {"notes": "Klipper shutdown"})

        serve(
            [("POST", "/api/Prints/9/complete", record), ("PATCH", "/api/Prints/9", record)], both
        )

        assert seen == [
            ("POST", "/api/Prints/9/complete", {"status": 3}),
            ("PATCH", "/api/Prints/9", {"notes": "Klipper shutdown"}),
        ]

    def test_a_refused_request_raises_api_rejected_with_the_apis_reason(self):
        async def bad(_request):
            return web.Response(status=400, text="Selected printer does not belong to you.")

        with pytest.raises(ApiRejected, match="does not belong"):
            serve([("POST", "/api/Prints", bad)], lambda c: c.create_print({}))

    def test_a_print_the_key_may_not_edit_raises_api_rejected(self):
        async def forbidden(_request):
            return web.Response(status=403)

        with pytest.raises(ApiRejected, match="403"):
            serve(
                [("POST", "/api/Prints/9/complete", forbidden)], lambda c: c.complete_print(9, {})
            )

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_throttling_and_server_errors_are_worth_retrying(self, status):
        async def busy(_request):
            return web.Response(status=status)

        with pytest.raises(ApiUnavailable, match=str(status)):
            serve([("POST", "/api/Prints", busy)], lambda c: c.create_print({}))

    def test_a_revoked_key_raises_auth_rejected_from_any_call(self):
        async def revoked(_request):
            return web.Response(status=401)

        with pytest.raises(AuthRejected):
            serve([("POST", "/api/Prints", revoked)], lambda c: c.create_print({}))

    def test_user_settings_are_listed(self):
        async def settings(_request):
            return web.json_response([{"userSettingTypeId": 1, "value": "Public"}])

        listed = serve(
            [("GET", "/api/Users/me/user-settings", settings)], lambda c: c.user_settings()
        )

        assert listed == [{"userSettingTypeId": 1, "value": "Public"}]
