"""The whole bridge, in process: #6's acceptance criteria against the two fakes."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import time
from pathlib import Path

import bridge_helpers as helpers
from aiohttp import web
from bridge_helpers import write_config
from fake_moonraker import DEFAULT_OBJECTS, FakeMoonraker, eventually
from fake_printlog_api import FakePrintLogApi
from test_moonraker_spools import EMPTY_CHANNEL, FILAMENT_DETECT, SNAPMAKER_PLA_RED

U1_OBJECTS = (*DEFAULT_OBJECTS, "extruder2", "extruder3", "filament_detect")
METADATA = {"estimated_time": 3900.0, "filament_total": 4400.0, "filament_weight_total": 13.2}


@contextlib.asynccontextmanager
async def bridge(tmp_path: Path, moonraker: FakeMoonraker, api: FakePrintLogApi):
    """The bridge, watching `moonraker` and logging to `api`, until the block ends."""
    moonraker_url, api_url = await moonraker.start(), await api.start()
    try:
        async with helpers.bridge(
            write_config(tmp_path, moonraker_url, api_url),
            connected=lambda: moonraker.client_count == 1,
            disconnected=lambda: moonraker.client_count == 0,
        ) as task:
            await eventually(lambda: moonraker.subscriptions)
            yield task
    finally:
        await moonraker.close()
        await api.close()


def test_set_spool_by_tag_assigns_the_slot_within_5_seconds(tmp_path):
    moonraker, api = FakeMoonraker(objects=U1_OBJECTS), FakePrintLogApi()
    spool = api.add_spool("Silk Gold", tags=("04:a2:3f:11",))

    async def scenario():
        async with bridge(tmp_path, moonraker, api):
            started = time.monotonic()
            # What PRINTLOG_SET_SPOOL TOOL=2 TAG=04A23F11 prints.
            await moonraker.respond("// printlog: SET_SPOOL TOOL=2 TAG=04A23F11")
            await eventually(lambda: api.slot_spools(42).get(2) == spool, timeout=5)
            took = time.monotonic() - started
            await eventually(lambda: moonraker.scripts)
        return took

    took = asyncio.run(scenario())

    assert took < 5
    assert api.slot_counts[42] == 4  # the printer's four tools
    [answer] = moonraker.scripts
    assert answer.startswith('_PRINTLOG_RESPOND MSG="printlog-bridge: T2 is now Silk Gold')


def test_the_u1_reader_assigns_the_spool_with_the_tag_it_reads(tmp_path):
    moonraker, api = FakeMoonraker(objects=U1_OBJECTS), FakePrintLogApi()
    moonraker.status["filament_detect"] = copy.deepcopy(FILAMENT_DETECT)
    red = api.add_spool("Snapmaker PLA Red", tags=("04A23F11",))
    blue = api.add_spool("Snapmaker PLA Blue", tags=("0A0B0C0D",))

    async def scenario():
        async with bridge(tmp_path, moonraker, api):
            await eventually(lambda: api.slot_spools(42).get(0) == red)
            info = copy.deepcopy(FILAMENT_DETECT["info"])
            info[3] = {**SNAPMAKER_PLA_RED, "CARD_UID": [0x0A, 0x0B, 0x0C, 0x0D]}
            await moonraker.set_status(filament_detect={"info": info})  # a spool goes in
            await eventually(lambda: api.slot_spools(42).get(3) == blue)

    asyncio.run(scenario())

    assert api.slot_spools(42) == {0: red, 3: blue}


def test_a_u1_mismatch_adds_a_note_and_the_print_is_still_logged(tmp_path):
    moonraker, api = FakeMoonraker(objects=U1_OBJECTS), FakePrintLogApi()
    api.user_loads(42, None, api.add_spool("Basic PETG", material="PETG", color_hex="#E02020"))
    detect = copy.deepcopy(FILAMENT_DETECT)
    detect["info"][0]["CARD_UID"] = [0xDE, 0xAD, 0xBE, 0xEF]  # a tag no spool of theirs has
    moonraker.status["filament_detect"] = detect

    async def scenario():
        async with bridge(tmp_path, moonraker, api):
            await eventually(lambda: ("GET", "/api/Filaments/by-tag/DEADBEEF") in api.requests)
            job = await moonraker.start_job("u1/vase.gcode", METADATA)
            await eventually(lambda: api.prints)
            await moonraker.finish_job(job, filament_used=4321.5)
            await eventually(lambda: api.prints[1]["status"] != 2)

    asyncio.run(scenario())

    print_ = api.prints[1]
    assert print_["status"] == 3
    assert "PLA" in print_["notes"]
    assert "Basic PETG" in print_["notes"]


def test_an_empty_u1_slot_changes_nothing(tmp_path):
    moonraker, api = FakeMoonraker(objects=U1_OBJECTS), FakePrintLogApi()
    spool = api.add_spool()
    api.user_loads(42, None, spool)
    moonraker.status["filament_detect"] = {"info": [EMPTY_CHANNEL] * 4}

    async def scenario():
        async with bridge(tmp_path, moonraker, api):
            await asyncio.sleep(0.3)

    asyncio.run(scenario())

    assert api.slot_spools(42) == {None: spool}
    assert not [r for r in api.requests if "/slots/" in r[1] or "by-tag" in r[1]]


def test_a_failed_read_at_create_doesnt_load_a_spool_unloaded_in_the_app(tmp_path):
    """On a single-tool printer, creating a print makes its spools the printer's loaded ones.
    The bridge read the printer with a spool loaded, the user unloaded it in the app (within the
    five minutes before the next read), and the read when the print is created fails: the print
    waits for a read that works, rather than loading that spool again from the older one."""
    from test_spool_swaps import FAST, fakes, feed, logged, printing, running

    moonraker, api = FakeMoonraker(), FakePrintLogApi()
    spool = api.add_spool("Unloaded in the app")
    api.user_loads(42, 0, spool)
    real = api._printer
    reads = {"n": 0}

    async def printer(request):
        reads["n"] += 1
        if reads["n"] == 1:  # the bridge's first read, as it starts
            response = await real(request)
            api.loaded[42].clear()  # and right after it, the user unloads the spool in the app
            return response
        if reads["n"] == 2:  # the read when the print is created
            return web.Response(status=503)
        return await real(request)

    api._printer = printer

    async def scenario():
        async with (
            fakes(tmp_path, moonraker, api) as config,
            running(config, moonraker, backoff=FAST),
        ):
            await eventually(lambda: reads["n"] >= 1)
            job = await moonraker.start_job("vase.gcode", METADATA)
            await eventually(lambda: api.prints)
            await printing(moonraker, "vase.gcode")
            await feed(moonraker, 4400.0)
            await moonraker.finish_job(job, filament_used=4400.0)
            await eventually(lambda: logged(tmp_path))

    asyncio.run(scenario())

    assert reads["n"] >= 3  # the failed read, then one that worked before the create
    assert api.slot_spools(42) == {}  # still unloaded
    [print_] = api.prints.values()
    assert [row.get("filament") for row in print_["filamentUsage"]] == [None]
