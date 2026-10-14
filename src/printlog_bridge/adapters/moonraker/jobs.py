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
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from printlog_bridge.core.events import (
    EventSink,
    ImageKind,
    JobFinished,
    JobImage,
    JobOutcome,
    JobPoint,
    JobStarted,
)
from printlog_bridge.core.status import NullReport, PrinterReport

from .changes import FilamentChanges
from .excluded import ExcludedObjects
from .images import MoonrakerImages
from .session import Call, MoonrakerRpcError
from .spools import RESPOND_MACRO, answer_script
from .tools import PRINTING, ToolUsage, estimated_grams_by_slot, per_slot, slot_of

log = logging.getLogger(__name__)

PAGE_SIZE = 50
HISTORY_TOPIC = "history"

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
    ) -> None:
        self.printer = printer
        self.sink = sink
        self.report = report or NullReport()
        self.images = images
        self._error_message: str | None = None
        self._call: Call | None = None
        self._usage = ToolUsage()
        self._excluded = ExcludedObjects()
        self._changes = FilamentChanges(printer)
        self._status: Mapping[str, Any] = {}
        self._active = 0
        self._job: tuple[str, str, dict[int, float]] | None = None
        """The job in progress: its id, its file, and the slicer's grams per tool."""
        self._can_respond: bool | None = None
        self._tasks: set[asyncio.Future] = set()

    async def on_connected(self, call: Call) -> None:
        self._call = call
        self._can_respond = None
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
        for n, job in enumerate(jobs, 1):
            # Only the newest job can be the print Klipper ran last, whose usage was tracked.
            await self._report(job, latest=n == len(jobs))

    async def on_klippy_state(self, state: str) -> None:
        if state == "ready":
            self._can_respond = None  # a config change may have added printlog.cfg

    async def on_status(self, status: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        self._status = status
        slot = slot_of((status.get("toolhead") or {}).get("extruder"))
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

    def _spawn(self, work) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        """Cancels the console prompts still being shown, and waits for them to stop."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
        start = started(self.printer, job)
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
        if job.get("status", "in_progress") != "in_progress":
            message = self._error_message if live else None
            filename = str(job.get("filename") or "")
            tracked = self._usage.take(filename) if latest else {}
            excluded = self._excluded.take(filename) if latest else None
            await self.sink.emit(finished(self.printer, job, message, tracked, excluded))
            if live and images is not None and images.snapshots and call is not None:
                await self.sink.emit(
                    JobImage(
                        self.printer,
                        start.job_id,
                        ImageKind.SNAPSHOT,
                        functools.partial(images.snapshot, call),
                    )
                )


def started(printer: str, job: Mapping[str, Any]) -> JobStarted:
    metadata = job.get("metadata") or {}
    return JobStarted(
        printer=printer,
        job_id=str(job["job_id"]),
        file_name=str(job.get("filename") or ""),
        started_at=_time(job["start_time"]),
        estimated_duration_s=_positive(metadata.get("estimated_time")),
        estimated_filament_mm=_positive(metadata.get("filament_total")),
        estimated_filament_g=_positive(metadata.get("filament_weight_total")),
        estimated_filament_g_by_slot=estimated_grams_by_slot(metadata),
    )


def finished(
    printer: str,
    job: Mapping[str, Any],
    error_message: str | None = None,
    tracked: Mapping[int, float] | None = None,
    excluded: str | None = None,
) -> JobFinished:
    """The job's end. `tracked` is what the bridge saw each slot feed (`tools.ToolUsage`), and
    `excluded` its note on the objects cancelled mid-print (`excluded.ExcludedObjects`)."""
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
        estimated_grams_by_slot(job.get("metadata") or {}),
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


def _time(timestamp: float) -> datetime:
    return datetime.fromtimestamp(float(timestamp), timezone.utc)


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
