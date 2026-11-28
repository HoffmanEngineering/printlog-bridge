"""Turns an adapter's "is the job paused?" on each status update into one event per change.

Every adapter already knows when its job is paused (Moonraker's `print_stats.state`, OctoPrint's
flags, Bambu's `gcode_state`...). Feeding that here on every update gives a `JobPaused` when it
starts and a `JobResumed` when it ends, and nothing in between. Short pauses are the sink's to
leave out (`core/lifecycle.py`), not the adapter's.
"""

from __future__ import annotations

from datetime import datetime
from typing import Union

from printlog_bridge.core.events import JobPaused, JobResumed

PauseEvent = Union[JobPaused, JobResumed]


class PauseTracker:
    """One printer's current job and whether it is paused."""

    def __init__(self, printer: str) -> None:
        self.printer = printer
        self._job: str | None = None
        self._paused = False
        self._seen_a_job = False
        """A job has been seen since the tracker began (since the bridge started)."""

    def update(self, job_id: str | None, paused: bool, now: datetime) -> PauseEvent | None:
        """The event for this update, if the job's paused state changed. `job_id` None means no
        job is running: the state resets, with no resume (the job's end closes its pause).

        The first job seen running gets a resume: the bridge may have stopped while it was paused
        and the printer resumed it meanwhile. The sink ignores it when no pause is open."""
        if job_id != self._job:
            self._job, self._paused = job_id, False
        if job_id is None:
            return None
        first, self._seen_a_job = not self._seen_a_job, True
        if first and not paused:
            return JobResumed(printer=self.printer, job_id=job_id, at=now)
        if paused == self._paused:
            return None
        self._paused = paused
        if paused:
            return JobPaused(printer=self.printer, job_id=job_id, at=now)
        return JobResumed(printer=self.printer, job_id=job_id, at=now)
