"""`CurrentJob`: the running job of a printer that keeps no job history (#82)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.state import PrinterState

STARTED_AT = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = STARTED_AT

    def __call__(self) -> datetime:
        return self.now


def start(**more) -> JobStarted:
    return JobStarted(
        "mk4",
        "job-17",
        "benchy.bgcode",
        STARTED_AT,
        estimated_duration_s=3600.0,
        estimated_filament_mm=4400.0,
        estimated_filament_g=13.1,
        **more,
    )


def current(tmp_path, clock: Clock | None = None) -> CurrentJob:
    return CurrentJob(PrinterState.load(tmp_path, "mk4"), clock=clock or Clock())


def test_nothing_remembered_reconciles_to_nothing(tmp_path):
    assert current(tmp_path).reconcile(None) == []
    assert current(tmp_path).job is None


def test_the_job_is_remembered_across_a_restart(tmp_path):
    current(tmp_path).started(start(estimated_filament_g_by_slot={0: 10.0, 2: 3.1}))

    assert current(tmp_path).job == start(estimated_filament_g_by_slot={0: 10.0, 2: 3.1})


def test_its_slot_notes_are_remembered_too(tmp_path):
    notes = {2: "Slot 3 · Blue (#1E88E5) · PLA"}
    current(tmp_path).started(start(slot_notes=notes))

    assert current(tmp_path).job.slot_notes == notes


def test_the_same_job_still_running_replays_its_start(tmp_path):
    current(tmp_path).started(start())

    events = current(tmp_path).reconcile("job-17")

    assert events == [start()]
    assert current(tmp_path).job == start()


def test_a_job_gone_with_no_outcome_is_logged_finished_at_when_last_seen(tmp_path):
    clock = Clock()
    job = current(tmp_path, clock)
    job.started(start())
    clock.now = STARTED_AT + timedelta(minutes=50)
    job.seen()

    clock.now = STARTED_AT + timedelta(hours=5)
    events = current(tmp_path, clock).reconcile(None)

    replayed, finished = events
    assert replayed == start()
    assert isinstance(finished, JobFinished)
    assert finished.job_id == "job-17"
    assert finished.outcome is JobOutcome.COMPLETED
    assert finished.finished_at == STARTED_AT + timedelta(minutes=50)
    assert finished.usage_estimated is True
    assert finished.filament_g == {0: 13.1}
    assert finished.filament_mm == {0: 4400.0}
    assert "wasn't connected" in (finished.note or "")
    assert current(tmp_path).job is None  # forgotten, so it isn't finished twice


def test_the_printers_own_outcome_is_used_when_it_says(tmp_path):
    current(tmp_path).started(start())

    _, finished = current(tmp_path).reconcile(None, outcome=JobOutcome.FAILED)

    assert finished.outcome is JobOutcome.FAILED
    assert finished.filament_g == {} and finished.filament_mm == {}  # how far it got is unknown


def test_an_end_time_the_printer_gives_wins_and_needs_no_note(tmp_path):
    current(tmp_path).started(start())
    ended = STARTED_AT + timedelta(hours=1, minutes=2)

    _, finished = current(tmp_path).reconcile(
        "job-18", outcome=JobOutcome.COMPLETED, finished_at=ended
    )

    assert finished.finished_at == ended
    assert finished.duration_s == 3720.0
    assert finished.note is None


def test_grams_per_slot_are_the_estimate_of_a_multi_tool_job(tmp_path):
    current(tmp_path).started(start(estimated_filament_g_by_slot={0: 10.0, 2: 3.1}))

    _, finished = current(tmp_path).reconcile(None)

    assert finished.filament_g == {0: 10.0, 2: 3.1}
    assert finished.filament_mm == {}


def test_never_seen_again_ends_when_it_was_remembered(tmp_path):
    clock = Clock()
    current(tmp_path, clock).started(start())
    clock.now = STARTED_AT + timedelta(hours=2)

    _, finished = current(tmp_path, clock).reconcile(None)

    assert finished.finished_at == STARTED_AT  # when it was last known to be running, not now


def test_seen_is_saved_at_most_once_a_minute(tmp_path):
    clock = Clock()
    job = current(tmp_path, clock)
    job.started(start())
    clock.now = STARTED_AT + timedelta(seconds=30)
    job.seen()

    assert current(tmp_path).last_seen == STARTED_AT
    clock.now = STARTED_AT + timedelta(seconds=61)
    job.seen()
    assert current(tmp_path).last_seen == STARTED_AT + timedelta(seconds=61)


def test_ended_forgets_it(tmp_path):
    job = current(tmp_path)
    job.started(start())
    job.ended()

    assert current(tmp_path).job is None
    assert current(tmp_path).reconcile(None) == []


def test_other_discovery_values_are_left_alone(tmp_path):
    state = PrinterState.load(tmp_path, "mk4")
    state.discovery["serial"] = "ABC"
    job = CurrentJob(state, clock=Clock())
    job.started(start())
    job.ended()

    assert PrinterState.load(tmp_path, "mk4").discovery == {"serial": "ABC"}


def test_a_broken_entry_is_treated_as_none(tmp_path):
    state = PrinterState.load(tmp_path, "mk4")
    state.discovery["current_job"] = {"start": {"job_id": 3}}
    state.save()

    assert current(tmp_path).job is None
    assert current(tmp_path).reconcile(None) == []
