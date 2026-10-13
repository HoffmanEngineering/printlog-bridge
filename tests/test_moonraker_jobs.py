from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone

import pytest
from fake_moonraker import FakeMoonraker, eventually
from session_helpers import ReportRecorder, running

from printlog_bridge.adapters.moonraker.jobs import JobTracker
from printlog_bridge.adapters.moonraker.session import Backoff, MoonrakerSession
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted

FAST = Backoff(initial=0.01, maximum=0.05)
T0 = 1_791_000_000.0  # 2026-10-03, on the printer's clock


class RecordingSink:
    def __init__(self, resume: datetime | None = None) -> None:
        self.events: list = []
        self.resume = resume

    async def emit(self, event) -> None:
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
