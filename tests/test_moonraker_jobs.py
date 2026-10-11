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
