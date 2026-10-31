from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fake_moonraker import FakeMoonraker, eventually, gcode_metadata
from session_helpers import ReportRecorder, running

from printlog_bridge.adapters.moonraker.images import MoonrakerImages
from printlog_bridge.adapters.moonraker.jobs import JobTracker
from printlog_bridge.adapters.moonraker.session import Backoff, MoonrakerSession
from printlog_bridge.core.events import JobFinished, JobOutcome, JobSlicerSettings, JobStarted

FAST = Backoff(initial=0.01, maximum=0.05)
T0 = 1_791_000_000.0  # 2026-10-03, on the printer's clock


class RecordingSink:
    def __init__(self, resume: datetime | None = None) -> None:
        self.events: list = []
        self.resume = resume

    async def emit(self, event) -> None:
        if not isinstance(event, JobSlicerSettings):  # test_moonraker_slicer.py has those
            self.events.append(event)

    async def resume_after(self, printer: str) -> datetime | None:
        return self.resume

    def of(self, kind):
        return [e for e in self.events if isinstance(e, kind)]


def at(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, timezone.utc)


@contextlib.asynccontextmanager
async def tracking(sink: RecordingSink, fake: FakeMoonraker | None = None):
    fake = fake or FakeMoonraker()
    base = await fake.start()
    session = MoonrakerSession("voron", url=base, listener=JobTracker("voron", sink), backoff=FAST)
    try:
        async with running(session):
            await eventually(lambda: fake.subscriptions)
            yield fake
    finally:
        await fake.close()


class TestLiveJobs:
    def test_a_job_start_is_reported_with_the_slicers_estimates(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking(sink) as fake:
                await fake.start_job(
                    "parts/bracket.gcode",
                    {"estimated_time": 3900.5, "filament_total": 4400.0,
                     "filament_weight_total": 13.27},
                    start_time=T0,
                )  # fmt: skip
                await eventually(lambda: sink.events)
            return sink

        [event] = asyncio.run(scenario()).events

        assert event == JobStarted(
            printer="voron",
            job_id="000001",
            file_name="parts/bracket.gcode",
            started_at=at(T0),
            estimated_duration_s=3900.5,
            estimated_filament_mm=4400.0,
            estimated_filament_g=13.27,
        )

    def test_a_job_without_metadata_has_no_estimates(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking(sink) as fake:
                await fake.start_job("bracket.gcode", {})
                await eventually(lambda: sink.events)
            return sink

        [event] = asyncio.run(scenario()).events

        assert event.estimated_duration_s is None
        assert event.estimated_filament_mm is None
        assert event.estimated_filament_g is None

    def test_a_job_end_is_reported_with_what_moonraker_measured(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking(sink) as fake:
                job = await fake.start_job(start_time=T0)
                await fake.finish_job(
                    job, filament_used=4321.5, print_duration=3725.4, total_duration=3900.0
                )
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(scenario()).of(JobFinished)

        assert event == JobFinished(
            printer="voron",
            job_id="000001",
            outcome=JobOutcome.COMPLETED,
            finished_at=at(T0 + 3900.0),
            duration_s=3725.4,
            filament_mm={0: 4321.5},
            note=None,
        )

    @pytest.mark.parametrize(
        ("status", "outcome", "note"),
        [
            ("completed", JobOutcome.COMPLETED, None),
            ("cancelled", JobOutcome.CANCELLED, None),
            ("error", JobOutcome.FAILED, "Klipper error"),
            ("klippy_shutdown", JobOutcome.FAILED, "Klipper shutdown"),
            ("klippy_disconnect", JobOutcome.FAILED, "Lost connection to Klipper"),
            ("interrupted", JobOutcome.FAILED, "Moonraker stopped during the print"),
            ("server_exit", JobOutcome.FAILED, "Moonraker stopped during the print"),
            ("something_new", JobOutcome.FAILED, "Moonraker ended the job as something_new"),
        ],
    )
    def test_maps_moonrakers_status_to_an_outcome_and_note(self, status, outcome, note):
        async def scenario():
            sink = RecordingSink()
            async with tracking(sink) as fake:
                await fake.finish_job(await fake.start_job(), status)
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(scenario()).of(JobFinished)

        assert (event.outcome, event.note) == (outcome, note)

    def test_an_error_note_carries_klippers_last_message(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking(sink) as fake:
                job = await fake.start_job()
                await fake.set_status(print_stats={"state": "printing", "message": ""})
                await fake.set_status(
                    print_stats={"state": "error", "message": "Move out of range: 250.0 0.0"}
                )
                await fake.finish_job(job, "error")
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(scenario()).of(JobFinished)

        assert event.note == "Klipper error: Move out of range: 250.0 0.0"

    def test_an_old_error_message_is_not_pinned_on_a_later_job(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking(sink) as fake:
                await fake.set_status(print_stats={"state": "error", "message": "old trouble"})
                job = await fake.start_job()
                await fake.set_status(print_stats={"state": "printing", "message": ""})
                await fake.finish_job(job, "error")
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(scenario()).of(JobFinished)

        assert event.note == "Klipper error"

    def test_a_finish_also_reports_the_start_in_case_it_was_missed(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking(sink) as fake:
                job = await fake.start_job(notify=False)
                await fake.finish_job(job)
                await eventually(lambda: sink.of(JobFinished))
            return sink

        events = asyncio.run(scenario()).events

        assert [type(e) for e in events] == [JobStarted, JobFinished]


class TestCatchingUp:
    def test_replays_every_job_since_the_resume_point_in_order(self):
        """Jobs that started, or ended, while the bridge was down."""

        async def scenario():
            fake = FakeMoonraker()
            before = await fake.start_job("old.gcode", notify=False, start_time=T0 - 3600)
            await fake.finish_job(before, notify=False)
            done = await fake.start_job("done.gcode", notify=False, start_time=T0)
            await fake.finish_job(done, "cancelled", notify=False)
            await fake.start_job("running.gcode", notify=False, start_time=T0 + 3600)

            sink = RecordingSink(resume=at(T0))
            async with tracking(sink, fake):
                await eventually(lambda: len(sink.events) == 3)
            return sink

        events = asyncio.run(scenario()).events

        assert [(type(e).__name__, e.job_id) for e in events] == [
            ("JobStarted", "000002"),
            ("JobFinished", "000002"),
            ("JobStarted", "000003"),
        ]
        assert events[1].outcome is JobOutcome.CANCELLED

    def test_an_interrupted_job_is_reported_failed_after_moonraker_restarts(self):
        async def scenario():
            fake = FakeMoonraker()
            sink = RecordingSink()
            async with tracking(sink, fake):
                job = await fake.start_job(start_time=T0)
                await eventually(lambda: sink.events)
                sink.resume = at(T0)
                # Moonraker restarts mid-print and marks the job interrupted, announcing nothing.
                await fake.finish_job(job, "interrupted", notify=False)
                await fake.drop()
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(scenario()).of(JobFinished)

        assert event.outcome is JobOutcome.FAILED
        assert event.note == "Moonraker stopped during the print"

    def test_reads_a_long_history_page_by_page(self):
        async def scenario():
            fake = FakeMoonraker()
            for n in range(120):
                await fake.finish_job(
                    await fake.start_job(notify=False, start_time=T0 + n), notify=False
                )
            sink = RecordingSink(resume=at(T0))
            async with tracking(sink, fake):
                await eventually(lambda: len(sink.of(JobFinished)) == 120)
            return sink

        finished = asyncio.run(scenario()).of(JobFinished)

        assert [e.job_id for e in finished] == [f"{n + 1:06X}" for n in range(120)]

    def test_on_its_first_run_picks_up_only_a_job_in_progress(self):
        """History from before the bridge was installed is for `backfill` (#8)."""

        async def scenario():
            fake = FakeMoonraker()
            await fake.finish_job(await fake.start_job(notify=False), notify=False)
            await fake.start_job("running.gcode", notify=False)
            sink = RecordingSink(resume=None)
            async with tracking(sink, fake):
                await eventually(lambda: sink.events)
                await asyncio.sleep(0.1)
            return sink

        events = asyncio.run(scenario()).events

        assert [(type(e).__name__, e.file_name) for e in events] == [
            ("JobStarted", "running.gcode")
        ]

    def test_on_its_first_run_with_nothing_printing_reports_nothing(self):
        async def scenario():
            fake = FakeMoonraker()
            await fake.finish_job(await fake.start_job(notify=False), notify=False)
            sink = RecordingSink(resume=None)
            async with tracking(sink, fake):
                await asyncio.sleep(0.2)
            return sink

        assert asyncio.run(scenario()).events == []

    def test_without_history_says_how_to_turn_it_on_and_stays_connected(self, caplog):
        async def scenario():
            fake = FakeMoonraker(history=False)
            async with tracking(RecordingSink(resume=at(T0)), fake):
                await eventually(lambda: "[history]" in caplog.text)
                await asyncio.sleep(0.1)
                return len(fake.identified)

        with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
            identified = asyncio.run(scenario())

        assert "moonraker.conf" in caplog.text
        assert identified == 1  # no reconnect loop

    def test_reports_the_printer_model_the_slicer_wrote_in_the_file(self):
        async def scenario():
            fake = FakeMoonraker()
            base = await fake.start()
            report = ReportRecorder()
            tracker = JobTracker("voron", RecordingSink(), report)
            session = MoonrakerSession("voron", url=base, listener=tracker, backoff=FAST)
            try:
                async with running(session):
                    await eventually(lambda: fake.subscriptions)
                    await fake.start_job("a.gcode", {"slicer": "OrcaSlicer"})
                    await fake.start_job("b.gcode", {"printer_model": "InfiMech EX"})
                    await eventually(lambda: report.named("printer_model"))
            finally:
                await fake.close()
            return report

        report = asyncio.run(scenario())

        assert report.named("printer_model") == [("printer_model", "InfiMech EX")]

    def test_without_history_warns_in_the_status(self):
        async def scenario():
            fake = FakeMoonraker(history=False)
            base = await fake.start()
            report = ReportRecorder()
            tracker = JobTracker("voron", RecordingSink(resume=at(T0)), report)
            session = MoonrakerSession("voron", url=base, listener=tracker, backoff=FAST)
            try:
                async with running(session):
                    await eventually(lambda: report.warnings)
            finally:
                await fake.close()
            return report

        report = asyncio.run(scenario())

        assert "[history]" in report.warnings["history"]


async def feed(fake: FakeMoonraker, filename: str, steps) -> None:
    """Klipper printing `filename`: (active extruder, filament_used so far) at each update.

    Klipper loads the file first (`print_stats.reset()`: standby, nothing used), then prints.
    """
    await fake.set_status(
        print_stats={"state": "standby", "filename": filename, "filament_used": 0.0}
    )
    await fake.set_status(print_stats={"state": "printing"})
    for tool, used in steps:
        await fake.set_status(toolhead={"extruder": tool}, print_stats={"filament_used": used})
    last = steps[-1][1]
    await eventually(lambda: fake.status["print_stats"]["filament_used"] == last)
    await asyncio.sleep(0.05)  # the bridge has the last update too


# A four-colour Snapmaker U1 print (Snapmaker Orca): T0 does most of it, T1 to T3 the details.
U1_METADATA = {
    "slicer": "SnapmakerOrca",
    "filament_total": 15123.4,
    "filament_weight_total": 45.1,
    "filament_weights": [30.2, 9.8, 3.6, 1.5],
    "filament_type": '["PLA", "PLA", "PLA", "PETG"]',
}
U1_STEPS = [
    ("extruder", 4000.0),
    ("extruder1", 6000.0),
    ("extruder", 9000.0),
    ("extruder2", 10000.0),
    ("extruder3", 10500.0),
    ("extruder", 14500.0),
    ("extruder1", 15000.0),
]


class TestPerToolUsage:
    def test_a_four_tool_print_is_split_by_the_tool_active_as_it_fed(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking(sink) as fake:
                job = await fake.start_job("u1/dragon.gcode", U1_METADATA, start_time=T0)
                await feed(fake, "u1/dragon.gcode", U1_STEPS)
                # Moonraker's final figure includes the last moves the bridge never saw.
                await fake.finish_job(job, filament_used=15060.0)
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(scenario()).of(JobFinished)

        split = event.filament_mm
        assert sorted(split) == [0, 1, 2, 3]
        assert sum(split.values()) == pytest.approx(15060.0, rel=0.01)
        assert split[0] == pytest.approx(11000.0, rel=0.01)  # 4000 + 3000 + 4000
        assert split[1] == pytest.approx(2500.0, rel=0.01)
        assert split[2] == pytest.approx(1000.0, rel=0.01)
        assert split[3] == pytest.approx(500.0, rel=0.01)

    def test_the_start_carries_the_slicers_grams_per_tool(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking(sink) as fake:
                await fake.start_job("u1/dragon.gcode", U1_METADATA, start_time=T0)
                await eventually(lambda: sink.events)
            return sink

        [event] = asyncio.run(scenario()).events

        assert event.estimated_filament_g_by_slot == {0: 30.2, 1: 9.8, 2: 3.6, 3: 1.5}
        assert event.estimated_filament_g == 45.1

    def test_a_single_tool_print_keeps_one_slot(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking(sink) as fake:
                job = await fake.start_job("benchy.gcode", {"filament_weights": [13.27]})
                await feed(fake, "benchy.gcode", [("extruder", 2000.0), ("extruder", 4300.0)])
                await fake.finish_job(job, filament_used=4321.5)
                await eventually(lambda: sink.of(JobFinished))
            return sink

        sink = asyncio.run(scenario())

        assert sink.of(JobStarted)[0].estimated_filament_g_by_slot == {}
        assert sink.of(JobFinished)[0].filament_mm == pytest.approx({0: 4321.5})

    def test_a_job_that_ended_while_away_is_split_by_the_slicers_estimate(self):
        async def scenario():
            fake = FakeMoonraker()
            job = await fake.start_job("u1/dragon.gcode", U1_METADATA, notify=False, start_time=T0)
            await fake.finish_job(job, filament_used=15060.0, notify=False)
            sink = RecordingSink(resume=at(T0))
            async with tracking(sink, fake):
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(scenario()).of(JobFinished)

        assert sum(event.filament_mm.values()) == pytest.approx(15060.0)
        assert event.filament_mm[0] == pytest.approx(15060.0 * 30.2 / 45.1)
        assert event.filament_mm[3] == pytest.approx(15060.0 * 1.5 / 45.1)

    def test_what_was_seen_of_one_print_is_not_given_to_the_next(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking(sink) as fake:
                first = await fake.start_job("cube.gcode")
                await feed(fake, "cube.gcode", [("extruder1", 800.0)])
                await fake.finish_job(first, filament_used=800.0)
                await eventually(lambda: sink.of(JobFinished))
                # The same file again, ending before the bridge saw any of it fed.
                second = await fake.start_job("cube.gcode")
                await fake.finish_job(second, filament_used=500.0)
                await eventually(lambda: len(sink.of(JobFinished)) == 2)
            return sink

        first, second = asyncio.run(scenario()).of(JobFinished)

        assert first.filament_mm == pytest.approx({1: 800.0})
        assert second.filament_mm == {0: 500.0}

    def test_an_earlier_job_replayed_on_a_reconnect_leaves_the_running_ones_usage(self):
        async def scenario():
            fake = FakeMoonraker()
            earlier = await fake.start_job("cube.gcode", notify=False, start_time=T0 + 1)
            await fake.finish_job(earlier, filament_used=700.0, notify=False)
            running = await fake.start_job("cube.gcode", notify=False, start_time=T0 + 2000)
            sink = RecordingSink(resume=at(T0))
            async with tracking(sink, fake):
                await eventually(lambda: len(sink.of(JobStarted)) == 2)
                await feed(fake, "cube.gcode", [("extruder1", 300.0)])
                await fake.drop()  # the earlier job is replayed on the reconnect
                await eventually(lambda: len(sink.of(JobFinished)) == 2)
                await eventually(lambda: fake.client_count == 1 and len(fake.subscriptions) == 2)
                await fake.finish_job(running, filament_used=300.0)
                await eventually(lambda: len(sink.of(JobFinished)) == 3)
            return sink

        last = asyncio.run(scenario()).of(JobFinished)[-1]

        assert last.job_id == "000002"
        assert last.filament_mm == pytest.approx({1: 300.0})


def exclude_object_status(names, excluded=()):
    """`exclude_object` as Klipper reports it, for a file that defines `names`."""
    return {
        "objects": [{"name": n, "center": [100.0, 100.0], "polygon": []} for n in names],
        "excluded_objects": list(excluded),
        "current_object": None,
    }


SIX_PARTS = [f"PART_{n}" for n in range(1, 7)]


@contextlib.asynccontextmanager
async def tracking_objects(sink: RecordingSink):
    fake = FakeMoonraker(objects=(*FakeMoonraker().objects, "exclude_object"))
    fake.status["exclude_object"] = exclude_object_status([])
    async with tracking(sink, fake):
        yield fake


async def print_with_exclusions(fake: FakeMoonraker, filename: str, *exclusions) -> None:
    """Klipper prints `filename` with six objects, cancelling each of `exclusions` in turn."""
    await fake.set_status(print_stats={"state": "standby", "filename": filename})
    await fake.set_status(
        print_stats={"state": "printing"}, exclude_object=exclude_object_status(SIX_PARTS)
    )
    excluded: list[str] = []
    for name in exclusions:
        excluded.append(name)
        await fake.set_status(exclude_object={"excluded_objects": list(excluded)})
    await asyncio.sleep(0.05)  # the bridge has the last update too


class TestCancelledObjects:
    def test_subscribes_to_exclude_object_when_the_printer_has_it(self):
        async def scenario():
            async with tracking_objects(RecordingSink()) as fake:
                return fake.subscriptions[0]

        objects = asyncio.run(scenario())

        assert objects["exclude_object"] == ["objects", "excluded_objects"]

    def test_a_job_with_cancelled_objects_ends_with_a_note_naming_them(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking_objects(sink) as fake:
                job = await fake.start_job("plate.gcode")
                await print_with_exclusions(fake, "plate.gcode", "PART_3", "PART_5")
                await fake.set_status(print_stats={"state": "complete"})
                await fake.finish_job(job)
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(scenario()).of(JobFinished)

        assert event.outcome is JobOutcome.COMPLETED
        assert event.note == "2 of 6 objects were cancelled: PART_3, PART_5"

    def test_a_job_without_cancelled_objects_gets_no_note(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking_objects(sink) as fake:
                job = await fake.start_job("plate.gcode")
                await print_with_exclusions(fake, "plate.gcode")
                await fake.set_status(print_stats={"state": "complete"})
                await fake.finish_job(job)
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(scenario()).of(JobFinished)

        assert event.note is None

    def test_the_note_survives_a_cancel_macro_that_resets_the_file(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking_objects(sink) as fake:
                job = await fake.start_job("plate.gcode")
                await print_with_exclusions(fake, "plate.gcode", "PART_2")
                # CANCEL_PRINT, then SDCARD_RESET_FILE: Klipper forgets the objects.
                await fake.set_status(
                    print_stats={"state": "cancelled"}, exclude_object=exclude_object_status([])
                )
                await fake.set_status(print_stats={"state": "standby", "filename": ""})
                await fake.finish_job(job, "cancelled")
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(scenario()).of(JobFinished)

        assert event.outcome is JobOutcome.CANCELLED
        assert event.note == "1 of 6 objects was cancelled: PART_2"

    def test_a_failure_keeps_its_reason_ahead_of_the_cancelled_objects(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking_objects(sink) as fake:
                job = await fake.start_job("plate.gcode")
                await print_with_exclusions(fake, "plate.gcode", "PART_1")
                await fake.set_status(print_stats={"state": "error", "message": "MCU shutdown"})
                await fake.finish_job(job, "error")
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(scenario()).of(JobFinished)

        assert event.note == "Klipper error: MCU shutdown\n\n1 of 6 objects was cancelled: PART_1"

    def test_the_cancelled_objects_of_one_print_are_not_given_to_the_next(self):
        async def scenario():
            sink = RecordingSink()
            async with tracking_objects(sink) as fake:
                first = await fake.start_job("plate.gcode")
                await print_with_exclusions(fake, "plate.gcode", "PART_4")
                await fake.set_status(print_stats={"state": "complete"})
                await fake.finish_job(first)
                await eventually(lambda: sink.of(JobFinished))
                second = await fake.start_job("plate.gcode")
                await print_with_exclusions(fake, "plate.gcode")
                await fake.set_status(print_stats={"state": "complete"})
                await fake.finish_job(second)
                await eventually(lambda: len(sink.of(JobFinished)) == 2)
            return sink

        first, second = asyncio.run(scenario()).of(JobFinished)

        assert first.note == "1 of 6 objects was cancelled: PART_4"
        assert second.note is None


# A Creality K3 on its stock firmware: four nozzles, each fed by one slot of its CFS. Klipper's
# `toolhead.extruder` stays `extruder` through every tool change (seen in a trace of a real
# four-color print), and `nozzle_manager.selected` names the nozzle printing, -1 for none.
K3_GCODE = Path(__file__).parent / "fixtures" / "gcode" / "creality-print-7.3-k3-four-color.gcode"
K3_NAME = "Fuecoco.stl 2_PLA_2h39m57s.gcode"
K3_OBJECTS = (*FakeMoonraker().objects, "nozzle_manager", "extruder1", "extruder2", "extruder3")
K3_GRAMS = (21.76, 10.33, 3.93, 5.58)
"""`; filament used [g] = 21.76, 10.33, 3.93, 5.58`, in the file's settings block."""


class SettingsSink(RecordingSink):
    """Reads each job's slicer settings as the lifecycle does, unless `slicer_settings` is off:
    while the event is emitted, before the job's end is reported."""

    def __init__(self, resume: datetime | None = None, slicer_settings: bool = True) -> None:
        super().__init__(resume)
        self.slicer_settings = slicer_settings
        self.estimates: list = []

    async def emit(self, event) -> None:
        if isinstance(event, JobStarted) and self.slicer_settings and event.read_estimate:
            self.estimates.append(await event.read_estimate())
        if isinstance(event, JobSlicerSettings) and self.slicer_settings and event.read:
            await event.read()
        await super().emit(event)


@contextlib.asynccontextmanager
async def tracking_k3(
    sink: RecordingSink, fake: FakeMoonraker | None = None, with_file: bool = True
):
    """A K3 with the job's file on it (unless not `with_file`), tracked by a remote bridge
    that reads files."""
    fake = fake or FakeMoonraker(objects=K3_OBJECTS)
    fake.status["nozzle_manager"] = {"selected": -1}
    if with_file:
        fake.files[K3_NAME] = K3_GCODE.read_bytes()
    base = await fake.start()
    images = MoonrakerImages("k3", url=base, socket=None)
    tracker = JobTracker("k3", sink, images=images)
    session = MoonrakerSession("k3", url=base, listener=tracker, backoff=FAST)
    try:
        async with running(session):
            await eventually(lambda: fake.subscriptions)
            yield fake
    finally:
        await fake.close()


def k3_metadata() -> dict:
    """What Moonraker keeps for a Creality Print file: no slicer, no weights."""
    return {**gcode_metadata(K3_GCODE.read_bytes()), "slicer": "Unknown"}


async def feed_k3(fake: FakeMoonraker, steps) -> None:
    """The K3 printing: (selected nozzle, filament_used so far) at each update."""
    await fake.set_status(
        print_stats={"state": "standby", "filename": K3_NAME, "filament_used": 0.0}
    )
    await fake.set_status(print_stats={"state": "printing"})
    for nozzle, used in steps:
        await fake.set_status(
            toolhead={"extruder": "extruder"},
            nozzle_manager={"selected": nozzle},
            print_stats={"filament_used": used},
        )
    last = steps[-1][1]
    await eventually(lambda: fake.status["print_stats"]["filament_used"] == last)
    await asyncio.sleep(0.05)


def ended_while_away(filament_used: float, sink: SettingsSink):
    async def scenario():
        fake = FakeMoonraker(objects=K3_OBJECTS)
        job = await fake.start_job(K3_NAME, k3_metadata(), notify=False, start_time=T0)
        await fake.finish_job(job, filament_used=filament_used, notify=False)
        async with tracking_k3(sink, fake):
            await eventually(lambda: sink.of(JobFinished))
            return list(fake.ranges)

    ranges = asyncio.run(scenario())
    [event] = sink.of(JobFinished)
    return event, ranges


K3_BOX = json.loads(
    (Path(__file__).parent / "fixtures" / "moonraker" / "k3" / "box.json").read_text(
        encoding="utf-8"
    )
)["result"]["status"]["box"]


def k3_start(sink: SettingsSink, box=K3_BOX):
    """A live four-color start on a K3 whose CFS reports `box`; the file's ranges read."""

    async def scenario():
        fake = FakeMoonraker(objects=(*K3_OBJECTS, "box"))
        fake.status["box"] = box
        async with tracking_k3(sink, fake) as fake:
            await fake.start_job(K3_NAME, k3_metadata(), start_time=T0)
            await eventually(lambda: sink.of(JobStarted))
            await asyncio.sleep(0.05)
            return list(fake.ranges)

    return asyncio.run(scenario())


class TestCrealityK3Estimate:
    """Creality Print's files give Moonraker no estimate, so the bridge reads it from the
    file's end before the print is created (printlog-bridge#128)."""

    def test_a_live_start_reads_the_files_estimate_placed_on_the_cfs_colors(self):
        sink = SettingsSink()
        k3_start(sink)

        [estimate] = sink.estimates
        assert estimate.duration_s == 9597.0
        assert estimate.by_slot == {0: 10.33, 1: 5.58, 2: 3.93, 3: 21.76}
        assert estimate.unplaced == []
        assert estimate.filament_g == pytest.approx(41.6)
        assert estimate.slot_colors == {0: "#FFFFFF", 1: "#000000", 2: "#FFF014", 3: "#FF1E1E"}

    def test_the_estimate_and_the_settings_read_the_file_once(self):
        both = k3_start(SettingsSink())

        class SettingsOnly(SettingsSink):
            async def emit(self, event) -> None:
                if isinstance(event, JobStarted):
                    event = dataclasses.replace(event, read_estimate=None)
                await super().emit(event)

        assert both == k3_start(SettingsOnly())
        assert both  # the settings were read

    def test_a_filament_with_no_matching_slot_is_unplaced(self):
        box = json.loads(json.dumps(K3_BOX))
        box["same_material"] = [e for e in box["same_material"] if e[2] != ["T1D"]]  # no red
        sink = SettingsSink()
        k3_start(sink, box)

        [estimate] = sink.estimates
        assert estimate.by_slot == {0: 10.33, 1: 5.58, 2: 3.93}
        [red] = estimate.unplaced
        assert (red.index, red.grams, red.color) == (0, 21.76, "#FF0000")
        assert red.note == "Sliced filament 1 · Red (#FF0000) · PLA: printer slot not known"

    def test_with_slicer_settings_off_nothing_is_read(self):
        sink = SettingsSink(slicer_settings=False)

        assert k3_start(sink) == []
        assert sink.estimates == []

    def test_metadata_with_an_estimate_isnt_offered_one(self):
        sink = SettingsSink()

        async def scenario():
            async with tracking_k3(sink) as fake:
                await fake.start_job(
                    K3_NAME, {**k3_metadata(), "estimated_time": 600.0}, start_time=T0
                )
                await eventually(lambda: sink.of(JobStarted))

        asyncio.run(scenario())

        assert sink.of(JobStarted)[0].read_estimate is None


class TestCrealityK3:
    def test_subscribes_to_the_selected_nozzle_when_the_printer_has_one(self):
        async def scenario():
            async with tracking_k3(RecordingSink()) as fake:
                return fake.subscriptions[0]

        assert asyncio.run(scenario())["nozzle_manager"] == ["selected"]

    def test_a_printer_without_one_isnt_asked_for_it(self):
        async def scenario():
            async with tracking(RecordingSink()) as fake:
                return fake.subscriptions[0]

        assert "nozzle_manager" not in asyncio.run(scenario())

    def test_a_four_color_print_is_split_by_the_nozzle_that_fed(self):
        """Mapped when it was sent, the G-code's T0 (red) printed from nozzle 4, slot 3: what
        the bridge saw wins over the file's grams, which are in the slicer's tool order."""

        async def scenario():
            sink = SettingsSink()
            async with tracking_k3(sink) as fake:
                job = await fake.start_job(K3_NAME, k3_metadata(), start_time=T0)
                await feed_k3(fake, [(3, 4000.0), (-1, 4010.0), (0, 6000.0), (2, 7000.0),
                                     (1, 8000.0), (3, 11000.0)])  # fmt: skip
                await fake.finish_job(job, filament_used=11000.0)
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(scenario()).of(JobFinished)

        assert event.filament_mm == pytest.approx({0: 1990.0, 1: 1000.0, 2: 1000.0, 3: 7010.0})

    def test_a_print_that_ended_while_away_is_split_by_the_files_grams_per_tool(self):
        """Moonraker reads no weights from Creality Print's files; its settings block has them."""
        event, _ = ended_while_away(14107.8, SettingsSink(resume=at(T0)))

        total = sum(K3_GRAMS)
        assert event.filament_mm == pytest.approx(
            {slot: 14107.8 * grams / total for slot, grams in enumerate(K3_GRAMS)}
        )

    def test_with_slicer_settings_off_the_file_is_never_read_and_its_all_slot_0(self):
        event, ranges = ended_while_away(500.0, SettingsSink(resume=at(T0), slicer_settings=False))

        assert event.filament_mm == {0: 500.0}
        assert ranges == []

    def test_a_file_that_cant_be_read_leaves_it_all_to_slot_0(self):
        sink = SettingsSink(resume=at(T0))

        async def scenario():
            fake = FakeMoonraker(objects=K3_OBJECTS)
            job = await fake.start_job(K3_NAME, k3_metadata(), notify=False, start_time=T0)
            await fake.finish_job(job, filament_used=500.0, notify=False)
            async with tracking_k3(sink, fake, with_file=False):
                await eventually(lambda: sink.of(JobFinished))

        asyncio.run(scenario())

        assert sink.of(JobFinished)[0].filament_mm == {0: 500.0}


# A Snapmaker U1 lets the screen send each of the slicer's filaments to any head. Its
# `print_task_config` says where (`extruder_map_table`) and what each head has loaded: a real
# answer, with the first tools remapped as the screen does it.
U1_CONFIG = json.loads(
    (Path(__file__).parent / "fixtures" / "moonraker" / "u1" / "print_task_config.json").read_text(
        encoding="utf-8"
    )
)["result"]["status"]["print_task_config"]
U1_OBJECTS = (*FakeMoonraker().objects, "print_task_config")


def u1_config(*table_start: int) -> dict:
    config = copy.deepcopy(U1_CONFIG)
    config["extruder_map_table"][: len(table_start)] = list(table_start)
    return config


@contextlib.asynccontextmanager
async def tracking_u1(sink: RecordingSink, *table_start: int, fake: FakeMoonraker | None = None):
    fake = fake or FakeMoonraker(objects=U1_OBJECTS)
    fake.status["print_task_config"] = u1_config(*table_start)
    async with tracking(sink, fake):
        yield fake


U1_NOTES = {
    0: "Slot 1 · White (#E2DEDB) · PLA SnapSpeed",
    1: "Slot 2 · Black (#000000) · PLA Basic",
    2: "Slot 3 · Blue (#1E88E5) · PLA Basic",
    3: "Slot 4 · Pink (#D81B60) · PLA Basic",
}


class TestSnapmakerU1Mapping:
    def test_subscribes_to_the_map_and_whats_loaded_not_the_rest(self):
        async def scenario():
            async with tracking_u1(RecordingSink()) as fake:
                return fake.subscriptions[0]

        assert asyncio.run(scenario())["print_task_config"] == [
            "extruder_map_table",
            "filament_color_rgba",
            "filament_type",
            "filament_sub_type",
            "filament_exist",
        ]

    def test_the_start_puts_the_slicers_grams_on_the_heads_they_were_sent_to(self):
        """T0 (30.2 g) printed on head 3 and T1 (9.8 g) on head 0, as the screen mapped them."""

        async def run():
            sink = RecordingSink()
            async with tracking_u1(sink, 3, 0, 2, 1) as fake:
                await fake.start_job("u1/dragon.gcode", U1_METADATA, start_time=T0)
                await eventually(lambda: sink.of(JobStarted))
            return sink

        [event] = asyncio.run(run()).of(JobStarted)

        assert event.estimated_filament_g_by_slot == {3: 30.2, 0: 9.8, 2: 3.6, 1: 1.5}
        assert event.slot_notes == U1_NOTES

    def test_a_one_color_print_sent_to_another_head_is_estimated_on_it(self):
        async def run():
            sink = RecordingSink()
            async with tracking_u1(sink, 2) as fake:
                metadata = {"filament_weights": [13.27], "filament_weight_total": 13.27}
                await fake.start_job("benchy.gcode", metadata, start_time=T0)
                await eventually(lambda: sink.of(JobStarted))
            return sink

        [event] = asyncio.run(run()).of(JobStarted)

        assert event.estimated_filament_g_by_slot == {2: 13.27}

    def test_a_print_it_didnt_see_is_split_on_the_mapped_heads_when_it_was_the_last(self):
        async def run():
            fake = FakeMoonraker(objects=U1_OBJECTS)
            job = await fake.start_job("u1/dragon.gcode", U1_METADATA, notify=False, start_time=T0)
            await fake.finish_job(job, filament_used=15060.0, notify=False)
            fake.status["print_stats"] = {"state": "complete", "filename": "u1/dragon.gcode"}
            sink = RecordingSink(resume=at(T0))
            async with tracking_u1(sink, 3, 0, 2, 1, fake=fake):
                await eventually(lambda: sink.of(JobFinished))
            return sink

        sink = asyncio.run(run())
        [start] = sink.of(JobStarted)
        [event] = sink.of(JobFinished)

        assert start.slot_notes == U1_NOTES
        assert sorted(event.filament_mm) == [0, 1, 2, 3]
        assert event.filament_mm[3] == pytest.approx(15060.0 * 30.2 / 45.1)
        assert event.filament_mm[0] == pytest.approx(15060.0 * 9.8 / 45.1)

    def test_an_older_job_isnt_given_todays_map_or_spools(self):
        """Klipper has run another file since: the map and the loaded heads are that one's."""

        async def run():
            fake = FakeMoonraker(objects=U1_OBJECTS)
            job = await fake.start_job("u1/dragon.gcode", U1_METADATA, notify=False, start_time=T0)
            await fake.finish_job(job, filament_used=15060.0, notify=False)
            fake.status["print_stats"] = {"state": "complete", "filename": "later.gcode"}
            sink = RecordingSink(resume=at(T0))
            async with tracking_u1(sink, 3, 0, 2, 1, fake=fake):
                await eventually(lambda: sink.of(JobFinished))
            return sink

        sink = asyncio.run(run())
        [start] = sink.of(JobStarted)
        [event] = sink.of(JobFinished)

        assert start.slot_notes == {}
        assert start.estimated_filament_g_by_slot == {0: 30.2, 1: 9.8, 2: 3.6, 3: 1.5}
        assert event.filament_mm[0] == pytest.approx(15060.0 * 30.2 / 45.1)

    def test_what_it_saw_each_head_feed_still_wins(self):
        async def run():
            sink = RecordingSink()
            async with tracking_u1(sink, 3, 0, 2, 1) as fake:
                job = await fake.start_job("u1/dragon.gcode", U1_METADATA, start_time=T0)
                await feed(fake, "u1/dragon.gcode", U1_STEPS)
                await fake.finish_job(job, filament_used=15060.0)
                await eventually(lambda: sink.of(JobFinished))
            return sink

        [event] = asyncio.run(run()).of(JobFinished)

        assert event.filament_mm[0] == pytest.approx(11000.0, rel=0.01)
        assert event.filament_mm[3] == pytest.approx(500.0, rel=0.01)
