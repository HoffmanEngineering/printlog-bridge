"""Turns a FlashForge printer's `/detail`, polled, into job events (`protocol.py` says what it
holds and where that comes from).

The printer keeps no job history the bridge can read (`/gcodeList` names the last files, with
no times or outcomes), so `CurrentJob` keeps the running job, as for PrusaLink and SDCP.

**A job's identity is fixed once**, when a status first shows a file printing or paused:
`started_at` is now less `printDuration`, and `job_id` is `<epoch seconds>-<file stem>`. Its
file, last progress and duration are kept beside `CurrentJob`'s (`state.discovery
["flashforge_job"]`), so a reconnect or a bridge restart finds the job again by its file, never
by recomputing a time. It stays that job while the status shows that file printing, paused or
busy (heating, calibrating), however long; another file, or the duration and progress going back
while printing (another print of the same file), is another job.

**The end seen live** is the status turning `completed`, `cancel` or `error` (an end naming no
file is this job's), at the poll that saw it. **An end missed** (the bridge was away, or the
printer unreachable) is decided on the first status after a connect: the printer still in an
end state for this file says how, at when the bridge last saw the job; anything else (`ready`,
another file) ends it by the progress kept: 99% or more complete, less cancelled, with a note.

**Usage is the slicer's estimate, as the printer read it from the file**: `/detail`'s
`estimatedRightWeight` (grams) and `estimatedRightLen` (millimeters) for the whole job (the
largest seen, since the printer may report 0 until it has read the file), times the progress for
a job that didn't complete, sent with `usage_estimated=True`. The printer measures nothing. On an
AD5X, `/gcodeList`'s tools for the file split the grams by IFS slot (`files.grams_by_slot`); a
one-slot print puts everything on its slot. The job's file is never downloaded: the API has no
read-only way to.

The AD5X's IFS slots (`matlStationInfo.slotInfos`) are reported as spool readings (material and
color; no tag) whenever one changes, for 3D Print Log's note when a slot holds something other
than its spool.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.events import (
    EventSink,
    ImageKind,
    JobFinished,
    JobImage,
    JobOutcome,
    JobPoint,
    JobStarted,
    SpoolReading,
)
from printlog_bridge.core.health import utc_now
from printlog_bridge.core.status import PrinterReport

from .protocol import CANCEL, COMPLETED, ERROR, PRINTING, Slot, View, view_of

log = logging.getLogger(__name__)

KEY = "flashforge_job"
"""Where in `state.discovery` the running job's file, progress and estimates are kept."""
COMPLETE_AT_PERCENT = 99.0
MAX_NAME_IN_ID = 80
SECONDS_BACK, PERCENT_BACK = 120.0, 5.0
"""How far the duration and progress may go back while printing before it's another print."""
OUTCOMES = {COMPLETED: JobOutcome.COMPLETED, CANCEL: JobOutcome.CANCELLED, ERROR: JobOutcome.FAILED}
UNSEEN_END_NOTE = (
    "The bridge didn't see this job end, so it is logged by how far it got: completed at 99% "
    "or more, else cancelled."
)
UNSEEN_TIME_NOTE = (
    "The bridge wasn't connected when this job ended, so its end is when the bridge last saw it "
    "running."
)


class JobFiles(Protocol):
    async def grams_by_slot(self, path: str) -> dict[int, float]:
        """The slicer's grams per slot for the file at `path`, when the printer says (AD5X)."""

    async def thumbnail(self, path: str) -> bytes | None:
        """The file's thumbnail, or None."""


SpoolReporter = Callable[[SpoolReading], Awaitable[Any]]


@dataclass
class Job:
    job_id: str
    path: str | None
    start: JobStarted
    progress: float = 0.0
    duration_s: float = 0.0
    grams: float = 0.0
    mm: float = 0.0
    by_slot: dict[int, float] = field(default_factory=dict)
    slot: int = 0
    """Where a one-slot print's filament goes: the IFS slot feeding at its start, else 0."""


def error_note(view: View) -> str:
    code = f" (error code {view.error_code})" if view.error_code else ""
    return f"The printer stopped the job with an error{code}."


class JobTracker:
    def __init__(
        self,
        printer: str,
        sink: EventSink,
        current: CurrentJob,
        *,
        files: JobFiles | None = None,
        report: PrinterReport | None = None,
        spools: SpoolReporter | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.printer = printer
        self.sink = sink
        self.current = current
        self.files = files
        self.report = report
        self.spools = spools
        self.clock = clock
        self.job: Job | None = None
        self.view: View | None = None
        self._reconciled = False
        self._ended: Job | None = None
        """The job that just ended, until the printer moves on: a late status still showing it
        printing doesn't start it again."""
        self._slots: dict[int, tuple[str | None, str | None]] = {}
        self._tasks: set[asyncio.Future] = set()

    @property
    def active(self) -> bool:
        """A job is under way or about to be: poll more often."""
        view = self.view
        return self.job is not None or (view is not None and (view.under_way or view.busy))

    def connected(self) -> None:
        """A new connection: the next status is checked against the kept job."""
        self._reconciled = False

    # -- the kept job ---------------------------------------------------------------------

    def _keep(self, job: Job) -> None:
        """In memory now; written with `CurrentJob`'s own saves."""
        self.current.state.discovery[KEY] = {
            "job_id": job.job_id,
            "path": job.path,
            "progress": round(job.progress, 2),
            "duration_s": round(job.duration_s, 1),
            "grams": job.grams,
            "mm": job.mm,
            "by_slot": {str(k): v for k, v in job.by_slot.items()},
            "slot": job.slot,
        }

    def _remembered(self) -> Job | None:
        start = self.current.job
        if start is None:
            return None
        job = Job(start.job_id, None, start)
        job.grams = start.estimated_filament_g or 0.0
        job.mm = start.estimated_filament_mm or 0.0
        job.by_slot = dict(start.estimated_filament_g_by_slot)
        kept = self.current.state.discovery.get(KEY)
        if not isinstance(kept, Mapping) or kept.get("job_id") != start.job_id:
            return job
        path = kept.get("path")
        job.path = path if isinstance(path, str) else None
        job.progress = _number(kept.get("progress"))
        job.duration_s = _number(kept.get("duration_s"))
        job.grams = _number(kept.get("grams")) or job.grams
        job.mm = _number(kept.get("mm")) or job.mm
        by_slot = kept.get("by_slot")
        if isinstance(by_slot, Mapping):
            job.by_slot = {
                int(k): float(v)
                for k, v in by_slot.items()
                if str(k).isdigit() and isinstance(v, (int, float))
            }
        slot = kept.get("slot")
        job.slot = slot if isinstance(slot, int) and not isinstance(slot, bool) else 0
        return job

    # -- statuses -------------------------------------------------------------------------

    async def on_detail(self, detail: Mapping[str, Any]) -> None:
        view = view_of(detail)
        self.view = view
        if self.report is not None and view.state is not None:
            self.report.printer_state(view.state)
        self._readings(view)
        if view.state is None:
            return
        if not self._reconciled:
            self._reconciled = True
            await self._reconcile(view)
        await self._step(view)

    def _continues(self, view: View, job: Job) -> bool:
        """`view` shows `job` still printing, paused or busy."""
        if not (view.under_way or view.busy):
            return False
        if view.path is None:
            return view.busy  # heating or calibrating may show no file (ASSUMPTION)
        if job.path is not None and view.path != job.path:
            return False
        if job.path is None and view.name != job.start.file_name:
            return False
        return not self._went_back(view, job)

    @staticmethod
    def _went_back(view: View, job: Job) -> bool:
        """Printing again from the start: another print of the same file."""
        if view.state != PRINTING or view.duration_s is None:
            return False
        if view.duration_s >= job.duration_s - SECONDS_BACK:
            return False
        return view.progress is None or view.progress < job.progress - PERCENT_BACK

    def _ends(self, view: View, job: Job) -> bool:
        if not view.ended:
            return False
        if view.path is None:
            return True
        return view.path == job.path if job.path is not None else view.name == job.start.file_name

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
                outcome = OUTCOMES[view.state]  # type: ignore[index]
                note = error_note(view) if outcome is JobOutcome.FAILED else None
                if view.progress:  # the end's own progress, when it keeps one, is the latest
                    job.progress = view.progress
                await self._finish(outcome, note)
                return
            if self._continues(view, job):
                self._progress(view, job)
                return
            await self._finish_unseen(view)  # another job, or none: this one's end wasn't seen
        if self._ended is not None and not self._just_ended(view):
            self._ended = None
        if view.under_way and self._ended is None:
            await self._start(view)

    def _just_ended(self, view: View) -> bool:
        ended = self._ended
        assert ended is not None
        if view.ended or view.busy:
            return True
        return view.under_way and view.path == ended.path and not self._went_back(view, ended)

    def _progress(self, view: View, job: Job) -> None:
        if view.under_way:
            if view.progress is not None:
                job.progress = view.progress
            if view.duration_s is not None:
                job.duration_s = view.duration_s
            job.grams = max(job.grams, view.estimated_g or 0.0)
            job.mm = max(job.mm, view.estimated_mm or 0.0)
            self._keep(job)
        self.current.seen()

    async def _start(self, view: View) -> None:
        by_slot: dict[int, float] = {}
        if self.files is not None:
            by_slot = await self.files.grams_by_slot(view.path)  # type: ignore[arg-type]
        started_at = self.clock() - timedelta(seconds=view.duration_s or 0.0)
        stem = posixpath.splitext(view.name)[0]
        stem = re.sub(r"\s+", " ", stem).strip()[:MAX_NAME_IN_ID] or "print"
        job_id = f"{int(started_at.timestamp())}-{stem}"
        grams = view.estimated_g or round(sum(by_slot.values()), 3) or None
        if view.duration_s is not None and view.remaining_s is not None:
            duration = view.duration_s + view.remaining_s
        else:
            duration = view.remaining_s
        start = JobStarted(
            printer=self.printer,
            job_id=job_id,
            file_name=view.name,
            started_at=started_at,
            estimated_duration_s=duration,
            estimated_filament_mm=round(view.estimated_mm, 1) if view.estimated_mm else None,
            estimated_filament_g=round(grams, 3) if grams else None,
            estimated_filament_g_by_slot=dict(by_slot) if len(by_slot) > 1 else {},
        )
        job = Job(job_id, view.path, start, view.progress or 0.0, view.duration_s or 0.0)
        job.grams, job.mm, job.by_slot = grams or 0.0, view.estimated_mm or 0.0, by_slot
        if len(by_slot) == 1:
            job.slot = next(iter(by_slot))
        elif view.current_slot is not None:
            job.slot = view.current_slot
        self.job = job
        self._keep(job)
        await self.sink.emit(start)
        self.current.started(start)
        log.info("[printer %s] job %s started", self.printer, job_id)
        if self.files is not None and view.path is not None:
            files, path = self.files, view.path

            async def fetch_thumbnail() -> bytes | None:
                return await files.thumbnail(path)

            await self.sink.emit(
                JobImage(self.printer, job_id, ImageKind.THUMBNAIL, fetch_thumbnail)
            )

    async def _finish_unseen(self, view: View) -> None:
        job = self.job
        assert job is not None
        if self._ends(view, job):
            outcome = OUTCOMES[view.state]  # type: ignore[index]
            note = UNSEEN_TIME_NOTE
            if outcome is JobOutcome.FAILED:
                note = f"{error_note(view)} {UNSEEN_TIME_NOTE}"
        else:
            complete = job.progress >= COMPLETE_AT_PERCENT
            outcome = JobOutcome.COMPLETED if complete else JobOutcome.CANCELLED
            note = UNSEEN_END_NOTE
        await self._finish(outcome, note, finished_at=self.current.last_seen)

    async def _finish(
        self, outcome: JobOutcome, note: str | None, *, finished_at: datetime | None = None
    ) -> None:
        job = self.job
        assert job is not None
        started = job.start.started_at
        finished = max(finished_at or self.clock(), started)
        fraction = 1.0 if outcome is JobOutcome.COMPLETED else job.progress / 100
        grams, mm = usage(job, fraction)
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

    # -- the IFS --------------------------------------------------------------------------

    def point(self) -> JobPoint | None:
        job = self.job
        if job is None:
            return None
        fraction = job.progress / 100
        _, mm = usage(job, fraction)
        return JobPoint(job.job_id, fed_mm=mm, progress=fraction)

    def _readings(self, view: View) -> None:
        if self.spools is None or not view.slots:
            return
        for slot in view.slots:
            reading = _reading(slot)
            if self._slots.get(slot.slot) == reading:
                continue
            first = slot.slot not in self._slots
            self._slots[slot.slot] = reading
            if first and not slot.loaded:
                continue  # an empty slot at the start says nothing
            task = asyncio.ensure_future(
                self.spools(
                    SpoolReading(
                        self.printer,
                        slot.slot,
                        material=reading[0],
                        color_hex=reading[1],
                        slot_count=view.slot_count or None,
                        during=self.point(),
                    )
                )
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


def _reading(slot: Slot) -> tuple[str | None, str | None]:
    return (slot.material, slot.color_hex) if slot.loaded else (None, None)


def usage(job: Job, fraction: float) -> tuple[dict[int, float], dict[int, float]]:
    """The printer's estimate of the job's grams and millimeters by slot, times `fraction`: by
    the file's IFS slots when the printer listed them, else all on the job's slot. The length
    goes only where it can't be split wrong: a one-slot job."""
    if fraction <= 0:
        return {}, {}
    fraction = min(fraction, 1.0)
    total = job.grams or sum(job.by_slot.values())
    if len(job.by_slot) > 1:
        share = sum(job.by_slot.values())
        grams = {s: total * g / share for s, g in job.by_slot.items()} if share else {}
        mm: dict[int, float] = {}
    else:
        grams = {job.slot: total} if total else {}
        mm = {job.slot: job.mm} if job.mm else {}
    return (
        {slot: round(g * fraction, 3) for slot, g in grams.items() if g > 0},
        {slot: round(m * fraction, 1) for slot, m in mm.items() if m > 0},
    )


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if number == number else 0.0
