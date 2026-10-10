"""The bridge against a real Moonraker (no Klipper), in Docker. `pytest -m integration`."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import pytest
from session_helpers import Recorder, running

from printlog_bridge import __version__
from printlog_bridge.adapters.moonraker import MoonrakerAdapter
from printlog_bridge.adapters.moonraker.session import Backoff, MoonrakerSession
from printlog_bridge.core.checks import Status

pytestmark = pytest.mark.integration


async def until(predicate: Callable[[], object], timeout: float = 30) -> None:
    """Polls `predicate` (which may shell out to docker) off the event loop."""
    deadline = time.monotonic() + timeout
    while not await asyncio.to_thread(predicate):
        if time.monotonic() > deadline:
            raise AssertionError("condition not met within the timeout")
        await asyncio.sleep(0.2)


def remote(moonraker, **options) -> MoonrakerAdapter:
    return MoonrakerAdapter.from_config("it", {"url": moonraker.url, **options})


def session(moonraker, recorder: Recorder, **kwargs) -> MoonrakerSession:
    kwargs.setdefault("backoff", Backoff(initial=0.1, maximum=1))
    return MoonrakerSession("it", url=moonraker.url, listener=recorder, **kwargs)


class TestDoctor:
    def test_without_a_key_says_what_to_do(self, moonraker):
        [result] = asyncio.run(remote(moonraker).check())

        assert result.status is Status.FAIL
        assert "rejected the bridge (Unauthorized)" in result.detail
        assert "moonraker_api_key in [printer it]" in result.detail

    def test_with_a_wrong_key_says_what_to_do(self, moonraker):
        [result] = asyncio.run(remote(moonraker, moonraker_api_key="not-the-key").check())

        assert result.status is Status.FAIL
        assert "rejected the bridge" in result.detail

    def test_with_the_key_reaches_moonraker(self, moonraker):
        [result] = asyncio.run(remote(moonraker, moonraker_api_key=moonraker.api_key).check())

        # No Klipper in the container, so Moonraker is reachable and Klipper is not.
        assert result.status is Status.WARN, result.detail
        assert "Klipper disconnected" in result.detail

    def test_does_not_take_the_agent_slot(self, moonraker):
        asyncio.run(remote(moonraker, moonraker_api_key=moonraker.api_key).check())

        assert moonraker.agents() == []


class TestRemoteSession:
    def test_appears_in_moonrakers_extensions_list(self, moonraker):
        async def scenario():
            recorder = Recorder()
            async with running(session(moonraker, recorder, api_key=moonraker.api_key)):
                await until(moonraker.agents)
                await until(lambda: recorder.klippy)
                return moonraker.agents(), recorder

        agents, recorder = asyncio.run(scenario())

        assert agents == [
            {
                "name": "printlog-bridge",
                "version": __version__,
                "type": "agent",
                "url": "https://github.com/HoffmanEngineering/printlog-bridge",
            }
        ]
        assert recorder.klippy == ["disconnected"]

    def test_leaves_the_extensions_list_when_stopped(self, moonraker):
        async def scenario():
            async with running(session(moonraker, Recorder(), api_key=moonraker.api_key)):
                await until(moonraker.agents)
            await until(lambda: moonraker.agents() == [])

        asyncio.run(scenario())

    def test_without_a_key_keeps_retrying_with_a_readable_error(self, moonraker, caplog):
        async def scenario():
            async with running(session(moonraker, Recorder())) as task:
                await until(lambda: sum("rejected" in r.getMessage() for r in caplog.records) >= 2)
                assert not task.done()

        with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
            asyncio.run(scenario())

        assert moonraker.agents() == []
        assert any("moonraker_api_key" in r.getMessage() for r in caplog.records)

    def test_a_second_bridge_waits_and_then_takes_over(self, moonraker, caplog):
        async def scenario():
            key = moonraker.api_key
            first = Recorder()
            async with running(session(moonraker, first, api_key=key)) as first_task:
                await until(lambda: first.klippy)
                second = Recorder()
                async with running(session(moonraker, second, api_key=key)):
                    await until(lambda: any("already" in r.getMessage() for r in caplog.records))
                    assert second.klippy == []
                    first_task.cancel()
                    await until(lambda: second.klippy)
                    return moonraker.agents()

        with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
            agents = asyncio.run(scenario())

        assert [a["name"] for a in agents] == ["printlog-bridge"]
        assert any("another printlog-bridge" in r.getMessage() for r in caplog.records)

    def test_reconnects_within_a_minute_after_moonraker_restarts(self, own_moonraker):
        """Acceptance for #3, with the real default backoff."""

        async def scenario():
            recorder = Recorder()
            live = MoonrakerSession(
                "it", url=own_moonraker.url, listener=recorder, api_key=own_moonraker.api_key
            )
            async with running(live):
                await until(lambda: recorder.klippy)
                connected_before = len(recorder.klippy)

                restarted_at = time.monotonic()
                await asyncio.to_thread(own_moonraker.restart)
                await until(lambda: len(recorder.klippy) > connected_before, timeout=60)
                elapsed = time.monotonic() - restarted_at
                await until(own_moonraker.agents)
                return elapsed

        elapsed = asyncio.run(scenario())

        assert elapsed < 60


@pytest.fixture(scope="module")
def installed(shared_moonraker):
    """The bridge installed inside Moonraker's container, to use its Unix socket."""
    shared_moonraker.exec(
        "sh",
        "-c",
        "rm -rf /tmp/bridge && mkdir /tmp/bridge"
        " && tar -C /src --exclude=.venv --exclude=.git -cf - . | tar -C /tmp/bridge -xf -"
        " && /bridge-venv/bin/pip install -q --no-deps /tmp/bridge"
        # run needs a key and a printer_id; nothing prints here, so the API is never called.
        " && printf '[bridge]\\napi_key = k\\nstate_dir = /tmp/state\\n"
        "[printer local]\\nprinter_id = 1\\n'"
        "    > /root/printer_data/config/printlog-bridge.cfg",
    )
    return shared_moonraker


class TestLocalMode:
    """The bridge on Moonraker's own host, over the Unix socket."""

    def test_doctor_reaches_moonraker_over_the_socket(self, installed, moonraker):
        out = installed.exec(
            "sh", "-c", "cd /root && /bridge-venv/bin/printlog-bridge doctor", check=False
        )

        [line] = [ln for ln in out.splitlines() if "Printer local" in ln]
        assert line.split()[0] == "warn", out  # reached; Klipper just isn't there
        assert "Klipper disconnected" in line

    def test_run_appears_in_the_extensions_list(self, installed, moonraker):
        installed.exec(
            "sh",
            "-c",
            "cd /root && nohup /bridge-venv/bin/printlog-bridge run > /tmp/run.log 2>&1 &",
        )
        try:
            deadline = time.monotonic() + 30
            while not installed.agents():
                assert time.monotonic() < deadline, installed.exec("cat", "/tmp/run.log")
                time.sleep(0.3)
            assert installed.agents()[0]["name"] == "printlog-bridge"
        finally:
            installed.exec("pkill", "-f", "bridge-venv/bin/printlog-bridge", check=False)
