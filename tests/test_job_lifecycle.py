"""The whole bridge, in process: Moonraker's job history in, prints in 3D Print Log out.

These are #4's acceptance criteria against the two fakes. tests/integration/ runs them against
a real Klipper and Moonraker.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import bridge_helpers as helpers
from bridge_helpers import write_config
from fake_moonraker import FakeMoonraker, eventually
from fake_printlog_api import FakePrintLogApi

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
async def bridge(config_path: Path, moonraker: FakeMoonraker):
    async with helpers.bridge(
        config_path,
        connected=lambda: moonraker.client_count == 1,
        disconnected=lambda: moonraker.client_count == 0,
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
