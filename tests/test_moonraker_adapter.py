from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fake_moonraker import FakeMoonraker, eventually

from printlog_bridge.adapters.moonraker import MoonrakerAdapter
from printlog_bridge.core.checks import Status
from printlog_bridge.core.config import ConfigError
from printlog_bridge.core.status import NullReport


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


def test_snapshots_are_on_unless_turned_off():
    assert MoonrakerAdapter.from_config("u1", {"url": "http://u1.local"}).snapshots is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("false", False), ("no", False), ("off", False), ("0", False), ("True", True), ("yes", True)],
)
def test_snapshots_can_be_turned_off(value, expected):
    options = {"url": "http://u1.local", "snapshots": value}

    assert MoonrakerAdapter.from_config("u1", options).snapshots is expected


def test_snapshots_must_be_yes_or_no():
    with pytest.raises(ConfigError, match=r"\[printer u1\] snapshots must be true or false"):
        MoonrakerAdapter.from_config("u1", {"url": "http://u1.local", "snapshots": "maybe"})


def test_unknown_option_is_an_error_so_typos_surface():
    with pytest.raises(ConfigError, match=r"\[printer u1\].*unknown option 'ulr'"):
        MoonrakerAdapter.from_config("u1", {"ulr": "http://x"})


def check_against(fake: FakeMoonraker, options=lambda base: {"url": base}, printer="u1"):
    """Runs a remote adapter's check() against `fake`."""

    async def scenario():
        base = await fake.start()
        try:
            return await MoonrakerAdapter.from_config(printer, options(base)).check()
        finally:
            await fake.close()

    return asyncio.run(scenario())


def test_check_reports_moonraker_reachable():
    fake = FakeMoonraker()

    [result] = check_against(fake)

    assert result.status is Status.OK
    assert result.name == "Printer u1"
    assert "Moonraker v0.9.3-fake" in result.detail
    assert "Klipper ready" in result.detail


def test_check_does_not_register_doctor_as_the_agent():
    fake = FakeMoonraker()

    check_against(fake)

    assert fake.identified == []


def test_check_warns_when_klipper_is_not_ready():
    [result] = check_against(FakeMoonraker(klippy_state="shutdown"))

    assert result.status is Status.WARN
    assert "Klipper shutdown" in result.detail


def test_check_accepts_the_websocket_url():
    [result] = check_against(
        FakeMoonraker(), lambda base: {"url": base.replace("http://", "ws://") + "/websocket"}
    )

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
    """Moonraker's socket speaks JSON-RPC framed by ETX bytes, not HTTP."""

    async def scenario(sock: str):
        fake = FakeMoonraker()
        await fake.start_unix(sock)
        try:
            return await MoonrakerAdapter.from_config("voron", {"socket": sock}).check()
        finally:
            await fake.close()

    # AF_UNIX paths are capped near 100 bytes, which pytest's tmp_path can exceed.
    with tempfile.TemporaryDirectory() as d:
        [result] = asyncio.run(scenario(os.path.join(d, "m.sock")))

    assert result.status is Status.OK, result.detail
    assert "Klipper ready" in result.detail


def test_moonraker_api_key_is_read_for_remote_mode():
    adapter = MoonrakerAdapter.from_config(
        "u1", {"url": "http://u1.local", "moonraker_api_key": " abc123 "}
    )

    assert adapter.api_key == "abc123"


def test_moonraker_api_key_is_refused_for_the_trusted_local_socket():
    with pytest.raises(
        ConfigError, match=r"\[printer voron\] moonraker_api_key is only for remote"
    ):
        MoonrakerAdapter.from_config("voron", {"moonraker_api_key": "abc123"})


def test_check_sends_the_moonraker_api_key():
    [result] = check_against(
        FakeMoonraker(api_key="secret"), lambda base: {"url": base, "moonraker_api_key": "secret"}
    )

    assert result.status is Status.OK


@pytest.mark.parametrize("api_key", [None, "wrong"])
def test_check_says_what_to_do_when_moonraker_refuses_the_bridge(api_key):
    def options(base):
        return {"url": base, **({"moonraker_api_key": api_key} if api_key else {})}

    [result] = check_against(FakeMoonraker(api_key="secret"), options)

    assert result.status is Status.FAIL
    assert "rejected" in result.detail
    assert "moonraker_api_key in [printer u1]" in result.detail
    assert "trusted_clients" in result.detail


class NoJobsSink:
    async def emit(self, event):  # pragma: no cover - no job runs here
        pass

    async def resume_after(self, printer):
        return None


def test_run_stays_connected_until_cancelled():
    async def scenario():
        fake = FakeMoonraker()
        base = await fake.start()
        try:
            adapter = MoonrakerAdapter.from_config("u1", {"url": base})
            task = asyncio.ensure_future(adapter.run(sink=NoJobsSink(), report=NullReport()))
            await eventually(lambda: fake.subscriptions)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await eventually(lambda: fake.client_count == 0)
            return fake
        finally:
            await fake.close()

    fake = asyncio.run(scenario())

    assert fake.identified[0]["client_name"] == "printlog-bridge"
