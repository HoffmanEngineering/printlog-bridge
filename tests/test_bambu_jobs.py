"""Turning a Bambu printer's status reports into job events (`adapters/bambu/jobs.py`).

Built against the documented protocol (OpenBambuAPI, ha-bambulab), not a recorded printer:
replace these reports with a real trace when one exists (#17).
"""

from __future__ import annotations

import asyncio
import io
from datetime import datetime, timedelta, timezone

import pytest
from fake_bambu import CANCELLED_ERROR, PNG, idle_status, make_3mf

from printlog_bridge.adapters.bambu.jobs import IDLE_NOTE, JobTracker
from printlog_bridge.adapters.bambu.threemf import ThreeMF
from printlog_bridge.core.current_job import UNSEEN_END_NOTE, UNSEEN_TIME_NOTE, CurrentJob
from printlog_bridge.core.events import (
    JobFinished,
    JobImage,
    JobOutcome,
    JobSlicerSettings,
    JobStarted,
    SpoolReading,
)
from printlog_bridge.core.state import PrinterState

START = 1_793_000_000  # gcode_start_time, epoch seconds
STARTED_AT = datetime.fromtimestamp(START, timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = STARTED_AT + timedelta(minutes=2)

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


class Files:
    """The printer's storage, without FTPS."""

    def __init__(self, data: bytes | None = None) -> None:
        self.data = data if data is not None else make_3mf()
        self.opened = 0

    async def open(self, status):
        self.opened += 1
        return ThreeMF(io.BytesIO(self.data)) if self.data else None

    def close(self) -> None:
        pass


class Spools:
    def __init__(self) -> None:
        self.readings: list[SpoolReading] = []

    async def spool_command(self, command) -> str:  # pragma: no cover
        return ""

    async def spool_reading(self, reading: SpoolReading) -> None:
        self.readings.append(reading)


def tracker(tmp_path, sink=None, files=None, clock=None, spools=None) -> JobTracker:
    clock = clock or Clock()
    current = CurrentJob(PrinterState.load(tmp_path, "p1s"), clock=clock)
    return JobTracker(
        "p1s",
        sink or Sink(),
        current,
        files=files if files is not None else Files(),
        spools=spools,
        clock=clock,
    )


def job(**fields):
    """The fields of a report while a job is under way."""
    return {
        "gcode_state": "RUNNING",
        "subtask_name": "Benchy",
        "gcode_file": "Benchy.gcode.3mf",
        "gcode_start_time": str(START),
        "mc_percent": 1,
        "mc_remaining_time": 59,
        **fields,
    }


async def feed(t: JobTracker, *reports) -> None:
    for report in reports:
        await t.on_message({"print": report})


def run(coroutine):
    return asyncio.run(coroutine)


class TestLifecycle:
    def test_a_job_that_completes(self, tmp_path):
        sink, clock = Sink(), Clock()
        t = tracker(tmp_path, sink, clock=clock)

        async def scenario():
            await feed(t, idle_status())
            await feed(
                t,
                {
                    "gcode_state": "PREPARE",
                    "subtask_name": "Benchy",
                    "gcode_file": "Benchy.gcode.3mf",
                    "gcode_start_time": str(START),
                },
            )
            assert sink.events == []  # not until the file is on the printer
            await feed(t, job())
            await feed(t, {"mc_percent": 50, "layer_num": 60})  # a P1's diff
            clock.now = STARTED_AT + timedelta(hours=1)
            await feed(t, {"gcode_state": "FINISH", "mc_percent": 100})
            image = sink.of(JobImage)[0]
            settings = sink.of(JobSlicerSettings)[0]
            return await image.fetch(), await settings.read()

        thumbnail, settings = run(scenario())

        start = sink.of(JobStarted)[0]
        assert start.job_id == f"{START}-Benchy"
        assert start.file_name == "Benchy.gcode.3mf"
        assert start.started_at == STARTED_AT
        assert start.estimated_duration_s == 3600
        assert start.estimated_filament_g == 12.34
        assert start.estimated_filament_mm == 4120
        assert start.estimated_filament_g_by_slot == {}
        assert thumbnail == PNG
        assert "; layer_height = 0.2" in settings.config_block
        finished = sink.of(JobFinished)
        assert finished == [
            JobFinished(
                "p1s",
                f"{START}-Benchy",
                JobOutcome.COMPLETED,
                STARTED_AT + timedelta(hours=1),
                duration_s=3600,
                filament_mm={0: 4120.0},
                filament_g={0: 12.34},
                usage_estimated=True,
            )
        ]
        assert t.current.job is None

    def test_a_cancelled_job_is_charged_for_how_far_it_got(self, tmp_path):
        sink = Sink()
        t = tracker(tmp_path, sink)

        run(
            feed(
                t,
                idle_status(),
                job(mc_percent=40),
                {"print_error": CANCELLED_ERROR},
                {"gcode_state": "FAILED"},
                {"print_error": 0},
            )
        )

        finished = sink.of(JobFinished)[0]
        assert finished.outcome is JobOutcome.CANCELLED
        assert finished.filament_g == {0: pytest.approx(4.94)}
        assert finished.usage_estimated is True
        assert finished.note is None

    def test_a_failed_job_names_the_printers_error(self, tmp_path):
        sink = Sink()
        t = tracker(tmp_path, sink)

        run(
            feed(
                t,
                idle_status(),
                job(mc_percent=10),
                {"gcode_state": "FAILED", "print_error": 0x07008011},
            )
        )

        finished = sink.of(JobFinished)[0]
        assert finished.outcome is JobOutcome.FAILED
        assert "0700-8011" in finished.note

    def test_back_to_idle_from_running_is_a_cancel_with_a_note(self, tmp_path):
        sink = Sink()
        t = tracker(tmp_path, sink)

        run(feed(t, idle_status(), job(), {"gcode_state": "IDLE"}))

        assert sink.of(JobFinished)[0].outcome is JobOutcome.CANCELLED
        assert sink.of(JobFinished)[0].note == IDLE_NOTE

    def test_a_job_cancelled_while_preparing_is_logged_without_usage(self, tmp_path):
        sink = Sink()
        t = tracker(tmp_path, sink)

        run(
            feed(
                t,
                idle_status(),
                job(gcode_state="PREPARE", mc_percent=0),
                {"gcode_state": "FAILED", "print_error": CANCELLED_ERROR},
            )
        )

        assert [type(e) for e in sink.events][0] is JobStarted
        finished = sink.of(JobFinished)[0]
        assert finished.outcome is JobOutcome.CANCELLED
        assert finished.filament_g == {}
        assert finished.usage_estimated is False

    def test_diffs_before_the_whole_status_are_kept_but_start_nothing(self, tmp_path):
        sink = Sink()
        t = tracker(tmp_path, sink)

        run(feed(t, {"mc_percent": 5}))
        assert sink.events == []
        run(feed(t, job(mc_percent=None) | {"mc_percent": 5}))

        assert len(sink.of(JobStarted)) == 1

    def test_without_the_file_the_job_is_logged_without_usage(self, tmp_path):
        sink = Sink()
        t = tracker(tmp_path, sink, files=Files(b""))

        run(feed(t, idle_status(), job(), {"gcode_state": "FINISH"}))

        start = sink.of(JobStarted)[0]
        assert start.estimated_filament_g is None
        assert start.estimated_duration_s == 59 * 60
        assert sink.of(JobImage) == []
        assert sink.of(JobFinished)[0].filament_g == {}
        assert sink.of(JobFinished)[0].usage_estimated is False

    def test_a_printer_without_a_start_time_keeps_one_id_for_the_job(self, tmp_path):
        sink, clock = Sink(), Clock()
        t = tracker(tmp_path, sink, clock=clock)

        async def scenario():
            await feed(t, idle_status(), job(gcode_start_time="0"))
            clock.now += timedelta(minutes=5)
            await feed(t, {"mc_percent": 30}, {"gcode_state": "FINISH"})

        run(scenario())

        assert sink.of(JobStarted)[0].job_id == sink.of(JobFinished)[0].job_id
        assert sink.of(JobStarted)[0].job_id.endswith("-Benchy")


class TestAms:
    def test_grams_go_to_each_filaments_tray(self, tmp_path):
        sink = Sink()
        files = Files(make_3mf({1: [(1, "PLA", 3.0, 9.0), (2, "PETG", 1.0, 3.0)]}))
        t = tracker(tmp_path, sink, files=files)

        run(feed(t, idle_status(), job(ams_mapping=[1, 0]), {"gcode_state": "FINISH"}))

        assert sink.of(JobStarted)[0].estimated_filament_g_by_slot == {2: 9.0, 1: 3.0}
        assert sink.of(JobFinished)[0].filament_g == {2: 9.0, 1: 3.0}
        assert sink.of(JobFinished)[0].filament_mm == {2: 3000.0, 1: 1000.0}

    def test_each_trays_reading_is_sent_and_a_change_mid_job_says_where(self, tmp_path):
        sink, spools = Sink(), Spools()
        t = tracker(tmp_path, sink, spools=spools)

        async def scenario():
            await feed(t, idle_status())
            await asyncio.sleep(0)
            first = list(spools.readings)
            status = idle_status()
            trays = status["ams"]["ams"][0]["tray"]
            trays[2] = {"id": "2", "tray_type": "ABS", "tray_color": "0000FFFF", "tag_uid": "0"}
            await feed(t, job(mc_percent=25, layer_num=40), {"ams": status["ams"]})
            await asyncio.sleep(0)
            return first, spools.readings[len(first) :]

        first, later = run(scenario())

        assert [(r.slot, r.tag, r.material, r.color_hex) for r in first] == [
            (1, "A1B2C3D4E5F60708", "PLA", "#FF0000"),
            (2, None, "PETG", "#00FF00"),
        ]
        assert all(r.slot_count == 5 and r.during is None for r in first)
        assert [(r.slot, r.material) for r in later] == [(3, "ABS")]
        assert later[0].during is not None
        assert later[0].during.progress == 0.25
        assert later[0].during.layer == 40
        assert later[0].during.fed_mm == {0: pytest.approx(1030.0)}


class TestReconnect:
    def test_the_same_job_still_running_after_a_restart_replays_its_start(self, tmp_path):
        first = Sink()
        run(feed(tracker(tmp_path, first), idle_status(), job()))

        sink = Sink()
        t = tracker(tmp_path, sink)
        run(feed(t, idle_status(**job(mc_percent=60))))

        assert sink.events == [first.of(JobStarted)[0]]
        run(feed(t, {"gcode_state": "FINISH", "mc_percent": 100}))
        finished = sink.of(JobFinished)[0]
        assert finished.outcome is JobOutcome.COMPLETED
        assert finished.filament_g == {0: 12.34}  # its file read again

    def test_a_job_that_ended_while_away_takes_the_printers_outcome(self, tmp_path):
        run(feed(tracker(tmp_path), idle_status(), job()))

        sink = Sink()
        run(
            feed(
                tracker(tmp_path, sink),
                idle_status(**job(gcode_state="FAILED", print_error=CANCELLED_ERROR)),
            )
        )

        finished = sink.of(JobFinished)[0]
        assert finished.outcome is JobOutcome.CANCELLED
        assert finished.note == UNSEEN_TIME_NOTE
        assert len(sink.of(JobFinished)) == 1

    def test_a_job_replaced_by_another_while_away_is_guessed_complete(self, tmp_path):
        run(feed(tracker(tmp_path), idle_status(), job()))

        sink = Sink()
        later = job(
            gcode_start_time=str(START + 7200), subtask_name="Cube", gcode_file="Cube.gcode.3mf"
        )
        run(feed(tracker(tmp_path, sink), idle_status(**later)))

        started = sink.of(JobStarted)
        assert [s.job_id for s in started] == [f"{START}-Benchy", f"{START + 7200}-Cube"]
        finished = sink.of(JobFinished)[0]
        assert finished.job_id == f"{START}-Benchy"
        assert finished.outcome is JobOutcome.COMPLETED
        assert finished.note == UNSEEN_END_NOTE
        assert finished.usage_estimated is True

    def test_a_dropped_connection_carries_on_with_the_job(self, tmp_path):
        sink = Sink()
        t = tracker(tmp_path, sink)
        run(feed(t, idle_status(), job()))

        t.connected()
        run(feed(t, idle_status(**job(mc_percent=70)), {"gcode_state": "FINISH"}))

        assert len(sink.of(JobStarted)) == 2  # the replay, which the sink recognizes
        assert sink.of(JobFinished)[0].filament_g == {0: 12.34}
