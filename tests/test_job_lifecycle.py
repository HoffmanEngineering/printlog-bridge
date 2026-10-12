"""The whole bridge, in process: Moonraker's job history in, prints in 3D Print Log out.

These are #4's acceptance criteria against the two fakes. tests/integration/ runs them against
a real Klipper and Moonraker.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import bridge_helpers as helpers
import pytest
from bridge_helpers import write_config
from fake_moonraker import FakeMoonraker, eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.core.api_client import REQUEST_TIMEOUT_S
from printlog_bridge.core.lifecycle import BACKOFF_S

METADATA = {"estimated_time": 3900.0, "filament_total": 4400.0, "filament_weight_total": 13.2}


@contextlib.asynccontextmanager
async def fakes():
    moonraker, api = FakeMoonraker(), FakePrintLogApi()
    moonraker_url, api_url = await moonraker.start(), await api.start()
    try:
        yield moonraker, moonraker_url, api, api_url
    finally:
        await moonraker.close()
        await api.close()


@contextlib.asynccontextmanager
async def bridge(config_path: Path, moonraker: FakeMoonraker, **options):
    async with helpers.bridge(
        config_path,
        connected=lambda: moonraker.client_count == 1,
        disconnected=lambda: moonraker.client_count == 0,
        **options,
    ) as task:
        yield task


def test_a_print_is_logged_when_it_starts_and_completed_when_it_ends(tmp_path):
    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            async with bridge(write_config(tmp_path, moonraker_url, api_url), moonraker):
                job = await moonraker.start_job("shelf_bracket_0.2mm_PLA_MK4_1h5m.gcode", METADATA)
                await eventually(lambda: api.prints)
                printing = dict(api.prints[1])
                await moonraker.finish_job(job, filament_used=4321.5, print_duration=3725.0)
                await eventually(lambda: api.prints[1]["status"] != 2)
            return printing, api.prints[1]

    printing, done = asyncio.run(scenario())

    assert printing["status"] == 2
    assert printing["title"] == "Shelf Bracket"
    assert done["status"] == 3
    assert done["printTimeInSeconds"] == 3725
    assert done["estimatedPrintTimeInSeconds"] == 3900
    [row] = done["filamentUsage"]
    assert abs(row["lengthInM"] * 1000 - 4321.5) <= 4321.5 * 0.02
    assert row["estimatedLengthInM"] == 4.4


def test_a_four_tool_print_is_logged_per_tool_and_adds_up_to_the_total(tmp_path):
    """#5's acceptance: per-slot totals within 1% of `filament_used`, unlinked tools kept."""
    metadata = {**METADATA, "filament_weights": [30.2, 9.8, 3.6, 1.5]}
    # (active extruder, filament_used so far), as Klipper reports them on a Snapmaker U1.
    steps = [("extruder", 4000.0), ("extruder1", 6000.0), ("extruder2", 7000.0),
             ("extruder3", 7500.0), ("extruder", 11500.0), ("extruder1", 12000.0)]  # fmt: skip

    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            async with bridge(write_config(tmp_path, moonraker_url, api_url), moonraker):
                job = await moonraker.start_job("u1/dragon.gcode", metadata)
                await eventually(lambda: api.prints)
                rows = api.prints[1]["filamentUsage"]
                [tool_1] = [r for r in rows if r["slot"] == 1]
                tool_1["filament"] = {"id": "spool-blue"}  # the user links tool 1's spool
                await moonraker.set_status(
                    print_stats={"state": "printing", "filename": "u1/dragon.gcode",
                                 "filament_used": 0.0}
                )  # fmt: skip
                for tool, used in steps:
                    await moonraker.set_status(
                        toolhead={"extruder": tool}, print_stats={"filament_used": used}
                    )
                await asyncio.sleep(0.2)
                await moonraker.finish_job(job, filament_used=12040.0)
                await eventually(lambda: api.prints[1]["status"] != 2)
            return api

    api = asyncio.run(scenario())

    rows = {r["slot"]: r for r in api.prints[1]["filamentUsage"]}
    assert sorted(rows) == [0, 1, 2, 3]
    assert abs(sum(r["lengthInM"] for r in rows.values()) * 1000 - 12040.0) <= 12040.0 * 0.01
    assert rows[0]["lengthInM"] * 1000 == pytest.approx(8000.0, rel=0.01)
    assert rows[1]["filament"] == {"id": "spool-blue"}
    assert rows[1]["lengthInM"] * 1000 == pytest.approx(2500.0, rel=0.01)
    assert rows[3]["filament"] is None  # no spool linked: recorded with its slot all the same
    assert rows[3]["lengthInM"] * 1000 == pytest.approx(500.0, rel=0.01)
    assert rows[3]["estimatedAmountMg"] == 1500


def test_a_job_that_ends_while_the_bridge_is_stopped_is_still_completed_once(tmp_path):
    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            config = write_config(tmp_path, moonraker_url, api_url)
            async with bridge(config, moonraker):
                job = await moonraker.start_job(metadata=METADATA)
                await eventually(lambda: api.prints)
            # The bridge is killed mid-print, and the print finishes without it.
            await moonraker.finish_job(job, notify=False)
            async with bridge(config, moonraker):
                await eventually(lambda: api.prints[1]["status"] == 3)
                await asyncio.sleep(0.2)
            return api

    api = asyncio.run(scenario())

    assert len(api.prints) == 1
    assert len(api.completions) == 1


def test_a_print_that_starts_and_ends_while_the_bridge_is_stopped_is_logged(tmp_path):
    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            config = write_config(tmp_path, moonraker_url, api_url)
            async with bridge(config, moonraker):
                await moonraker.finish_job(await moonraker.start_job("first.gcode"))
                await eventually(lambda: api.prints and api.prints[1]["status"] == 3)
            await moonraker.finish_job(
                await moonraker.start_job("second.gcode", notify=False), "cancelled", notify=False
            )
            async with bridge(config, moonraker):
                await eventually(lambda: len(api.prints) == 2 and api.prints[2]["status"] == 4)
            return api

    api = asyncio.run(scenario())

    assert [p["title"] for p in api.prints.values()] == ["First", "Second"]


def test_a_print_cut_short_by_a_moonraker_restart_is_failed_with_a_note(tmp_path):
    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            async with bridge(write_config(tmp_path, moonraker_url, api_url), moonraker):
                job = await moonraker.start_job()
                await eventually(lambda: api.prints)
                await moonraker.finish_job(job, "interrupted", notify=False)
                await moonraker.drop()
                await eventually(lambda: api.prints[1]["notes"])
            return api.prints[1]

    print_ = asyncio.run(scenario())

    assert print_["status"] == 5
    assert print_["notes"] == "Moonraker stopped during the print"


def test_a_print_links_to_the_connection_that_logged_it(tmp_path):
    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            async with bridge(write_config(tmp_path, moonraker_url, api_url), moonraker):
                await moonraker.start_job("benchy.gcode", METADATA)
                await eventually(lambda: api.prints)
            return api.prints[1], api.connections

    print_, connections = asyncio.run(scenario())

    [connection] = connections.values()
    assert print_["connectionId"] == connection["id"]
    assert connection["displayName"] == "printlog-bridge (voron)"
    assert connection["kind"] == "moonraker"
    assert connection["printerId"] == 42


def test_a_print_made_with_the_internet_unplugged_is_logged_once_it_is_back(tmp_path):
    """#9's acceptance criterion: the whole print offline, then logged soon after reconnecting.

    The backoff is scaled down from 2 s .. 60 s to 0.05 s .. 0.5 s, so "within 2 minutes" is
    "within 1 s" here; test_the_backoff_cap_keeps_a_print_within_2_minutes covers the real one.
    """

    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            config = write_config(tmp_path, moonraker_url, api_url)
            api.offline = True
            async with bridge(config, moonraker, backoff=(0.05, 0.5)):
                job = await moonraker.start_job("benchy.gcode", METADATA)
                await asyncio.sleep(0.3)
                await moonraker.finish_job(job, filament_used=4321.5, print_duration=3725.0)
                await asyncio.sleep(1.0)  # retries pile up into the backoff
                assert api.prints == {}
                api.offline = False
                loop = asyncio.get_running_loop()
                back = loop.time()
                await eventually(lambda: api.prints and api.prints[1]["status"] == 3, timeout=3)
                took = loop.time() - back
            return api, took

    api, took = asyncio.run(scenario())

    assert took < 1.0
    [print_] = api.prints.values()
    assert print_["title"] == "Benchy"
    assert print_["printTimeInSeconds"] == 3725
    assert len(api.completions) == 1
    [connection] = api.connections.values()
    assert print_["connectionId"] == connection["id"]


def test_the_backoff_cap_keeps_a_print_within_2_minutes():
    assert BACKOFF_S[1] + 2 * REQUEST_TIMEOUT_S < 120


def test_a_bridge_killed_while_offline_logs_the_print_after_it_restarts(tmp_path):
    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            config = write_config(tmp_path, moonraker_url, api_url)
            api.offline = True
            async with bridge(config, moonraker, backoff=(0.05, 0.5)):
                job = await moonraker.start_job("benchy.gcode", METADATA)
                await moonraker.finish_job(job, filament_used=4321.5, print_duration=3725.0)
                await asyncio.sleep(0.3)
            api.offline = False
            async with bridge(config, moonraker, backoff=(0.05, 0.5)):
                await eventually(lambda: api.prints and api.prints[1]["status"] == 3)
                await asyncio.sleep(0.2)
            return api

    api = asyncio.run(scenario())

    assert len(api.prints) == 1
    assert len(api.completions) == 1
