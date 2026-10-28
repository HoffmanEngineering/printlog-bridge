"""Turns OctoPrint's push socket into job events.

Checked against OctoPrint 1.11.8 with its virtual printer (`tests/fixtures/octoprint/`):

- After `auth`, the socket sends `history` once, then `current` every half second times the
  throttle: both have `state` (`text` and `flags`: `printing`, `paused`, `pausing`, `resuming`,
  `cancelling`, `finishing`, `error`, `closedOrError`...), `job` (`file` with `origin`, `path`,
  `display`, `size`; `estimatedPrintTime`; `filament.tool<N>.length`/`volume`), `progress`
  (`completion` in percent, `printTime` in seconds), `serverTime` and `markings`: the print
  chart's marks, each a `type` and an epoch `time`. A print's start is a `print` mark
  (`Start`), its end a `done` or `cancel` mark.
- `event` messages: `PrintStarted`, `PrintDone` (`time`, seconds), `PrintCancelled`
  (`progress`, `time`) followed by `PrintFailed` with `reason` `cancelled`, or `PrintFailed`
  with `reason` `error` after an `Error` (`error`, the firmware's words).

A job starts when a status first shows the printer printing (or paused) a file: by then the
status carries the start mark, whose time is the job's `started_at` and, with the file's name,
its `job_id` (`<epoch seconds>-<file stem>`). OctoPrint has no job id of its own; the mark stays
the same across reconnects and restarts of the bridge (not of OctoPrint, which ends the print
anyway). An OctoPrint without marks gives `serverTime` less `printTime`, which moves a little
from one connect to the next, so a remembered job of the same file that started within
`SAME_JOB_S` of it is the same job. The end comes from the events. Missed (the bridge was
away), it is read from the marks: a `done` or `cancel` mark after the start says how and when
the job ended; without one, the printer in error is a failure, else 99% or more is complete
and less is cancelled, with a note.

Usage is the slicer's estimate per tool (`files.py`), times the progress for a job that didn't
complete, marked estimated.
"""

from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from printlog_bridge.adapters.prusalink.files import JobFile
from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.events import (
    EventSink,
    ImageKind,
    JobFinished,
    JobImage,
    JobOutcome,
    JobSlicerSettings,
    JobStarted,
    SlicerFile,
)
from printlog_bridge.core.health import utc_now
from printlog_bridge.core.status import PrinterReport

from .files import from_analysis

log = logging.getLogger(__name__)

ACTIVE_FLAGS = ("printing", "paused", "pausing", "resuming")
BUSY_FLAGS = ("cancelling", "finishing")
"""On the way out of a job: its end event is coming."""
COMPLETE_AT_PERCENT = 99.0
SAME_JOB_S = 120.0
MAX_NAME_IN_ID = 80
UNSEEN_END_NOTE = (
    "The bridge didn't see this job end, so it is logged by how far it got: completed at 99% "
    "or more, else cancelled."
)
ERROR_NOTE = "OctoPrint stopped the job with an error"


class JobFiles(Protocol):
    async def read(self, job: Mapping[str, Any]) -> JobFile:
        """The file of `/api/job`'s (or a status's) `job`."""


@dataclass(frozen=True)
class View:
    """What a `history` or `current` message says."""

    state: str
    flags: Mapping[str, Any]
    job: Mapping[str, Any]
    path: str | None
    name: str
    completion: float | None
    print_time: float | None
    server_time: float | None
    marks: tuple[tuple[str, float], ...]

    @property
    def active(self) -> bool:
        return self.path is not None and any(self.flags.get(f) is True for f in ACTIVE_FLAGS)

    @property
    def busy(self) -> bool:
        return any(self.flags.get(f) is True for f in BUSY_FLAGS)

    @property
    def failed(self) -> bool:
        return self.flags.get("error") is True or self.flags.get("closedOrError") is True

    def start_mark(self) -> float | None:
        starts = [t for kind, t in self.marks if kind == "print"]
        return max(starts) if starts else None

    def end_mark(self, after: float) -> tuple[str, float] | None:
        """The first `done` or `cancel` mark after `after` (epoch seconds), less a second."""
        ends = sorted((t, kind) for kind, t in self.marks if kind in ("done", "cancel"))
        for t, kind in ends:
            if t >= after - 1:
                return kind, t
        return None


def view_of(status: Mapping[str, Any]) -> View | None:
    state = status.get("state")
    if not isinstance(state, Mapping):
        return None
    flags = state.get("flags") if isinstance(state.get("flags"), Mapping) else {}
    job = status.get("job") if isinstance(status.get("job"), Mapping) else {}
    file = job.get("file") if isinstance(job.get("file"), Mapping) else {}  # type: ignore[union-attr]
    path = file.get("path")  # type: ignore[union-attr]
    name = file.get("display") or file.get("name") or path or ""  # type: ignore[union-attr]
    progress = status.get("progress") if isinstance(status.get("progress"), Mapping) else {}
    marks = []
    for mark in status.get("markings") or ():
        if isinstance(mark, Mapping) and isinstance(mark.get("type"), str):
            t = _number(mark.get("time"))
            if t is not None:
                marks.append((mark["type"], t))
    return View(
        state=str(state.get("text") or ""),
        flags=flags,  # type: ignore[arg-type]
        job=job,  # type: ignore[arg-type]
        path=path if isinstance(path, str) and path else None,
        name=posixpath.basename(str(name)),
        completion=_number(progress.get("completion")),  # type: ignore[union-attr]
        print_time=_number(progress.get("printTime")),  # type: ignore[union-attr]
        server_time=_number(status.get("serverTime")),
        marks=tuple(marks),
    )


@dataclass
class Job:
    job_id: str
    path: str | None
    start: JobStarted
    file: JobFile | None = None
    progress: float = 0.0
    """Percent, as OctoPrint last said."""


class JobTracker:
    def __init__(
        self,
        printer: str,
        sink: EventSink,
        current: CurrentJob,
        *,
        files: JobFiles | None = None,
        report: PrinterReport | None = None,
        snapshot: Callable[[], Awaitable[bytes | None]] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.printer = printer
        self.sink = sink
        self.current = current
        self.files = files
        self.report = report
        self.snapshot = snapshot
        """Fetches a webcam frame, for each job's end; None for no webcam."""
        self.clock = clock
        self.job: Job | None = None
        self._reconciled = False
        self._ended: str | None = None
        """The last job ended here: a late status still showing it never starts it again."""
        self._error: str | None = None

    @property
    def active(self) -> bool:
        return self.job is not None

    def connected(self) -> None:
        """A new connection: the next status is checked against the remembered job."""
        self._reconciled = False

    # -- statuses -------------------------------------------------------------------------

    async def on_status(self, status: Mapping[str, Any]) -> None:
        view = view_of(status)
        if view is None:
            return
        if self.report is not None and view.state:
            self.report.printer_state(view.state.lower())
        if not self._reconciled:
            await self._reconcile(view)
            self._reconciled = True
        await self._step(view)

    def _started_at(self, view: View) -> datetime:
        mark = view.start_mark()
        if mark is not None:
            return datetime.fromtimestamp(mark, timezone.utc)
        if view.server_time is not None and view.print_time is not None:
            return datetime.fromtimestamp(view.server_time - view.print_time, timezone.utc)
        return self.clock() - timedelta(seconds=view.print_time or 0)

    def _job_id(self, view: View) -> str:
        stem = posixpath.splitext(view.name)[0]
        stem = re.sub(r"\s+", " ", stem).strip()[:MAX_NAME_IN_ID] or "print"
        return f"{int(self._started_at(view).timestamp())}-{stem}"

    def _same(self, start: JobStarted, view: View) -> bool:
        """`start` is the job `view` shows: the same id, or, without a start mark to make the
        id from, the same file started within `SAME_JOB_S`."""
        if start.job_id == self._job_id(view):
            return True
        if view.start_mark() is not None or start.file_name != view.name:
            return False
        gap = abs((start.started_at - self._started_at(view)).total_seconds())
        return gap <= SAME_JOB_S

    async def _reconcile(self, view: View) -> None:
        remembered = self.current.job
        running = None
        if view.active or (view.busy and self.job is not None):
            if remembered is not None and self._same(remembered, view):
                running = remembered.job_id
            elif self.job is not None and self._same(self.job.start, view):
                running = self.job.job_id
            else:
                running = self._job_id(view)
        outcome = finished_at = None
        if remembered is not None and running != remembered.job_id:
            outcome, finished_at = self._ending(view, remembered.started_at)
        for event in self.current.reconcile(running, outcome, finished_at):
            await self.sink.emit(event)
        if self.job is not None and self.job.job_id == running:
            pass  # the connection dropped while it ran: carry on with it
        elif remembered is not None and running == remembered.job_id:
            # The bridge restarted while it ran: the start's estimates are its usage.
            self.job = Job(remembered.job_id, view.path, remembered)
        else:
            self.job = None

    def _ending(
        self, view: View, started_at: datetime
    ) -> tuple[JobOutcome | None, datetime | None]:
        """How and when a job that started at `started_at` ended, by the marks; (None, None)
        when they don't say."""
        end = view.end_mark(started_at.timestamp())
        if end is None:
            return None, None
        kind, t = end
        outcome = JobOutcome.COMPLETED if kind == "done" else JobOutcome.CANCELLED
        return outcome, datetime.fromtimestamp(t, timezone.utc)

    async def _step(self, view: View) -> None:
        if view.active:
            if self.job is not None and not self._same(self.job.start, view):
                await self._finish_unseen(view)  # another job, without this one's end seen
            if self.job is None:
                if self._job_id(view) != self._ended:
                    await self._start(view)
                return
            if view.completion is not None:
                self.job.progress = min(max(view.completion, 0.0), 100.0)
            self.current.seen()
            return
        if self.job is not None and not view.busy:
            await self._finish_unseen(view)

    async def _start(self, view: View) -> None:
        file = (
            await self.files.read(view.job) if self.files is not None else from_analysis(view.job)
        )
        if file.printer_model and self.report is not None:
            self.report.printer_model(file.printer_model)
        job_id = self._job_id(view)
        start = JobStarted(
            printer=self.printer,
            job_id=job_id,
            file_name=view.name,
            started_at=self._started_at(view),
            estimated_duration_s=file.duration_s,
            estimated_filament_mm=round(sum(file.mm.values()), 1) or None,
            estimated_filament_g=round(sum(file.grams.values()), 3) or None,
            estimated_filament_g_by_slot=dict(file.grams) if len(file.grams) > 1 else {},
        )
        self.job = Job(job_id, view.path, start, file, view.completion or 0.0)
        self._error = None
        await self.sink.emit(start)
        self.current.started(start)
        log.info("[printer %s] job %s started", self.printer, job_id)
        if file.thumbnail is not None:
            thumbnail = file.thumbnail

            async def fetch_thumbnail() -> bytes | None:
                return thumbnail

            await self.sink.emit(
                JobImage(self.printer, job_id, ImageKind.THUMBNAIL, fetch_thumbnail)
            )
        if file.settings is not None or file.metadata is not None:
            settings = file.settings

            async def read_settings() -> SlicerFile | None:
                return settings

            await self.sink.emit(
                JobSlicerSettings(self.printer, job_id, metadata=file.metadata, read=read_settings)
            )

    # -- events ---------------------------------------------------------------------------

    async def on_event(self, kind: str, payload: Any) -> None:
        payload = payload if isinstance(payload, Mapping) else {}
        if kind == "Error":
            error = payload.get("error")
            self._error = error.strip() if isinstance(error, str) and error.strip() else None
            return
        if kind not in ("PrintDone", "PrintCancelled", "PrintFailed") or self.job is None:
            return
        path = payload.get("path")
        if isinstance(path, str) and self.job.path is not None and path != self.job.path:
            return  # not this job's: its own end was missed, which a status will tell
        seconds = _number(payload.get("time"))
        progress = _number(payload.get("progress"))
        if kind == "PrintDone":
            await self._finish(JobOutcome.COMPLETED, None, seconds)
        elif kind == "PrintCancelled" or payload.get("reason") == "cancelled":
            await self._finish(JobOutcome.CANCELLED, None, seconds, progress)
        else:
            note = f"{ERROR_NOTE}: {self._error}." if self._error else f"{ERROR_NOTE}."
            await self._finish(JobOutcome.FAILED, note, seconds, progress)

    async def _finish_unseen(self, view: View) -> None:
        job = self.job
        assert job is not None
        outcome, finished_at = self._ending(view, job.start.started_at)
        note = None
        if outcome is None:
            if view.failed:
                outcome = JobOutcome.FAILED
                note = f"{ERROR_NOTE}: {self._error}." if self._error else f"{ERROR_NOTE}."
            else:
                complete = job.progress >= COMPLETE_AT_PERCENT
                outcome = JobOutcome.COMPLETED if complete else JobOutcome.CANCELLED
                note = UNSEEN_END_NOTE
        await self._finish(outcome, note, finished_at=finished_at)

    async def _finish(
        self,
        outcome: JobOutcome,
        note: str | None,
        seconds: float | None = None,
        progress: float | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        job = self.job
        assert job is not None
        if outcome is JobOutcome.COMPLETED:
            fraction = 1.0
        else:
            fraction = min(max(progress if progress is not None else job.progress, 0.0), 100.0)
            fraction /= 100
        grams, mm = _usage(job, fraction)
        now = finished_at or self.clock()
        duration = seconds if seconds is not None and seconds >= 0 else None
        if duration is None:
            duration = max(0.0, (now - job.start.started_at).total_seconds())
        await self.sink.emit(
            JobFinished(
                printer=self.printer,
                job_id=job.job_id,
                outcome=outcome,
                finished_at=now,
                duration_s=duration,
                filament_mm=mm,
                filament_g=grams,
                usage_estimated=bool(grams or mm),
                note=note,
            )
        )
        self.current.ended()
        self._ended = job.job_id
        self.job = None
        log.info("[printer %s] job %s %s", self.printer, job.job_id, outcome.value)
        if self.snapshot is not None and finished_at is None:
            await self.sink.emit(
                JobImage(self.printer, job.job_id, ImageKind.SNAPSHOT, self.snapshot)
            )


def _usage(job: Job, fraction: float) -> tuple[dict[int, float], dict[int, float]]:
    """The slicer's grams and millimeters per slot times `fraction`: the file's when it was
    read, else the remembered start's (after a restart)."""
    if fraction <= 0:
        return {}, {}
    if job.file is not None:
        grams, mm = job.file.grams, job.file.mm
    else:
        start = job.start
        grams = dict(start.estimated_filament_g_by_slot) or (
            {0: start.estimated_filament_g} if start.estimated_filament_g else {}
        )
        mm = {0: start.estimated_filament_mm} if start.estimated_filament_mm else {}
        if len(grams) > 1:
            mm = {}  # the length per tool isn't remembered: weight alone
    return (
        {slot: round(g * fraction, 3) for slot, g in grams.items()},
        {slot: round(m * fraction, 1) for slot, m in mm.items()},
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number else None
