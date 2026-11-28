"""Turns an adapter's "is the job paused?" on each status update into one event per change.

Every adapter already knows when its job is paused (Moonraker's `print_stats.state`, OctoPrint's
flags, Bambu's `gcode_state`...). Feeding that here on every update gives a `JobPaused` when it
starts and a `JobResumed` when it ends, and nothing in between. Short pauses are the sink's to
leave out (`core/lifecycle.py`), not the adapter's.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Union

from printlog_bridge.core.events import JobPaused, JobResumed

PauseEvent = Union[JobPaused, JobResumed]


class PauseTracker:
    """One printer's current job and whether it is paused."""

    def __init__(self, printer: str) -> None:
        self.printer = printer
        self._job: Optional[str] = None
        self._paused = False

    def update(self, job_id: Optional[str], paused: bool, now: datetime) -> Optional[PauseEvent]:
        """The event for this update, if the job's paused state changed. `job_id` None means no
        job is running: the state resets, with no resume (the job's end closes its pause)."""
        if job_id != self._job:
            self._job, self._paused = job_id, False
        if job_id is None or paused == self._paused:
            return None
        self._paused = paused
        if paused:
            return JobPaused(printer=self.printer, job_id=job_id, at=now)
        return JobResumed(printer=self.printer, job_id=job_id, at=now)
