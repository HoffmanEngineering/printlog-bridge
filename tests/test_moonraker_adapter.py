from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from printlog_bridge.adapters.moonraker import MoonrakerAdapter
from printlog_bridge.core.checks import Status
from printlog_bridge.core.config import ConfigError


def test_url_selects_remote_mode():
    adapter = MoonrakerAdapter.from_config("u1", {"url": "http://u1.local:7125/"})

    assert adapter.url == "http://u1.local:7125"
    assert adapter.socket is None
    assert adapter.describe() == "moonraker, remote http://u1.local:7125"


def test_socket_selects_local_mode():
    adapter = MoonrakerAdapter.from_config("voron", {"socket": "/tmp/moonraker.sock"})

    assert adapter.socket == Path("/tmp/moonraker.sock")
    assert adapter.url is None
    assert adapter.describe() == f"moonraker, local socket {Path('/tmp/moonraker.sock')}"


def test_defaults_to_the_standard_local_socket():
    adapter = MoonrakerAdapter.from_config("voron", {})

    assert adapter.socket == Path("~/printer_data/comms/moonraker.sock").expanduser()


def test_url_and_socket_together_is_an_error():
    with pytest.raises(ConfigError, match=r"\[printer voron\].*either url or socket"):
        MoonrakerAdapter.from_config("voron", {"url": "http://x", "socket": "/tmp/s"})


@pytest.mark.parametrize("url", ["ftp://u1.local", "u1.local:7125", ""])
def test_url_must_be_http_or_websocket(url):
    with pytest.raises(ConfigError, match=r"\[printer u1\] url"):
        MoonrakerAdapter.from_config("u1", {"url": url})


def test_websocket_url_is_accepted_and_kept():
    adapter = MoonrakerAdapter.from_config("u1", {"url": "ws://u1.local:7125/websocket"})

    assert adapter.url == "ws://u1.local:7125/websocket"


def test_unknown_option_is_an_error_so_typos_surface():
    with pytest.raises(ConfigError, match=r"\[printer u1\].*unknown option 'ulr'"):
        MoonrakerAdapter.from_config("u1", {"ulr": "http://x"})


async def _serve_server_info(handler):
    app = web.Application()
    app.router.add_get("/server/info", handler)
    server = TestServer(app)
    await server.start_server()
    return server


def test_check_reports_moonraker_reachable():
    async def scenario():
        async def info(_request):
            return web.json_response(
                {"result": {"klippy_state": "ready", "moonraker_version": "v0.9.3"}}
            )

        server = await _serve_server_info(info)
        try:
            adapter = MoonrakerAdapter.from_config(
                "u1", {"url": str(server.make_url("/")).rstrip("/")}
            )
            return await adapter.check()
        finally:
            await server.close()

    [result] = asyncio.run(scenario())

    assert result.status is Status.OK
    assert result.name == "Printer u1"
    assert "Moonraker v0.9.3" in result.detail
    assert "Klipper ready" in result.detail


def test_check_warns_when_klipper_is_not_ready():
    async def scenario():
        async def info(_request):
            return web.json_response({"result": {"klippy_state": "shutdown"}})

        server = await _serve_server_info(info)
        try:
            adapter = MoonrakerAdapter.from_config("u1", {"url": str(server.make_url("/"))})
            return await adapter.check()
        finally:
            await server.close()

    [result] = asyncio.run(scenario())

    assert result.status is Status.WARN
    assert "Klipper shutdown" in result.detail


def test_check_uses_the_websocket_urls_host():
    async def scenario():
        async def info(_request):
            return web.json_response({"result": {"klippy_state": "ready"}})

        server = await _serve_server_info(info)
        try:
            ws_url = str(server.make_url("/websocket")).replace("http://", "ws://")
            adapter = MoonrakerAdapter.from_config("u1", {"url": ws_url})
            return await adapter.check()
        finally:
            await server.close()

    [result] = asyncio.run(scenario())

    assert result.status is Status.OK


def test_check_fails_when_moonraker_is_unreachable():
    # Port 9 (discard) is closed on any sane test host.
    adapter = MoonrakerAdapter.from_config("u1", {"url": "http://127.0.0.1:9"})

    [result] = asyncio.run(adapter.check())

    assert result.status is Status.FAIL
    assert "can't reach Moonraker at http://127.0.0.1:9" in result.detail


def test_check_fails_when_the_socket_is_missing(tmp_path):
    adapter = MoonrakerAdapter.from_config("voron", {"socket": str(tmp_path / "none.sock")})

    [result] = asyncio.run(adapter.check())

    assert result.status is Status.FAIL
    assert "no Moonraker socket at" in result.detail


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")
def test_check_reaches_moonraker_over_its_unix_socket():
    async def scenario(sock: str):
        async def info(_request):
            return web.json_response({"result": {"klippy_state": "ready"}})

        app = web.Application()
        app.router.add_get("/server/info", info)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.UnixSite(runner, sock)
        await site.start()
        try:
            adapter = MoonrakerAdapter.from_config("voron", {"socket": sock})
            return await adapter.check()
        finally:
            await runner.cleanup()

    # AF_UNIX paths are capped near 100 bytes, which pytest's tmp_path can exceed.
    with tempfile.TemporaryDirectory() as d:
        [result] = asyncio.run(scenario(os.path.join(d, "m.sock")))

    assert result.status is Status.OK


def test_run_is_not_implemented_yet():
    adapter = MoonrakerAdapter.from_config("u1", {"url": "http://u1.local"})

    with pytest.raises(NotImplementedError, match="#3"):
        asyncio.run(adapter.run(sink=None))
