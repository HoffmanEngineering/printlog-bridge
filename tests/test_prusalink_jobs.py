"""Turning PrusaLink's polled status into job events (`adapters/prusalink/jobs.py`).

Built against Prusa's published API and Buddy firmware's sources, not a recorded printer:
replace these with a real trace when one exists (#17).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fake_prusalink import PNG

from printlog_bridge.adapters.prusalink.files import JobFile
from printlog_bridge.adapters.prusalink.jobs import UNSEEN_STATE_NOTE, JobTracker
from printlog_bridge.core.current_job import UNSEEN_END_NOTE, UNSEEN_TIME_NOTE, CurrentJob
from printlog_bridge.core.events import (
    JobFinished,
    JobImage,
    JobOutcome,
    JobSlicerSettings,
    JobStarted,
    SlicerFile,
    SlicerMetadata,
)
from printlog_bridge.core.state import PrinterState

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Sink:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)

    async def resume_after(self, printer):  # pragma: no cover - not used without history
        return None

    def of(self, kind):
        return [e for e in self.events if isinstance(e, kind)]


class Printer:
    """What `/api/v1/status` and `/api/v1/job` say, and the job's file."""

    def __init__(self) -> None:
        self.state = "IDLE"
        self.job: dict | None = None
        self.file = JobFile(
            grams={0: 12.0},
            mm={0: 4000.0},
            duration_s=3600.0,
            printer_model="MK4S",
            metadata=SlicerMetadata(slicer="PrusaSlicer", slicer_version="2.9.2"),
            settings=SlicerFile(config_block="; layer_height = 0.2\n"),
            thumbnail=PNG,
        )
        self.reads = 0
        self.job_requests = 0

    def print(self, job_id=7, name="Benchy.bgcode", progress=0.0, time_printing=0, state=None):
        self.state = state or "PRINTING"
        self.job = {
            "id": job_id,
            "progress": progress,
            "time_printing": time_printing,
            "time_remaining": 3600,
            "name": name,
        }

    def status(self) -> dict:
        status: dict = {"printer": {"state": self.state}}
        if self.job is not None and self.state in ("PRINTING", "PAUSED", "ATTENTION", "BUSY"):
            status["job"] = {k: v for k, v in self.job.items() if k != "name"}
        return status

    async def fetch_job(self):
        self.job_requests += 1
        if self.job is None or self.state not in ("PRINTING", "PAUSED", "ATTENTION"):
            return None
        return {
            "id": self.job["id"],
            "state": self.state,
            "progress": self.job["progress"],
            "file": {"display_name": self.job["name"], "refs": {"download": "/usb/X.BGC"}},
        }

    async def read(self, job) -> JobFile:
        self.reads += 1
        return self.file


def tracker(tmp_path, printer: Printer, sink=None, clock=None, report=None) -> JobTracker:
    clock = clock or Clock()
    current = CurrentJob(PrinterState.load(tmp_path, "mk4s"), clock=clock)
    return JobTracker(
        "mk4s",
        sink or Sink(),
        current,
        fetch_job=printer.fetch_job,
        files=printer,
        report=report,
        clock=clock,
    )


async def poll(t: JobTracker, printer: Printer, times: int = 1) -> None:
    for _ in range(times):
        await t.on_status(printer.status())


def run(coroutine):
    return asyncio.run(coroutine)


class TestLifecycle:
    def test_a_job_that_finishes(self, tmp_path):
        printer, sink, clock = Printer(), Sink(), Clock()
        t = tracker(tmp_path, printer, sink, clock)

        async def scenario():
            await poll(t, printer)
            printer.print(time_printing=90)
            await poll(t, printer)
            assert t.active
            printer.job["progress"] = 50.0  # type: ignore[index]
            await poll(t, printer, 3)
            clock.now += timedelta(hours=1)
            printer.state = "FINISHED"
            await poll(t, printer, 2)

        run(scenario())

        [start] = sink.of(JobStarted)
        assert start.job_id == "7-Benchy"
        assert start.file_name == "Benchy.bgcode"
        assert start.started_at == NOW - timedelta(seconds=90)
        assert start.estimated_duration_s == 3600.0
        assert start.estimated_filament_g == 12.0
        assert start.estimated_filament_g_by_slot == {}
        [image] = sink.of(JobImage)
        assert run(image.fetch()) == PNG
        [settings] = sink.of(JobSlicerSettings)
        assert settings.metadata.slicer == "PrusaSlicer"
        assert run(settings.read()).config_block == "; layer_height = 0.2\n"
        [finished] = sink.of(JobFinished)
        assert finished.outcome is JobOutcome.COMPLETED
        assert finished.filament_g == {0: 12.0}
        assert finished.filament_mm == {0: 4000.0}
        assert finished.usage_estimated is True
        assert finished.duration_s == 3690.0
        assert printer.reads == 1 and printer.job_requests == 1
        assert not t.active

    def test_a_stopped_job_is_cancelled_and_charged_for_how_far_it_got(self, tmp_path):
        printer, sink = Printer(), Sink()
        t = tracker(tmp_path, printer, sink)

        async def scenario():
            printer.print()
            await poll(t, printer)
            printer.job["progress"] = 25.0  # type: ignore[index]
            await poll(t, printer)
            printer.state = "STOPPED"
            await poll(t, printer)

        run(scenario())

        [finished] = sink.of(JobFinished)
        assert finished.outcome is JobOutcome.CANCELLED
        assert finished.filament_g == {0: 3.0}
        assert finished.note is None

    def test_an_error_fails_the_job_with_the_printers_message(self, tmp_path):
        printer, sink = Printer(), Sink()
        t = tracker(tmp_path, printer, sink)

        async def scenario():
            printer.print()
            await poll(t, printer)
            await t.on_status(
                {"printer": {"state": "ERROR", "status_printer": {"message": "Thermal runaway"}}}
            )

        run(scenario())

        [finished] = sink.of(JobFinished)
        assert finished.outcome is JobOutcome.FAILED
        assert "Thermal runaway" in finished.note

    @pytest.mark.parametrize(
        ("progress", "outcome"), [(100.0, JobOutcome.COMPLETED), (40.0, JobOutcome.CANCELLED)]
    )
    def test_back_to_idle_without_an_end_state_goes_by_progress(self, tmp_path, progress, outcome):
        printer, sink = Printer(), Sink()
        t = tracker(tmp_path, printer, sink)

        async def scenario():
            printer.print(progress=progress)
            await poll(t, printer)
            printer.state, printer.job = "IDLE", None
            await poll(t, printer)

        run(scenario())

        [finished] = sink.of(JobFinished)
        assert finished.outcome is outcome
        assert finished.note == UNSEEN_STATE_NOTE

    def test_a_job_waiting_for_an_answer_on_the_screen_starts_once_it_prints(self, tmp_path):
        printer, sink = Printer(), Sink()
        t = tracker(tmp_path, printer, sink)

        async def scenario():
            printer.print(state="ATTENTION")  # "wrong filament?" before the print
            await poll(t, printer, 2)
            assert sink.events == []
            assert t.active
            printer.state = "PRINTING"
            await poll(t, printer)

        run(scenario())

        assert len(sink.of(JobStarted)) == 1

    def test_attention_or_busy_mid_job_doesnt_end_it(self, tmp_path):
        printer, sink = Printer(), Sink()
        t = tracker(tmp_path, printer, sink)

        async def scenario():
            printer.print()
            await poll(t, printer)
            printer.state = "ATTENTION"
            await poll(t, printer)
            await t.on_status({"printer": {"state": "BUSY"}})  # no job: crash recovery
            printer.state = "PRINTING"
            await poll(t, printer)

        run(scenario())

        assert sink.of(JobFinished) == []
        assert len(sink.of(JobStarted)) == 1

    def test_an_xl_job_has_grams_per_tool(self, tmp_path):
        printer, sink = Printer(), Sink()
        printer.file = JobFile(grams={0: 10.0, 2: 2.5, 4: 1.25}, mm={0: 3300.0, 2: 800.0})
        t = tracker(tmp_path, printer, sink)

        async def scenario():
            printer.print(name="XL toolchanger.bgcode")
            await poll(t, printer)
            printer.state = "FINISHED"
            await poll(t, printer)

        run(scenario())

        start = sink.of(JobStarted)[0]
        assert start.job_id == "7-XL toolchanger"
        assert start.estimated_filament_g_by_slot == {0: 10.0, 2: 2.5, 4: 1.25}
        assert start.estimated_filament_g == 13.75
        finished = sink.of(JobFinished)[0]
        assert finished.filament_g == {0: 10.0, 2: 2.5, 4: 1.25}
        assert finished.filament_mm == {0: 3300.0, 2: 800.0}

    def test_a_job_without_a_file_read_is_logged_without_usage(self, tmp_path):
        printer, sink = Printer(), Sink()
        printer.file = JobFile()
        t = tracker(tmp_path, printer, sink)

        async def scenario():
            printer.print()
            await poll(t, printer)
            printer.state = "FINISHED"
            await poll(t, printer)

        run(scenario())

        assert sink.of(JobImage) == [] and sink.of(JobSlicerSettings) == []
        finished = sink.of(JobFinished)[0]
        assert finished.filament_g == {} and finished.usage_estimated is False

    def test_a_new_job_id_without_the_last_ones_end(self, tmp_path):
        printer, sink = Printer(), Sink()
        t = tracker(tmp_path, printer, sink)

        async def scenario():
            printer.print(job_id=7)
            await poll(t, printer)
            printer.print(job_id=8, name="Cube.bgcode")
            await poll(t, printer)

        run(scenario())

        # The first one's start comes again with its end, which the sink recognizes.
        assert [s.job_id for s in sink.of(JobStarted)] == ["7-Benchy", "7-Benchy", "8-Cube"]
        [finished] = sink.of(JobFinished)
        assert finished.job_id == "7-Benchy"
        assert finished.note == UNSEEN_END_NOTE


class TestReconnect:
    def test_the_same_job_still_running_after_a_restart_replays_its_start(self, tmp_path):
        printer, first = Printer(), Sink()
        printer.print()
        run(poll(tracker(tmp_path, printer, first), printer))

        sink = Sink()
        t = tracker(tmp_path, printer, sink)
        printer.job["progress"] = 60.0  # type: ignore[index]
        run(poll(t, printer))

        assert sink.events == [first.of(JobStarted)[0]]
        printer.state = "STOPPED"
        run(poll(t, printer))
        finished = sink.of(JobFinished)[0]
        assert finished.outcome is JobOutcome.CANCELLED
        assert finished.filament_g == {0: pytest.approx(7.2)}  # from the start's estimate

    def test_a_job_that_ended_while_away_takes_the_printers_outcome(self, tmp_path):
        printer = Printer()
        printer.print()
        run(poll(tracker(tmp_path, printer), printer))

        sink = Sink()
        printer.state = "STOPPED"
        run(poll(tracker(tmp_path, printer, sink), printer))

        [finished] = sink.of(JobFinished)
        assert finished.outcome is JobOutcome.CANCELLED
        assert finished.note == UNSEEN_TIME_NOTE

    def test_a_job_replaced_by_another_while_away_is_guessed_complete(self, tmp_path):
        printer = Printer()
        printer.print()
        run(poll(tracker(tmp_path, printer), printer))

        sink = Sink()
        printer.print(job_id=9, name="Cube.bgcode")
        run(poll(tracker(tmp_path, printer, sink), printer))

        assert [s.job_id for s in sink.of(JobStarted)] == ["7-Benchy", "9-Cube"]
        finished = sink.of(JobFinished)[0]
        assert finished.job_id == "7-Benchy"
        assert finished.outcome is JobOutcome.COMPLETED
        assert finished.note == UNSEEN_END_NOTE

    def test_a_dropped_connection_carries_on_with_the_job(self, tmp_path):
        printer, sink = Printer(), Sink()
        t = tracker(tmp_path, printer, sink)
        printer.print()
        run(poll(t, printer))

        t.connected()
        printer.state = "FINISHED"
        run(poll(t, printer))

        assert len(sink.of(JobFinished)) == 1
        assert sink.of(JobFinished)[0].outcome is JobOutcome.COMPLETED
        assert printer.reads == 1


def test_a_job_already_running_at_the_first_connect_is_asked_about_once(tmp_path):
    printer, sink = Printer(), Sink()
    printer.print(time_printing=600)
    t = tracker(tmp_path, printer, sink)

    run(poll(t, printer, 3))

    assert len(sink.of(JobStarted)) == 1
    assert sink.of(JobStarted)[0].started_at == NOW - timedelta(minutes=10)
    assert printer.job_requests == 1
