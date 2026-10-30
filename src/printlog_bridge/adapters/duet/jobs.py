"""Turns a Duet's object model, polled, into job events, with the filament each tool measured.

The model is read as `model.py` says. A job:

- **starts** the first time the model shows a file (`job.file.fileName`) with the board
  `processing`, `pausing`, `paused` or `resuming`, never `simulating` (a simulation feeds
  nothing). The file's facts (`files.py`, or the model's own `job.file`) give the slicer's
  estimate. `started_at` is now less `job.duration` and `job.pauseDuration`, and `job_id` is
  `<epoch>-<stem>`, since RRF gives a job no id;
- **carries on** while the same file is under way (any state of `UNDER_WAY`) and `job.duration`
  hasn't gone back (it did: another print of the file). Never by the wall clock, which jumps
  (NTP on a Pi without a battery) and runs on through a pause. `disconnected`, `starting` and
  `updating` (an SBC that lost the board for a moment) change nothing;
- **ends** when the file is gone or the board is idle: cancelled when `lastFileCancelled`,
  failed when `lastFileAborted`, else completed, at the poll that saw it; `halted` (an
  emergency stop) fails it at once, and so does `state.upTime` going back (the board restarted:
  a power cut or a reset), with what it had fed.

**Usage is measured**: what each extruder's `rawPosition` gained since the last poll, times the
extrusion factor (M221) it has then, added up and summed by slot (`model.slots_of`: the tool
that drives it), and sent as millimeters with `usage_estimated` off, as Klipper's is. The first
reading of a job (and the first after a bridge restart) counts what came before it at the
factor of that moment. A mixing hotend (one tool, several extruders) sums them into the tool's
one slot (a follow-up). 3D Print Log turns millimeters into grams with the spool's own diameter
and density, so the bridge needs neither. A board without `rawPosition` gives `job.rawExtrusion`
(the job's total, at the extruders' mean factor, added up the same way; split by the slicer's
share per tool on more than one); without either, the slicer's estimate, scaled by
`filePosition` for a job that didn't complete, marked estimated. The slicer's figures
(`rr_fileinfo`'s `filament`) are per tool, T0 first, so they go to the tools' slots as they are.

RRF keeps no job history, so `CurrentJob` keeps the running job (with its file in
`state.discovery["duet_job"]`). After each connect the model is reconciled: the same file under
way with the same start (within `SAME_START_S`, or paused, when `duration` stands still) is the
same job (the wall clock is used only here, after a restart). A board whose `upTime` is lower
than the one kept restarted: the job failed, with the filament kept beside it (written once a
minute). A remembered job gone while the board says its `lastFileName` was that job's file
ended as the flags say, with what `rawPosition` still holds (it is only reset when the next
print starts), at its start plus `lastDuration`. Otherwise (another print ran), it is logged by
`CurrentJob`'s rules: complete, with the slicer's estimate.
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
REBOOT_NOTE = (
    "The Duet restarted during the job (a power cut or a reset), so the job is logged as failed "
    "with the filament the bridge saw it feed."
)
REPRINT_NOTE = (
    "Another print started before the bridge saw this job end, so it is logged as completed. "
    "Check its status."
)
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
    raw_last: dict[int, float] | None = None
    """Each extruder's `rawPosition` (before its factor) at the last poll."""
    fed: dict[int, float] = field(default_factory=dict)
    """What each extruder fed, its factor applied poll by poll."""
    total_last: float | None = None
    total_fed: float = 0.0
    """`job.rawExtrusion` the same way, for a board without `rawPosition`."""
    duration: float | None = None
    up: float | None = None
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

    def _continues(self, job: Job, model: Mapping[str, Any]) -> bool:
        """While connected: the same file, and `job.duration` not gone back. Never the wall
        clock, which can jump (NTP on a Pi without a battery) or stand still in a pause."""
        if om.job_file(model) != job.path:
            return False
        duration = om.job_number(model, "duration")
        return duration is None or job.duration is None or duration >= job.duration - 1.0

    def _rebooted(self, model: Mapping[str, Any], before: float | None) -> bool:
        up = om.up_time(model)
        return up is not None and before is not None and up < before - 1.0

    def _same_start(self, start: JobStarted, path: str, model: Mapping[str, Any]) -> bool:
        """After a bridge restart, with nothing kept in memory: the same file, started when the
        remembered job did (by the wall clock, within `SAME_START_S`)."""
        if om.job_file(model) != path:
            return False
        if om.status(model) in om.PAUSED:
            return True  # `duration` stands still while paused
        drift = abs((self._started_at(model) - start.started_at).total_seconds())
        return drift <= SAME_START_S

    def _remembered_path(self) -> str | None:
        path = self._remembered("file")
        return path if isinstance(path, str) else None

    def _remembered(self, name: str) -> Any:
        entry = self.state.discovery.get(KEY)
        return entry.get(name) if isinstance(entry, Mapping) else None

    def _remembered_fed(self) -> tuple[dict[int, float], float]:
        fed = self._remembered("fed")
        by_extruder: dict[int, float] = {}
        if isinstance(fed, Mapping):
            try:
                by_extruder = {int(k): float(v) for k, v in fed.items()}
            except (TypeError, ValueError):
                by_extruder = {}
        return by_extruder, om.number(self._remembered("total_fed")) or 0.0

    # -- after a connect ------------------------------------------------------------------

    async def _reconcile(self, model: Mapping[str, Any], path: str | None) -> None:
        remembered = self.current.job
        remembered_path = self._remembered_path()
        kept = self.job  # still in memory: the connection dropped, the bridge didn't restart
        before = kept.up if kept is not None else om.number(self._remembered("up"))
        rebooted = remembered is not None and self._rebooted(model, before)
        running = None
        if path is not None and not rebooted:
            if kept is not None and self._continues(kept, model):
                running = kept.job_id
            elif (
                kept is None
                and remembered is not None
                and remembered_path is not None
                and self._same_start(remembered, remembered_path, model)
            ):
                running = remembered.job_id
        outcome = finished_at = None
        measured: dict[int, float] = {}
        note = None
        gone = remembered is not None and running != remembered.job_id
        slots = om.slots_of(model)
        if gone and rebooted:
            outcome, note = JobOutcome.FAILED, REBOOT_NOTE
            if kept is not None:
                measured = _measured(kept.fed, kept.total_fed, kept.slots or slots, kept.estimate)
            else:
                fed, total = self._remembered_fed()
                measured = _measured(fed, total, slots, self._remembered_estimate(remembered))
        elif gone and path is None and remembered_path is not None:
            job = _get(model, "job")
            if job.get("lastFileName") == remembered_path and not job.get("lastFileSimulated"):
                outcome, _ = _outcome(job)
                last = om.number(job.get("lastDuration"))
                if last is not None and last > 0:
                    end = remembered.started_at + timedelta(seconds=last)
                    seen = self.current.last_seen
                    finished_at = min(max(end, seen) if seen else end, self.clock())
                if kept is not None:
                    self._update(model, kept)
                    measured = _measured(kept.fed, kept.total_fed, kept.slots, kept.estimate)
                else:
                    measured = _end_values(model, self._remembered_estimate(remembered))
        for event in self.current.reconcile(running, outcome, finished_at):
            if isinstance(event, JobFinished) and (measured or note):
                event = dataclasses.replace(
                    event,
                    filament_mm=measured or event.filament_mm,
                    filament_g={} if measured else event.filament_g,
                    usage_estimated=False if measured else event.usage_estimated,
                    note=note or event.note,
                )
            await self.sink.emit(event)
        if kept is not None and kept.job_id == running:
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
        if self.job is not None and self._rebooted(model, self.job.up):
            await self._finish(JobOutcome.FAILED, REBOOT_NOTE)
        if path is not None:
            if self.job is not None and not self._continues(self.job, model):
                # Another print, without this one's end seen (the same file again, or another).
                await self._finish(JobOutcome.COMPLETED, REPRINT_NOTE)
            if self.job is None:
                if status in om.STARTS:
                    await self._start(model, path)
                return
            self._update(model, self.job)
            self.current.seen()
            if status == om.HALTED:
                await self._finish(JobOutcome.FAILED, HALTED_NOTE)
            return
        if self.job is None:
            return
        self._update(model, self.job)
        job = _get(model, "job")
        last = job.get("lastFileName")
        outcome, note = (JobOutcome.COMPLETED, None)
        if last in (None, self.job.path):
            outcome, note = _outcome(job)
        await self._finish(outcome, note)

    def _update(self, model: Mapping[str, Any], job: Job) -> None:
        """Adds what each extruder fed since the last poll, at the factor it has now. The first
        reading (the job's first poll, or the first after a bridge restart) counts everything
        before it at the factor it has then. A total that went back belongs to a print after
        this one, and is left out."""
        factors = om.factors(model)
        raw = om.raw_positions(model)
        if raw is not None:
            if job.raw_last is None:
                job.fed = {e: v * factors.get(e, 1.0) for e, v in raw.items()}
                job.raw_last = raw
            elif sum(raw.values()) >= sum(job.raw_last.values()) - 1.0:
                for e, v in raw.items():
                    step = v - job.raw_last.get(e, 0.0)
                    job.fed[e] = job.fed.get(e, 0.0) + step * factors.get(e, 1.0)
                job.raw_last = raw
        total = om.job_number(model, "rawExtrusion")
        if total is not None:
            factor = sum(factors.values()) / len(factors) if factors else 1.0
            if job.total_last is None:
                job.total_fed, job.total_last = total * factor, total
            elif total >= job.total_last - 1.0:
                job.total_fed += (total - job.total_last) * factor
                job.total_last = total
        duration = om.job_number(model, "duration")
        if duration is not None:
            job.duration = duration
        up = om.up_time(model)
        if up is not None:
            job.up = up
        slots = om.slots_of(model)
        if slots:
            job.slots = slots
        position = om.job_number(model, "filePosition")
        size = job.size or om.number(_get(_get(model, "job"), "file").get("size"))
        if position is not None and size:
            job.progress = max(job.progress, min(position / size, 1.0))
        entry = self.state.discovery.get(KEY)
        if isinstance(entry, dict):  # written with the next `seen` (once a minute)
            entry["fed"] = {str(e): round(v, 3) for e, v in job.fed.items()}
            entry["total_fed"] = round(job.total_fed, 3)
            entry["up"] = job.up

    async def _start(self, model: Mapping[str, Any], path: str) -> None:
        file = await self.files.read(path) if self.files is not None else JobFile()
        from_model = file_from(_get(_get(model, "job"), "file"), path)
        slots = om.slots_of(model)
        # The slicer's figures are per tool (T0, T1...), and a tool's number is its slot.
        by_tool = dict(file.mm) or dict(from_model.mm)
        estimate = {tool: round(mm, 1) for tool, mm in by_tool.items()}
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
        await self.sink.emit(start)
        self.current.started(start)
        self.state.discovery[KEY] = {
            "file": path,
            "mm": {str(k): v for k, v in estimate.items()},
            "size": size,
        }
        self._update(model, self.job)
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
        measured = _measured(job.fed, job.total_fed, job.slots, job.estimate)
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


def _end_values(model: Mapping[str, Any], estimate: Mapping[int, float]) -> dict[int, float]:
    """What the board's totals say the job fed, at the factors it has now: for a job that ended
    while the bridge wasn't watching."""
    factors = om.factors(model)
    raw = om.raw_positions(model) or {}
    fed = {e: v * factors.get(e, 1.0) for e, v in raw.items()}
    total = om.job_number(model, "rawExtrusion") or 0.0
    factor = sum(factors.values()) / len(factors) if factors else 1.0
    return _measured(fed, total * factor, om.slots_of(model), estimate)


def _measured(
    fed: Mapping[int, float],
    total: float | None,
    slots: Mapping[int, int],
    estimate: Mapping[int, float],
) -> dict[int, float]:
    """The mm each slot fed, from what each extruder measured, else the job's total (split by
    the slicer's share per slot); empty when the board measured nothing."""
    mm: dict[int, float] = {}
    if fed:
        for e, fed_mm in fed.items():
            slot = slots.get(e, e)
            mm[slot] = mm.get(slot, 0.0) + fed_mm
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
