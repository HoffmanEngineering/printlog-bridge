"""The unit tests' FakeMoonraker must answer like the real one, or they test a fiction.

Each test runs the same raw JSON-RPC exchange against both and expects the same answer. When a
Moonraker upgrade changes one of these, this fails here, not on someone's printer.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from typing import Any

import aiohttp
import pytest
from fake_moonraker import FakeMoonraker

pytestmark = pytest.mark.integration

AGENT = {"client_name": "printlog-bridge-contract", "version": "0", "type": "agent", "url": "x"}


class Target:
    def __init__(self, url: str, api_key: str) -> None:
        self.url = url
        self.api_key = api_key


@pytest.fixture(params=["fake", "real"])
def kind(request):
    return request.param


@contextlib.asynccontextmanager
async def target(kind: str, real):
    if kind == "real":
        yield Target(real.url, real.api_key)
        return
    fake = FakeMoonraker(api_key="fake-key", klippy_state="disconnected")
    url = await fake.start()
    try:
        yield Target(url, fake.api_key)
    finally:
        await fake.close()


class Rpc:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self.ids = itertools.count(1)

    async def call(self, method: str, params: dict | None = None) -> dict[str, Any]:
        """The response, as {"result": ...} or {"error": {...}}. Skips notifications."""
        request_id = next(self.ids)
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        await self.ws.send_str(json.dumps(message))
        while True:
            reply = json.loads((await asyncio.wait_for(self.ws.receive(), 10)).data)
            if reply.get("id") == request_id:
                return reply


@contextlib.asynccontextmanager
async def connect(url: str, headers: dict[str, str] | None = None):
    # Not one parenthesized `async with`: Python 3.9 can't parse that.
    http = aiohttp.ClientSession()
    async with http, http.ws_connect(f"{url}/websocket", headers=headers or {}) as ws:
        yield Rpc(ws)


def exchange(kind, real, steps):
    async def scenario():
        async with target(kind, real) as t:
            return await steps(t)

    return asyncio.run(scenario())


def test_an_untrusted_client_is_unauthorized(kind, moonraker):
    async def steps(t):
        async with connect(t.url) as rpc:
            return await rpc.call("server.info")

    reply = exchange(kind, moonraker, steps)

    assert reply["error"]["code"] == -32602
    assert reply["error"]["message"] == "Unauthorized"


def test_a_wrong_key_in_identify_is_an_invalid_api_key(kind, moonraker):
    async def steps(t):
        async with connect(t.url) as rpc:
            return await rpc.call("server.connection.identify", {**AGENT, "api_key": "wrong"})

    reply = exchange(kind, moonraker, steps)

    assert reply["error"]["code"] == -32602
    assert reply["error"]["message"] == "Invalid API Key"


def test_the_key_as_an_upgrade_header_authorizes_the_connection(kind, moonraker):
    async def steps(t):
        async with connect(t.url, {"X-Api-Key": t.api_key}) as rpc:
            return await rpc.call("server.info")

    reply = exchange(kind, moonraker, steps)

    assert reply["result"]["klippy_state"] == "disconnected"


def test_the_key_in_identify_authorizes_the_connection(kind, moonraker):
    async def steps(t):
        async with connect(t.url) as rpc:
            identified = await rpc.call(
                "server.connection.identify", {**AGENT, "api_key": t.api_key}
            )
            info = await rpc.call("server.info")
            return identified, info

    identified, info = exchange(kind, moonraker, steps)

    assert "connection_id" in identified["result"]
    assert "klippy_state" in info["result"]


def test_only_one_agent_per_name(kind, moonraker):
    async def steps(t):
        headers = {"X-Api-Key": t.api_key}
        async with connect(t.url, headers) as first, connect(t.url, headers) as second:
            await first.call("server.connection.identify", AGENT)
            return await second.call("server.connection.identify", AGENT)

    reply = exchange(kind, moonraker, steps)

    assert reply["error"]["code"] == 400
    assert "already registered" in reply["error"]["message"]
