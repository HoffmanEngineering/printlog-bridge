"""The shared pause tracker adapters feed their paused state to (api#343)."""

from datetime import datetime, timedelta, timezone

from printlog_bridge.adapters.common.pauses import PauseTracker
from printlog_bridge.core.events import JobPaused, JobResumed

T0 = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def test_nothing_while_the_state_is_unchanged():
    tracker = PauseTracker("k3")
    tracker.update("job1", False, T0)
    assert tracker.update("job1", False, T0 + timedelta(seconds=5)) is None


def test_the_first_running_job_seen_closes_a_pause_left_open_before():
    """After a restart, the job may have been paused when the bridge stopped and resumed while
    it was away: the first sight of it running says so (the sink ignores it if no pause is open)."""
    tracker = PauseTracker("k3")

    assert tracker.update("job1", False, T0) == JobResumed(printer="k3", job_id="job1", at=T0)


def test_a_later_job_seen_running_needs_no_resume():
    tracker = PauseTracker("k3")
    tracker.update("job1", False, T0)
    tracker.update(None, False, T0 + timedelta(minutes=1))

    assert tracker.update("job2", False, T0 + timedelta(minutes=2)) is None


def test_one_pause_then_one_resume():
    tracker = PauseTracker("k3")
    tracker.update("job1", False, T0)

    paused = tracker.update("job1", True, T0 + timedelta(minutes=1))
    again = tracker.update("job1", True, T0 + timedelta(minutes=2))
    resumed = tracker.update("job1", False, T0 + timedelta(minutes=3))

    assert paused == JobPaused(printer="k3", job_id="job1", at=T0 + timedelta(minutes=1))
    assert again is None
    assert resumed == JobResumed(printer="k3", job_id="job1", at=T0 + timedelta(minutes=3))


def test_first_sight_of_a_paused_job_is_a_pause():
    tracker = PauseTracker("k3")

    assert tracker.update("job1", True, T0) == JobPaused(printer="k3", job_id="job1", at=T0)


def test_a_new_job_starts_unpaused():
    tracker = PauseTracker("k3")
    tracker.update("job1", True, T0)

    assert tracker.update("job2", False, T0 + timedelta(minutes=1)) is None
    assert tracker.update("job2", True, T0 + timedelta(minutes=2)) == JobPaused(
        printer="k3", job_id="job2", at=T0 + timedelta(minutes=2)
    )


def test_no_job_resets_without_a_resume():
    tracker = PauseTracker("k3")
    tracker.update("job1", True, T0)

    assert tracker.update(None, False, T0 + timedelta(minutes=1)) is None
    assert tracker.update("job1", False, T0 + timedelta(minutes=2)) is None
