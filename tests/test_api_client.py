from __future__ import annotations

import asyncio
from datetime import datetime, timezone

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

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("30", 30.0),
            ("0", 0.0),
            ("Sat, 10 Oct 2026 09:31:00 GMT", 60.0),  # an HTTP date, against the API's Date
            ("soon", None),
            (None, None),
        ],
    )
    def test_throttling_says_when_to_try_again(self, header, expected):
        async def throttled(_request):
            headers = {"Date": "Sat, 10 Oct 2026 09:30:00 GMT"}
            if header is not None:
                headers["Retry-After"] = header
            return web.Response(status=429, headers=headers)

        with pytest.raises(ApiUnavailable) as raised:
            serve([("POST", "/api/Prints", throttled)], lambda c: c.create_print({}))

        assert raised.value.retry_after == expected

    def test_an_unreachable_api_has_no_retry_after(self):
        async def scenario():
            async with ApiClient("http://127.0.0.1:9", api_key="k") as client:
                await client.create_print({})

        with pytest.raises(ApiUnavailable) as raised:
            asyncio.run(scenario())

        assert raised.value.retry_after is None

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


class TestConnections:
    def test_put_connection_registers_under_the_instance_id(self):
        seen = []

        async def put(request):
            seen.append((request.match_info["instance"], await request.json()))
            return web.json_response({"instanceId": request.match_info["instance"]}, status=201)

        body = {"kind": "moonraker", "displayName": "printlog-bridge (voron)", "printerId": 42}
        answer = serve(
            [("PUT", "/api/Connections/{instance}", put)],
            lambda c: c.put_connection("abc-123", body),
        )

        assert seen == [("abc-123", body)]
        assert answer == {"instanceId": "abc-123"}


class TestServerClock:
    def test_remembers_the_apis_clock_from_its_last_answer(self):
        async def me(_request):
            return web.json_response({"id": 7}, headers={"Date": "Sat, 10 Oct 2026 09:30:00 GMT"})

        async def ask(client):
            assert client.server_time is None
            await client.current_user()
            return client.server_time

        assert serve([("GET", "/api/Users/me", me)], ask) == datetime(
            2026, 10, 10, 9, 30, tzinfo=timezone.utc
        )

    def test_an_answer_without_a_date_leaves_it_unknown(self):
        async def me(_request):
            response = web.json_response({"id": 7})
            response.headers["Date"] = "not a date"
            return response

        async def ask(client):
            await client.current_user()
            return client.server_time

        assert serve([("GET", "/api/Users/me", me)], ask) is None


class TestImages:
    def test_upload_posts_the_image_as_a_form_and_says_whether_it_is_the_default(self):
        seen = {}

        async def upload(request):
            form = await request.post()
            image = form["image"]
            seen.update(
                data=image.file.read(),
                content_type=image.content_type,
                filename=image.filename,
                is_default=form["isDefault"],
                api_key=request.headers.get("X-Api-Key"),
            )
            return web.json_response({"id": 3, "isDefault": True, "displayOrder": 0}, status=201)

        result = serve(
            [("POST", "/api/Prints/9/image", upload)],
            lambda c: c.upload_print_image(9, b"\x89PNG-data", "image/png", "thumbnail.png", True),
        )

        assert result == {"id": 3, "isDefault": True, "displayOrder": 0}
        assert seen == {
            "data": b"\x89PNG-data",
            "content_type": "image/png",
            "filename": "thumbnail.png",
            "is_default": "true",
            "api_key": "key-123",
        }

    def test_an_upload_to_a_print_that_is_gone_is_none(self):
        async def gone(_request):
            return web.Response(status=404)

        result = serve(
            [("POST", "/api/Prints/9/image", gone)],
            lambda c: c.upload_print_image(9, b"x", "image/png", "t.png", False),
        )

        assert result is None

    @pytest.mark.parametrize("status", [413, 415])
    def test_an_image_too_large_or_of_the_wrong_kind_is_rejected(self, status):
        async def refuse(_request):
            return web.Response(status=status)

        with pytest.raises(ApiRejected, match=str(status)):
            serve(
                [("POST", "/api/Prints/9/image", refuse)],
                lambda c: c.upload_print_image(9, b"x", "image/png", "t.png", False),
            )

    def test_a_server_error_says_the_api_answered(self):
        async def fail(_request):
            return web.Response(status=500)

        with pytest.raises(ApiUnavailable) as e:
            serve(
                [("POST", "/api/Prints/9/image", fail)],
                lambda c: c.upload_print_image(9, b"x", "image/png", "t.png", False),
            )

        assert e.value.status == 500

    def test_an_unreachable_api_has_no_status(self):
        async def scenario():
            async with ApiClient("http://127.0.0.1:9", api_key="k") as client:
                await client.upload_print_image(9, b"x", "image/png", "t.png", False)

        with pytest.raises(ApiUnavailable) as e:
            asyncio.run(scenario())

        assert e.value.status is None
