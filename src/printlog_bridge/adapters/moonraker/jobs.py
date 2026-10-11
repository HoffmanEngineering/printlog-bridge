"""Moonraker's job history, as the bridge's job events.

History is the authoritative record of a job: Moonraker adds an entry when a print starts and
finishes it with a status, timings and `filament_used`, and announces both. On every connect the
tracker also reads the history since the sink's resume point, so a job that started or ended
while the bridge (or its connection) was down is still reported. The sink recognises replays.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from printlog_bridge.core.events import EventSink, JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.status import NullReport, PrinterReport

from .session import Call, MoonrakerRpcError

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

    def __init__(self, printer: str, sink: EventSink, report: PrinterReport | None = None) -> None:
        self.printer = printer
        self.sink = sink
        self.report = report or NullReport()
        self._error_message: str | None = None

    async def on_connected(self, call: Call) -> None:
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
        for job in jobs:
            await self._report(job)

    async def on_klippy_state(self, state: str) -> None:
        pass

    async def on_status(self, status: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
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
            await self._report(job, live=True)

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

    async def _report(self, job: Mapping[str, Any], live: bool = False) -> None:
        await self.sink.emit(started(self.printer, job))
        if job.get("status", "in_progress") != "in_progress":
            message = self._error_message if live else None
            await self.sink.emit(finished(self.printer, job, message))


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
    )


def finished(printer: str, job: Mapping[str, Any], error_message: str | None = None) -> JobFinished:
    status = str(job.get("status"))
    outcome, note = OUTCOMES.get(
        status, (JobOutcome.FAILED, f"Moonraker ended the job as {status}")
    )
    if status == "error" and error_message:
        note = f"{note}: {error_message}"
    end_time = job.get("end_time") or job["start_time"] + (job.get("total_duration") or 0)
    filament = job.get("filament_used") or 0
    return JobFinished(
        printer=printer,
        job_id=str(job["job_id"]),
        outcome=outcome,
        finished_at=_time(end_time),
        duration_s=job.get("print_duration"),
        # One row on slot 0 until per-tool usage (printlog-bridge#5) splits it by extruder.
        filament_mm={0: float(filament)},
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
