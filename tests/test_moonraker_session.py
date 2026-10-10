from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fake_moonraker import FakeMoonraker, eventually

from printlog_bridge import __version__
from printlog_bridge.adapters.moonraker.session import (
    Backoff,
    MoonrakerSession,
    websocket_url,
)

FAST = Backoff(initial=0.01, maximum=0.05)


class Recorder:
    """A listener that keeps everything it's told."""

    def __init__(self) -> None:
        self.klippy: list[str] = []
        self.status: dict[str, dict[str, Any]] = {}
        self.changes: list[Mapping[str, Any]] = []
        self.history: list[tuple[str, Mapping[str, Any]]] = []

    async def on_klippy_state(self, state: str) -> None:
        self.klippy.append(state)

    async def on_status(self, status: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        self.status = {k: dict(v) for k, v in status.items()}
        self.changes.append(changed)

    async def on_history(self, action: str, job: Mapping[str, Any]) -> None:
        self.history.append((action, job))


@contextlib.asynccontextmanager
async def running(session: MoonrakerSession):
    task = asyncio.ensure_future(session.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@contextlib.asynccontextmanager
async def fake_moonraker(**kwargs):
    fake = FakeMoonraker(**kwargs)
    base = await fake.start()
    try:
        yield fake, base
    finally:
        await fake.close()


def session_for(base: str, listener: Recorder, **kwargs) -> MoonrakerSession:
    kwargs.setdefault("backoff", FAST)
    return MoonrakerSession("u1", url=base, listener=listener, **kwargs)


def test_identifies_as_an_agent_so_moonraker_lists_the_bridge():
    async def scenario():
        async with fake_moonraker() as (fake, base):
            async with running(session_for(base, Recorder())):
                await eventually(lambda: fake.identified)
            return fake.identified[0]

    identity = asyncio.run(scenario())

    assert identity["client_name"] == "printlog-bridge"
    assert identity["type"] == "agent"
    assert identity["version"] == __version__
    assert identity["url"] == "https://github.com/HoffmanEngineering/printlog-bridge"


def test_subscribes_to_the_objects_a_job_needs():
    async def scenario():
        async with fake_moonraker() as (fake, base):
            async with running(session_for(base, Recorder())):
                await eventually(lambda: fake.subscriptions)
            return fake.subscriptions[0]

    objects = asyncio.run(scenario())

    assert objects == {
        "print_stats": None,
        "toolhead": ["extruder"],
        "virtual_sdcard": None,
        "webhooks": None,
    }


def test_subscribes_to_filament_detect_when_the_printer_has_it():
    async def scenario():
        fake_objects = ("webhooks", "print_stats", "toolhead", "virtual_sdcard", "filament_detect")
        async with fake_moonraker(objects=fake_objects) as (fake, base):
            async with running(session_for(base, Recorder())):
                await eventually(lambda: fake.subscriptions)
            return fake.subscriptions[0]

    objects = asyncio.run(scenario())

    assert objects["filament_detect"] is None


def test_reports_the_initial_status_then_merges_each_update():
    async def scenario():
        recorder = Recorder()
        async with fake_moonraker() as (fake, base), running(session_for(base, recorder)):
            await eventually(lambda: recorder.status)
            initial = recorder.status
            await fake.notify(
                "notify_status_update",
                {"print_stats": {"state": "printing", "filename": "benchy.gcode"}},
                12.5,
            )
            await eventually(lambda: len(recorder.changes) >= 2)
        return initial, recorder

    initial, recorder = asyncio.run(scenario())

    assert initial["print_stats"] == {"state": "standby", "filename": ""}
    assert recorder.klippy == ["ready"]
    assert recorder.changes[-1] == {
        "print_stats": {"state": "printing", "filename": "benchy.gcode"}
    }
    assert recorder.status["print_stats"] == {"state": "printing", "filename": "benchy.gcode"}
    assert recorder.status["toolhead"] == {"extruder": "extruder"}


def test_passes_history_notifications_on():
    async def scenario():
        recorder = Recorder()
        async with fake_moonraker() as (fake, base), running(session_for(base, recorder)):
            await eventually(lambda: recorder.status)
            job = {"job_id": "000042", "filename": "benchy.gcode", "status": "completed"}
            await fake.notify("notify_history_changed", {"action": "finished", "job": job})
            await eventually(lambda: recorder.history)
        return recorder.history

    history = asyncio.run(scenario())

    assert history == [
        ("finished", {"job_id": "000042", "filename": "benchy.gcode", "status": "completed"})
    ]


def test_reconnects_after_moonraker_drops_and_sets_everything_up_again():
    async def scenario():
        async def on_set_spool(params):  # pragma: no cover - only registered here
            pass

        async with fake_moonraker() as (fake, base):
            session = session_for(base, Recorder(), remote_methods={"set_spool": on_set_spool})
            async with running(session):
                await eventually(lambda: fake.subscriptions)
                await fake.drop()
                await eventually(lambda: len(fake.subscriptions) >= 2)
            return fake

    fake = asyncio.run(scenario())

    assert len(fake.identified) == 2
    assert fake.registered == ["set_spool", "set_spool"]


def test_keeps_trying_while_moonraker_is_down():
    async def scenario():
        fake = FakeMoonraker()
        base = await fake.start()
        port_url = base
        await fake.close()  # nothing listening now

        recorder = Recorder()
        session = MoonrakerSession("u1", url=port_url, listener=recorder, backoff=FAST)
        async with running(session) as task:
            await asyncio.sleep(0.2)
            assert not task.done()

    asyncio.run(scenario())


def test_waits_for_klippy_before_subscribing():
    async def scenario():
        recorder = Recorder()
        async with fake_moonraker(klippy_state="startup") as (fake, base):
            async with running(session_for(base, recorder)):
                await eventually(lambda: fake.identified)
                await eventually(lambda: recorder.klippy)
                assert fake.subscriptions == []

                fake.klippy_state = "ready"
                await fake.notify("notify_klippy_ready")
                await eventually(lambda: len(recorder.klippy) >= 2)
            return fake, recorder

    fake, recorder = asyncio.run(scenario())

    assert len(fake.subscriptions) == 1
    assert recorder.klippy == ["startup", "ready"]


def test_resubscribes_after_a_klipper_restart():
    async def scenario():
        recorder = Recorder()
        async with fake_moonraker() as (fake, base), running(session_for(base, recorder)):
            await eventually(lambda: fake.subscriptions)

            fake.klippy_state = "disconnected"
            await fake.notify("notify_klippy_disconnected")
            await eventually(lambda: len(recorder.klippy) >= 2)

            fake.klippy_state = "ready"
            await fake.notify("notify_klippy_ready")
            await eventually(lambda: len(fake.subscriptions) >= 2)
            await eventually(lambda: len(recorder.klippy) >= 3)
        return recorder

    recorder = asyncio.run(scenario())

    assert recorder.klippy == ["ready", "disconnected", "ready"]


def test_registers_remote_methods_and_calls_them():
    async def scenario():
        calls: list[Mapping[str, Any]] = []

        async def set_spool(params):
            calls.append(params)

        async with fake_moonraker() as (fake, base):
            session = session_for(
                base, Recorder(), remote_methods={"printlog_set_spool": set_spool}
            )
            async with running(session):
                await eventually(lambda: fake.registered)
                await fake.call_remote_method("printlog_set_spool", {"tool": 1, "spool": 7})
                await eventually(lambda: calls)
            return fake.registered, calls

    registered, calls = asyncio.run(scenario())

    assert registered == ["printlog_set_spool"]
    assert calls == [{"tool": 1, "spool": 7}]


def test_sends_the_moonraker_api_key():
    async def scenario():
        async with fake_moonraker(api_key="secret") as (fake, base):
            async with running(session_for(base, Recorder(), api_key="secret")):
                await eventually(lambda: fake.subscriptions)
            return fake

    fake = asyncio.run(scenario())

    assert fake.upgrade_headers[0]["X-Api-Key"] == "secret"
    assert fake.identified[0]["api_key"] == "secret"


@pytest.mark.parametrize("api_key", [None, "wrong"])
def test_says_what_to_do_when_moonraker_refuses_the_bridge(caplog, api_key):
    async def scenario():
        async with fake_moonraker(api_key="secret") as (fake, base):
            async with running(session_for(base, Recorder(), api_key=api_key)) as task:
                await eventually(lambda: any("rejected" in r.getMessage() for r in caplog.records))
                assert not task.done()  # keeps retrying; the key may be fixed on the printer
            return fake

    with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
        fake = asyncio.run(scenario())

    [message, *_] = [r.getMessage() for r in caplog.records if "rejected" in r.getMessage()]
    assert "[printer u1]" in message
    assert "moonraker_api_key" in message
    assert "trusted_clients" in message
    assert fake.subscriptions == []


def test_says_what_to_do_when_a_proxy_refuses_the_upgrade(caplog):
    async def scenario():
        async with fake_moonraker(reject_upgrade=True) as (fake, base):
            session = session_for(base, Recorder())
            async with running(session):
                await eventually(lambda: any("rejected" in r.getMessage() for r in caplog.records))

    with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
        asyncio.run(scenario())

    [message, *_] = [r.getMessage() for r in caplog.records if "rejected" in r.getMessage()]
    assert "HTTP 401" in message


def test_says_so_when_another_bridge_already_has_the_printer(caplog):
    async def scenario():
        async with fake_moonraker() as (fake, base):
            first = session_for(base, Recorder())
            async with running(first) as first_task:
                await eventually(lambda: fake.subscriptions)
                second = Recorder()
                async with running(session_for(base, second)):
                    await eventually(
                        lambda: any("already" in r.getMessage() for r in caplog.records)
                    )
                    first_task.cancel()  # the other bridge goes away...
                    await eventually(lambda: second.status)  # ...and this one takes over

    with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
        asyncio.run(scenario())

    [message, *_] = [r.getMessage() for r in caplog.records if "already" in r.getMessage()]
    assert "another printlog-bridge is already connected" in message
    assert "[printer u1]" in message


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")
def test_connects_over_the_local_unix_socket():
    async def scenario(sock: str):
        fake = FakeMoonraker()
        await fake.start_unix(sock)
        recorder = Recorder()
        try:
            session = MoonrakerSession("voron", socket=Path(sock), listener=recorder, backoff=FAST)
            async with running(session):
                await eventually(lambda: recorder.status)
                await fake.drop()
                await eventually(lambda: len(fake.identified) >= 2)
        finally:
            await fake.close()
        return fake, recorder

    with tempfile.TemporaryDirectory() as d:
        fake, recorder = asyncio.run(scenario(os.path.join(d, "m.sock")))

    assert fake.identified[0]["type"] == "agent"
    assert recorder.status["webhooks"]["state"] == "ready"


class TestBackoff:
    def test_doubles_from_one_second_up_to_a_minute(self):
        backoff = Backoff()

        delays = [backoff.next() for _ in range(9)]

        assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]

    def test_reset_starts_over(self):
        backoff = Backoff()
        backoff.next()
        backoff.next()

        backoff.reset()

        assert backoff.next() == 1


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("http://u1.local:7125", "ws://u1.local:7125/websocket"),
        ("https://u1.example", "wss://u1.example/websocket"),
        ("ws://u1.local:7125/websocket", "ws://u1.local:7125/websocket"),
        ("http://host/moonraker", "ws://host/moonraker/websocket"),
    ],
)
def test_websocket_url_from_the_configured_url(configured, expected):
    assert websocket_url(configured) == expected


def test_a_good_connection_resets_the_backoff():
    async def scenario():
        backoff = Backoff(initial=0.01, maximum=0.05)
        for _ in range(3):
            backoff.next()
        async with fake_moonraker() as (fake, base):
            session = session_for(base, Recorder(), backoff=backoff)
            async with running(session):
                await eventually(lambda: fake.subscriptions)
        return backoff

    backoff = asyncio.run(scenario())

    assert backoff.next() == 0.01
