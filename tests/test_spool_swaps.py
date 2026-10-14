"""The whole bridge, in process: #44's acceptance criteria against the two fakes.

A spool changes partway through a print (a runout, an M600 color change, PRINTLOG_SET_SPOOL
while paused, a new tag in a U1's slot), and the tool's usage is split between the two spools at
the point of the change.

Every wait here is on something the bridge did (an answer on the console, a swap in its state
file, a request to the fake API), never on time alone, so the steps happen in the order written.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bridge_helpers as helpers
import pytest
from bridge_helpers import write_config
from fake_moonraker import DEFAULT_OBJECTS, FakeMoonraker, eventually
from fake_printlog_api import FakePrintLogApi
from test_moonraker_spools import FILAMENT_DETECT, SNAPMAKER_PLA_RED

from printlog_bridge.adapters.moonraker.changes import FilamentChanges, sensor_slot
from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.events import (
    FilamentChange,
    JobFinished,
    JobOutcome,
    JobPoint,
    SpoolCommand,
)
from printlog_bridge.core.lifecycle import _usage_rows
from printlog_bridge.core.spools import Spools
from printlog_bridge.core.swaps import SpoolSwap, change_hints, swap_notes

METADATA = {"estimated_time": 3900.0, "filament_total": 5000.0, "filament_weight_total": 15.0}
RESPOND_MACRO = "gcode_macro _PRINTLOG_RESPOND"
RUNOUT_SENSOR = "filament_switch_sensor runout"
U1_OBJECTS = (*DEFAULT_OBJECTS, "extruder2", "extruder3", "filament_detect", RESPOND_MACRO)
HINT = "T0 was paused for a filament change; run PRINTLOG_SET_SPOOL during the pause to split usage"


@contextlib.asynccontextmanager
async def running(config: Path, moonraker: FakeMoonraker):
    async with helpers.bridge(
        config,
        connected=lambda: moonraker.client_count == 1,
        disconnected=lambda: moonraker.client_count == 0,
    ) as task:
        await eventually(lambda: moonraker.subscriptions)
        yield task


@contextlib.asynccontextmanager
async def fakes(tmp_path: Path, moonraker: FakeMoonraker, api: FakePrintLogApi):
    moonraker_url, api_url = await moonraker.start(), await api.start()
    try:
        yield write_config(tmp_path, moonraker_url, api_url)
    finally:
        await moonraker.close()
        await api.close()


def recorded(tmp_path: Path, key: str = "swaps") -> list[dict[str, Any]]:
    """What the bridge's state file holds for its jobs under `key`, all jobs together."""
    path = tmp_path / "state" / "printers" / "voron.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for job in data["jobs"].values() for item in job.get(key) or []]


async def feed(moonraker: FakeMoonraker, used: float, tool: str = "extruder", **more) -> None:
    """Klipper's filament count grows to `used` with `tool` active."""
    await moonraker.set_status(
        toolhead={"extruder": tool}, print_stats={"filament_used": used}, **more
    )


async def printing(moonraker: FakeMoonraker, filename: str) -> None:
    await moonraker.set_status(
        print_stats={"state": "printing", "filename": filename, "filament_used": 0.0}
    )


async def pause(moonraker: FakeMoonraker, progress: float, layer: int) -> None:
    await moonraker.set_status(
        print_stats={"state": "paused", "info": {"current_layer": layer, "total_layer": 200}},
        virtual_sdcard={"progress": progress},
    )


async def resume(moonraker: FakeMoonraker) -> None:
    await moonraker.set_status(print_stats={"state": "printing"})


def rows_on(print_: dict[str, Any], slot: int) -> dict[str | None, dict[str, Any]]:
    """The print's usage rows for `slot`, by spool id."""
    return {
        (r["filament"] or {}).get("id"): r for r in print_["filamentUsage"] if r["slot"] == slot
    }


def test_set_spool_during_a_pause_splits_the_tool_between_the_two_spools(tmp_path):
    moonraker, api = FakeMoonraker(), FakePrintLogApi()
    first = api.add_spool("Spool A")
    second = api.add_spool("Spool B")
    api.user_loads(42, 0, first)

    async def scenario():
        async with fakes(tmp_path, moonraker, api) as config, running(config, moonraker):
            job = await moonraker.start_job("vase.gcode", METADATA)
            await eventually(lambda: api.prints)
            await printing(moonraker, "vase.gcode")
            await feed(moonraker, 3150.0)
            await pause(moonraker, 0.63, 112)
            await moonraker.respond(f"// printlog: SET_SPOOL TOOL=0 ID={second}")
            await eventually(lambda: moonraker.scripts)
            await resume(moonraker)
            await feed(moonraker, 5000.0)
            await moonraker.finish_job(job, filament_used=5000.0)
            await eventually(lambda: api.prints[1]["status"] != 2)

    asyncio.run(scenario())

    print_ = api.prints[1]
    rows = rows_on(print_, 0)
    assert set(rows) == {first, second}
    assert rows[first]["lengthInM"] == pytest.approx(3.15)
    assert rows[second]["lengthInM"] == pytest.approx(1.85)
    assert rows[first]["lengthInM"] + rows[second]["lengthInM"] == pytest.approx(5.0)
    assert rows[first]["estimatedLengthInM"] == 5.0  # the estimate stays on the original row
    assert rows[second].get("estimatedLengthInM") is None
    assert "T0: Spool A → Spool B at 63% (layer 112)" in print_["notes"]
    [answer] = moonraker.scripts
    assert "T0 is now Spool B" in answer
    assert "split" in answer
    assert api.slot_spools(42) == {0: second}


def test_a_runout_pause_resumed_without_the_macro_keeps_one_spool_and_adds_a_hint(tmp_path):
    moonraker = FakeMoonraker(objects=(*DEFAULT_OBJECTS, RUNOUT_SENSOR, RESPOND_MACRO))
    moonraker.status[RUNOUT_SENSOR] = {"filament_detected": True, "enabled": True}
    api = FakePrintLogApi()
    spool = api.add_spool("Spool A")
    api.user_loads(42, 0, spool)

    async def scenario():
        async with fakes(tmp_path, moonraker, api) as config, running(config, moonraker):
            job = await moonraker.start_job("vase.gcode", METADATA)
            await eventually(lambda: api.prints)
            await printing(moonraker, "vase.gcode")
            await feed(moonraker, 3000.0)
            await moonraker.set_status(**{RUNOUT_SENSOR: {"filament_detected": False}})
            await pause(moonraker, 0.6, 100)
            await eventually(lambda: moonraker.scripts)  # the bridge prompts for the macro
            await moonraker.set_status(**{RUNOUT_SENSOR: {"filament_detected": True}})
            await resume(moonraker)
            await eventually(lambda: recorded(tmp_path, "filament_changes"))
            await feed(moonraker, 5000.0)
            await moonraker.finish_job(job, filament_used=5000.0)
            await eventually(lambda: api.prints[1]["status"] != 2)

    asyncio.run(scenario())

    print_ = api.prints[1]
    [row] = print_["filamentUsage"]
    assert row["filament"] == {"id": spool}
    assert row["lengthInM"] == pytest.approx(5.0)
    assert HINT in print_["notes"]
    [prompt] = moonraker.scripts
    assert "Spool ran out on T0. Run PRINTLOG_SET_SPOOL to log the new one." in prompt


def test_a_runout_with_the_macro_during_the_pause_splits_and_has_no_hint(tmp_path):
    moonraker = FakeMoonraker(objects=(*DEFAULT_OBJECTS, RUNOUT_SENSOR, RESPOND_MACRO))
    moonraker.status[RUNOUT_SENSOR] = {"filament_detected": True, "enabled": True}
    api = FakePrintLogApi()
    first, second = api.add_spool("Spool A"), api.add_spool("Spool B")
    api.user_loads(42, 0, first)

    async def scenario():
        async with fakes(tmp_path, moonraker, api) as config, running(config, moonraker):
            job = await moonraker.start_job("vase.gcode", METADATA)
            await eventually(lambda: api.prints)
            await printing(moonraker, "vase.gcode")
            await feed(moonraker, 3000.0)
            await moonraker.set_status(**{RUNOUT_SENSOR: {"filament_detected": False}})
            await pause(moonraker, 0.6, 100)
            await eventually(lambda: moonraker.scripts)
            await moonraker.respond(f"// printlog: SET_SPOOL ID={second}")
            await eventually(lambda: len(moonraker.scripts) == 2)
            await moonraker.set_status(**{RUNOUT_SENSOR: {"filament_detected": True}})
            await resume(moonraker)
            await eventually(lambda: recorded(tmp_path, "filament_changes"))
            await feed(moonraker, 5000.0)
            await moonraker.finish_job(job, filament_used=5000.0)
            await eventually(lambda: api.prints[1]["status"] != 2)

    asyncio.run(scenario())

    print_ = api.prints[1]
    rows = rows_on(print_, 0)
    assert rows[first]["lengthInM"] == pytest.approx(3.0)
    assert rows[second]["lengthInM"] == pytest.approx(2.0)
    assert "paused for a filament change" not in print_["notes"]


def test_a_u1_tag_change_mid_print_splits_the_same_way(tmp_path):
    moonraker, api = FakeMoonraker(objects=U1_OBJECTS), FakePrintLogApi()
    moonraker.status["filament_detect"] = copy.deepcopy(FILAMENT_DETECT)
    red = api.add_spool("Snapmaker PLA Red", tags=("04A23F11",))
    blue = api.add_spool("Snapmaker PLA Blue", tags=("0A0B0C0D",))

    async def scenario():
        async with fakes(tmp_path, moonraker, api) as config, running(config, moonraker):
            await eventually(lambda: api.slot_spools(42).get(0) == red)
            job = await moonraker.start_job("u1/vase.gcode", METADATA)
            await eventually(lambda: api.prints)
            await printing(moonraker, "u1/vase.gcode")
            await feed(moonraker, 2000.0)
            await pause(moonraker, 0.4, 80)
            info = copy.deepcopy(FILAMENT_DETECT["info"])
            info[0] = {**SNAPMAKER_PLA_RED, "CARD_UID": [0x0A, 0x0B, 0x0C, 0x0D], "RGB_1": 0x2020E0}
            await moonraker.set_status(filament_detect={"info": info})  # the new spool goes in
            await eventually(lambda: recorded(tmp_path))
            await resume(moonraker)
            await feed(moonraker, 3000.0)
            await moonraker.finish_job(job, filament_used=3000.0)
            await eventually(lambda: api.prints[1]["status"] != 2)

    asyncio.run(scenario())

    print_ = api.prints[1]
    rows = rows_on(print_, 0)
    assert set(rows) == {red, blue}
    assert rows[red]["lengthInM"] == pytest.approx(2.0)
    assert rows[blue]["lengthInM"] == pytest.approx(1.0)
    assert "T0: Snapmaker PLA Red → Snapmaker PLA Blue at 40% (layer 80)" in print_["notes"]


def test_a_restart_between_the_swap_and_the_finish_keeps_the_split(tmp_path):
    moonraker, api = FakeMoonraker(), FakePrintLogApi()
    first, second = api.add_spool("Spool A"), api.add_spool("Spool B")
    api.user_loads(42, 0, first)

    async def scenario():
        async with fakes(tmp_path, moonraker, api) as config:
            async with running(config, moonraker):
                job = await moonraker.start_job("vase.gcode", METADATA)
                await eventually(lambda: api.prints)
                await printing(moonraker, "vase.gcode")
                await feed(moonraker, 3000.0)
                await pause(moonraker, 0.6, 100)
                await moonraker.respond(f"// printlog: SET_SPOOL TOOL=0 ID={second}")
                await eventually(lambda: moonraker.scripts)
            await resume(moonraker)  # while the bridge is stopped
            moonraker.status["print_stats"]["filament_used"] = 4500.0
            async with running(config, moonraker):
                await feed(moonraker, 5000.0)
                await moonraker.finish_job(job, filament_used=5000.0)
                await eventually(lambda: api.prints[1]["status"] != 2)

    asyncio.run(scenario())

    rows = rows_on(api.prints[1], 0)
    assert rows[first]["lengthInM"] == pytest.approx(3.0)
    assert rows[second]["lengthInM"] == pytest.approx(2.0)
    assert len(api.completions) == 1


def test_a_multi_tool_printer_splits_each_tool_on_its_own(tmp_path):
    moonraker, api = FakeMoonraker(), FakePrintLogApi()
    api.slot_counts[42] = 2
    a, b, c, d = (api.add_spool(f"Spool {name}") for name in "ABCD")
    api.user_loads(42, 0, a)
    api.user_loads(42, 1, b)
    metadata = {**METADATA, "filament_weights": [10.0, 5.0]}

    async def swap(command: str) -> None:
        answers = len(moonraker.scripts)
        await moonraker.respond(command)
        await eventually(lambda: len(moonraker.scripts) > answers)

    async def scenario():
        async with fakes(tmp_path, moonraker, api) as config, running(config, moonraker):
            job = await moonraker.start_job("two.gcode", metadata)
            await eventually(lambda: api.prints)
            await printing(moonraker, "two.gcode")
            await feed(moonraker, 2000.0, "extruder")  # T0 2000
            await pause(moonraker, 0.3, 30)
            await swap(f"// printlog: SET_SPOOL TOOL=0 ID={d}")
            await resume(moonraker)
            await feed(moonraker, 2000.0, "extruder1")
            await feed(moonraker, 3000.0, "extruder1")  # T1 1000
            await pause(moonraker, 0.6, 60)
            await swap(f"// printlog: SET_SPOOL TOOL=1 ID={c}")
            await resume(moonraker)
            await feed(moonraker, 3500.0, "extruder1")  # T1 1500
            await feed(moonraker, 3500.0, "extruder")
            await feed(moonraker, 5000.0, "extruder")  # T0 3500
            await moonraker.finish_job(job, filament_used=5000.0)
            await eventually(lambda: api.prints[1]["status"] != 2)

    asyncio.run(scenario())

    print_ = api.prints[1]
    tool_0, tool_1 = rows_on(print_, 0), rows_on(print_, 1)
    assert tool_0[a]["lengthInM"] == pytest.approx(2.0)
    assert tool_0[d]["lengthInM"] == pytest.approx(1.5)
    assert tool_1[b]["lengthInM"] == pytest.approx(1.0)
    assert tool_1[c]["lengthInM"] == pytest.approx(0.5)
    assert sum(r["lengthInM"] for r in print_["filamentUsage"]) == pytest.approx(5.0)
    assert "T0: Spool A → Spool D at 30% (layer 30)" in print_["notes"]
    assert "T1: Spool B → Spool C at 60% (layer 60)" in print_["notes"]


def test_set_spool_between_prints_splits_nothing(tmp_path):
    moonraker, api = FakeMoonraker(), FakePrintLogApi()
    first, second = api.add_spool("Spool A"), api.add_spool("Spool B")
    api.user_loads(42, 0, first)

    async def scenario():
        async with fakes(tmp_path, moonraker, api) as config, running(config, moonraker):
            await moonraker.respond(f"// printlog: SET_SPOOL TOOL=0 ID={second}")
            await eventually(lambda: moonraker.scripts)
            job = await moonraker.start_job("vase.gcode", METADATA)
            await eventually(lambda: api.prints)
            await printing(moonraker, "vase.gcode")
            await feed(moonraker, 5000.0)
            await moonraker.finish_job(job, filament_used=5000.0)
            await eventually(lambda: api.prints[1]["status"] != 2)

    asyncio.run(scenario())

    print_ = api.prints[1]
    [row] = print_["filamentUsage"]
    assert row["filament"] == {"id": second}
    assert not print_.get("notes")
    assert "split" not in moonraker.scripts[0]


# -- the pieces on their own ---------------------------------------------------------------------


def finished(filament_mm: dict[int, float]) -> JobFinished:
    return JobFinished(
        "voron", "000001", JobOutcome.COMPLETED, datetime(2026, 10, 14, tzinfo=timezone.utc),
        filament_mm=filament_mm,
    )  # fmt: skip


def swap(slot: int, fed_mm: float, old: str | None, new: str | None, **more) -> dict[str, Any]:
    return {"slot": slot, "fed_mm": fed_mm, "old": old, "new": new, **more}


class TestUsageRows:
    def test_without_swaps_nothing_changes(self):
        rows = [{"slot": 0, "filament": {"id": "a"}}]

        assert _usage_rows(finished({0: 5000.0}), rows, {0: "b"}) == [
            {"slot": 0, "lengthInM": 5.0, "source": 2, "filamentId": "a"}
        ]

    def test_the_old_spool_comes_first_so_it_keeps_the_estimate_row(self):
        rows = [{"slot": 0, "filament": {"id": "a"}, "estimatedLengthInM": 5.0}]

        usage = _usage_rows(finished({0: 5000.0}), rows, {0: "b"}, True, [swap(0, 3000, "a", "b")])

        assert usage == [
            {"slot": 0, "lengthInM": 3.0, "source": 2, "filamentId": "a"},
            {"slot": 0, "lengthInM": 2.0, "source": 2, "filamentId": "b"},
        ]

    def test_with_no_spool_before_the_swap_the_first_part_has_none(self):
        """What's loaded now is the new spool, which mustn't take the part before it."""
        usage = _usage_rows(finished({0: 5000.0}), [{"slot": 0}], {0: "b"}, True,
                            [swap(0, 1000, None, "b")])  # fmt: skip

        assert usage == [
            {"slot": 0, "lengthInM": 1.0, "source": 2},
            {"slot": 0, "lengthInM": 4.0, "source": 2, "filamentId": "b"},
        ]

    def test_the_spool_linked_on_the_print_is_the_first_part(self):
        rows = [{"slot": 0, "filament": {"id": "mine"}}]

        usage = _usage_rows(finished({0: 5000.0}), rows, {0: "b"}, True, [swap(0, 3000, "a", "b")])

        assert [(r.get("filamentId"), r["lengthInM"]) for r in usage] == [("mine", 3.0), ("b", 2.0)]

    def test_a_spool_back_in_after_another_gets_one_row_with_both_parts(self):
        swaps = [swap(0, 1000, "a", "b"), swap(0, 3000, "b", "a")]

        usage = _usage_rows(finished({0: 5000.0}), [], {0: "a"}, True, swaps)

        assert [(r["filamentId"], r["lengthInM"]) for r in usage] == [("a", 3.0), ("b", 2.0)]

    def test_two_parts_with_no_spool_on_a_slot_are_one_row(self):
        swaps = [swap(0, 1000, None, "b"), swap(0, 3000, "b", None)]

        usage = _usage_rows(finished({0: 5000.0}), [], {}, True, swaps)

        assert [(r.get("filamentId"), r["lengthInM"]) for r in usage] == [(None, 3.0), ("b", 2.0)]

    def test_a_swap_past_what_the_slot_fed_gives_the_new_spool_nothing(self):
        usage = _usage_rows(finished({0: 2000.0}), [], {}, True, [swap(0, 2500, "a", "b")])

        assert usage == [{"slot": 0, "lengthInM": 2.0, "source": 2, "filamentId": "a"}]

    def test_the_parts_always_add_up_to_the_slot(self):
        swaps = [swap(0, 1234.5, "a", "b"), swap(0, 2222.2, "b", "c")]

        usage = _usage_rows(finished({0: 4321.0}), [], {}, True, swaps)

        assert sum(r["lengthInM"] for r in usage) == pytest.approx(4.321, abs=0.0002)

    def test_each_tool_is_split_on_its_own(self):
        rows = [{"slot": 0, "filament": {"id": "a"}}, {"slot": 1, "filament": {"id": "b"}}]
        swaps = [swap(1, 500, "b", "c")]

        usage = _usage_rows(finished({0: 3000.0, 1: 2000.0}), rows, {0: "a", 1: "c"}, False, swaps)

        assert [(r["slot"], r["filamentId"], r["lengthInM"]) for r in usage] == [
            (0, "a", 3.0), (1, "b", 0.5), (1, "c", 1.5),
        ]  # fmt: skip


class TestNotes:
    def test_a_swap_names_both_spools_and_where(self):
        record = swap(0, 10, "a", "b", old_name="Spool A", new_name="Spool B",
                      progress=0.634, layer=112)  # fmt: skip

        assert swap_notes([record]) == ["T0: Spool A → Spool B at 63% (layer 112)"]

    def test_a_slot_left_without_a_spool_says_so(self):
        record = swap(1, 10, "a", None, old_name="Spool A", progress=None, layer=None)

        assert swap_notes([record]) == ["T1: Spool A → no spool"]

    def test_a_filament_change_no_swap_followed_gets_the_hint(self):
        changes = [{"slot": 0, "fed_mm": 3000.0, "reason": "runout"}]

        assert change_hints(changes, []) == [HINT]
        assert change_hints(changes, [swap(0, 3000.0, "a", "b")]) == []
        assert change_hints(changes, [swap(0, 3500.0, "a", "b")]) == []  # just after resuming
        assert change_hints(changes, [swap(0, 1000.0, "a", "b")]) == [HINT]  # an earlier swap
        assert change_hints(changes, [swap(1, 3000.0, "a", "b")]) == [HINT]  # another tool


def status(state: str, *, e: float | None = None, **objects) -> dict[str, Any]:
    out: dict[str, Any] = {"print_stats": {"state": state}, **objects}
    if e is not None:
        out["toolhead"] = {"position": [0.0, 0.0, 10.0, e]}
    return out


def point() -> JobPoint:
    return JobPoint("000001", {0: 3000.0}, 0.6, 100)


class TestFilamentChanges:
    def run(self, *updates: dict[str, Any]) -> tuple[list[str], list[FilamentChange]]:
        changes = FilamentChanges("voron")
        prompts, found = [], []
        for update in updates:
            more, change = changes.update(update, point, 0)
            prompts += more
            if change is not None:
                found.append(change)
        return prompts, found

    def test_an_unload_while_paused_is_a_filament_change_once_resumed(self):
        prompts, found = self.run(
            status("printing", e=500.0), status("paused", e=499.0),
            status("paused", e=470.0), status("paused", e=440.0),
            status("paused", e=480.0), status("printing", e=480.0),
        )  # fmt: skip

        assert found == [FilamentChange("voron", 0, point(), "unload")]
        assert prompts == [
            "Filament unloaded from T0. Run PRINTLOG_SET_SPOOL to log the new spool."
        ]

    def test_a_pause_that_only_retracts_a_little_is_not(self):
        prompts, found = self.run(
            status("printing", e=500.0), status("paused", e=495.0), status("paused", e=490.0),
            status("printing", e=500.0),
        )  # fmt: skip

        assert (prompts, found) == ([], [])

    def test_a_job_cancelled_while_paused_reports_nothing(self):
        prompts, found = self.run(
            status("printing", e=500.0), status("paused", e=500.0), status("paused", e=400.0),
            status("cancelled", e=400.0), status("printing", e=0.0),
        )  # fmt: skip

        assert len(prompts) == 1
        assert found == []

    def test_a_sensor_with_no_filament_between_jobs_is_not_a_runout(self):
        sensor = "filament_switch_sensor runout"
        prompts, found = self.run(
            status("standby", **{sensor: {"filament_detected": True, "enabled": True}}),
            status("standby", **{sensor: {"filament_detected": False, "enabled": True}}),
        )

        assert (prompts, found) == ([], [])

    def test_a_disabled_sensor_is_not_a_runout(self):
        sensor = "filament_motion_sensor encoder"
        prompts, found = self.run(
            status("printing", **{sensor: {"filament_detected": True, "enabled": False}}),
            status("printing", **{sensor: {"filament_detected": False, "enabled": False}}),
            status("paused"), status("printing"),
        )  # fmt: skip

        assert (prompts, found) == ([], [])

    def test_a_motion_sensor_runout_is_a_filament_change(self):
        sensor = "filament_motion_sensor encoder"
        prompts, found = self.run(
            status("printing", **{sensor: {"filament_detected": True, "enabled": True}}),
            status("printing", **{sensor: {"filament_detected": False, "enabled": True}}),
            status("paused", **{sensor: {"filament_detected": False, "enabled": True}}),
            status("printing", **{sensor: {"filament_detected": True, "enabled": True}}),
        )

        assert [c.reason for c in found] == ["runout"]
        assert prompts == ["Spool ran out on T0. Run PRINTLOG_SET_SPOOL to log the new one."]


class TestSensorSlot:
    @pytest.mark.parametrize(
        ("name", "slot"),
        [
            ("filament_motion_sensor e0_filament", 0),  # the Snapmaker U1's
            ("filament_motion_sensor e3_filament", 3),
            ("filament_switch_sensor extruder1", 1),
            ("filament_switch_sensor T2_runout", 2),
            ("filament_switch_sensor runout", None),
            ("filament_switch_sensor encoder2", None),
            ("filament_switch_sensor e1x", None),
        ],
    )
    def test_a_sensor_named_for_a_tool_is_that_tools(self, name, slot):
        assert sensor_slot(name) == slot

    def test_a_runout_on_a_tools_own_sensor_is_on_that_tool(self):
        sensor = "filament_motion_sensor e2_filament"
        changes = FilamentChanges("u1")
        found = []
        for update in (
            status("printing", **{sensor: {"filament_detected": True, "enabled": True}}),
            status("printing", **{sensor: {"filament_detected": False, "enabled": True}}),
            status("paused"),
            status("printing"),
        ):
            prompts, change = changes.update(update, point, 0)
            found += [change] if change else []

        assert [c.slot for c in found] == [2]


class TestSwapHook:
    def test_a_spool_set_while_a_job_runs_is_a_swap_and_the_answer_says_so(self):
        api = FakePrintLogApi()
        first, second = api.add_spool("Spool A"), api.add_spool("Spool B")
        api.user_loads(42, 0, first)
        heard: list[SpoolSwap] = []

        async def scenario(spools):
            spools.on_swap = lambda s: heard.append(s) or True
            await spools.refresh("u1")
            return await spools.spool_command(
                SpoolCommand("u1", 0, filament_id=second, during=point())
            )

        answer = run_spools(api, scenario)

        assert heard == [SpoolSwap("u1", 0, first, second, "Spool A", "Spool B", point())]
        assert answer.endswith("This print's T0 usage is split here")

    def test_between_jobs_or_when_nothing_changed_there_is_none(self):
        api = FakePrintLogApi()
        first, second = api.add_spool("Spool A"), api.add_spool("Spool B")
        api.user_loads(42, 0, first)
        heard: list[SpoolSwap] = []

        async def scenario(spools):
            spools.on_swap = lambda s: heard.append(s) or True
            await spools.refresh("u1")
            await spools.spool_command(SpoolCommand("u1", 0, filament_id=second))
            await spools.spool_command(SpoolCommand("u1", 0, filament_id=second, during=point()))
            await spools.spool_command(SpoolCommand("u1", 0, tag="DEADBEEF", during=point()))

        run_spools(api, scenario)

        assert heard == []


def run_spools(api: FakePrintLogApi, scenario):
    async def main():
        base = await api.start()
        try:
            async with ApiClient(base, api.api_key) as client:
                return await scenario(Spools(client, {"u1": 42}))
        finally:
            await api.close()

    return asyncio.run(main())
