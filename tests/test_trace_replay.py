"""A recorded print, replayed: the trace of a session becomes a regression test (#17).

`record_u1_print` records a SYNTHETIC trace: the bridge, with `trace = true`, watches the fake
Moonraker set up as the real U1 capture (its objects, and the two Snapmaker spools its RFID
reader saw), through a three-tool print. Replaying that trace into a fresh bridge must log the
same print to 3D Print Log. A real printer's trace is replayed the same way.

To regenerate the committed trace (after a change to what the bridge asks Moonraker):

    PYTHONPATH=src python tests/test_trace_replay.py \
        tests/fixtures/traces/SYNTHETIC-u1-three-tool-print.jsonl
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import bridge_helpers as helpers
from captures import U1, job, moonraker_like
from fake_moonraker import eventually
from fake_printlog_api import FakePrintLogApi
from trace_replay import ReplayMoonraker, read_trace, replay

from printlog_bridge.core.trace import trace_files

COMMITTED = Path(__file__).resolve().parent / "fixtures" / "traces"
U1_PRINT = COMMITTED / "SYNTHETIC-u1-three-tool-print.jsonl"
FILE = "Chillet_PLA_9h38m.gcode"
FED = {0: 1000.0, 1: 2000.0, 2: 500.0}
"""Millimetres each tool feeds in the print."""


def spools(api: FakePrintLogApi) -> dict[str, str]:
    """The user's spools for the two tags the U1 capture's reader saw, by name."""
    return {
        "white": api.add_spool("PLA SnapSpeed White", color_hex="#E2DEDB", tags=("A05D1C3F",)),
        "pink": api.add_spool("PLA Full Spectrum Pink", color_hex="#D93B90", tags=("909DB5E2",)),
    }


async def record_u1_print(tmp_path: Path) -> tuple[Path, FakePrintLogApi]:
    """The trace of a whole three-tool print on a U1, and the fake API it was logged to."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    moonraker, api = moonraker_like(U1), FakePrintLogApi()
    spools(api)
    moonraker_url, api_url = await moonraker.start(), await api.start()
    try:
        async with helpers.bridge(
            helpers.write_config(tmp_path, moonraker_url, api_url, trace=True),
            connected=lambda: moonraker.client_count == 1,
            disconnected=lambda: moonraker.client_count == 0,
        ):
            await eventually(lambda: len(api.slot_spools(42)) == 2)
            started = await moonraker.start_job(FILE, job(U1, "00000C")["metadata"])
            await eventually(lambda: api.prints)
            used = 0.0
            await moonraker.set_status(
                print_stats={"state": "printing", "filename": FILE, "filament_used": used}
            )
            for slot, mm in FED.items():
                await moonraker.set_status(
                    toolhead={"extruder": f"extruder{slot or ''}"},
                    print_stats={"filament_used": used},
                )
                used += mm
                await moonraker.set_status(print_stats={"filament_used": used})
            await moonraker.set_status(print_stats={"state": "complete"})
            await moonraker.finish_job(started, filament_used=used, print_duration=1200.0)
            await eventually(lambda: api.prints[1]["status"] == 3)
    finally:
        await moonraker.close()
        await api.close()
    [trace] = trace_files(tmp_path / "state", "voron")
    return trace, api


def logged(api: FakePrintLogApi) -> dict[str, Any]:
    """The print as 3D Print Log has it, with spools by name, not by (random) id."""
    names = {i: f["displayName"] for i, f in api.filaments.items()}
    [print_] = api.prints.values()
    [completion] = api.completions
    return {
        "fileName": print_["fileName"],
        "status": print_["status"],
        "estimated_mg": {
            r["slot"]: r["estimatedAmountMg"]
            for r in print_["filamentUsage"]
            if r.get("estimatedAmountMg")
        },
        "completed": {
            r["slot"]: (round(r["lengthInM"], 3), names.get(r.get("filamentId")))
            for r in completion["filamentUsage"]
        },
        "slots": {slot: names[i] for slot, i in api.slot_spools(42).items()},
    }


EXPECTED = {
    "fileName": FILE,
    "status": 3,  # completed
    "estimated_mg": {0: 54220, 1: 67090, 2: 20690},  # the slicer's grams per tool, from the U1
    "completed": {
        0: (1.0, "PLA SnapSpeed White"),
        1: (2.0, "PLA Full Spectrum Pink"),
        2: (0.5, None),  # nothing loaded in T2: the U1's reader saw no spool there
    },
    "slots": {0: "PLA SnapSpeed White", 1: "PLA Full Spectrum Pink"},
}


async def replayed(trace: Path, tmp_path: Path) -> dict[str, Any]:
    api = FakePrintLogApi()
    spools(api)
    await replay(
        trace,
        tmp_path,
        api,
        until=lambda: api.prints and api.prints[1]["status"] == 3,
        ready=lambda: len(api.slot_spools(42)) == 2,
    )
    return logged(api)


def test_recording_a_print_logs_it(tmp_path):
    _, api = asyncio.run(record_u1_print(tmp_path))

    assert logged(api) == EXPECTED


def test_a_recorded_print_replayed_logs_the_same_print(tmp_path):
    async def scenario():
        trace, _ = await record_u1_print(tmp_path / "recorded")
        return await replayed(trace, tmp_path / "replayed")

    assert asyncio.run(scenario()) == EXPECTED


def test_the_committed_u1_trace_replays(tmp_path):
    assert asyncio.run(replayed(U1_PRINT, tmp_path)) == EXPECTED


def test_a_trace_is_read_as_answers_by_method_and_notifications_in_order():
    moonraker = ReplayMoonraker(
        [
            {"t": 1.0, "dir": "open"},
            {"t": 1.0, "dir": "send", "msg": {"method": "server.info", "id": 1}},
            {"t": 1.1, "dir": "recv", "msg": {"method": "notify_proc_stat_update"}},
            {"t": 1.2, "dir": "recv", "msg": {"result": {"klippy_state": "ready"}, "id": 1}},
            {"t": 2.0, "dir": "send", "msg": {"method": "server.history.list", "id": 2}},
            {"t": 2.1, "dir": "recv", "msg": {"error": {"code": -32601}, "id": 2}},
            {"t": 3.0, "dir": "close"},
            {"t": 4.0, "dir": "open"},  # ids start again on a new connection
            {"t": 4.0, "dir": "send", "msg": {"method": "server.info", "id": 1}},
            {"t": 4.1, "dir": "recv", "msg": {"result": {"klippy_state": "startup"}, "id": 1}},
        ]
    )

    assert [a["result"]["klippy_state"] for a in moonraker.answers["server.info"]] == [
        "ready",
        "startup",
    ]
    assert moonraker.answers["server.history.list"][0]["error"]["code"] == -32601
    assert [n["method"] for n in moonraker.notifications] == ["notify_proc_stat_update"]


def test_the_committed_trace_is_a_trace():
    lines = read_trace(U1_PRINT)

    assert lines[0]["dir"] == "open"
    assert {line["dir"] for line in lines} == {"open", "send", "recv", "close"}


if __name__ == "__main__":
    out = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as folder:
        recorded, _ = asyncio.run(record_u1_print(Path(folder)))
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(recorded, out)
    print(f"wrote {out}")
