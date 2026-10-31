"""Moonraker's job history, as the bridge's job events.

History is the authoritative record of a job: Moonraker adds an entry when a print starts and
finishes it with a status, timings and `filament_used`, and announces both. On every connect the
tracker also reads the history since the sink's resume point, so a job that started or ended
while the bridge (or its connection) was down is still reported. The sink recognises replays.

With `images`, each start is followed by the job's thumbnail, and each end seen as it happens
by a webcam snapshot (when snapshots are on). A job that ended while the bridge was away gets
no snapshot: the camera would show the bed as it is now, not as the job left it.

While a job runs, the tracker also knows how far it has got (`point`), which a spool assigned
mid-print is split at (#44), and reports a pause for a filament change (`changes.py`) once the
job resumes from it, prompting on the console for the new spool when it sees one.

After the thumbnail comes the job's slicer settings (#36): the summary in its metadata, and a
way to read its file's header and settings block (`slicer.py`), which the sink uses only when
it wants them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from printlog_bridge.core.colors import normalized
from printlog_bridge.core.events import (
    EventSink,
    FileEstimate,
    ImageKind,
    JobFinished,
    JobImage,
    JobOutcome,
    JobPoint,
    JobSlicerSettings,
    JobStarted,
    SlicerFile,
    UnplacedFilament,
)
from printlog_bridge.core.status import NullReport, PrinterReport

from .changes import FilamentChanges
from .excluded import ExcludedObjects
from .images import MoonrakerImages
from .session import Call, MoonrakerRpcError
from .slicer import MoonrakerSlicerFiles, slicer_metadata
from .spools import RESPOND_MACRO, answer_script
from .task_config import (
    K3_FIELDS,
    U1_FIELDS,
    loaded,
    placed,
    placed_by_color,
    slot_notes,
    tool_map,
    unplaced_note,
)
from .timelapse import TimelapseWatcher
from .tools import (
    PRINTING,
    ToolUsage,
    active_slot,
    duration_from_file,
    estimated_grams_by_slot,
    grams_by_slot_from_file,
    per_slot,
    sliced_filaments_from_file,
)

log = logging.getLogger(__name__)

PAGE_SIZE = 50
HISTORY_TOPIC = "history"
MAX_FILE_GRAMS = 8
"""Jobs whose grams per tool from the file are kept until they end: one runs at a time."""

STOPPED = "Moonraker stopped during the print"
OUTCOMES: Mapping[str, tuple[JobOutcome, str | None]] = {
    "completed": (JobOutcome.COMPLETED, None),
    "cancelled": (JobOutcome.CANCELLED, None),
    "error": (JobOutcome.FAILED, "Klipper error"),
    "klippy_shutdown": (JobOutcome.FAILED, "Klipper shutdown"),
    "klippy_disconnect": (JobOutcome.FAILED, "Lost connection to Klipper"),
    "interrupted": (JobOutcome.FAILED, STOPPED),  # found in progress when Moonraker started
    "server_exit": (JobOutcome.FAILED, STOPPED),  # Moonraker shut down cleanly mid-print
}


class JobTracker:
    """A MoonrakerListener that reports each job in history to an EventSink."""

    def __init__(
        self,
        printer: str,
        sink: EventSink,
        report: PrinterReport | None = None,
        images: MoonrakerImages | None = None,
        timelapse: TimelapseWatcher | None = None,
    ) -> None:
        self.printer = printer
        self.sink = sink
        self.report = report or NullReport()
        self.images = images
        self.slicer = MoonrakerSlicerFiles(printer, images) if images is not None else None
        self.timelapse = timelapse
        self._error_message: str | None = None
        self._call: Call | None = None
        self._usage = ToolUsage()
        self._excluded = ExcludedObjects()
        self._changes = FilamentChanges(printer)
        self._status: Mapping[str, Any] = {}
        self._active = 0
        self._job: tuple[str, str, dict[int, float]] | None = None
        """The job in progress: its id, its file, and the slicer's grams per tool."""
        self._file_grams: dict[str, dict[int, float]] = {}
        """The slicer's grams per tool found in a job's settings block, by job id, kept for its
        end when Moonraker's metadata has none (Creality Print's files)."""
        self._files: dict[str, SlicerFile] = {}
        """What was read of each job's file, by job id and file name, so its estimate and its
        slicer settings read it once between them (the last `MAX_FILE_GRAMS` jobs)."""
        self._replay_status: Mapping[str, Any] = {}
        """Klipper's status as asked for on connecting, for the jobs replayed then."""
        self._can_respond: bool | None = None
        self._tasks: set[asyncio.Future] = set()

    async def on_connected(self, call: Call) -> None:
        self._call = call
        self._can_respond = None
        if self.timelapse is not None:
            await self.timelapse.on_connected(call)
        try:
            jobs = await self._jobs_to_replay(call)
        except MoonrakerRpcError as e:
            if e.code != -32601:  # anything but "Method not found"
                raise
            problem = (
                "Moonraker's job history is off, so no print can be logged. Add a [history] "
                "section to moonraker.conf and restart Moonraker."
            )
            log.error("[printer %s] %s", self.printer, problem)
            self.report.warn(HISTORY_TOPIC, problem)
            return
        self.report.warn(HISTORY_TOPIC, None)
        # Replayed before the subscription answers: what Klipper ran last, and its tool map and
        # loaded slots, are asked for once, so the job they describe gets them (`_here`).
        self._replay_status = await _replay_status(call) if jobs else {}
        try:
            for n, job in enumerate(jobs, 1):
                # Only the newest job can be the print Klipper ran last, whose usage was tracked.
                await self._report(job, latest=n == len(jobs))
        finally:
            self._replay_status = {}

    async def on_klippy_state(self, state: str) -> None:
        if state == "ready":
            self._can_respond = None  # a config change may have added printlog.cfg

    async def on_status(self, status: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        self._status = status
        slot = active_slot(status)
        if slot is not None:
            self._active = slot
        self._usage.update(status)
        self._excluded.update(status)
        prompts, change = self._changes.update(status, self.point, self._active)
        for prompt in prompts:
            self._spawn(self._prompt(prompt))
        if change is not None:
            await self.sink.emit(change)
        if "print_stats" not in changed:
            return
        print_stats = status.get("print_stats") or {}
        state = print_stats.get("state")
        if state == "error":
            self._error_message = print_stats.get("message") or None
        elif state in ("printing", "paused"):
            self._error_message = None  # a new job; an earlier error isn't its story

    async def on_history(self, action: str, job: Mapping[str, Any]) -> None:
        if action in ("added", "finished"):
            await self._report(job, live=True, latest=True)

    def point(self) -> JobPoint | None:
        """How far the job Klipper is running has got, or None when none is."""
        stats = self._status.get("print_stats") or {}
        if self._job is None or stats.get("state") not in PRINTING:
            return None
        job_id, filename, estimated = self._job
        if (stats.get("filename") or filename) != filename:
            return None  # Klipper is on another file than the job history last announced
        used = stats.get("filament_used")
        used = float(used) if isinstance(used, (int, float)) else 0.0
        tracked = self._usage.slots if self._usage.filename == filename else {}
        progress = (self._status.get("virtual_sdcard") or {}).get("progress")
        layer = (stats.get("info") or {}).get("current_layer")
        return JobPoint(
            job_id=job_id,
            fed_mm=per_slot(used, tracked, estimated),
            progress=float(progress) if isinstance(progress, (int, float)) else None,
            layer=layer if isinstance(layer, int) and not isinstance(layer, bool) else None,
        )

    async def _prompt(self, text: str) -> None:
        """Shows `text` on the console, when printlog.cfg is there to show it."""
        log.info("[printer %s] %s", self.printer, text)
        call = self._call
        if call is None:
            return
        try:
            if self._can_respond is None:
                objects = (await call("printer.objects.list", None))["objects"]
                self._can_respond = RESPOND_MACRO in objects
            if self._can_respond:
                await call("printer.gcode.script", {"script": answer_script(text)})
        except Exception as e:
            log.debug("[printer %s] couldn't show it on the console: %s", self.printer, e)

    async def _read_file(self, call: Call, job: Mapping[str, Any]) -> SlicerFile | None:
        """The job's slicer settings, as the sink asks for them (`JobSlicerSettings.read`, only
        with `slicer_settings` on). The settings block holds the slicer's grams per tool too,
        kept for the job's end, so the file is read for them no more than for its settings.
        Read once per job: the estimate before the print is created (`_estimate_from_file`) and
        the settings after it get the same read."""
        assert self.slicer is not None
        job_id = str(job["job_id"])
        # By file as well: Moonraker numbers jobs again after its history is cleared.
        key = f"{job_id}:{job.get('filename') or ''}"
        if key in self._files:
            return self._files[key]
        file = await self.slicer.read(call, job)
        if file is not None:  # nothing read can be tried again by the settings
            self._files[key] = file
            while len(self._files) > MAX_FILE_GRAMS:
                del self._files[next(iter(self._files))]
        grams = grams_by_slot_from_file(file.config_block or "") if file is not None else {}
        if grams:
            self._file_grams[job_id] = grams
            while len(self._file_grams) > MAX_FILE_GRAMS:
                del self._file_grams[next(iter(self._file_grams))]
        return file

    async def _estimate_from_file(
        self, call: Call, job: Mapping[str, Any], here: Mapping[str, Any] | None
    ) -> FileEstimate | None:
        """The slicer's estimate from the end of the job's file, for a job whose metadata has
        none (`JobStarted.read_estimate`, printlog-bridge#128). Each sliced filament goes to the
        slot the printer sent it to (the U1's tool map), else to the slot nearest in color (the
        K3's CFS), else to its own number; one that can't be placed is `unplaced`."""
        file = await self._read_file(call, job)
        text = (file.config_block or "") if file is not None else ""
        duration = duration_from_file(text)
        filaments = sliced_filaments_from_file(text)
        if not filaments:
            return FileEstimate(duration_s=duration) if duration else None
        grams = {index: f.grams for index, f in enumerate(filaments) if f.grams > 0}
        table = tool_map(here) if here is not None else None
        slots = loaded(here) if here is not None else {}
        unplaced: list[UnplacedFilament] = []
        if table or (here is not None and "box" not in here):
            # The U1's map, or a printer with no CFS: the slicer's tools are its slots.
            by_slot = placed(grams, table)
        else:
            # A K3, or a job whose printer state is long gone (`_here`): by color, and with
            # nothing loaded to go by, nothing is placed rather than guessed.
            by_slot, missing = placed_by_color(filaments, slots)
            unplaced = [
                UnplacedFilament(
                    index,
                    filaments[index].grams,
                    normalized(filaments[index].color),
                    unplaced_note(index, filaments[index].color, filaments[index].material),
                )
                for index in missing
            ]
        return FileEstimate(
            duration_s=duration,
            filament_g=round(sum(grams.values()), 4),
            by_slot=by_slot,
            unplaced=unplaced,
            slot_colors={slot: color for slot, (color, _) in slots.items() if color},
        )

    def _here(self, job: Mapping[str, Any], live: bool) -> Mapping[str, Any] | None:
        """Klipper's status, when it describes `job`: a job announced as it happens, or the
        file Klipper ran last. Only then are its tool map and loaded filaments the job's
        (`task_config.py`); an older job's are long gone."""
        if live:
            return self._status
        status = self._replay_status or self._status
        filename = (status.get("print_stats") or {}).get("filename")
        return status if filename and filename == job.get("filename") else None

    def _spawn(self, work) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        """Cancels the console prompts still being shown, and the timelapse videos still being
        fetched, and waits for them to stop."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.timelapse is not None:
            await self.timelapse.close()

    async def on_gcode_response(self, line: str) -> None:
        pass

    async def _jobs_to_replay(self, call: Call) -> list[Mapping[str, Any]]:
        resume = await self.sink.resume_after(self.printer)
        if resume is None:
            # The first run: pick up a print already running, and leave older history to
            # `printlog-bridge backfill`.
            latest = await call("server.history.list", {"limit": 1, "order": "desc"})
            return [j for j in latest["jobs"] if j.get("status") == "in_progress"]

        # `since` keeps jobs that started strictly after it. A second's margin keeps the job
        # at the resume point, whose time has made a round trip through a datetime.
        since = resume.timestamp() - 1
        jobs: list[Mapping[str, Any]] = []
        while True:
            page = await call(
                "server.history.list",
                {"since": since, "order": "asc", "limit": PAGE_SIZE, "start": len(jobs)},
            )
            jobs.extend(page["jobs"])
            if len(page["jobs"]) < PAGE_SIZE:
                return jobs

    async def _report(
        self, job: Mapping[str, Any], live: bool = False, latest: bool = False
    ) -> None:
        here = self._here(job, live)
        start = started(self.printer, job, here)
        if self.slicer is not None and self._call is not None and _no_estimate(job):
            start = dataclasses.replace(
                start,
                read_estimate=functools.partial(self._estimate_from_file, self._call, job, here),
            )
        model = (job.get("metadata") or {}).get("printer_model")
        if isinstance(model, str) and model.strip():
            self.report.printer_model(model.strip())
        if job.get("status", "in_progress") == "in_progress":
            self._job = (start.job_id, start.file_name, dict(start.estimated_filament_g_by_slot))
        elif self._job is not None and self._job[0] == start.job_id:
            self._job = None
        await self.sink.emit(start)
        images, call = self.images, self._call
        if images is not None and call is not None:
            await self.sink.emit(
                JobImage(
                    self.printer,
                    start.job_id,
                    ImageKind.THUMBNAIL,
                    functools.partial(images.thumbnail, call, job),
                )
            )
        if call is not None:
            read = functools.partial(self._read_file, call, job) if self.slicer else None
            metadata = job.get("metadata")
            await self.sink.emit(
                JobSlicerSettings(
                    self.printer,
                    start.job_id,
                    slicer_metadata(metadata) if isinstance(metadata, Mapping) else None,
                    read,
                )
            )
        if job.get("status", "in_progress") != "in_progress":
            message = self._error_message if live else None
            filename = str(job.get("filename") or "")
            tracked = self._usage.take(filename) if latest else {}
            excluded = self._excluded.take(filename) if latest else None
            from_file = self._file_grams.pop(start.job_id, {})
            table = tool_map(here) if here is not None else None
            await self.sink.emit(
                finished(self.printer, job, message, tracked, excluded, from_file, table)
            )
            if live and self.timelapse is not None:
                self.timelapse.job_ended(start.job_id, start.file_name)
            if live and images is not None and images.snapshots and call is not None:
                await self.sink.emit(
                    JobImage(
                        self.printer,
                        start.job_id,
                        ImageKind.SNAPSHOT,
                        functools.partial(images.snapshot, call),
                    )
                )


async def _replay_status(call: Call) -> Mapping[str, Any]:
    """The file Klipper ran last, and where it sent each tool and what each slot has loaded;
    empty when Klipper can't say (not ready yet, or an object it doesn't have)."""
    objects = {"print_stats": ["filename"], "print_task_config": U1_FIELDS, "box": K3_FIELDS}
    try:
        result = await call("printer.objects.query", {"objects": objects})
    except Exception as e:
        log.debug("couldn't ask Klipper what it ran last: %s", e)
        return {}
    status = (result or {}).get("status") if isinstance(result, Mapping) else None
    return status if isinstance(status, Mapping) else {}


def started(
    printer: str, job: Mapping[str, Any], here: Mapping[str, Any] | None = None
) -> JobStarted:
    """The job's start. `here` is Klipper's status when it describes this job (`_here`): the
    slicer's grams per tool then go to the slots the printer sent each tool to, and each slot's
    usage row is noted with what it has loaded."""
    metadata = job.get("metadata") or {}
    table = tool_map(here) if here is not None else None
    total_g = _positive(metadata.get("filament_weight_total"))
    return JobStarted(
        printer=printer,
        job_id=str(job["job_id"]),
        file_name=str(job.get("filename") or ""),
        started_at=_time(job["start_time"]),
        estimated_duration_s=_positive(metadata.get("estimated_time")),
        estimated_filament_mm=_positive(metadata.get("filament_total")),
        estimated_filament_g=_positive(metadata.get("filament_weight_total")),
        estimated_filament_g_by_slot=placed(estimated_grams_by_slot(metadata), table, total_g),
        slot_notes=slot_notes(here) if here is not None else {},
    )


def finished(
    printer: str,
    job: Mapping[str, Any],
    error_message: str | None = None,
    tracked: Mapping[int, float] | None = None,
    excluded: str | None = None,
    from_file: Mapping[int, float] | None = None,
    table: list[int] | None = None,
) -> JobFinished:
    """The job's end. `tracked` is what the bridge saw each slot feed (`tools.ToolUsage`),
    `excluded` its note on the objects cancelled mid-print (`excluded.ExcludedObjects`), and
    `from_file` the slicer's grams per tool read from the file, for when the metadata has none.
    `table` is where the printer sent each of the slicer's tools (`task_config.tool_map`), which
    places those estimates when the bridge saw no tool feed."""
    status = str(job.get("status"))
    outcome, note = OUTCOMES.get(
        status, (JobOutcome.FAILED, f"Moonraker ended the job as {status}")
    )
    if status == "error" and error_message:
        note = f"{note}: {error_message}"
    note = "\n\n".join(part for part in (note, excluded) if part) or None
    end_time = job.get("end_time") or job["start_time"] + (job.get("total_duration") or 0)
    filament_mm = per_slot(
        job.get("filament_used") or 0,
        tracked or {},
        placed(
            estimated_grams_by_slot(job.get("metadata") or {}) or dict(from_file or {}), table, 1.0
        ),
    )
    return JobFinished(
        printer=printer,
        job_id=str(job["job_id"]),
        outcome=outcome,
        finished_at=_time(end_time),
        duration_s=job.get("print_duration"),
        filament_mm=filament_mm,
        note=note,
    )


def _no_estimate(job: Mapping[str, Any]) -> bool:
    """Whether Moonraker's metadata for the job has no estimate at all (no time, no filament):
    a Creality Print file, whose estimate is then read from the file's end."""
    metadata = job.get("metadata") or {}
    keys = ("estimated_time", "filament_total", "filament_weight_total", "filament_weights")
    return not any(_positive(metadata.get(key)) or metadata.get(key) for key in keys)


def _time(timestamp: float) -> datetime:
    return datetime.fromtimestamp(float(timestamp), timezone.utc)


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
