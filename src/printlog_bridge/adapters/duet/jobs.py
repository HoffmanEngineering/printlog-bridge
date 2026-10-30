"""Turns a Duet's object model, polled, into job events, with the filament each tool measured.

The model is read as `model.py` says. A job:

- **starts** the first time the model shows a file (`job.file.fileName`) with the board
  `processing`, `pausing`, `paused` or `resuming`, never `simulating` (a simulation feeds
  nothing). The file's facts (`files.py`, or the model's own `job.file`) give the slicer's
  estimate. `started_at` is now less `job.duration` and `job.pauseDuration`, and `job_id` is
  `<epoch>-<stem>`, since RRF gives a job no id;
- **carries on** while the same file is under way (any state of `UNDER_WAY`); `disconnected`,
  `starting` and `updating` (an SBC that lost the board for a moment) change nothing;
- **ends** when the file is gone or the board is idle: cancelled when `lastFileCancelled`,
  failed when `lastFileAborted`, else completed, at the poll that saw it; `halted` (an
  emergency stop) fails it at once.

**Usage is measured**: each extruder's `rawPosition` times its extrusion factor, summed by slot
(`model.slots_of`: the tool that drives it), and sent as millimeters with `usage_estimated`
off, as Klipper's is. 3D Print Log turns millimeters into grams with the spool's own diameter
and density, so the bridge needs neither. A board without `rawPosition` gives `job.rawExtrusion`
(the job's total; split by the slicer's share per tool on more than one); without either, the
slicer's estimate, scaled by `filePosition` for a job that didn't complete, marked estimated.

RRF keeps no job history, so `CurrentJob` keeps the running job (with its file in
`state.discovery["duet_job"]`). After each connect the model is reconciled: the same file under
way with the same start (within `SAME_START_S`, or paused, when `duration` stands still) is the
same job. A remembered job gone while the board says its `lastFileName` was that job's file
ended as the flags say, with what `rawPosition` still holds (it is only reset when the next
print starts), at its start plus `lastDuration`. Otherwise (the board restarted, another print
ran), it is logged by `CurrentJob`'s rules: complete, with the slicer's estimate.
"""

from __future__ import annotations

import dataclasses
import logging
import posixpath
import re
from collections.abc import Callable, Mapping
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
    JobSlicerSettings,
    JobStarted,
)
from printlog_bridge.core.health import utc_now
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.status import PrinterReport

from . import model as om
from .files import JobFile, file_from

log = logging.getLogger(__name__)

KEY = "duet_job"
SAME_START_S = 300.0
WAITING = frozenset({"disconnected", "starting", "updating"})
MAX_NAME_IN_ID = 80
HALTED_NOTE = "The printer halted (an emergency stop or a fault) during the job."
ABORTED_NOTE = "RepRapFirmware stopped the job before its end (lastFileAborted)."


class JobFiles(Protocol):
    async def read(self, path: str) -> JobFile: ...


@dataclass
class Job:
    job_id: str
    path: str
    start: JobStarted
    estimate: Mapping[int, float]
    """The slicer's mm per slot."""
    size: int | None = None
    raw: dict[int, float] = field(default_factory=dict)
    """What each extruder fed, as last seen."""
    raw_total: float | None = None
    progress: float = 0.0
    slots: dict[int, int] = field(default_factory=dict)


class JobTracker:
    def __init__(
        self,
        printer: str,
        sink: EventSink,
        current: CurrentJob,
        state: PrinterState,
        *,
        files: JobFiles | None = None,
        report: PrinterReport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.printer = printer
        self.sink = sink
        self.current = current
        self.state = state
        self.files = files
        self.report = report
        self.clock = clock
        self.job: Job | None = None
        self.status: str | None = None
        self._reconciled = False

    @property
    def active(self) -> bool:
        return self.job is not None or self.status in om.UNDER_WAY

    def connected(self) -> None:
        self._reconciled = False

    async def on_model(self, model: Mapping[str, Any]) -> None:
        status = om.status(model)
        if status is None:
            return
        self.status = status
        if self.report is not None:
            self.report.printer_state(status)
        if status in WAITING:
            return
        path = om.job_file(model)
        under_way = path is not None and status in om.UNDER_WAY
        if not self._reconciled:
            await self._reconcile(model, path if under_way else None)
            self._reconciled = True
        await self._step(model, status, path if under_way else None)

    # -- identity -------------------------------------------------------------------------

    def _started_at(self, model: Mapping[str, Any]) -> datetime:
        seconds = (om.job_number(model, "duration") or 0.0) + (
            om.job_number(model, "pauseDuration") or 0.0
        )
        return self.clock() - timedelta(seconds=max(0.0, seconds))

    def _same(self, start: JobStarted, path: str, model: Mapping[str, Any]) -> bool:
        if om.job_file(model) != path:
            return False
        if om.status(model) in om.PAUSED:
            return True  # `duration` stands still while paused
        drift = abs((self._started_at(model) - start.started_at).total_seconds())
        return drift <= SAME_START_S

    def _remembered_path(self) -> str | None:
        entry = self.state.discovery.get(KEY)
        path = entry.get("file") if isinstance(entry, Mapping) else None
        return path if isinstance(path, str) else None

    # -- after a connect ------------------------------------------------------------------

    async def _reconcile(self, model: Mapping[str, Any], path: str | None) -> None:
        remembered = self.current.job
        remembered_path = self._remembered_path()
        running = None
        if path is not None:
            if self.job is not None and self._same(self.job.start, self.job.path, model):
                running = self.job.job_id
            elif (
                remembered is not None
                and remembered_path is not None
                and self._same(remembered, remembered_path, model)
            ):
                running = remembered.job_id
        outcome = finished_at = measured = None
        gone = remembered is not None and running != remembered.job_id
        if gone and path is None and remembered_path is not None:
            job = _get(model, "job")
            if job.get("lastFileName") == remembered_path and not job.get("lastFileSimulated"):
                outcome, _ = _outcome(job)
                last = om.number(job.get("lastDuration"))
                if last is not None and last > 0:
                    end = remembered.started_at + timedelta(seconds=last)
                    seen = self.current.last_seen
                    finished_at = min(max(end, seen) if seen else end, self.clock())
                estimate = self._remembered_estimate(remembered)
                measured = _measured(
                    om.raw_by_extruder(model),
                    om.job_number(model, "rawExtrusion"),
                    om.slots_of(model),
                    estimate,
                )
        for event in self.current.reconcile(running, outcome, finished_at):
            if isinstance(event, JobFinished) and measured:
                event = dataclasses.replace(
                    event, filament_mm=measured, filament_g={}, usage_estimated=False
                )
            await self.sink.emit(event)
        if self.job is not None and self.job.job_id == running:
            return  # the connection dropped while it ran: carry on with it
        if remembered is not None and running == remembered.job_id:
            self.job = Job(
                remembered.job_id,
                remembered_path or "",
                remembered,
                self._remembered_estimate(remembered),
                size=self._remembered_size(),
            )
            return
        self.job = None
        if running is None and self.state.discovery.pop(KEY, None) is not None:
            self.state.save()

    def _remembered_estimate(self, start: JobStarted) -> dict[int, float]:
        entry = self.state.discovery.get(KEY)
        mm = entry.get("mm") if isinstance(entry, Mapping) else None
        if isinstance(mm, Mapping):
            try:
                return {int(k): float(v) for k, v in mm.items()}
            except (TypeError, ValueError):
                pass
        return {0: start.estimated_filament_mm} if start.estimated_filament_mm else {}

    def _remembered_size(self) -> int | None:
        entry = self.state.discovery.get(KEY)
        size = entry.get("size") if isinstance(entry, Mapping) else None
        return size if isinstance(size, int) and size > 0 else None

    # -- each poll ------------------------------------------------------------------------

    async def _step(self, model: Mapping[str, Any], status: str, path: str | None) -> None:
        if path is not None:
            if self.job is not None and not self._same(self.job.start, self.job.path, model):
                # Another job, without this one's end seen: what the board can't tell.
                for event in self.current.reconcile(None):
                    await self.sink.emit(event)
                self._forget()
            if self.job is None:
                if status in om.STARTS:
                    await self._start(model, path)
                return
            self._update(model)
            self.current.seen()
            if status == om.HALTED:
                await self._finish(JobOutcome.FAILED, HALTED_NOTE)
            return
        if self.job is None:
            return
        self._update(model)
        job = _get(model, "job")
        last = job.get("lastFileName")
        outcome, note = (JobOutcome.COMPLETED, None)
        if last in (None, self.job.path):
            outcome, note = _outcome(job)
        await self._finish(outcome, note)

    def _update(self, model: Mapping[str, Any]) -> None:
        job = self.job
        assert job is not None
        raw = om.raw_by_extruder(model)
        if raw and sum(raw.values()) >= sum(job.raw.values()) - 1.0:
            job.raw = raw  # a total that went back belongs to a print after this one
        total = om.job_number(model, "rawExtrusion")
        if total is not None and total >= (job.raw_total or 0.0) - 1.0:
            job.raw_total = total
        slots = om.slots_of(model)
        if slots:
            job.slots = slots
        position = om.job_number(model, "filePosition")
        size = job.size or om.number(_get(_get(model, "job"), "file").get("size"))
        if position is not None and size:
            job.progress = max(job.progress, min(position / size, 1.0))

    async def _start(self, model: Mapping[str, Any], path: str) -> None:
        file = await self.files.read(path) if self.files is not None else JobFile()
        from_model = file_from(_get(_get(model, "job"), "file"), path)
        slots = om.slots_of(model)
        by_extruder = dict(file.mm) or dict(from_model.mm)
        estimate: dict[int, float] = {}
        for e, mm in by_extruder.items():
            slot = slots.get(e, e)
            estimate[slot] = round(estimate.get(slot, 0.0) + mm, 1)
        started_at = self._started_at(model)
        job_id = f"{int(started_at.timestamp())}-{_stem(path)}"
        start = JobStarted(
            printer=self.printer,
            job_id=job_id,
            file_name=posixpath.basename(path),
            started_at=started_at,
            estimated_duration_s=file.duration_s or from_model.duration_s,
            estimated_filament_mm=round(sum(estimate.values()), 1) or None,
        )
        size = file.size or from_model.size
        self.job = Job(job_id, path, start, estimate, size=size, slots=slots)
        self._update(model)
        await self.sink.emit(start)
        self.current.started(start)
        self.state.discovery[KEY] = {
            "file": path,
            "mm": {str(k): v for k, v in estimate.items()},
            "size": size,
        }
        self.state.save()
        log.info("[printer %s] job %s started", self.printer, job_id)
        if file.thumbnail is not None:
            await self.sink.emit(JobImage(self.printer, job_id, ImageKind.THUMBNAIL,
                                          file.thumbnail))  # fmt: skip
        metadata = file.metadata or from_model.metadata
        if metadata is not None:
            await self.sink.emit(JobSlicerSettings(self.printer, job_id, metadata=metadata))

    async def _finish(self, outcome: JobOutcome, note: str | None) -> None:
        job = self.job
        assert job is not None
        measured = _measured(job.raw or None, job.raw_total, job.slots, job.estimate)
        if measured:
            mm, estimated = measured, False
        else:
            share = 1.0 if outcome is JobOutcome.COMPLETED else job.progress
            mm = {s: round(v * share, 1) for s, v in job.estimate.items() if v * share > 0}
            estimated = bool(mm)
        now = self.clock()
        await self.sink.emit(
            JobFinished(
                printer=self.printer,
                job_id=job.job_id,
                outcome=outcome,
                finished_at=now,
                duration_s=max(0.0, (now - job.start.started_at).total_seconds()),
                filament_mm=mm,
                usage_estimated=estimated,
                note=note,
            )
        )
        self.current.ended()
        self._forget()
        log.info("[printer %s] job %s %s", self.printer, job.job_id, outcome.value)

    def _forget(self) -> None:
        self.job = None
        if self.state.discovery.pop(KEY, None) is not None:
            self.state.save()


def _measured(
    raw: Mapping[int, float] | None,
    total: float | None,
    slots: Mapping[int, int],
    estimate: Mapping[int, float],
) -> dict[int, float]:
    """The mm each slot fed, from what each extruder measured, else the job's total; empty when
    the board measured nothing."""
    mm: dict[int, float] = {}
    if raw:
        for e, fed in raw.items():
            slot = slots.get(e, e)
            mm[slot] = mm.get(slot, 0.0) + fed
    elif total is not None and total > 0:
        shares = {s: v for s, v in estimate.items() if v > 0}
        if len(shares) > 1:
            whole = sum(shares.values())
            mm = {s: total * v / whole for s, v in shares.items()}
        else:
            mm = {next(iter(shares), slots.get(0, 0)): total}
    return {s: round(v, 1) for s, v in mm.items() if round(v, 1) > 0}


def _outcome(job: Mapping[str, Any]) -> tuple[JobOutcome, str | None]:
    if job.get("lastFileCancelled") is True:
        return JobOutcome.CANCELLED, None
    if job.get("lastFileAborted") is True:
        return JobOutcome.FAILED, ABORTED_NOTE
    return JobOutcome.COMPLETED, None


def _get(value: Any, key: str) -> Mapping[str, Any]:
    found = value.get(key) if isinstance(value, Mapping) else None
    return found if isinstance(found, Mapping) else {}


def _stem(path: str) -> str:
    stem = posixpath.splitext(posixpath.basename(path))[0]
    return re.sub(r"\s+", " ", stem).strip()[:MAX_NAME_IN_ID] or "print"
