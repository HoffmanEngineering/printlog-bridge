"""Turns an Anycubic printer's MQTT reports into job events.

**What was seen on a real printer** (a Kobra S1 on firmware 2.5.6.4, idle, October 2026,
`tests/fixtures/anycubic/kobra_s1_idle.json`): the answer to `info`/`query` is the whole state,
with `data.state` `free` and `data.project` null while idle. **What wasn't** (no print has been
recorded yet): the job fields below come from Nino6689/anycubic-lan's `docs/PROTOCOL.md` (MIT,
captured on a Kobra S1 mid-print) for the FDM printers and modrzew/hass-anycubic-photon-p1's
`docs/protocol.md` (MIT, a Photon P1) for resin. Each point taken from them is marked
ASSUMPTION below; replace them with a recorded print (`record_anycubic.py`) when there is one.

**The job.** An FDM printer has it in `info`'s `data.project` (null between jobs) and in the
`print` report's `data`; a resin printer only in the `print` report, whose envelope `state` is
the job's (`printing`, `paused`, `monitoring`...), while its `status` report says `idle` or
`busy`. Fields: `filename`, `task_id` (`taskid` on resin), `progress` (0-100), `curr_layer`,
`total_layers`, `print_time` and `remain_time` (ASSUMPTION: minutes), `supplies_usage`
(ASSUMPTION: millimeters of filament fed so far on FDM; on resin, the job's resin in mL, already
the whole job's at layer 0), `state` and, on FDM, `print_status` (1 printing, 2 complete, 3
cancelled, 4 to 9 the start routine).

**A job's identity is fixed once**, when a report first shows it under way (`printing`, `paused`,
`pausing`, `resuming`, `resumed`; never the start routine: `init`, `downloading`, `checking`,
`preheating`, `auto_leveling`...): `started_at` is now less `print_time`, and `job_id` is
`<epoch seconds>-<file stem>`. Its file, task id, progress and layer are kept beside
`CurrentJob`'s (`state.discovery["anycubic_job"]`), so a reconnect or a bridge restart finds it
again by its file and task. Another file, another task id, or layer and progress going back while
printing is another job.

**The end seen live** is `state` `finished` (completed), `stoped` (sic; or `stopped`,
`canceled`: cancelled) or `failed`, or `print_status` 2 or 3. **An end missed** (the bridge was
away; the project is null by the next report) is decided by `last_project` when it names the job
(ASSUMPTION: it holds the job's last state), else by the progress kept: 99% or more is complete,
less cancelled, with a note.

**Usage.** FDM: the last `supplies_usage` seen, as slot 0's millimeters, marked estimated until a
recorded print shows whether it is measured. Resin: `supplies_usage` as the job's estimated
volume at its start, and that times the layers printed over the layer count at its end (the whole
of it for a completed job), always estimated. The job's file can't be read for a slicer header:
the printer serves no download (only uploads), so `resin_files.py` has nothing to read.
"""

from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.events import EventSink, JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.health import utc_now
from printlog_bridge.core.status import PrinterReport

log = logging.getLogger(__name__)

KEY = "anycubic_job"
COMPLETE_AT_PERCENT = 99.0
MAX_NAME_IN_ID = 80
LAYERS_BACK, PERCENT_BACK = 1, 5.0

UNDER_WAY = frozenset({"printing", "paused", "pausing", "resuming", "resumed"})
"""Job states that start a job (ASSUMPTION: the sources' lists)."""
BUSY = frozenset({"init", "downloading", "checking", "preheating", "auto_leveling", "vibrating",
                  "flow_calibrating", "updated", "stopping", "monitoring", "slicing",
                  "leveling", "levelling"})  # fmt: skip
"""Job states that carry a running job on without starting one: the start routine, a resume's
checks, stopping, resin's pre-print `monitoring`."""
ENDS = {
    "finished": JobOutcome.COMPLETED,
    "complete": JobOutcome.COMPLETED,
    "completed": JobOutcome.COMPLETED,
    "stoped": JobOutcome.CANCELLED,
    "stopped": JobOutcome.CANCELLED,
    "canceled": JobOutcome.CANCELLED,
    "cancelled": JobOutcome.CANCELLED,
    "failed": JobOutcome.FAILED,
    "error": JobOutcome.FAILED,
}
PRINT_STATUS_ENDS = {2: JobOutcome.COMPLETED, 3: JobOutcome.CANCELLED}
FAILED_NOTE = "The printer reported the job failed."
UNSEEN_END_NOTE = (
    "The bridge didn't see this job end, so it is logged by how far it got: completed at 99% "
    "or more, else cancelled."
)
UNSEEN_TIME_NOTE = (
    "The bridge wasn't connected when this job ended, so its end is when the bridge last saw it "
    "running."
)


@dataclass(frozen=True)
class View:
    """What the latest reports say of the job. `known` is False until a report has said
    whether there is one."""

    known: bool
    state: str | None = None
    path: str | None = None
    task_id: str | None = None
    progress: float | None = None
    layer: int | None = None
    total_layers: int | None = None
    print_time_s: float | None = None
    remain_s: float | None = None
    usage: float | None = None
    last: Mapping[str, Any] | None = None
    """`last_project`, the job that ended last (FDM)."""

    @property
    def name(self) -> str:
        return posixpath.basename(self.path) if self.path else ""

    @property
    def under_way(self) -> bool:
        return self.state in UNDER_WAY and self.path is not None

    @property
    def busy(self) -> bool:
        return self.state in BUSY

    @property
    def outcome(self) -> JobOutcome | None:
        return ENDS.get(self.state) if self.state else None


def view_of(project: Mapping[str, Any] | None, last: Any = None) -> View:
    """The view of a job block (`project`, or a `print` report's `data` with its `state`);
    None is no job."""
    last = last if isinstance(last, Mapping) else None
    if not isinstance(project, Mapping):
        return View(known=True, last=last)
    path = project.get("filename")
    path = path.strip() if isinstance(path, str) and path.strip() else None
    task = project.get("task_id", project.get("taskid"))
    plain = isinstance(task, (str, int)) and not isinstance(task, bool)
    task_text = str(task).strip() if plain else ""
    state = project.get("state")
    state = state.strip().lower() if isinstance(state, str) and state.strip() else None
    status = project.get("print_status")
    ended = isinstance(status, int) and not isinstance(status, bool) and status in PRINT_STATUS_ENDS
    if ended and state not in ENDS:
        state = "finished" if status == 2 else "stoped"
    progress = _number(project.get("progress"))
    layer = _number(project.get("curr_layer"))
    total = _number(project.get("total_layers"))
    if progress is None and layer is not None and total:
        progress = layer / total * 100
    minutes = _number(project.get("print_time"))
    remain = _number(project.get("remain_time"))
    return View(
        known=True,
        state=state,
        path=path,
        task_id=task_text if task_text not in ("", "-1", "0") else None,
        progress=min(max(progress, 0.0), 100.0) if progress is not None else None,
        layer=int(layer) if layer is not None else None,
        total_layers=int(total) if total else None,
        print_time_s=minutes * 60 if minutes is not None else None,
        remain_s=remain * 60 if remain is not None else None,
        usage=_number(project.get("supplies_usage")),
        last=last,
    )


@dataclass
class Job:
    job_id: str
    path: str | None
    task_id: str | None
    start: JobStarted
    progress: float = 0.0
    layer: int | None = None
    total_layers: int | None = None
    usage: float | None = None


class JobTracker:
    def __init__(
        self,
        printer: str,
        sink: EventSink,
        current: CurrentJob,
        *,
        resin: bool = False,
        report: PrinterReport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.printer = printer
        self.sink = sink
        self.current = current
        self.resin = resin
        self.report = report
        self.clock = clock
        self.job: Job | None = None
        self.project: dict[str, Any] = {}
        """The job block, each `print` report merged in (a report may carry only part)."""
        self._reconciled = False
        self._ended: Job | None = None

    def connected(self) -> None:
        self.project = {}
        self._reconciled = False

    # -- reports --------------------------------------------------------------------------

    async def on_message(self, kind: str | None, message: Mapping[str, Any]) -> None:
        """One report: `kind` is its `type` (or its topic's)."""
        data = message.get("data")
        if kind == "info" and isinstance(data, Mapping) and "project" in data:
            if isinstance(data.get("state"), str) and self.report is not None:
                self.report.printer_state(data["state"])
            project = data.get("project")
            self.project = dict(project) if isinstance(project, Mapping) else {}
            await self._decide(view_of(project, data.get("last_project")))
        elif kind == "print" and isinstance(data, Mapping) and _is_job(data):
            block = {**self.project, **data}
            state = message.get("state")
            if self.resin and isinstance(state, str) and state.strip():
                block["state"] = state  # the resin job's state is the envelope's
            self.project = block
            await self._decide(view_of(block))
        elif kind == "status" and self.resin:
            state = message.get("state")
            if isinstance(state, str) and self.report is not None:
                self.report.printer_state(state)
            if isinstance(state, str) and state.strip().lower() in ("idle", "free"):
                self.project = {}
                await self._decide(view_of(None))

    async def _decide(self, view: View) -> None:
        if not self._reconciled:
            self._reconciled = True
            await self._reconcile(view)
        await self._step(view)

    # -- the kept job ---------------------------------------------------------------------

    def _keep(self, job: Job) -> None:
        self.current.state.discovery[KEY] = {
            "job_id": job.job_id,
            "path": job.path,
            "task_id": job.task_id,
            "progress": round(job.progress, 2),
            "layer": job.layer,
            "total_layers": job.total_layers,
            "usage": job.usage,
        }

    def _remembered(self) -> Job | None:
        start = self.current.job
        if start is None:
            return None
        kept = self.current.state.discovery.get(KEY)
        if not isinstance(kept, Mapping) or kept.get("job_id") != start.job_id:
            return Job(start.job_id, None, None, start)
        path, task = kept.get("path"), kept.get("task_id")
        layer, total = kept.get("layer"), kept.get("total_layers")
        return Job(
            start.job_id,
            path if isinstance(path, str) else None,
            task if isinstance(task, str) else None,
            start,
            progress=_number(kept.get("progress")) or 0.0,
            layer=layer if isinstance(layer, int) else None,
            total_layers=total if isinstance(total, int) else None,
            usage=_number(kept.get("usage")),
        )

    # -- deciding -------------------------------------------------------------------------

    def _same_task(self, view: View, path: str | None, task: str | None) -> bool:
        if view.path is not None and path is not None and view.path != path:
            return False
        return view.task_id is None or task is None or view.task_id == task

    def _continues(self, view: View, job: Job) -> bool:
        if not (view.under_way or view.busy) or view.path is None:
            return False
        if job.path is not None and view.path != job.path:
            return False
        if job.path is None and view.name != job.start.file_name:
            return False
        if not self._same_task(view, job.path, job.task_id):
            return False
        return not _went_back(view, job)

    def _ends(self, view: View, job: Job) -> bool:
        return view.outcome is not None and self._same_task(view, job.path, job.task_id)

    async def _reconcile(self, view: View) -> None:
        if self.job is None:
            remembered = self._remembered()
            if remembered is None:
                return
            self.job = remembered
            await self.sink.emit(remembered.start)
        if self._continues(view, self.job) or self._ends(view, self.job):
            self.current.seen()
        else:
            await self._finish_unseen(view)

    async def _step(self, view: View) -> None:
        job = self.job
        if job is not None:
            if self._ends(view, job):
                self._progress(view)
                outcome: JobOutcome = view.outcome  # type: ignore[assignment]
                note = FAILED_NOTE if outcome is JobOutcome.FAILED else None
                await self._finish(outcome, note)
                return
            if self._continues(view, job):
                self._progress(view)
                return
            await self._finish_unseen(view)
        if self._ended is not None and not self._just_ended(view):
            self._ended = None
        if view.under_way and self._ended is None:
            await self._start(view)

    def _just_ended(self, view: View) -> bool:
        ended = self._ended
        assert ended is not None
        if view.outcome is not None:
            return True
        return (
            view.under_way
            and view.path == ended.path
            and self._same_task(view, ended.path, ended.task_id)
            and not _went_back(view, ended)
        )

    def _progress(self, view: View) -> None:
        job = self.job
        assert job is not None
        if view.progress is not None:
            job.progress = view.progress
        if view.layer is not None:
            job.layer = view.layer
        if view.total_layers:
            job.total_layers = view.total_layers
        if view.usage is not None and view.usage > 0:
            job.usage = view.usage
        self._keep(job)
        self.current.seen()

    async def _start(self, view: View) -> None:
        now = self.clock()
        started_at = now - timedelta(seconds=max(view.print_time_s or 0.0, 0.0))
        stem = posixpath.splitext(view.name)[0]
        stem = re.sub(r"\s+", " ", stem).strip()[:MAX_NAME_IN_ID] or "print"
        job_id = f"{int(started_at.timestamp())}-{stem}"
        duration = None
        if view.print_time_s is not None and view.remain_s is not None:
            duration = (view.print_time_s + view.remain_s) or None
        volume = round(view.usage, 2) if self.resin and view.usage and view.usage > 0 else None
        start = JobStarted(
            printer=self.printer,
            job_id=job_id,
            file_name=view.name,
            started_at=started_at,
            estimated_duration_s=duration,
            estimated_volume_ml=volume,
        )
        self.job = Job(
            job_id,
            view.path,
            view.task_id,
            start,
            progress=view.progress or 0.0,
            layer=view.layer,
            total_layers=view.total_layers,
            usage=view.usage if view.usage and view.usage > 0 else None,
        )
        self._keep(self.job)
        await self.sink.emit(start)
        self.current.started(start)
        log.info("[printer %s] job %s started", self.printer, job_id)

    def _last_outcome(self, view: View, job: Job) -> JobOutcome | None:
        """How `last_project` says the job ended, when it names this job."""
        if view.last is None:
            return None
        last = view_of(view.last)
        if last.path is None or not self._same_task(last, job.path, job.task_id):
            return None
        if job.path is None and last.name != job.start.file_name:
            return None
        if last.progress is not None:
            job.progress = last.progress
        if last.usage is not None and last.usage > 0:
            job.usage = last.usage
        return last.outcome

    async def _finish_unseen(self, view: View) -> None:
        job = self.job
        assert job is not None
        outcome = view.outcome if self._ends(view, job) else self._last_outcome(view, job)
        if outcome is not None:
            note = UNSEEN_TIME_NOTE
            if outcome is JobOutcome.FAILED:
                note = f"{FAILED_NOTE} {UNSEEN_TIME_NOTE}"
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
        mm, ml = self._usage(job, outcome)
        await self.sink.emit(
            JobFinished(
                printer=self.printer,
                job_id=job.job_id,
                outcome=outcome,
                finished_at=finished,
                duration_s=(finished - started).total_seconds(),
                filament_mm=mm,
                usage_estimated=bool(mm or ml),
                note=note,
                volume_ml=ml,
            )
        )
        self.current.state.discovery.pop(KEY, None)
        self.current.ended()
        self._ended = job
        self.job = None
        log.info("[printer %s] job %s %s", self.printer, job.job_id, outcome.value)

    def _usage(self, job: Job, outcome: JobOutcome) -> tuple[dict[int, float], dict[int, float]]:
        if self.resin:
            total = job.usage or job.start.estimated_volume_ml
            if not total:
                return {}, {}
            if outcome is JobOutcome.COMPLETED:
                fraction = 1.0
            elif job.layer is not None and job.total_layers:
                fraction = min(job.layer / job.total_layers, 1.0)
            else:
                fraction = job.progress / 100
            volume = round(total * fraction, 2)
            return {}, ({0: volume} if volume > 0 else {})
        if job.usage:
            return {0: round(job.usage, 1)}, {}
        return {}, {}


def _is_job(data: Mapping[str, Any]) -> bool:
    """A `print` report that describes a job (not a bare acknowledgement)."""
    return any(k in data for k in ("filename", "progress", "curr_layer", "print_status"))


def _went_back(view: View, job: Job) -> bool:
    if view.state != "printing":
        return False
    layer_back = (
        view.layer is not None and job.layer is not None and view.layer < job.layer - LAYERS_BACK
    )
    percent_back = view.progress is not None and view.progress < job.progress - PERCENT_BACK
    if view.layer is None or job.layer is None:
        return percent_back
    if view.progress is None:
        return layer_back
    return layer_back and percent_back


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number else None
