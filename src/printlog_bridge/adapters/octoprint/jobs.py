"""Turns OctoPrint's push socket into job events.

Checked against OctoPrint 1.11.8 with its virtual printer (`tests/fixtures/octoprint/`) and its
sources:

- After `auth`, the socket sends `history` once, then `current` every half second times the
  throttle: both have `state` (`text` and `flags`: `printing`, `paused`, `pausing`, `resuming`,
  `cancelling`, `finishing`, `error`, `closedOrError`...), `job` (`file` with `origin`, `path`,
  `display`, `size`; `estimatedPrintTime`; `filament.tool<N>.length`/`volume`), `progress`
  (`completion`, the percent of the file's bytes sent; `filepos`; `printTime`, seconds printing
  without the time paused), `serverTime` and `markings`: the print chart's marks, each a `type`
  and an epoch `time`. A print's start is a `print` mark, its end a `done` or `cancel` mark.
  OctoPrint keeps marks only for `temperature.cutoff` (30 minutes by default): each new mark
  (pause, resume, done, cancel, connected) drops the older ones, so a long print's start mark
  is soon gone.
- `event` messages: `PrintStarted`, `PrintDone` (`time`, seconds), `PrintCancelled`
  (`progress`, `time`) followed by `PrintFailed` with `reason` `cancelled`, or `PrintFailed`
  with `reason` `error` after an `Error` (`error`, the firmware's words).

**A job's identity is fixed once**, when a status first shows the printer printing (or paused)
a file: `started_at` is the latest start mark, else `serverTime` less `printTime`, and the
`job_id` is `<epoch seconds>-<file stem>`. It stays that job while the statuses show the same
file under way, paused or on its way out; only a start mark after its start (or another file)
is another job. Its file's path and last progress are kept in the printer's state beside
`CurrentJob`'s (`state.discovery["octoprint_job"]`), so a reconnect or a restart of the bridge
finds it again by its path, without recomputing anything. A status that is `finishing` or
`cancelling` never starts a job, nor does one for the file that just ended, until OctoPrint
says it started again (`PrintStarted`, or a newer start mark).

The end comes from the events. Missed (the bridge was away, or OctoPrint restarted and lost the
print), it is read from the marks: the first `done` or `cancel` mark after the start says how
and when; without one, the printer in error is a failure, else the last progress seen says
(99% or more is complete, less is cancelled), with a note.

Every time is OctoPrint's clock (the marks, `serverTime`): the bridge's clock is offset by the
difference seen in the last status, and a job ends at its start plus OctoPrint's `time` when
the event gives one, so its end less its start is its duration.

Usage is the slicer's estimate per tool (`files.py`), times the progress for a job that didn't
complete, marked estimated. The progress is the share of the file's G-code sent (`filepos`
between the end of the header and thumbnails and the start of the settings block, when the file
was read), else OctoPrint's `completion`.
"""

from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Awaitable, Callable, Mapping
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

from .files import from_analysis

log = logging.getLogger(__name__)

ACTIVE_FLAGS = ("printing", "paused", "pausing", "resuming")
BUSY_FLAGS = ("cancelling", "finishing")
"""On the way out of a job: its end event is coming."""
COMPLETE_AT_PERCENT = 99.0
NEWER_MARK_S = 2.0
"""A start mark this much after a job's start is another job's (the id keeps whole seconds)."""
MAX_NAME_IN_ID = 80
KEY = "octoprint_job"
"""Where in `state.discovery` the running job's path and progress are kept."""
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
    filepos: float | None
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
    def starting(self) -> bool:
        """OctoPrint is starting a print: `printing` is true, but its start mark comes later."""
        return self.state.lower().startswith("starting")

    @property
    def failed(self) -> bool:
        """The printer in error. Not `closedOrError` alone: OctoPrint without a printer
        connection (Offline, as after its own restart) says that too."""
        return self.flags.get("error") is True or "error" in self.state.lower()

    def start_mark(self) -> float | None:
        """The latest start mark not followed by an end: the running print's. A start already
        ended is the last print's, while the running one's mark isn't there yet."""
        starts = [t for kind, t in self.marks if kind == "print"]
        if not starts:
            return None
        latest = max(starts)
        ended = any(kind in ("done", "cancel") and t >= latest for kind, t in self.marks)
        return None if ended else latest

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
        filepos=_number(progress.get("filepos")),  # type: ignore[union-attr]
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
    """Percent of the G-code sent, as OctoPrint last said."""
    marked: bool = False
    """`started_at` is its start mark's, so a later start mark is another print's."""
    span: tuple[int, int] | None = None
    """Where its file's G-code is, in bytes (`JobFile.gcode_span`), kept across restarts."""
    filepos: float | None = None
    print_time: float | None = None
    first_seen: float = 0.0
    """When the bridge first saw it, on OctoPrint's clock: an end mark before that isn't its."""


@dataclass(frozen=True)
class Ended:
    job_id: str
    path: str | None
    started_at: datetime
    filepos: float | None = None


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
        self._ended: Ended | None = None
        """The last job ended here, so a late status still showing its file doesn't start it
        again."""
        self._started_again = False
        """`PrintStarted` came since the last end."""
        self._error: str | None = None
        self._offset = timedelta(0)
        """OctoPrint's clock less the bridge's, from the last status."""

    @property
    def active(self) -> bool:
        return self.job is not None

    def connected(self) -> None:
        """A new connection: the next status is checked against the remembered job."""
        self._reconciled = False
        self._ended = None

    def _now(self) -> datetime:
        """Now, on OctoPrint's clock."""
        return self.clock() + self._offset

    # -- the kept job ---------------------------------------------------------------------

    def _kept(self) -> Mapping[str, Any]:
        kept = self.current.state.discovery.get(KEY)
        return kept if isinstance(kept, Mapping) else {}

    def _keep(self, job: Job) -> None:
        """In memory now; written with `CurrentJob`'s own saves (at its start, then at most once
        a minute)."""
        self.current.state.discovery[KEY] = {
            "job_id": job.job_id,
            "path": job.path,
            "progress": round(job.progress, 2),
            "marked": job.marked,
            "span": list(job.span) if job.span is not None else None,
            "filepos": job.filepos,
            "print_time": job.print_time,
            "first_seen": job.first_seen,
        }

    def _remembered(self) -> Job | None:
        start = self.current.job
        if start is None:
            return None
        kept = self._kept()
        same = kept.get("job_id") == start.job_id
        path = kept.get("path") if same and isinstance(kept.get("path"), str) else None
        if not same:
            return Job(start.job_id, None, start)
        span = kept.get("span")
        good_span = (
            isinstance(span, list) and len(span) == 2 and all(isinstance(n, int) for n in span)
        )
        return Job(
            start.job_id,
            path,
            start,
            None,
            _number(kept.get("progress")) or 0.0,
            marked=kept.get("marked") is True,
            span=(span[0], span[1]) if good_span else None,  # type: ignore[index]
            filepos=_number(kept.get("filepos")),
            print_time=_number(kept.get("print_time")),
            first_seen=_number(kept.get("first_seen")) or 0.0,
        )

    # -- statuses -------------------------------------------------------------------------

    async def on_status(self, status: Mapping[str, Any]) -> None:
        view = view_of(status)
        if view is None:
            return
        if view.server_time is not None:
            self._offset = timedelta(seconds=view.server_time - self.clock().timestamp())
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
        return self._now() - timedelta(seconds=view.print_time or 0)

    def _job_id(self, started_at: datetime, view: View) -> str:
        stem = posixpath.splitext(view.name)[0]
        stem = re.sub(r"\s+", " ", stem).strip()[:MAX_NAME_IN_ID] or "print"
        return f"{int(started_at.timestamp())}-{stem}"

    @staticmethod
    def _newer_start(view: View, started_at: datetime) -> bool:
        mark = view.start_mark()
        return mark is not None and mark > started_at.timestamp() + NEWER_MARK_S

    def _continues(self, view: View, job: Job) -> bool:
        """`view` still shows `job` under way, paused or on its way out."""
        if not (view.active or view.busy) or view.path is None:
            return False
        if job.path is not None:
            same_file = view.path == job.path
        else:
            same_file = view.name == job.start.file_name  # kept before paths were
        if not same_file:
            return False
        if job.marked and self._newer_start(view, job.start.started_at):
            return False  # a later start mark: another print of the same file
        # A new print of the same file starts its bytes again. (Not `printTime`: it drops at
        # each resume by the time paused, comm.py's getPrintTime.)
        return not (
            view.filepos is not None and job.filepos is not None and view.filepos < job.filepos
        )

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
        if view.active or view.busy:
            if self.job is not None:
                if self._continues(view, self.job):
                    self._progress(view)
                    return
                await self._finish_unseen(view)  # another job: this one's end wasn't seen
            if view.active and not view.busy and not view.starting and not self._just_ended(view):
                await self._start(view)
            return
        if self.job is not None:
            await self._finish_unseen(view)

    def _just_ended(self, view: View) -> bool:
        """`view` shows the file of the job that just ended, with nothing saying it started
        again: a late status of that job."""
        ended = self._ended
        if ended is None or self._started_again or view.path != ended.path:
            return False
        if view.filepos is not None and ended.filepos is not None and view.filepos < ended.filepos:
            return False  # its bytes started again: a new print of the same file
        return not self._newer_start(view, ended.started_at)

    def _progress(self, view: View) -> None:
        job = self.job
        assert job is not None
        percent = _gcode_percent(job.span, view.filepos)
        if percent is None:
            percent = view.completion
        if view.active:
            if percent is not None:
                job.progress = min(max(percent, 0.0), 100.0)
            job.filepos = view.filepos if view.filepos is not None else job.filepos
            job.print_time = view.print_time if view.print_time is not None else job.print_time
            self._keep(job)
        self.current.seen()

    async def _start(self, view: View) -> None:
        file = (
            await self.files.read(view.job) if self.files is not None else from_analysis(view.job)
        )
        if file.printer_model and self.report is not None:
            self.report.printer_model(file.printer_model)
        started_at = self._started_at(view)
        job_id = self._job_id(started_at, view)
        start = JobStarted(
            printer=self.printer,
            job_id=job_id,
            file_name=view.name,
            started_at=started_at,
            estimated_duration_s=file.duration_s,
            estimated_filament_mm=round(sum(file.mm.values()), 1) or None,
            estimated_filament_g=round(sum(file.grams.values()), 3) or None,
            estimated_filament_g_by_slot=dict(file.grams) if len(file.grams) > 1 else {},
        )
        percent = _gcode_percent(file.gcode_span, view.filepos)
        self.job = Job(
            job_id,
            view.path,
            start,
            file,
            percent or view.completion or 0.0,
            marked=view.start_mark() is not None,
            span=file.gcode_span,
            filepos=view.filepos,
            print_time=view.print_time,
            first_seen=view.server_time or self._now().timestamp(),
        )
        self._started_again = False
        self._error = None
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

    # -- events ---------------------------------------------------------------------------

    async def on_event(self, kind: str, payload: Any) -> None:
        payload = payload if isinstance(payload, Mapping) else {}
        if kind == "PrintStarted":
            self._started_again = True
            return
        if kind == "Error":
            error = payload.get("error")
            self._error = error.strip() if isinstance(error, str) and error.strip() else None
            return
        if kind not in ("PrintDone", "PrintCancelled", "PrintFailed") or self.job is None:
            return
        path = payload.get("path")
        if isinstance(path, str) and self.job.path is not None and path != self.job.path:
            return  # not this job's: its own end was missed, which a status will tell
        # Ends now, on OctoPrint's clock: the payload's `time` leaves out the time paused.
        if kind == "PrintDone":
            await self._finish(JobOutcome.COMPLETED, None, live=True)
        elif kind == "PrintCancelled" or payload.get("reason") == "cancelled":
            await self._finish(JobOutcome.CANCELLED, None, live=True)
        else:
            note = f"{ERROR_NOTE}: {self._error}." if self._error else f"{ERROR_NOTE}."
            await self._finish(JobOutcome.FAILED, note, live=True)

    async def _finish_unseen(self, view: View) -> None:
        job = self.job
        assert job is not None
        # An end mark before the bridge first saw the job running is another print's.
        end = view.end_mark(max(job.start.started_at.timestamp(), job.first_seen))
        note = None
        finished_at = None
        if end is not None:
            kind, t = end
            outcome = JobOutcome.COMPLETED if kind == "done" else JobOutcome.CANCELLED
            finished_at = datetime.fromtimestamp(t, timezone.utc)
        elif view.failed or self._error is not None:
            outcome = JobOutcome.FAILED
            note = f"{ERROR_NOTE}: {self._error}." if self._error else f"{ERROR_NOTE}."
        else:
            complete = job.progress >= COMPLETE_AT_PERCENT
            outcome = JobOutcome.COMPLETED if complete else JobOutcome.CANCELLED
            note = UNSEEN_END_NOTE
        if finished_at is None:
            seen = self.current.last_seen
            finished_at = (seen + self._offset) if seen is not None else self._now()
        await self._finish(outcome, note, finished_at=finished_at)

    async def _finish(
        self,
        outcome: JobOutcome,
        note: str | None,
        *,
        finished_at: datetime | None = None,
        live: bool = False,
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
        self._ended = Ended(job.job_id, job.path, started, job.filepos)
        self._started_again = False
        self.job = None
        log.info("[printer %s] job %s %s", self.printer, job.job_id, outcome.value)
        if self.snapshot is not None and live:
            await self.sink.emit(
                JobImage(self.printer, job.job_id, ImageKind.SNAPSHOT, self.snapshot)
            )


def _gcode_percent(span: tuple[int, int] | None, filepos: float | None) -> float | None:
    """How much of the file's G-code was sent, in percent, leaving out the header with its
    thumbnails and the settings block after it: those are a large share of a small file."""
    if span is None or filepos is None:
        return None
    first, last = span
    if last <= first:
        return None
    return min(max((filepos - first) / (last - first) * 100, 0.0), 100.0)


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
