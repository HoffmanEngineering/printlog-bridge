"""Moonraker's Spoolman integration pointed at the user's own Spoolman server (#38): the spool
active there is assigned to its material in 3D Print Log, and nothing is written to Spoolman."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fake_moonraker import DEFAULT_OBJECTS, FakeMoonraker, eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.adapters.moonraker.session import Backoff, MoonrakerSession
from printlog_bridge.adapters.moonraker.spoolman import SpoolmanSync
from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.events import JobPoint, SpoolCommand, SpoolmanSpool, SpoolReading
from printlog_bridge.core.spools import Spools

API_URL = "https://api.3dprintlog.com"
OWN_SPOOLMAN = {"server": "http://spoolman.local:7912"}
RED = SpoolmanSpool(12, "Polymaker PolyTerra PLA Red", ("04A23F11",))


def spoolman_spool(
    spool_id: int, name: str = "PolyTerra PLA Red", vendor: str | None = "Polymaker", nfc_id=None
) -> dict:
    """A spool as Spoolman's `GET /v1/spool/{id}` returns it. Extra fields are JSON text."""
    filament = {"id": 3, "name": name, "material": "PLA"}
    if vendor is not None:
        filament["vendor"] = {"id": 1, "name": vendor}
    extra = {} if nfc_id is None else {"nfc_id": json.dumps(nfc_id)}
    return {"id": spool_id, "filament": filament, "remaining_weight": 800.0, "extra": extra}


def own_spoolman(**options) -> FakeMoonraker:
    fake = FakeMoonraker(**options)
    fake.config["spoolman"] = dict(OWN_SPOOLMAN)
    fake.spoolman_spools[12] = spoolman_spool(12, nfc_id="04A23F11")
    fake.spoolman_spools[13] = spoolman_spool(13, name="PolyTerra PLA Blue")
    return fake


class Recorder:
    """A SpoolSink that keeps what it's told."""

    def __init__(self) -> None:
        self.commands: list[SpoolCommand] = []

    async def spool_command(self, command: SpoolCommand) -> str:
        self.commands.append(command)
        return "T0 is now Red in 3D Print Log"

    async def spool_reading(self, reading: SpoolReading) -> None:
        pass


@contextlib.asynccontextmanager
async def syncing(fake: FakeMoonraker, sink, **options):
    base = await fake.start()
    sync = SpoolmanSync("u1", API_URL, sink, **options)
    session = MoonrakerSession(
        "u1", url=base, listener=sync, backoff=Backoff(initial=0.01, maximum=0.05)
    )
    try:
        task = asyncio.ensure_future(session.run())
        try:
            await eventually(lambda: fake.subscriptions)
            yield sync
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        await fake.close()


async def quiet() -> None:
    """Long enough for an assignment the bridge was going to make to reach the sink."""
    await asyncio.sleep(0.1)


def scenario_of(fake: FakeMoonraker, sink, steps=None, **options):
    async def scenario():
        async with syncing(fake, sink, **options) as sync:
            if steps is not None:
                await steps(sync)
            await quiet()
            return sync

    return asyncio.run(scenario())


def assert_nothing_written(fake: FakeMoonraker) -> None:
    assert fake.posted_spools == []
    assert "server.spoolman.post_spool_id" not in fake.calls
    assert all(request["request_method"] == "GET" for request in fake.proxied)


class TestConnect:
    def test_own_spoolman_is_followed_read_only(self, caplog):
        fake = own_spoolman()

        with caplog.at_level(logging.INFO):
            sync = scenario_of(fake, Recorder())

        assert sync.external
        assert not sync.enabled  # the two-way sync is for 3D Print Log's own Spoolman API
        lines = [r.getMessage() for r in caplog.records if "Spoolman" in r.getMessage()]
        assert len(lines) == 1
        assert "spoolman.local" in lines[0]
        assert "nothing is written to Spoolman" in lines[0]

    def test_the_active_spool_is_assigned_to_the_active_tool_on_connect(self):
        fake, sink = own_spoolman(), Recorder()
        fake.spool_id = 12

        scenario_of(fake, sink)

        # The fake has extruder and extruder1: a printer with two tools, on T0.
        assert sink.commands == [SpoolCommand("u1", 0, spoolman=RED, slot_count=2)]
        assert fake.proxied == [
            {"request_method": "GET", "path": "/v1/spool/12", "use_v2_response": True}
        ]

    def test_a_single_tool_printer_assigns_slot_0(self):
        fake = own_spoolman(objects=("webhooks", "print_stats", "toolhead", "extruder"))
        sink = Recorder()
        fake.spool_id = 12

        scenario_of(fake, sink)

        assert [(c.slot, c.slot_count) for c in sink.commands] == [(0, 1)]

    def test_no_active_spool_assigns_nothing(self):
        fake, sink = own_spoolman(), Recorder()

        scenario_of(fake, sink)

        assert sink.commands == []
        assert fake.proxied == []

    def test_a_print_under_way_is_left_on_its_spool(self, caplog):
        fake, sink = own_spoolman(), Recorder()
        fake.spool_id = 12
        fake.status["print_stats"] = {"state": "printing", "filename": "a.gcode"}

        with caplog.at_level(logging.INFO):
            scenario_of(fake, sink)

        assert sink.commands == []
        assert "a print is under way" in caplog.text

    def test_a_reconnect_assigns_only_a_spool_changed_while_away(self):
        fake, sink = own_spoolman(), Recorder()
        fake.spool_id = 12

        async def steps(sync):
            await eventually(lambda: sink.commands)
            fake.subscriptions.clear()
            await fake.drop()
            await eventually(lambda: fake.subscriptions)
            await quiet()
            assert len(sink.commands) == 1  # still 12: nothing to do
            fake.spool_id = 13  # picked while the bridge was away
            fake.subscriptions.clear()
            await fake.drop()
            await eventually(lambda: fake.subscriptions)
            await eventually(lambda: len(sink.commands) == 2)

        scenario_of(fake, sink, steps)

        assert [c.spoolman.id for c in sink.commands] == [12, 13]

    def test_a_spoolman_section_without_the_component_is_left_alone(self):
        fake, sink = own_spoolman(), Recorder()
        fake.spoolman_loaded = False

        sync = scenario_of(fake, sink)

        assert not sync.external
        assert fake.proxied == []

    def test_the_server_url_never_reaches_the_log(self, caplog):
        fake = own_spoolman()
        fake.config["spoolman"] = {"server": "http://user:hunter2@spoolman.local:7912"}

        with caplog.at_level(logging.DEBUG):
            scenario_of(fake, Recorder())

        assert "hunter2" not in caplog.text


class TestPicks:
    def test_a_pick_assigns_the_spool_to_the_active_tool(self):
        fake, sink = own_spoolman(), Recorder()

        async def steps(sync):
            await fake.set_status(toolhead={"extruder": "extruder1"})
            await eventually(lambda: sync.active_slot == 1)
            await fake.pick_spool(12)
            await eventually(lambda: sink.commands)

        scenario_of(fake, sink, steps)

        assert sink.commands == [SpoolCommand("u1", 1, spoolman=RED, slot_count=2)]
        assert fake.scripts == []  # a pick isn't answered on the console
        assert_nothing_written(fake)

    def test_a_pick_mid_print_carries_where_the_job_was(self):
        fake, sink = own_spoolman(), Recorder()
        where = JobPoint("job-1", {0: 120.0}, progress=0.4)

        async def steps(sync):
            await fake.pick_spool(13)
            await eventually(lambda: sink.commands)

        scenario_of(fake, sink, steps, point=lambda: where)

        assert sink.commands[0].during == where
        assert sink.commands[0].spoolman == SpoolmanSpool(13, "Polymaker PolyTerra PLA Blue")

    def test_a_cleared_spool_changes_nothing(self):
        fake, sink = own_spoolman(), Recorder()
        fake.spool_id = 12

        async def steps(sync):
            await eventually(lambda: sink.commands)
            await fake.pick_spool(None)
            await eventually(lambda: sync.moonraker_spool is None)

        scenario_of(fake, sink, steps)

        assert len(sink.commands) == 1

    def test_picks_are_assigned_in_the_order_they_were_made(self):
        fake, sink = own_spoolman(), Recorder()

        async def steps(sync):
            await fake.pick_spool(12)
            await fake.pick_spool(13)
            await eventually(lambda: len(sink.commands) == 2)

        scenario_of(fake, sink, steps)

        assert [c.spoolman.id for c in sink.commands] == [12, 13]

    def test_a_spool_spoolman_cant_describe_is_looked_up_by_its_id_alone(self):
        fake, sink = own_spoolman(), Recorder()
        fake.spoolman_down = True

        async def steps(sync):
            await fake.pick_spool(12)
            await eventually(lambda: sink.commands)

        scenario_of(fake, sink, steps)

        assert sink.commands[0].spoolman == SpoolmanSpool(12)

    def test_an_older_moonrakers_proxy_answer_is_read_too(self):
        fake, sink = own_spoolman(), Recorder()
        fake.proxy_v2 = False

        async def steps(sync):
            await fake.pick_spool(12)
            await eventually(lambda: sink.commands)

        scenario_of(fake, sink, steps)

        assert sink.commands[0].spoolman == RED

    def test_a_spool_with_no_vendor_or_tag(self):
        fake, sink = own_spoolman(), Recorder()
        fake.spoolman_spools[14] = spoolman_spool(14, name="Mystery PETG", vendor=None)
        fake.spoolman_spools[14]["extra"]["nfc_id"] = "not json"

        async def steps(sync):
            await fake.pick_spool(14)
            await eventually(lambda: sink.commands)

        scenario_of(fake, sink, steps)

        assert sink.commands[0].spoolman == SpoolmanSpool(14, "Mystery PETG", ("not json",))


def toolchanger(*tools: str) -> FakeMoonraker:
    fake = own_spoolman(objects=(*DEFAULT_OBJECTS, *tools, "save_variables"))
    fake.status["save_variables"] = {"variables": {}}
    return fake


class TestPerTool:
    def test_each_tools_spool_is_assigned_to_its_slot(self):
        fake, sink = toolchanger("gcode_macro T0", "gcode_macro T1", "gcode_macro T5"), Recorder()
        fake.status["gcode_macro T0"] = {"spool_id": 13, "color": "blue"}
        fake.status["gcode_macro T1"] = {"spool_id": 12}
        fake.status["gcode_macro T5"] = {"spool_id": 14}  # no extruder5: not a tool

        scenario_of(fake, sink)

        assert sorted((c.slot, c.spoolman.id) for c in sink.commands) == [(0, 13), (1, 12)]
        assert fake.subscriptions[0]["gcode_macro T0"] == ["spool_id"]
        assert "gcode_macro T5" not in fake.subscriptions[0]

    def test_a_tools_spool_changing_is_assigned_to_that_slot_alone(self):
        fake, sink = toolchanger("gcode_macro T0", "gcode_macro T1"), Recorder()
        fake.status["gcode_macro T0"] = {"spool_id": 13}
        fake.status["gcode_macro T1"] = {"spool_id": None}

        async def steps(sync):
            await eventually(lambda: sink.commands)
            await fake.set_status(**{"gcode_macro T1": {"spool_id": 12}})
            await eventually(lambda: len(sink.commands) == 2)

        scenario_of(fake, sink, steps)

        assert [(c.slot, c.spoolman.id) for c in sink.commands] == [(0, 13), (1, 12)]

    def test_the_active_spool_is_left_to_the_tools(self):
        # A tool change macro sets the active spool to the tool's: it says nothing new.
        fake, sink = toolchanger("gcode_macro T0", "gcode_macro T1"), Recorder()
        fake.status["gcode_macro T0"] = {"spool_id": 13}
        fake.status["gcode_macro T1"] = {"spool_id": None}
        fake.spool_id = 13

        async def steps(sync):
            await eventually(lambda: sink.commands)
            await fake.pick_spool(12)
            await quiet()

        scenario_of(fake, sink, steps)

        assert [(c.slot, c.spoolman.id) for c in sink.commands] == [(0, 13)]

    def test_saved_variables_name_each_tools_spool(self):
        fake, sink = toolchanger(), Recorder()
        fake.status["save_variables"] = {"variables": {"t1__spool_id": 12, "other": 3}}

        scenario_of(fake, sink)

        assert [(c.slot, c.spoolman.id) for c in sink.commands] == [(1, 12)]

    def test_a_klipper_restart_assigns_nothing_again(self):
        fake, sink = toolchanger("gcode_macro T0"), Recorder()
        fake.status["gcode_macro T0"] = {"spool_id": 13}

        async def steps(sync):
            await eventually(lambda: sink.commands)
            fake.subscriptions.clear()
            await fake.notify("notify_klippy_ready")
            await eventually(lambda: fake.subscriptions)

        scenario_of(fake, sink, steps)

        assert len(sink.commands) == 1

    def test_tool_macros_without_a_spool_variable_leave_the_active_spool_in_charge(self):
        fake, sink = toolchanger("gcode_macro T0", "gcode_macro T1"), Recorder()
        fake.spool_id = 12

        scenario_of(fake, sink)

        assert [(c.slot, c.spoolman.id) for c in sink.commands] == [(0, 12)]


class TestReadOnly:
    def test_nothing_is_written_to_spoolman_or_moonraker(self):
        fake, sink = toolchanger("gcode_macro T0"), Recorder()
        fake.status["gcode_macro T0"] = {"spool_id": None}
        fake.spool_id = 12

        async def steps(sync):
            # Everything that makes the two-way sync post: a tool change, a spool changed in
            # the app, a pick.
            sync.on_loaded({0: {"id": "a", "spoolNumber": 5}, 1: {"id": "b", "spoolNumber": 6}})
            await fake.set_status(toolhead={"extruder": "extruder1"})
            await fake.set_status(**{"gcode_macro T0": {"spool_id": 13}})
            await fake.pick_spool(12)
            await eventually(lambda: sink.commands)

        scenario_of(fake, sink, steps)

        assert_nothing_written(fake)


class TestWithThePrintLogApi:
    """The acceptance criteria, with a real `Spools` between the fakes."""

    @staticmethod
    def run(fake: FakeMoonraker, api: FakePrintLogApi, steps) -> None:
        async def scenario():
            base = await api.start()
            try:
                async with ApiClient(base, api.api_key) as client:
                    spools = Spools(client, {"u1": 42})
                    async with syncing(fake, spools) as sync:
                        await steps(sync)
                        await quiet()
            finally:
                await api.close()

        asyncio.run(scenario())

    def test_setting_the_active_spool_assigns_its_material_to_slot_0(self):
        fake, api = own_spoolman(), FakePrintLogApi()
        red = api.add_spool("PolyTerra Red", spoolman_id=12)

        async def steps(sync):
            await fake.pick_spool(12)
            await eventually(lambda: api.slot_spools(42) == {0: red})

        self.run(fake, api, steps)

        assert api.slot_spools(42) == {0: red}
        assert_nothing_written(fake)

    def test_a_spool_matched_by_its_tag(self):
        fake, api = own_spoolman(), FakePrintLogApi()
        red = api.add_spool("PolyTerra Red", tags=("04:A2:3F:11",))

        async def steps(sync):
            await fake.pick_spool(12)
            await eventually(lambda: api.slot_spools(42) == {0: red})

        self.run(fake, api, steps)

        # Of the Spoolman spool, 3D Print Log is sent its id and tag, and nothing else.
        assert [path for _, path in api.requests if "/Filaments/" in path] == [
            "/api/Filaments/by-spoolman-id/12",
            "/api/Filaments/by-tag/04A23F11",
        ]
        assert [set(body) for body in api.slot_puts] == [{"filamentId", "slotLabel"}]

    def test_an_unmapped_spool_gets_a_hint_and_no_assignment(self, caplog):
        fake, api = own_spoolman(), FakePrintLogApi()
        loaded = api.add_spool("Loaded")
        api.user_loads(42, 0, loaded)

        async def steps(sync):
            await fake.pick_spool(13)
            await eventually(lambda: "Spoolman spool 13" in caplog.text)

        with caplog.at_level(logging.INFO):
            self.run(fake, api, steps)

        assert api.slot_spools(42) == {0: loaded}
        assert api.slot_puts == []
        assert (
            "no material in 3D Print Log has Spoolman spool 13 (Polymaker PolyTerra PLA Blue); "
            "nothing changed. Link it in the app." in caplog.text
        )
        assert_nothing_written(fake)
