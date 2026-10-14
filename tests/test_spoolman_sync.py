"""Moonraker's Spoolman integration pointed at 3D Print Log: a spool picked on the printer."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fake_moonraker import FakeMoonraker, eventually
from session_helpers import Recorder as SessionRecorder
from session_helpers import running

from printlog_bridge.adapters.moonraker.session import Backoff, Listeners, MoonrakerSession
from printlog_bridge.adapters.moonraker.spoolman import SpoolmanSync
from printlog_bridge.core.events import JobPoint, SpoolCommand, SpoolReading

API_URL = "https://api.3dprintlog.com"
PRINTLOG_SPOOLMAN = {"server": "https://printer-3:abc@api.3dprintlog.com/spoolman"}


class Recorder:
    """A SpoolSink that keeps what it's told."""

    def __init__(self) -> None:
        self.commands: list[SpoolCommand] = []

    async def spool_command(self, command: SpoolCommand) -> str:
        self.commands.append(command)
        return "T1 is now Silk Gold in 3D Print Log"

    async def spool_reading(self, reading: SpoolReading) -> None:
        pass


@contextlib.asynccontextmanager
async def syncing(fake: FakeMoonraker, sink: Recorder, **options):
    base = await fake.start()
    sync = SpoolmanSync("u1", API_URL, sink, **options)
    session = MoonrakerSession(
        "u1", url=base, listener=sync, backoff=Backoff(initial=0.01, maximum=0.05)
    )
    try:
        async with running(session):
            await eventually(lambda: fake.subscriptions)
            yield sync
    finally:
        await fake.close()


def connected(fake: FakeMoonraker) -> SpoolmanSync:
    async def scenario():
        async with syncing(fake, Recorder()) as sync:
            return sync

    return asyncio.run(scenario())


class TestEnabled:
    def test_enabled_when_spoolman_server_is_the_api_host(self):
        fake = FakeMoonraker()
        fake.config["spoolman"] = {"server": "https://printer-3:abc@API.3dprintlog.com/spoolman"}
        fake.spool_id = 7

        sync = connected(fake)

        assert sync.enabled
        assert sync.moonraker_spool == 7

    def test_disabled_for_another_spoolman_server(self, caplog):
        fake = FakeMoonraker()
        fake.config["spoolman"] = {"server": "http://192.168.1.5:7912"}

        with caplog.at_level(logging.INFO):
            sync = connected(fake)

        assert not sync.enabled
        lines = [r.getMessage() for r in caplog.records if "Spoolman" in r.getMessage()]
        assert len(lines) == 1
        assert lines[0].startswith("[printer u1] ")

    def test_disabled_without_spoolman_section(self):
        fake = FakeMoonraker()

        sync = connected(fake)

        assert not sync.enabled
        assert "server.spoolman.get_spool_id" not in fake.calls

    def test_the_secret_in_the_server_url_is_never_logged(self, caplog):
        fake = FakeMoonraker()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN

        with caplog.at_level(logging.DEBUG):
            connected(fake)

        assert "abc" not in caplog.text

    def test_a_reconnect_reads_the_config_again(self):
        fake, sink = FakeMoonraker(), Recorder()

        async def scenario():
            async with syncing(fake, sink) as sync:
                assert not sync.enabled
                fake.config["spoolman"] = PRINTLOG_SPOOLMAN  # Moonraker restarted with it
                fake.subscriptions.clear()
                await fake.drop()
                await eventually(lambda: fake.subscriptions)
                return sync

        assert asyncio.run(scenario()).enabled
        assert fake.calls.count("server.config") == 2


class TestPicks:
    def test_picked_spool_assigns_the_active_slot(self):
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN

        async def scenario():
            async with syncing(fake, sink) as sync:
                await fake.set_status(toolhead={"extruder": "extruder1"})
                await eventually(lambda: sync.active_slot == 1)
                await fake.pick_spool(12)
                await eventually(lambda: sink.commands)
                return sync

        sync = asyncio.run(scenario())

        # The fake has extruder and extruder1: a printer with two tools.
        assert sink.commands == [SpoolCommand("u1", 1, spool_number=12, slot_count=2)]
        assert sync.moonraker_spool == 12
        assert fake.scripts == []  # a pick isn't answered on the console

    def test_a_pick_mid_print_carries_where_the_job_was(self):
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN
        where = JobPoint("job-1", {0: 120.0}, progress=0.4)

        async def scenario():
            async with syncing(fake, sink, point=lambda: where):
                await fake.pick_spool(12)
                await eventually(lambda: sink.commands)

        asyncio.run(scenario())

        assert sink.commands[0].slot == 0
        assert sink.commands[0].during == where

    def test_cleared_spool_changes_nothing(self):
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN
        fake.spool_id = 12

        async def scenario():
            async with syncing(fake, sink) as sync:
                await fake.pick_spool(None)
                await eventually(lambda: sync.moonraker_spool is None)
                await asyncio.sleep(0.05)

        asyncio.run(scenario())

        assert sink.commands == []

    def test_disabled_sync_ignores_picks(self):
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = {"server": "http://192.168.1.5:7912"}

        async def scenario():
            async with syncing(fake, sink):
                await fake.pick_spool(12)
                await asyncio.sleep(0.1)

        asyncio.run(scenario())

        assert sink.commands == []

    def test_a_listener_without_spoolman_hears_nothing_of_picks(self):
        # Listeners written before Spoolman sync have no on_active_spool: a pick passes them by.
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN
        other = SessionRecorder()

        async def scenario():
            base = await fake.start()
            sync = SpoolmanSync("u1", API_URL, sink)
            session = MoonrakerSession(
                "u1",
                url=base,
                listener=Listeners(other, sync),
                backoff=Backoff(initial=0.01, maximum=0.05),
            )
            try:
                async with running(session) as task:
                    await eventually(lambda: fake.subscriptions)
                    await fake.pick_spool(12)
                    await eventually(lambda: sink.commands)
                    await fake.respond("// still connected")
                    await eventually(lambda: other.console)
                    assert not task.done()
            finally:
                await fake.close()

        asyncio.run(scenario())

        assert len(sink.commands) == 1
        assert len(other.connected) == 1  # the connection never dropped
