"""Turns a FlashForge printer's `/detail`, polled, into job events (`protocol.py` says what it
holds and where that comes from).

The printer keeps no job history the bridge can read (`/gcodeList` names the last files, with
no times or outcomes), so `CurrentJob` keeps the running job, as for PrusaLink and SDCP.

**A job's identity is fixed once**, when a status first shows a file printing or paused:
`started_at` is now less `printDuration`, and `job_id` is `<epoch seconds>-<file stem>`. Its
file, last progress, duration and estimates are kept beside `CurrentJob`'s (`state.discovery
["flashforge_job"]`), so a reconnect or a bridge restart finds the job again by its file, never
by recomputing a time.

**It stays that job** through every status but an end, `ready`, another file, or the duration
and progress going back while printing (another print of the same file). Busy states (heating,
calibrating) keep it, and so does a status the bridge doesn't know (logged once, at info, so a
trace shows it): a new firmware's word for resuming must not end a print.

**The end seen live** is the status turning `completed` or `cancel` (an end naming no file is
this job's), at the poll that saw it. `error` is held for `ERROR_POLLS` polls in a row before the
job fails, at the first of them: a filament runout may show `error` and then resume (ASSUMPTION).
`ready`, or another file, ends it by the progress kept: 99% or more complete, less cancelled,
with a note. **An end missed** (the bridge was away, or the printer unreachable) is decided on
the first status after a connect, the same way, at when the bridge last saw the job.

**After an end**, a status still showing that file printing (a late answer) starts nothing, while
the start it implies (now less `printDuration`) is the ended job's. Once a status shows the
printer moved on (nothing under way), or the implied start is another, printing is a new job: a
reprint of the same file right after a cancel is logged.

**Usage is the slicer's estimate, as the printer read it from the file**: `/detail`'s
`estimatedRightWeight` (grams) and `estimatedRightLen` (millimeters). ASSUMPTION, from
ff-5mp-api-py's code: they are the job's totals. Should one shrink between polls, it is taken as
what is left instead, and the total is its first value over the share of the job left then
(`Estimate`). All of it for a completed job, times the progress otherwise, sent with
`usage_estimated=True`. The printer measures nothing. On an AD5X, `/gcodeList`'s tools for the
file split the grams by IFS slot (`files.grams_by_slot`: the slicer's mapping, which may differ
from what was picked on the screen); a one-slot print puts everything on its slot. The job's file
is never downloaded: the API has no read-only way to.

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

from .protocol import (
    BUSY,
    CANCEL,
    COMPLETED,
    ENDS,
    ERROR,
    PRINTING,
    READY,
    UNDER_WAY,
    Slot,
    View,
    view_of,
)

log = logging.getLogger(__name__)

KEY = "flashforge_job"
"""Where in `state.discovery` the running job's file, progress and estimates are kept."""
COMPLETE_AT_PERCENT = 99.0
MAX_NAME_IN_ID = 80
SECONDS_BACK, PERCENT_BACK = 120.0, 5.0
"""How far the duration and progress may go back while printing before it's another print."""
SAME_START_S = 120.0
"""How far apart two implied starts of one job may be."""
ERROR_POLLS = 3
"""`error` on this many polls in a row fails the job (it is held for the two after the first)."""
SHRINK = 0.999
"""An estimate below this share of the last one shrank."""
MAX_SHARE_DONE = 0.95
KNOWN = UNDER_WAY | BUSY | ENDS | {READY}
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
class Estimate:
    """One of the printer's estimates for the job, as polls report it: the job's total
    (ASSUMPTION), unless it shrinks between polls, when it is what is left."""

    first: float = 0.0
    at: float = 0.0
    """How much of the job was done (0 to 1) when `first` was seen."""
    last: float = 0.0
    remaining: bool = False

    def see(self, value: float | None, done: float) -> None:
        if not value:
            return  # none yet (the file not read), or nothing left at the end
        if self.first and value < self.last * SHRINK:
            self.remaining = True
        elif not self.remaining and value >= self.last:
            self.first, self.at = value, done
        self.last = value

    @property
    def total(self) -> float:
        if not self.remaining:
            return self.first
        return self.first / (1.0 - min(self.at, MAX_SHARE_DONE))

    def kept(self) -> dict[str, Any]:
        return {"first": self.first, "at": self.at, "last": self.last, "remaining": self.remaining}

    @classmethod
    def from_kept(cls, kept: Any, default: float) -> Estimate:
        if not isinstance(kept, Mapping):
            return cls(default, 0.0, default)
        return cls(
            _number(kept.get("first")),
            _number(kept.get("at")),
            _number(kept.get("last")),
            kept.get("remaining") is True,
        )


@dataclass
class Job:
    job_id: str
    path: str | None
    start: JobStarted
    progress: float = 0.0
    duration_s: float = 0.0
    grams_estimate: Estimate = field(default_factory=Estimate)
    mm_estimate: Estimate = field(default_factory=Estimate)
    by_slot: dict[int, float] = field(default_factory=dict)
    slot: int = 0
    """Where a one-slot print's filament goes: the IFS slot feeding at its start, else 0."""

    @property
    def grams(self) -> float:
        return self.grams_estimate.total

    @property
    def mm(self) -> float:
        return self.mm_estimate.total


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
        self._polls = 0
        self._ended: Job | None = None
        """The job that just ended, until the printer moves on: a late status still showing it
        printing doesn't start it again."""
        self._ended_poll = 0
        self._moved_on = False
        self._errors = 0
        """Polls in a row showing `error` for the job."""
        self._error_at: datetime | None = None
        self._error_unseen = False
        self._unknown: set[str] = set()
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

    async def close(self) -> None:
        """Stops the spool readings still being sent."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # -- the kept job ---------------------------------------------------------------------

    def _keep(self, job: Job) -> None:
        """In memory now; written with `CurrentJob`'s own saves."""
        self.current.state.discovery[KEY] = {
            "job_id": job.job_id,
            "path": job.path,
            "progress": round(job.progress, 2),
            "duration_s": round(job.duration_s, 1),
            "grams": job.grams_estimate.kept(),
            "mm": job.mm_estimate.kept(),
            "by_slot": {str(k): v for k, v in job.by_slot.items()},
            "slot": job.slot,
        }

    def _remembered(self) -> Job | None:
        start = self.current.job
        if start is None:
            return None
        grams = start.estimated_filament_g or 0.0
        mm = start.estimated_filament_mm or 0.0
        job = Job(start.job_id, None, start)
        job.grams_estimate = Estimate(grams, 0.0, grams)
        job.mm_estimate = Estimate(mm, 0.0, mm)
        job.by_slot = dict(start.estimated_filament_g_by_slot)
        kept = self.current.state.discovery.get(KEY)
        if not isinstance(kept, Mapping) or kept.get("job_id") != start.job_id:
            return job
        path = kept.get("path")
        job.path = path if isinstance(path, str) else None
        job.progress = _number(kept.get("progress"))
        job.duration_s = _number(kept.get("duration_s"))
        job.grams_estimate = Estimate.from_kept(kept.get("grams"), grams)
        job.mm_estimate = Estimate.from_kept(kept.get("mm"), mm)
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
        self._polls += 1
        if self.report is not None and view.state is not None:
            self.report.printer_state(view.state)
        self._readings(view)
        if view.state is None:
            return
        if view.state not in KNOWN and view.state not in self._unknown:
            self._unknown.add(view.state)
            log.info(
                "[printer %s] the printer says %r, a status the bridge doesn't know: any job "
                "carries on", self.printer, view.state,
            )  # fmt: skip
        if not self._reconciled:
            self._reconciled = True
            await self._reconcile(view)
        await self._step(view)

    def _same_file(self, view: View, job: Job) -> bool:
        if job.path is not None:
            return view.path == job.path
        return view.name == job.start.file_name

    @staticmethod
    def _went_back(view: View, job: Job) -> bool:
        """Printing again from the start: another print of the same file."""
        if view.state != PRINTING or view.duration_s is None:
            return False
        if view.duration_s >= job.duration_s - SECONDS_BACK:
            return False
        return view.progress is None or view.progress < job.progress - PERCENT_BACK

    def _decide(self, view: View, job: Job) -> str:
        """What `view` says of `job`: `end` (an end state of it), `gone` (`ready`), `other`
        (another file, or another print of it) or `keep` (anything else, unknown included)."""
        if view.ended and (view.path is None or self._same_file(view, job)):
            return "end"
        if view.path is not None and not self._same_file(view, job):
            return "other"
        if view.state == READY:
            return "gone"
        if view.under_way and self._went_back(view, job):
            return "other"
        return "keep"

    async def _reconcile(self, view: View) -> None:
        if self.job is None:
            remembered = self._remembered()
            if remembered is None:
                return
            # The bridge restarted: its start again (the sink recognizes it), then either the
            # job carries on or it ended while the bridge was away.
            self.job = remembered
            await self.sink.emit(remembered.start)
        decision = self._decide(view, self.job)
        if decision == "keep":
            self.current.seen()
        elif decision == "end" and view.state == ERROR:
            self._error_unseen = True  # held as a live error is, then ended when last seen
        else:
            await self._finish_unseen(view, decision)

    async def _step(self, view: View) -> None:
        job = self.job
        if job is not None:
            decision = self._decide(view, job)
            if view.state != ERROR:
                self._errors, self._error_at, self._error_unseen = 0, None, False
            if decision == "end":
                await self._end(view, job)
                return
            if decision == "keep":
                self._progress(view, job)
                return
            await self._finish_unseen(view, decision)
        if self._ended is not None and not self._just_ended(view):
            self._ended = None
        if view.under_way and self._ended is None:
            await self._start(view)

    async def _end(self, view: View, job: Job) -> None:
        if view.progress:  # the end's own progress, when it keeps one, is the latest
            job.progress = view.progress
        if view.state != ERROR:
            await self._finish(OUTCOMES[view.state], None)  # type: ignore[index]
            return
        self._errors += 1
        if self._errors == 1:
            self._error_at = self.current.last_seen if self._error_unseen else self.clock()
        if self._errors < ERROR_POLLS:
            self.current.seen()
            return
        note = error_note(view) + (f" {UNSEEN_TIME_NOTE}" if self._error_unseen else "")
        await self._finish(JobOutcome.FAILED, note, finished_at=self._error_at)

    def _just_ended(self, view: View) -> bool:
        """`view` is still the job that just ended (a late answer), not a new print."""
        ended = self._ended
        assert ended is not None
        if not view.under_way:
            if self._polls > self._ended_poll:
                self._moved_on = True
            return True  # nothing to start anyway
        if self._moved_on or view.path != ended.path or self._went_back(view, ended):
            return False
        implied = self.clock() - timedelta(seconds=view.duration_s or 0.0)
        return abs((implied - ended.start.started_at).total_seconds()) <= SAME_START_S

    def _progress(self, view: View, job: Job) -> None:
        if view.under_way:
            if view.progress is not None:
                job.progress = view.progress
            if view.duration_s is not None:
                job.duration_s = view.duration_s
            done = job.progress / 100
            job.grams_estimate.see(view.estimated_g, done)
            job.mm_estimate.see(view.estimated_mm, done)
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
        done = job.progress / 100
        job.grams_estimate.see(grams, done)
        job.mm_estimate.see(view.estimated_mm, done)
        job.by_slot = by_slot
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

    async def _finish_unseen(self, view: View, decision: str) -> None:
        job = self.job
        assert job is not None
        if decision == "end":
            outcome = OUTCOMES[view.state]  # type: ignore[index]
            note = UNSEEN_TIME_NOTE
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
        self._ended, self._ended_poll, self._moved_on = job, self._polls, False
        self._errors, self._error_at, self._error_unseen = 0, None, False
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
