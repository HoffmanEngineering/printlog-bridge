"""The Duet adapter's job tracking (`adapters/duet/jobs.py`): RepRapFirmware's object model,
polled, turned into job events with the filament each tool measured.

The models come from `tests/fake_duet.py`, built from Duet3D's documentation, not a recorded
board (#89)."""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone

from fake_duet import FakeDuet

from printlog_bridge.adapters.duet.files import JobFile
from printlog_bridge.adapters.duet.jobs import JobTracker
from printlog_bridge.adapters.duet.model import merge, slots_of
from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.events import (
    JobFinished,
    JobImage,
    JobOutcome,
    JobSlicerSettings,
    JobStarted,
)
from printlog_bridge.core.state import MemoryState

T0 = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Sink:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)

    def of(self, kind):
        return [e for e in self.events if isinstance(e, kind)]


class Files:
    def __init__(self, mm=(4130.2,)) -> None:
        self.reads: list[str] = []
        self.mm = mm

    async def read(self, path: str) -> JobFile:
        self.reads.append(path)
        return JobFile(
            duration_s=3723.0,
            mm={i: v for i, v in enumerate(self.mm) if v},
            size=1_000_000,
        )


class Rig:
    """A tracker over a fake board's model, with a clock and state it can be restarted on."""

    def __init__(self, fake: FakeDuet, state: MemoryState | None = None, clock=None) -> None:
        self.fake = fake
        self.state = state or MemoryState()
        self.clock = clock or Clock()
        self.sink = Sink()
        self.files = Files()
        self.tracker = self.new_tracker()

    def new_tracker(self) -> JobTracker:
        tracker = JobTracker(
            "railcore",
            self.sink,
            CurrentJob(self.state, clock=self.clock),
            self.state,
            files=self.files,
            clock=self.clock,
        )
        tracker.connected()
        return tracker

    def restart(self) -> None:
        self.sink = Sink()
        self.tracker = self.new_tracker()

    def poll(self, seconds: float = 5.0) -> None:
        """`seconds` pass, on the board's job clock too while it prints."""
        self.clock.tick(seconds)
        self.fake.model["state"]["upTime"] += int(seconds)
        job = self.fake.job
        if job["duration"] is not None and self.fake.model["state"]["status"] == "processing":
            job["duration"] += seconds
        asyncio.run(self.tracker.on_model(copy.deepcopy(self.fake.model)))


def test_a_print_logs_what_the_extruder_measured():
    fake = FakeDuet()
    rig = Rig(fake)
    rig.poll()
    fake.print_file("benchy.gcode")
    rig.poll()
    fake.extrude([1200.0])
    rig.poll(600)
    fake.extrude([800.5])
    rig.poll(600)
    fake.end()
    rig.poll()

    [start] = rig.sink.of(JobStarted)
    assert start.file_name == "benchy.gcode"
    assert start.estimated_filament_mm == 4130.2
    assert start.estimated_duration_s == 3723.0
    assert start.job_id.endswith("-benchy")
    [finished] = rig.sink.of(JobFinished)
    assert finished.outcome is JobOutcome.COMPLETED
    assert finished.filament_mm == {0: 2000.5}
    assert finished.filament_g == {}
    assert finished.usage_estimated is False
    assert rig.files.reads == ["0:/gcodes/benchy.gcode"]
    assert CurrentJob(rig.state).job is None


def test_the_extrusion_factor_is_applied():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.print_file("a.gcode")
    rig.poll()
    fake.model["move"]["extruders"][0]["factor"] = 1.1
    fake.extrude([1000.0])
    rig.poll()
    fake.end()
    rig.poll()

    assert rig.sink.of(JobFinished)[0].filament_mm == {0: 1100.0}


def test_a_toolchanger_reports_each_tools_extruder_on_its_slot():
    fake = FakeDuet(extruders=3, tools=[[2], [0], [1]])
    rig = Rig(fake)
    fake.print_file("tc.gcode")
    rig.poll()
    fake.extrude([100.0, 0.0, 300.0])
    rig.poll()
    fake.end()
    rig.poll()

    # T0 drives extruder 2, T1 extruder 0; extruder 1 (T2) fed nothing and is left out.
    assert rig.sink.of(JobFinished)[0].filament_mm == {1: 100.0, 0: 300.0}


def test_slots_follow_tool_numbers_and_unmapped_extruders_keep_their_index():
    model = {
        "move": {"extruders": [{}, {}, {}]},
        "tools": [{"number": 3, "extruders": [1]}, {"number": 5, "extruders": [0, 1]}],
    }
    assert slots_of(model) == {0: 5, 1: 3, 2: 2}
    assert slots_of({"move": {"extruders": [{}, {}]}}) == {0: 0, 1: 1}


def test_cancelled_and_aborted_jobs():
    for kwargs, outcome in (({"cancelled": True}, JobOutcome.CANCELLED),
                            ({"aborted": True}, JobOutcome.FAILED)):  # fmt: skip
        fake = FakeDuet()
        rig = Rig(fake)
        fake.print_file("a.gcode")
        rig.poll()
        fake.extrude([250.0])
        rig.poll()
        fake.end(**kwargs)
        rig.poll()

        [finished] = rig.sink.of(JobFinished)
        assert finished.outcome is outcome
        assert finished.filament_mm == {0: 250.0}
        assert finished.usage_estimated is False


def test_a_halted_board_fails_the_job():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.print_file("a.gcode")
    rig.poll()
    fake.extrude([50.0])
    fake.status("halted")
    rig.poll()

    [finished] = rig.sink.of(JobFinished)
    assert finished.outcome is JobOutcome.FAILED
    assert "halted" in (finished.note or "")
    assert finished.filament_mm == {0: 50.0}


def test_a_simulation_isnt_a_print():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.simulate_file("a.gcode")
    rig.poll()
    fake.end()
    rig.poll()

    assert rig.sink.events == []


def test_pauses_carry_on_the_same_job():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.print_file("a.gcode")
    rig.poll()
    for status in ("pausing", "paused", "resuming", "changingTool", "processing"):
        fake.status(status)
        fake.extrude([10.0])
        rig.poll()
    fake.end()
    rig.poll()

    assert len(rig.sink.of(JobStarted)) == 1
    assert rig.sink.of(JobFinished)[0].filament_mm == {0: 50.0}


def test_a_restart_mid_print_carries_on_and_the_total_is_still_measured():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.print_file("a.gcode")
    rig.poll()
    fake.extrude([400.0])
    rig.poll(600)
    first = rig.sink.of(JobStarted)[0]

    rig.restart()
    fake.extrude([600.0])
    rig.poll(300)
    fake.end()
    rig.poll()

    [again] = rig.sink.of(JobStarted)
    assert again.job_id == first.job_id
    [finished] = rig.sink.of(JobFinished)
    assert finished.filament_mm == {0: 1000.0}  # rawPosition counts from the job's start
    assert finished.usage_estimated is False
    assert rig.files.reads == ["0:/gcodes/a.gcode"]  # not read again


def test_an_end_while_the_bridge_was_away_is_measured_and_told_by_the_board():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.print_file("a.gcode")
    rig.poll()
    fake.extrude([400.0])
    rig.poll(600)

    rig.restart()
    fake.extrude([100.0])
    fake.end(cancelled=True)
    rig.poll(seconds=3600)

    [start] = rig.sink.of(JobStarted)
    [finished] = rig.sink.of(JobFinished)
    assert finished.job_id == start.job_id
    assert finished.outcome is JobOutcome.CANCELLED
    assert finished.filament_mm == {0: 500.0}
    assert finished.usage_estimated is False
    assert CurrentJob(rig.state).job is None


def test_a_reprint_of_the_same_file_while_away_is_another_job():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.print_file("a.gcode")
    rig.poll()
    fake.extrude([400.0])
    rig.poll(600)
    first = rig.sink.of(JobStarted)[0]

    rig.restart()
    rig.clock.tick(4 * 3600)
    fake.end()
    fake.print_file("a.gcode", duration=1200)
    rig.poll()

    starts = rig.sink.of(JobStarted)
    assert [s.job_id for s in starts][0] == first.job_id
    [finished] = rig.sink.of(JobFinished)
    assert finished.job_id == first.job_id
    assert starts[-1].job_id != first.job_id


def test_a_board_that_restarted_mid_print_fails_the_job_with_what_it_had_fed():
    for restart_bridge in (False, True):
        fake = FakeDuet()
        rig = Rig(fake)
        fake.print_file("a.gcode")
        rig.poll()
        fake.extrude([400.0])
        rig.poll()

        if restart_bridge:
            rig.restart()
        fake.reset()  # a power cut: upTime starts again, rawPosition is 0
        rig.poll()

        [finished] = rig.sink.of(JobFinished)
        assert finished.outcome is JobOutcome.FAILED
        assert "restarted" in (finished.note or "")
        assert finished.filament_mm == {0: 400.0}
        assert finished.usage_estimated is False


def test_after_another_print_ran_while_away_the_job_is_logged_with_the_estimate():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.print_file("a.gcode")
    rig.poll()
    fake.extrude([400.0])
    rig.poll()

    rig.restart()
    fake.end()
    fake.print_file("b.gcode")
    fake.end()
    rig.poll(3600)

    [finished] = rig.sink.of(JobFinished)
    assert finished.outcome is JobOutcome.COMPLETED
    assert finished.usage_estimated is True  # nothing on the board says how it went
    assert finished.filament_mm == {0: 4130.2}


def test_a_clock_jump_doesnt_split_the_job():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.print_file("a.gcode")
    rig.poll()
    rig.clock.tick(3600)  # NTP sets the clock of a Pi without a battery
    fake.extrude([10.0])
    rig.poll()
    fake.end()
    rig.poll()

    assert len(rig.sink.of(JobStarted)) == 1
    [finished] = rig.sink.of(JobFinished)
    assert finished.outcome is JobOutcome.COMPLETED and finished.filament_mm == {0: 10.0}


def test_a_cancel_after_a_long_pause_is_a_cancel():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.print_file("a.gcode")
    rig.poll(600)
    fake.status("paused")
    rig.poll(7200)  # duration stands still while paused
    fake.status("cancelling")
    rig.poll()
    fake.end(cancelled=True)
    rig.poll()

    assert len(rig.sink.of(JobStarted)) == 1
    [finished] = rig.sink.of(JobFinished)
    assert finished.outcome is JobOutcome.CANCELLED


def test_a_job_whose_duration_goes_back_is_another_print_of_the_file():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.print_file("a.gcode")
    rig.poll(600)
    fake.print_file("a.gcode")  # printed again between two polls
    rig.poll()

    assert len(rig.sink.of(JobStarted)) == 2
    assert len(rig.sink.of(JobFinished)) == 1


def test_each_stretch_counts_at_the_extrusion_factor_it_had():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.print_file("a.gcode")
    rig.poll()
    fake.extrude([1000.0])
    rig.poll()
    fake.model["move"]["extruders"][0]["factor"] = 0.5
    fake.extrude([1000.0])
    rig.poll()
    fake.end()
    rig.poll()

    assert rig.sink.of(JobFinished)[0].filament_mm == {0: 1500.0}


def test_the_slicers_estimate_is_per_tool_not_per_extruder():
    fake = FakeDuet(extruders=2, tools=[[1], [0]])
    rig = Rig(fake)
    rig.files.mm = (300.0, 100.0)
    fake.print_file("a.gcode")
    for e in fake.model["move"]["extruders"]:
        del e["rawPosition"]
    fake.job["rawExtrusion"] = None
    rig.poll()
    fake.end()
    rig.poll()

    [finished] = rig.sink.of(JobFinished)
    assert finished.usage_estimated is True
    assert finished.filament_mm == {0: 300.0, 1: 100.0}  # T0's and T1's, whatever they drive


def test_without_measured_positions_the_estimate_is_scaled_by_progress():
    fake = FakeDuet()
    rig = Rig(fake)
    for e in fake.model["move"]["extruders"]:
        del e["rawPosition"]
    fake.print_file("a.gcode")
    fake.job["rawExtrusion"] = None
    rig.poll()
    fake.job["filePosition"] = 250_000
    rig.poll()
    fake.end(cancelled=True)
    rig.poll()

    [finished] = rig.sink.of(JobFinished)
    assert finished.usage_estimated is True
    assert finished.filament_mm == {0: round(4130.2 * 0.25, 1)}


def test_the_job_total_is_used_when_extruders_dont_report_theirs():
    fake = FakeDuet()
    rig = Rig(fake)
    fake.print_file("a.gcode")
    for e in fake.model["move"]["extruders"]:
        del e["rawPosition"]
    rig.poll()
    fake.job["rawExtrusion"] = 700.0
    rig.poll()
    fake.model["move"]["extruders"][0]["factor"] = 1.1
    fake.job["rawExtrusion"] = 800.0
    rig.poll()
    fake.end()
    rig.poll()

    [finished] = rig.sink.of(JobFinished)
    assert finished.filament_mm == {0: 810.0}  # 700 at 1.0, then 100 at 1.1
    assert finished.usage_estimated is False


def test_the_start_carries_a_thumbnail_and_the_slicer_summary_when_the_file_has_them():
    fake = FakeDuet()
    rig = Rig(fake)

    async def thumbnail() -> bytes:
        return b"png"

    from printlog_bridge.core.events import SlicerMetadata

    async def read(path: str) -> JobFile:
        return JobFile(mm={0: 10.0}, thumbnail=thumbnail, metadata=SlicerMetadata(slicer="X"))

    rig.files.read = read  # type: ignore[method-assign]
    fake.print_file("a.gcode")
    rig.poll()

    [image] = rig.sink.of(JobImage)
    assert asyncio.run(image.fetch()) == b"png"
    [settings] = rig.sink.of(JobSlicerSettings)
    assert settings.metadata.slicer == "X" and settings.read is None


def test_merge_patches_objects_and_arrays_in_place():
    base = {"a": {"b": 1, "c": [{"x": 1, "y": 2}, {"x": 3}]}, "d": 1}
    merge(base, {"a": {"c": [{"x": 9}, {"x": 8}]}, "e": 2})
    assert base == {"a": {"b": 1, "c": [{"x": 9, "y": 2}, {"x": 8}]}, "d": 1, "e": 2}
    merge(base, {"a": {"c": [{"x": 0}]}})
    assert base["a"]["c"] == [{"x": 0}]  # another length: replaced
