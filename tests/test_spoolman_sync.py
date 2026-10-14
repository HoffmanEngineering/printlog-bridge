"""Moonraker's Spoolman integration pointed at 3D Print Log: a spool picked on the printer, and
Moonraker's active spool kept on the active tool's spool in 3D Print Log."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from fake_moonraker import DEFAULT_OBJECTS, FakeMoonraker, eventually
from session_helpers import Recorder as SessionRecorder
from session_helpers import running

from printlog_bridge.adapters.moonraker.session import (
    Backoff,
    ConnectionLost,
    Listeners,
    MoonrakerSession,
)
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
async def syncing(fake: FakeMoonraker, sink: Recorder, loaded=None, **options):
    """`loaded` is what 3D Print Log has in each slot, known before the bridge connects."""
    base = await fake.start()
    sync = SpoolmanSync("u1", API_URL, sink, **options)
    if loaded is not None:
        sync.on_loaded(loaded)
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


def slots(**numbers: int | None) -> dict[int, dict]:
    """What 3D Print Log has loaded, as `Spools` reports it: `t2=9` is spool number 9 in T2."""
    return {
        int(tool[1:]): {"id": f"spool-{tool}", "spoolNumber": number}
        for tool, number in numbers.items()
    }


async def settled() -> None:
    """Long enough for a post the bridge was going to make to reach the fake."""
    await asyncio.sleep(0.1)


class TestPushes:
    def test_tool_change_sets_the_new_tools_spool(self):
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN
        fake.spool_id = 4

        async def scenario():
            async with syncing(fake, sink, loaded=slots(t0=4, t2=9)) as sync:
                await settled()
                assert fake.posted_spools == []  # T0's spool is already Moonraker's
                await fake.set_status(toolhead={"extruder": "extruder2"})
                await eventually(lambda: fake.posted_spools)
                await settled()
                return sync

        sync = asyncio.run(scenario())

        assert fake.posted_spools == [9]
        assert sync.moonraker_spool == 9
        assert sink.commands == []  # the bridge's own post isn't a pick to load back

    def test_tool_change_to_an_empty_slot_leaves_moonraker_alone(self):
        fake, sink = FakeMoonraker(objects=(*DEFAULT_OBJECTS, "extruder2")), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN
        fake.spool_id = 4

        async def scenario():
            async with syncing(fake, sink, loaded=slots(t0=4, t1=None)) as sync:
                await fake.set_status(toolhead={"extruder": "extruder2"})  # T2: nothing loaded
                await eventually(lambda: sync.active_slot == 2)
                await fake.set_status(toolhead={"extruder": "extruder1"})  # a spool, no number
                await eventually(lambda: sync.active_slot == 1)
                await settled()
                return sync

        sync = asyncio.run(scenario())

        assert fake.posted_spools == []
        assert sync.moonraker_spool == 4

    def test_slot_change_in_the_app_sets_the_active_spool(self):
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN
        fake.spool_id = 4

        async def scenario():
            async with syncing(fake, sink, loaded=slots(t0=4)) as sync:
                await settled()
                sync.on_loaded(slots(t0=4, t1=5))  # T1 isn't the active tool
                await settled()
                assert fake.posted_spools == []
                sync.on_loaded(slots(t0=7, t1=5))  # a refresh: T0's spool was changed
                await eventually(lambda: fake.posted_spools)
                await settled()

        asyncio.run(scenario())

        assert fake.posted_spools == [7]
        assert sink.commands == []

    def test_pick_from_the_printer_is_not_posted_back(self):
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN
        fake.spool_id = 4

        async def scenario():
            async with syncing(fake, sink, loaded=slots(t0=4)) as sync:
                await fake.pick_spool(12)
                await eventually(lambda: sink.commands)
                sync.on_loaded(slots(t0=12))  # 3D Print Log loaded the pick
                await settled()

        asyncio.run(scenario())

        assert sink.commands == [SpoolCommand("u1", 0, spool_number=12, slot_count=2)]
        assert fake.posted_spools == []

    def test_a_pick_after_a_post_moonraker_did_not_announce_is_still_a_pick(self):
        # Moonraker announces only a change: a post of the spool it already had goes unanswered,
        # and the next pick of that spool must not be taken for the answer.
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN
        fake.spool_id = 4

        async def scenario():
            async with syncing(fake, sink, loaded=slots(t0=4)) as sync:
                await settled()
                fake.spool_id = 7  # set by another client while the bridge wasn't looking
                sync.on_loaded(slots(t0=7))
                await eventually(lambda: fake.posted_spools)
                await fake.pick_spool(9)
                await fake.pick_spool(7)
                await eventually(lambda: len(sink.commands) == 2)

        asyncio.run(scenario())

        assert [c.spool_number for c in sink.commands] == [9, 7]

    def test_posts_made_back_to_back_are_not_taken_for_picks(self):
        # The second change comes before Moonraker has announced the first post.
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN
        fake.spool_id = 4

        async def scenario():
            async with syncing(fake, sink, loaded=slots(t0=4)) as sync:
                await settled()
                sync.on_loaded(slots(t0=9))
                sync.on_loaded(slots(t0=5))
                await eventually(lambda: len(fake.posted_spools) == 2)
                await settled()
                assert sink.commands == []
                # Once they're answered, a pick on the printer is carried out as ever.
                await fake.pick_spool(9)
                await eventually(lambda: sink.commands)
                return sync

        sync = asyncio.run(scenario())

        assert fake.posted_spools == [9, 5]
        assert [c.spool_number for c in sink.commands] == [9]
        assert sync.moonraker_spool == 9

    def test_no_post_while_moonraker_is_unreachable(self, caplog):
        sync = SpoolmanSync("u1", API_URL, Recorder())
        calls = []

        async def call(method, params):
            calls.append(method)
            if method == "server.config":
                return {"config": {"spoolman": PRINTLOG_SPOOLMAN}}
            if method == "server.spoolman.get_spool_id":
                return {"spool_id": 4}
            raise ConnectionLost("Moonraker closed the connection")

        async def scenario():
            await sync.on_connected(call)
            sync.on_loaded(slots(t0=4))
            await sync.on_status(
                {"toolhead": {"extruder": "extruder"}, "print_stats": {"state": "standby"}}, {}
            )
            sync.on_loaded(slots(t0=7))  # changed in the app while Moonraker restarts
            await settled()

        with caplog.at_level(logging.DEBUG):
            asyncio.run(scenario())

        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert sync.moonraker_spool == 4  # as Moonraker last said; a reconnect reads it again

    @pytest.mark.parametrize("state", ["printing", "paused"])
    def test_connect_while_printing_does_not_push(self, state):
        # The running print's usage goes to Moonraker's active spool: changing it on connect
        # would put what's left of the print on another spool than the one it started on.
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN
        fake.spool_id = 4
        fake.status["print_stats"] = {"state": state, "filename": "vase.gcode"}

        async def scenario():
            async with syncing(fake, sink, loaded=slots(t0=7, t1=5)):
                await settled()
                assert fake.posted_spools == []
                # A tool change after connecting is followed, print or no print.
                await fake.set_status(toolhead={"extruder": "extruder1"})
                await eventually(lambda: fake.posted_spools)

        asyncio.run(scenario())

        assert fake.posted_spools == [5]

    def test_connect_while_idle_pushes_the_active_slot(self):
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN
        fake.spool_id = 4

        async def scenario():
            async with syncing(fake, sink, loaded=slots(t0=7)) as sync:
                await eventually(lambda: fake.posted_spools)
                await settled()
                return sync

        sync = asyncio.run(scenario())

        assert fake.posted_spools == [7]
        assert fake.spool_id == sync.moonraker_spool == 7
        assert sink.commands == []

    def test_connect_while_idle_pushes_once_the_apps_spools_are_read(self):
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN

        async def scenario():
            async with syncing(fake, sink) as sync:
                await settled()
                assert fake.posted_spools == []
                sync.on_loaded(slots(t0=7))
                await eventually(lambda: fake.posted_spools)

        asyncio.run(scenario())

        assert fake.posted_spools == [7]

    def test_a_reconnect_does_not_post_again(self):
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN

        async def scenario():
            async with syncing(fake, sink, loaded=slots(t0=7)):
                await eventually(lambda: fake.posted_spools)
                fake.subscriptions.clear()
                await fake.drop()  # Moonraker restarts; its active spool is still 7
                await eventually(lambda: fake.subscriptions)
                await settled()

        asyncio.run(scenario())

        assert fake.posted_spools == [7]
        assert fake.calls.count("server.spoolman.get_spool_id") == 2

    def test_disabled_sync_never_posts(self):
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = {"server": "http://192.168.1.5:7912"}

        async def scenario():
            async with syncing(fake, sink, loaded=slots(t0=7, t1=5)) as sync:
                await fake.set_status(toolhead={"extruder": "extruder1"})
                await eventually(lambda: sync.active_slot == 1)
                sync.on_loaded(slots(t0=7, t1=6))
                await settled()

        asyncio.run(scenario())

        assert "server.spoolman.post_spool_id" not in fake.calls

    def test_moonraker_without_spoolman_is_ignored(self, caplog):
        # moonraker.conf names 3D Print Log, but the spoolman component didn't load.
        fake, sink = FakeMoonraker(), Recorder()
        fake.config["spoolman"] = PRINTLOG_SPOOLMAN
        fake.spoolman_loaded = False

        async def scenario():
            async with running_sync(fake, sink, loaded=slots(t0=7, t1=5)) as (sync, task):
                await fake.set_status(toolhead={"extruder": "extruder1"})
                await eventually(lambda: sync.active_slot == 1)
                sync.on_loaded(slots(t0=7, t1=6))
                await settled()
                assert not task.done()
                return sync

        with caplog.at_level(logging.DEBUG):
            sync = asyncio.run(scenario())

        assert not sync.enabled
        assert fake.posted_spools == []
        assert "server.spoolman.post_spool_id" not in fake.calls
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        missing = [r for r in caplog.records if "no spoolman component" in r.getMessage()]
        assert [r.levelno for r in missing] == [logging.DEBUG]


@contextlib.asynccontextmanager
async def running_sync(fake: FakeMoonraker, sink: Recorder, loaded=None):
    """`syncing`, with the session's task as well, to see the connection stays up."""
    base = await fake.start()
    sync = SpoolmanSync("u1", API_URL, sink)
    if loaded is not None:
        sync.on_loaded(loaded)
    session = MoonrakerSession(
        "u1", url=base, listener=sync, backoff=Backoff(initial=0.01, maximum=0.05)
    )
    try:
        async with running(session) as task:
            await eventually(lambda: fake.subscriptions)
            yield sync, task
    finally:
        await fake.close()
