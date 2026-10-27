"""Turns a Bambu Lab printer's status reports into job events.

The printer publishes its status as `{"print": {...}}` on `device/<serial>/report`. An X1 sends
every field each time; a P1 or A1 sends only the fields that changed, so the tracker keeps the
status merged from every report since the connect (whose `pushall` answer is a whole one).

`gcode_state` is the job's lifecycle (OpenBambuAPI, ha-bambulab): `IDLE`, then `PREPARE` (and
`SLICING` for a cloud print) while the file arrives and the printer heats and levels, `RUNNING`
and `PAUSE`, then `FINISH` or `FAILED`, which stays until the next job. A job cancelled from the
screen or the slicer ends `FAILED` with `print_error` 0x0300400C (ha-bambulab) for a moment
before it goes back to 0; 0x0500400E is taken as a cancel too.

- A job starts when it reaches `RUNNING` (or ends before it did): its file is on the printer by
  then, so its 3MF can be read for the slicer's grams per filament, which go on the start as
  estimates. `started_at` is the printer's `gcode_start_time`, which counts the preparation.
- It ends at `FINISH` (completed), `FAILED` (cancelled or failed, by `print_error`), or `IDLE`
  straight from a running state (cancelled, with a note). Its usage is the slicer's grams per
  filament, scaled by `mc_percent` for a job that didn't complete, marked estimated.
- The local API keeps no history, so the running job is kept with `CurrentJob`, and after each
  connect `reconcile` says what became of it.

`job_id` is `<gcode_start_time>-<subtask_name>`: the printer's `task_id` is 0 for a print that
didn't go through the cloud. A printer that gives no start time gets the remembered job's id
when the file matches, and the time the bridge first saw it otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.events import (
    EventSink,
    ImageKind,
    JobFinished,
    JobImage,
    JobOutcome,
    JobPoint,
    JobSlicerSettings,
    JobStarted,
    SlicerFile,
    SpoolReading,
    SpoolSink,
)
from printlog_bridge.core.health import utc_now
from printlog_bridge.core.slicer_settings import config_block_from_settings
from printlog_bridge.core.status import PrinterReport

from .files import JobFiles
from .slots import TrayReading, filament_slots, readings, slot_count
from .threemf import Plate, ThreeMF, plate_of

log = logging.getLogger(__name__)

ACTIVE = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})
PRINTING = frozenset({"RUNNING", "PAUSE"})
ENDED = frozenset({"FINISH", "FAILED", "IDLE"})
CANCEL_ERRORS = frozenset({0x0300400C, 0x0500400E})
MAX_NAME_IN_ID = 80
IDLE_NOTE = (
    "The printer went back to idle without saying the job finished or failed, so it is logged "
    "as cancelled."
)


@dataclass
class Job:
    job_id: str
    file_name: str
    started_at: datetime
    start: JobStarted | None = None
    """Set once the start is emitted."""
    error: int = 0
    """The last nonzero `print_error` seen while it ran."""
    plate: Plate | None = None
    """The slicer's numbers for the plate being printed, from the 3MF."""
    slots: dict[int, int] = field(default_factory=dict)
    """Each filament's slot, by its id in the project."""


class JobTracker:
    def __init__(
        self,
        printer: str,
        sink: EventSink,
        current: CurrentJob,
        files: JobFiles | None = None,
        report: PrinterReport | None = None,
        spools: SpoolSink | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.printer = printer
        self.sink = sink
        self.current = current
        self.files = files
        self.report = report
        self.spools = spools
        self.clock = clock
        self.status: dict[str, Any] = {}
        self.job: Job | None = None
        self._reconciled = False
        self._readings: dict[int, TrayReading] = {}
        self._pending: set[asyncio.Future] = set()

    def connected(self) -> None:
        """A new connection: the next whole status is checked against the remembered job."""
        self.status = {}
        self._reconciled = False

    async def on_message(self, message: Mapping[str, Any]) -> None:
        report = message.get("print")
        if not isinstance(report, Mapping):
            return
        _merge(self.status, report)
        state = self.status.get("gcode_state")
        if not isinstance(state, str):
            return  # a P1's first diffs, before the pushall answer
        if self.report is not None:
            self.report.printer_state(state.lower())
        if not self._reconciled:
            self._reconciled = True
            await self._reconcile(state)
        await self._step(state)
        self._send_readings()

    def point(self) -> JobPoint | None:
        """Where the running job is, for a spool reading taken now."""
        job = self.job
        if job is None or job.start is None:
            return None
        progress = self._progress()
        fed = {slot: mm * progress for slot, mm in self._usage(job, 1.0)[1].items()}
        layer = _int(self.status.get("layer_num"))
        return JobPoint(job.job_id, fed_mm=fed, progress=progress, layer=layer or None)

    async def close(self) -> None:
        for task in list(self._pending):
            task.cancel()
        if self.files is not None:
            self.files.close()

    async def _reconcile(self, state: str) -> None:
        remembered = self.current.job
        running = self._job_id() if state in ACTIVE else None
        outcome = None
        last_job_ended = state in ("FINISH", "FAILED") and remembered is not None
        if last_job_ended and self._job_id() == remembered.job_id:  # type: ignore[union-attr]
            outcome = self._outcome(state, _int(self.status.get("print_error")) or 0)[0]
        events = self.current.reconcile(running, outcome)
        for event in events:
            await self.sink.emit(event)
        if self.job is not None and self.job.job_id == running:
            pass  # the connection dropped while it ran: carry on with it
        elif remembered is not None and running == remembered.job_id:
            # The bridge restarted while it ran. Its file is read again at its end.
            self.job = Job(remembered.job_id, remembered.file_name, remembered.started_at)
            self.job.start = remembered
        elif self.job is not None:
            self.job = None  # ended while disconnected: reconciled above

    async def _step(self, state: str) -> None:
        if state in ACTIVE:
            job_id = self._job_id()
            if self.job is not None and self.job.job_id != job_id:
                # Another job without this one's end seen: what the printer can't tell.
                for event in self.current.reconcile(job_id):
                    await self.sink.emit(event)
                self.job = None
            if self.job is None:
                self.job = Job(job_id, self._file_name(), self._started_at())
                log.info("[printer %s] job %s is under way", self.printer, job_id)
            self._note_error()
            if self.job.start is None and state in PRINTING:
                await self._start(self.job)
            elif self.job.start is not None:
                self.current.seen()
        elif state in ENDED and self.job is not None:
            job = self.job
            self._note_error()
            if job.start is None:
                await self._start(job)
            outcome, note = self._outcome(state, job.error)
            await self._finish(job, outcome, note)

    async def _start(self, job: Job) -> None:
        three = await self._open()
        if three is not None:
            plate = three.plate(
                plate_of(str(self.status.get("gcode_file") or ""), self.status.get("plate_idx"))
            )
            if plate is not None:
                job.plate = plate
                job.slots = filament_slots([f.id for f in plate.filaments], self.status)
        grams, mm = self._usage(job, 1.0)
        duration = job.plate.prediction_s if job.plate else None
        if duration is None:
            minutes = _int(self.status.get("mc_remaining_time"))
            duration = minutes * 60.0 if minutes else None
        start = JobStarted(
            printer=self.printer,
            job_id=job.job_id,
            file_name=job.file_name,
            started_at=job.started_at,
            estimated_duration_s=duration,
            estimated_filament_mm=sum(mm.values()) or None,
            estimated_filament_g=sum(grams.values()) or None,
            estimated_filament_g_by_slot=grams if len(grams) > 1 else {},
        )
        job.start = start
        await self.sink.emit(start)
        self.current.started(start)
        if three is None or job.plate is None:
            return
        thumbnail = three.thumbnail(job.plate.index)

        async def fetch_thumbnail() -> bytes | None:
            return thumbnail

        await self.sink.emit(
            JobImage(self.printer, job.job_id, ImageKind.THUMBNAIL, fetch_thumbnail)
        )

        async def read_settings() -> SlicerFile | None:
            settings = await asyncio.get_running_loop().run_in_executor(None, three.settings)
            block = config_block_from_settings(settings) if settings else None
            return SlicerFile(config_block=block) if block else None

        await self.sink.emit(JobSlicerSettings(self.printer, job.job_id, read=read_settings))

    async def _finish(self, job: Job, outcome: JobOutcome, note: str | None) -> None:
        if job.plate is None:  # its file couldn't be had at the start: once more
            three = await self._open()
            plate = (
                three.plate(plate_of(str(self.status.get("gcode_file") or ""))) if three else None
            )
            if plate is not None:
                job.plate = plate
                job.slots = filament_slots([f.id for f in plate.filaments], self.status)
        progress = 1.0 if outcome is JobOutcome.COMPLETED else self._progress()
        grams, mm = self._usage(job, progress)
        now = self.clock()
        finished = JobFinished(
            printer=self.printer,
            job_id=job.job_id,
            outcome=outcome,
            finished_at=now,
            duration_s=max(0.0, (now - job.started_at).total_seconds()),
            filament_mm=mm,
            filament_g=grams,
            usage_estimated=bool(grams or mm),
            note=note,
        )
        await self.sink.emit(finished)
        self.current.ended()
        self.job = None
        log.info("[printer %s] job %s %s", self.printer, job.job_id, outcome.value)

    async def _open(self) -> ThreeMF | None:
        if self.files is None:
            return None
        return await self.files.open(self.status)

    def _usage(self, job: Job, progress: float) -> tuple[dict[int, float], dict[int, float]]:
        """The slicer's grams and millimeters per slot, times `progress`; nothing at 0."""
        grams: dict[int, float] = {}
        mm: dict[int, float] = {}
        if job.plate is None or progress <= 0:
            return grams, mm
        for filament in job.plate.filaments:
            slot = job.slots.get(filament.id, 0)
            if filament.used_g > 0:
                grams[slot] = grams.get(slot, 0.0) + round(filament.used_g * progress, 2)
            if filament.used_m > 0:
                mm[slot] = mm.get(slot, 0.0) + round(filament.used_m * 1000 * progress, 1)
        return grams, mm

    def _outcome(self, state: str, error: int) -> tuple[JobOutcome, str | None]:
        if state == "FINISH":
            return JobOutcome.COMPLETED, None
        if state == "IDLE":
            return JobOutcome.CANCELLED, IDLE_NOTE
        if error in CANCEL_ERRORS:
            return JobOutcome.CANCELLED, None
        if error:
            code = f"{error >> 16:04X}-{error & 0xFFFF:04X}"
            return JobOutcome.FAILED, f"The printer stopped the job with error {code}."
        return JobOutcome.FAILED, "The printer reported the job as failed."

    def _note_error(self) -> None:
        error = _int(self.status.get("print_error"))
        if error and self.job is not None:
            self.job.error = error

    def _progress(self) -> float:
        percent = _int(self.status.get("mc_percent")) or 0
        return min(max(percent, 0), 100) / 100

    def _file_name(self) -> str:
        gcode_file = str(self.status.get("gcode_file") or "").strip()
        subtask = str(self.status.get("subtask_name") or "").strip()
        if gcode_file.lower().endswith(".3mf") and not gcode_file.startswith("/data/"):
            return posixpath.basename(gcode_file)
        if subtask:
            return subtask if subtask.lower().endswith(".3mf") else f"{subtask}.3mf"
        if gcode_file and not gcode_file.startswith("/data/"):
            return posixpath.basename(gcode_file)
        return ""

    def _started_at(self) -> datetime:
        seconds = _int(self.status.get("gcode_start_time"))
        if seconds and seconds > 0:
            return datetime.fromtimestamp(seconds, timezone.utc)
        return self.clock()

    def _job_id(self) -> str:
        file_name = self._file_name()
        name = re.sub(r"\s+", " ", re.sub(r"\.(gcode\.)?3mf$", "", file_name, flags=re.I))
        name = (name or "print")[:MAX_NAME_IN_ID]
        seconds = _int(self.status.get("gcode_start_time"))
        if seconds and seconds > 0:
            return f"{seconds}-{name}"
        if self.job is not None and self.job.file_name == file_name:
            return self.job.job_id
        remembered = self.current.job
        if remembered is not None and remembered.file_name == file_name:
            return remembered.job_id
        return f"{int(self.clock().timestamp())}-{name}"

    def _send_readings(self) -> None:
        if self.spools is None:
            return
        count = slot_count(self.status)
        for slot, reading in sorted(readings(self.status).items()):
            known = self._readings.get(slot)
            if known == reading:
                continue
            self._readings[slot] = reading
            if known is None and reading.empty:
                continue  # an empty tray at the start says nothing
            task = asyncio.ensure_future(
                self.spools.spool_reading(
                    SpoolReading(
                        self.printer,
                        slot,
                        tag=reading.tag,
                        material=reading.material,
                        color_hex=reading.color_hex,
                        slot_count=count,
                        during=self.point(),
                    )
                )
            )
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)


def _merge(into: dict[str, Any], diff: Mapping[str, Any]) -> None:
    """`diff`'s fields into `into`, objects field by field; a list replaces the list."""
    for key, value in diff.items():
        if isinstance(value, Mapping) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = dict(value) if isinstance(value, Mapping) else value


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
