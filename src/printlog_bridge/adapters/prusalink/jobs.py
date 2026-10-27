"""Turns PrusaLink's status, polled, into job events.

`/api/v1/status` has the printer's `state` and, while there is a job, `job` with its `id` (the
printer's job counter), `progress` (percent) and `time_printing` (seconds). On Buddy firmware
(`src/state/printer_state.cpp`) a job is there while the state is `PRINTING` (which takes in
preparing, finishing and aborting), `PAUSED`, or `ATTENTION` in a print (a question on the
screen, such as a filament mismatch before it starts). When it ends the state is `FINISHED` or
`STOPPED` (cancelled), without the job, and stays so until someone dismisses it on the screen;
`ERROR` is the printer halting. `BUSY` without a job is crash recovery or a power panic mid-job.

- A job starts the first time it is seen `PRINTING` or `PAUSED`: one waiting on a question
  before it starts isn't a print yet. `/api/v1/job` is asked then, once, for the file's name and
  `refs`, and the file is read for the slicer's estimates (`files.py`), which go on the start.
  `started_at` is now less `time_printing`.
- It ends at `FINISHED` (completed), `STOPPED` (cancelled) or `ERROR` (failed). Gone with the
  printer back at `IDLE` or `READY`, which the bridge sees only when it missed the end state,
  it is taken as completed at 99% or more, else cancelled, with a note.
- Usage is the slicer's estimate per tool (tool N is slot N, for an XL's five tools or an MMU's
  five filaments), times the last progress seen for a job that didn't complete, marked
  estimated.

`job_id` is `<printer's job id>-<file name>`: the counter alone would repeat after a reset of the
printer. With no history on the printer, `CurrentJob` keeps the running job, and the first status
after each connect is reconciled: `FINISHED`, `STOPPED` or `ERROR` then is taken as how the
remembered job ended, which can't be checked, as the job is gone from the status by then.
"""

from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

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

from .files import JobFile

log = logging.getLogger(__name__)

WITH_JOB = frozenset({"PRINTING", "PAUSED", "ATTENTION", "BUSY"})
"""States a job carries on through."""
STARTS = frozenset({"PRINTING", "PAUSED"})
ENDS = {
    "FINISHED": JobOutcome.COMPLETED,
    "STOPPED": JobOutcome.CANCELLED,
    "ERROR": JobOutcome.FAILED,
}
AT_REST = frozenset({"IDLE", "READY"})
COMPLETE_AT_PERCENT = 99.0
MAX_NAME_IN_ID = 80
UNSEEN_STATE_NOTE = (
    "The printer went back to idle without the bridge seeing it finish or stop, so it is logged "
    "by how far it got: completed at 99% or more, else cancelled."
)


class JobFiles(Protocol):
    async def read(self, job: Mapping[str, Any]) -> JobFile:
        """The job's file, from `/api/v1/job`'s answer."""


@dataclass
class Job:
    printer_job: Any
    """The printer's id for it, `status.job.id`."""
    job_id: str
    start: JobStarted
    file: JobFile | None = None
    progress: float = 0.0
    """Percent, as the printer last said."""


class JobTracker:
    def __init__(
        self,
        printer: str,
        sink: EventSink,
        current: CurrentJob,
        *,
        fetch_job: Callable[[], Awaitable[Mapping[str, Any] | None]],
        files: JobFiles | None = None,
        report: PrinterReport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.printer = printer
        self.sink = sink
        self.current = current
        self.fetch_job = fetch_job
        self.files = files
        self.report = report
        self.clock = clock
        self.job: Job | None = None
        self.state: str | None = None
        self._reconciled = False
        self._known: Mapping[str, Any] | None = None

    @property
    def active(self) -> bool:
        """A job is under way or about to be: poll more often."""
        return self.job is not None or self.state in WITH_JOB

    def connected(self) -> None:
        """A new connection: the next status is checked against the remembered job."""
        self._reconciled = False

    async def on_status(self, status: Mapping[str, Any]) -> None:
        printer = status.get("printer")
        if not isinstance(printer, Mapping) or not isinstance(printer.get("state"), str):
            return
        state = printer["state"].upper()
        self.state = state
        if self.report is not None:
            self.report.printer_state(state.lower())
        job = status.get("job")
        job = job if isinstance(job, Mapping) and job.get("id") is not None else None
        if not self._reconciled:
            if not await self._reconcile(state, job):
                return  # the job's details couldn't be had: again at the next poll
            self._reconciled = True
        await self._step(state, job, printer)

    async def _reconcile(self, state: str, job: Mapping[str, Any] | None) -> bool:
        remembered = self.current.job
        running = None
        if job is not None and state in WITH_JOB:
            if remembered is not None and _printer_job_of(remembered.job_id) == str(job["id"]):
                running = remembered.job_id
            elif self.job is not None and self.job.printer_job == job["id"]:
                running = self.job.job_id
            else:
                details = await self._details(job)
                if details is None:
                    return False
                running = self._job_id(job["id"], details)
        outcome = ENDS.get(state) if running is None and remembered is not None else None
        for event in self.current.reconcile(running, outcome):
            await self.sink.emit(event)
        if self.job is not None and self.job.job_id == running:
            pass  # the connection dropped while it ran: carry on with it
        elif remembered is not None and running == remembered.job_id:
            # The bridge restarted while it ran. Its file isn't read again: the start's
            # estimates are its usage.
            self.job = Job(job["id"], remembered.job_id, remembered)  # type: ignore[index]
        else:
            self.job = None
        return True

    async def _step(
        self, state: str, job: Mapping[str, Any] | None, printer: Mapping[str, Any]
    ) -> None:
        if job is not None and state in WITH_JOB:
            if self.job is not None and self.job.printer_job != job["id"]:
                # Another job, without this one's end seen: what the printer can't tell.
                for event in self.current.reconcile(None):
                    await self.sink.emit(event)
                self.job = None
            if self.job is None:
                if state in STARTS:
                    await self._start(job)
                return
            self.job.progress = _percent(job.get("progress"), self.job.progress)
            self.current.seen()
            return
        if self.job is None:
            return
        if state in ENDS:
            outcome = ENDS[state]
            note = None
            if outcome is JobOutcome.FAILED:
                message = printer.get("status_printer")
                text = message.get("message") if isinstance(message, Mapping) else None
                note = "The printer stopped the job with an error" + (
                    f": {text}." if isinstance(text, str) and text.strip() else "."
                )
            await self._finish(outcome, note)
        elif state in AT_REST:
            complete = self.job.progress >= COMPLETE_AT_PERCENT
            outcome = JobOutcome.COMPLETED if complete else JobOutcome.CANCELLED
            await self._finish(outcome, UNSEEN_STATE_NOTE)
        # BUSY or ATTENTION without a job: recovering mid-job; wait.

    async def _details(self, job: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """`/api/v1/job` for `job`, asked once per job (a reconnect's answer serves its start);
        None when the printer has moved on."""
        if self._known is not None and self._known.get("id") == job["id"]:
            return self._known
        details = await self.fetch_job()
        if not isinstance(details, Mapping) or details.get("id") != job["id"]:
            return None
        self._known = details
        return details

    async def _start(self, job: Mapping[str, Any]) -> None:
        details = await self._details(job)
        if details is None:
            return  # gone, or not the same job: the next poll says
        file = await self.files.read(details) if self.files is not None else JobFile()
        if file.printer_model and self.report is not None:
            self.report.printer_model(file.printer_model)
        seconds = job.get("time_printing")
        printing = float(seconds) if isinstance(seconds, (int, float)) and seconds > 0 else 0.0
        job_id = self._job_id(job["id"], details)
        start = JobStarted(
            printer=self.printer,
            job_id=job_id,
            file_name=_file_name(details),
            started_at=self.clock() - timedelta(seconds=printing),
            estimated_duration_s=file.duration_s,
            estimated_filament_mm=round(sum(file.mm.values()), 1) or None,
            estimated_filament_g=round(sum(file.grams.values()), 3) or None,
            estimated_filament_g_by_slot=dict(file.grams) if len(file.grams) > 1 else {},
        )
        self.job = Job(job["id"], job_id, start, file, _percent(job.get("progress"), 0.0))
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

    async def _finish(self, outcome: JobOutcome, note: str | None) -> None:
        job = self.job
        assert job is not None
        progress = 1.0 if outcome is JobOutcome.COMPLETED else job.progress / 100
        grams, mm = _usage(job, progress)
        now = self.clock()
        await self.sink.emit(
            JobFinished(
                printer=self.printer,
                job_id=job.job_id,
                outcome=outcome,
                finished_at=now,
                duration_s=max(0.0, (now - job.start.started_at).total_seconds()),
                filament_mm=mm,
                filament_g=grams,
                usage_estimated=bool(grams or mm),
                note=note,
            )
        )
        self.current.ended()
        self.job = None
        log.info("[printer %s] job %s %s", self.printer, job.job_id, outcome.value)

    def _job_id(self, printer_job: Any, details: Mapping[str, Any]) -> str:
        stem = posixpath.splitext(_file_name(details))[0]
        stem = re.sub(r"\s+", " ", stem).strip()[:MAX_NAME_IN_ID] or "print"
        return f"{printer_job}-{stem}"


def _usage(job: Job, progress: float) -> tuple[dict[int, float], dict[int, float]]:
    """The slicer's grams and millimeters per slot times `progress`: the file's when it was read,
    else the remembered start's (after a restart)."""
    if progress <= 0:
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
        {slot: round(g * progress, 3) for slot, g in grams.items()},
        {slot: round(m * progress, 1) for slot, m in mm.items()},
    )


def _file_name(details: Mapping[str, Any]) -> str:
    file = details.get("file")
    if not isinstance(file, Mapping):
        return ""
    name = file.get("display_name") or file.get("name") or ""
    return posixpath.basename(str(name))


def _printer_job_of(job_id: str) -> str:
    return job_id.split("-", 1)[0]


def _percent(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return min(max(float(value), 0.0), 100.0)
