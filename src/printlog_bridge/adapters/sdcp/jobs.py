"""Turns an SDCP printer's status pushes into job events (`protocol.py` says what a status
holds and where that comes from).

SDCP keeps no history the bridge can rely on (its history commands are described only for resin
printers), so `CurrentJob` keeps the running job, as for PrusaLink and OctoPrint.

**A job's identity is fixed once**, when a status first shows a file printing or paused (print
state 13, 5, 6 or 12; never the start routine, which may still show the last job's file and
task): `started_at` is the status's time less `CurrentTicks`, and `job_id` is
`<epoch seconds>-<file stem>`. Its file, task id, last progress and layer and when it was first
seen are kept beside `CurrentJob`'s (`state.discovery["sdcp_job"]`), so a reconnect or a bridge
restart finds the job again by its file and task, never by recomputing a time. It stays that job
through pauses, however long, and the start routine of a resume (preheating again); another file,
another task id (ASSUMPTION: one per print, as the field's name says), or its layer and progress
going back while printing is another job.

**The end seen live** is the print state turning complete (9), stopped (8) or error (14), at the
status's time on the printer's clock. The file name may be cleared by then (seen after a cancel),
so an end that names no file or task is this job's. ASSUMPTION: a print that ends goes through
one of those states, which the spec says stay until the next print.

**An end missed** (the bridge was away, or the socket dropped) is decided by evidence on the
first status after a connect: the printer still in an end state for this job (or one naming no
file) says how; else by the progress kept: 99% or more is complete, less is cancelled, with a
note. A printer that was off or restarted is idle with nothing of the job: not an error. The end
is when the bridge last saw the job, on the printer's clock.

Usage is the slicer's estimate from the job's file when it can be read (`files.py`: the start
of its download from the printer's web server, an ASSUMPTION), times the progress for a job
that didn't complete, marked estimated. Without it a job logs with no usage. The file's
thumbnail and slicer header go with the job's start.
"""

from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from printlog_bridge.adapters.common.slicer_meta import JobFile
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

from .protocol import BUSY, COMPLETE, ENDS, ERROR, PRINT_STATES, PRINTING, STOPPED, UNDER_WAY

log = logging.getLogger(__name__)

KEY = "sdcp_job"
"""Where in `state.discovery` the running job's file, task and progress are kept."""
COMPLETE_AT_PERCENT = 99.0
MAX_NAME_IN_ID = 80
SYNCED_AFTER = 1_600_000_000
"""A printer clock before September 2020 isn't set (no network time yet): not used."""
LAYERS_BACK, PERCENT_BACK = 1, 5.0
"""How far layer and progress may go back while printing before it's another print."""
OUTCOMES = {
    COMPLETE: JobOutcome.COMPLETED,
    STOPPED: JobOutcome.CANCELLED,
    ERROR: JobOutcome.FAILED,
}
ERROR_NOTE = "The printer stopped the job with an error."
UNSEEN_END_NOTE = (
    "The bridge didn't see this job end, so it is logged by how far it got: completed at 99% "
    "or more, else cancelled."
)
UNSEEN_TIME_NOTE = (
    "The bridge wasn't connected when this job ended, so its end is when the bridge last saw it "
    "running."
)


class JobFiles(Protocol):
    async def read(self, path: str) -> JobFile:
        """What can be read of the job's file at `path` (as `PrintInfo.Filename` names it)."""


@dataclass(frozen=True)
class View:
    """What the merged status says of the print."""

    state: int | None
    path: str | None
    name: str
    task_id: str | None
    progress: float | None
    layer: int | None
    ticks: float | None
    total_ticks: float | None
    timestamp: float | None
    """The printer's clock, when it is set."""

    @property
    def under_way(self) -> bool:
        return self.state in UNDER_WAY and self.path is not None

    @property
    def busy(self) -> bool:
        return self.state in BUSY

    @property
    def outcome(self) -> JobOutcome | None:
        return OUTCOMES.get(self.state) if self.state in ENDS else None  # type: ignore[arg-type]

    @property
    def words(self) -> str | None:
        if self.state is None:
            return None
        return PRINT_STATES.get(self.state, f"state {self.state}")


def merge(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """`new` over `old`, `PrintInfo` field by field: a push may carry only what changed."""
    merged = dict(old)
    for key, value in new.items():
        if key == "PrintInfo" and isinstance(value, Mapping):
            info = merged.get("PrintInfo")
            merged[key] = {**(info if isinstance(info, Mapping) else {}), **value}
        else:
            merged[key] = value
    return merged


def view_of(status: Mapping[str, Any], timestamp: Any) -> View:
    info = status.get("PrintInfo")
    info = info if isinstance(info, Mapping) else {}
    path = info.get("Filename")
    path = path.strip() if isinstance(path, str) and path.strip() else None
    task = info.get("TaskId")
    task = task.strip() if isinstance(task, str) and task.strip() else None
    state = info.get("Status")
    progress = _number(info.get("Progress"))
    layer = _number(info.get("CurrentLayer"))
    total = _number(info.get("TotalLayer"))
    if progress is None and layer is not None and total:
        progress = layer / total * 100
    when = _number(timestamp)
    return View(
        state=state if isinstance(state, int) and not isinstance(state, bool) else None,
        path=path,
        name=posixpath.basename(path) if path else "",
        task_id=task,
        progress=min(max(progress, 0.0), 100.0) if progress is not None else None,
        layer=int(layer) if layer is not None else None,
        ticks=_number(info.get("CurrentTicks")),
        total_ticks=_number(info.get("TotalTicks")),
        timestamp=when if when is not None and when >= SYNCED_AFTER else None,
    )


@dataclass
class Job:
    job_id: str
    path: str | None
    task_id: str | None
    start: JobStarted
    file: JobFile | None = None
    progress: float = 0.0
    layer: int | None = None
    first_seen: float = 0.0
    """When the bridge first saw it, on the printer's clock."""


class JobTracker:
    def __init__(
        self,
        printer: str,
        sink: EventSink,
        current: CurrentJob,
        *,
        files: JobFiles | None = None,
        report: PrinterReport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.printer = printer
        self.sink = sink
        self.current = current
        self.files = files
        self.report = report
        self.clock = clock
        self.job: Job | None = None
        self.status: dict[str, Any] = {}
        """The printer's status, every push merged in."""
        self._reconciled = False
        self._ended: Job | None = None
        """The job that just ended, until the printer moves on from it: a late status still
        showing it printing doesn't start it again."""
        self._offset = timedelta(0)
        """The printer's clock less the bridge's."""

    def connected(self) -> None:
        """A new connection: a full status comes first, and is checked against the kept job."""
        self.status = {}
        self._reconciled = False

    def _now(self) -> datetime:
        """Now, on the printer's clock."""
        return self.clock() + self._offset

    # -- the kept job ---------------------------------------------------------------------

    def _keep(self, job: Job) -> None:
        """In memory now; written with `CurrentJob`'s own saves (at its start, then at most once
        a minute)."""
        self.current.state.discovery[KEY] = {
            "job_id": job.job_id,
            "path": job.path,
            "task_id": job.task_id,
            "progress": round(job.progress, 2),
            "layer": job.layer,
            "first_seen": job.first_seen,
        }

    def _remembered(self) -> Job | None:
        start = self.current.job
        if start is None:
            return None
        kept = self.current.state.discovery.get(KEY)
        if not isinstance(kept, Mapping) or kept.get("job_id") != start.job_id:
            return Job(start.job_id, None, None, start)
        path, task, layer = kept.get("path"), kept.get("task_id"), kept.get("layer")
        return Job(
            start.job_id,
            path if isinstance(path, str) else None,
            task if isinstance(task, str) else None,
            start,
            progress=_number(kept.get("progress")) or 0.0,
            layer=layer if isinstance(layer, int) else None,
            first_seen=_number(kept.get("first_seen")) or 0.0,
        )

    # -- statuses -------------------------------------------------------------------------

    async def on_status(self, message: Mapping[str, Any]) -> None:
        status = message.get("Status")
        if not isinstance(status, Mapping):
            return
        self.status = merge(self.status, status)
        view = view_of(self.status, message.get("TimeStamp"))
        if view.timestamp is not None:
            self._offset = timedelta(seconds=view.timestamp - self.clock().timestamp())
        if self.report is not None and view.words is not None:
            self.report.printer_state(view.words)
        if not self._reconciled:
            self._reconciled = True
            await self._reconcile(view)
        await self._step(view)

    def _same_task(self, view: View, path: str | None, task: str | None) -> bool:
        """`view` names the same file (or none) and no other task."""
        if view.path is not None and path is not None and view.path != path:
            return False
        return view.task_id is None or task is None or view.task_id == task

    def _continues(self, view: View, job: Job) -> bool:
        """`view` shows `job` still printing, paused or busy (preheating again, a filament
        change, stopping)."""
        if not (view.under_way or view.busy) or view.path is None:
            return False
        if job.path is not None and view.path != job.path:
            return False
        if job.path is None and view.name != job.start.file_name:
            return False  # a job kept before its path was
        if not self._same_task(view, job.path, job.task_id):
            return False
        return not self._went_back(view, job)

    @staticmethod
    def _went_back(view: View, job: Job) -> bool:
        """Printing again from an earlier layer and percent: another print of the same file."""
        if view.state != PRINTING:
            return False
        layer_back = (
            view.layer is not None
            and job.layer is not None
            and view.layer < job.layer - LAYERS_BACK
        )
        percent_back = view.progress is not None and view.progress < job.progress - PERCENT_BACK
        if view.layer is None or job.layer is None:
            return percent_back
        if view.progress is None:
            return layer_back
        return layer_back and percent_back

    def _ends(self, view: View, job: Job) -> bool:
        """`view` is an end state of `job`, or of a job it doesn't name."""
        return view.outcome is not None and self._same_task(view, job.path, job.task_id)

    async def _reconcile(self, view: View) -> None:
        if self.job is None:
            remembered = self._remembered()
            if remembered is None:
                return
            # The bridge restarted: its start again (the sink recognizes it), then either the
            # job carries on or it ended while the bridge was away.
            self.job = remembered
            await self.sink.emit(remembered.start)
        if self._continues(view, self.job):
            self.current.seen()
        else:
            await self._finish_unseen(view)

    async def _step(self, view: View) -> None:
        job = self.job
        if job is not None:
            if self._ends(view, job):
                note = ERROR_NOTE if view.outcome is JobOutcome.FAILED else None
                at = (
                    datetime.fromtimestamp(view.timestamp, timezone.utc)
                    if view.timestamp is not None
                    else None
                )
                await self._finish(view.outcome, note, finished_at=at)  # type: ignore[arg-type]
                return
            if self._continues(view, job):
                self._progress(view)
                return
            await self._finish_unseen(view)  # another job, or none: this one's end wasn't seen
        if self._ended is not None and not self._just_ended(view):
            self._ended = None
        if view.under_way and self._ended is None:
            await self._start(view)

    def _just_ended(self, view: View) -> bool:
        """`view` is still the job that just ended: its end state, or a late status of it."""
        ended = self._ended
        assert ended is not None
        if view.outcome is not None:
            return True
        return (
            view.under_way
            and view.path == ended.path
            and self._same_task(view, ended.path, ended.task_id)
            and not self._went_back(view, ended)
        )

    def _progress(self, view: View) -> None:
        job = self.job
        assert job is not None
        if view.under_way:  # the start routine and a filament change may show no progress
            if view.progress is not None:
                job.progress = view.progress
            if view.layer is not None:
                job.layer = view.layer
            self._keep(job)
        self.current.seen()

    async def _start(self, view: View) -> None:
        file = await self.files.read(view.path) if self.files is not None else JobFile()  # type: ignore[arg-type]
        when = (
            datetime.fromtimestamp(view.timestamp, timezone.utc)
            if view.timestamp is not None
            else self._now()
        )
        started_at = when - timedelta(seconds=max(view.ticks or 0.0, 0.0))
        stem = posixpath.splitext(view.name)[0]
        stem = re.sub(r"\s+", " ", stem).strip()[:MAX_NAME_IN_ID] or "print"
        job_id = f"{int(started_at.timestamp())}-{stem}"
        start = JobStarted(
            printer=self.printer,
            job_id=job_id,
            file_name=view.name,
            started_at=started_at,
            estimated_duration_s=file.duration_s or view.total_ticks or None,
            estimated_filament_mm=round(sum(file.mm.values()), 1) or None,
            estimated_filament_g=round(sum(file.grams.values()), 3) or None,
            estimated_filament_g_by_slot=dict(file.grams) if len(file.grams) > 1 else {},
        )
        self.job = Job(
            job_id,
            view.path,
            view.task_id,
            start,
            file,
            progress=view.progress or 0.0,
            layer=view.layer,
            first_seen=when.timestamp(),
        )
        self._keep(self.job)
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

    async def _finish_unseen(self, view: View) -> None:
        job = self.job
        assert job is not None
        if self._ends(view, job):
            outcome: JobOutcome = view.outcome  # type: ignore[assignment]
            note = UNSEEN_TIME_NOTE
            if outcome is JobOutcome.FAILED:
                note = f"{ERROR_NOTE} {UNSEEN_TIME_NOTE}"
        else:
            complete = job.progress >= COMPLETE_AT_PERCENT
            outcome = JobOutcome.COMPLETED if complete else JobOutcome.CANCELLED
            note = UNSEEN_END_NOTE
        seen = self.current.last_seen
        await self._finish(
            outcome, note, finished_at=(seen + self._offset) if seen is not None else None
        )

    async def _finish(
        self, outcome: JobOutcome, note: str | None, *, finished_at: datetime | None = None
    ) -> None:
        job = self.job
        assert job is not None
        started = job.start.started_at
        finished = max(finished_at or self._now(), started)
        fraction = 1.0 if outcome is JobOutcome.COMPLETED else job.progress / 100
        grams, mm = _usage(job, fraction)
        await self.sink.emit(
            JobFinished(
                printer=self.printer,
                job_id=job.job_id,
                outcome=outcome,
                finished_at=finished,
                duration_s=(finished - started).total_seconds(),
                filament_mm=mm,
                filament_g=grams,
                usage_estimated=bool(grams or mm),
                note=note,
            )
        )
        self.current.state.discovery.pop(KEY, None)
        self.current.ended()
        self._ended = job
        self.job = None
        log.info("[printer %s] job %s %s", self.printer, job.job_id, outcome.value)


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
