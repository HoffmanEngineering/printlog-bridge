"""Turns job events into prints in 3D Print Log: created when a job starts, completed when it ends.

Every call is safe to repeat. The print is created with an external ID (`<instance>:<job_id>`)
the API deduplicates on, the state file recognises a replayed event before any call is made, and
completing only ever sets what the printer measured. What the user edits in the app while the
job runs (title, notes, a PartialSuccess status, the spool on a usage row) is kept.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from printlog_bridge.core.api_client import ApiClient, ApiRejected, ApiUnavailable, AuthRejected
from printlog_bridge.core.events import JobEvent, JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.state import JobRecord, PrinterState
from printlog_bridge.core.status import BoardReport, StatusBoard
from printlog_bridge.core.titles import title_from_file_name

log = logging.getLogger(__name__)

PRINTING = 2
STATUS_FOR = {JobOutcome.COMPLETED: 3, JobOutcome.CANCELLED: 4, JobOutcome.FAILED: 5}
FINISHED = frozenset({3, 4, 5, 6})  # Success, Cancelled, Failed, PartialSuccess
WEIGHT, LENGTH = 1, 2
PRIVATE = 3

# The account settings the web app's new-print form starts from.
DEFAULT_VIEW_STATUS_SETTING = 1
LAST_ALLOW_COMMENTS_SETTING = 3
VIEW_STATUSES = {"public": 1, "unlisted": 2, "private": 3, "1": 1, "2": 2, "3": 3}

RETRY_DELAYS_S = (2, 10, 30)
API_TOPIC = "api"


@dataclass(frozen=True)
class LoggedPrinter:
    printer_id: int
    """The printer in 3D Print Log."""
    source: str
    """The API's `externalSource`: the adapter's name, such as `moonraker`."""


class JobLogger:
    """The bridge's EventSink: logs each job to the API."""

    def __init__(
        self,
        api: ApiClient,
        printers: Mapping[str, LoggedPrinter],
        state_dir: Path,
        retry_delays: Sequence[float] = RETRY_DELAYS_S,
        board: StatusBoard | None = None,
        before_create: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """`before_create(printer)` runs before each print is created. The bridge registers the
        printer's connection there, so the print links to it."""
        self.api = api
        self.printers = dict(printers)
        self.state_dir = state_dir
        self.retry_delays = tuple(retry_delays)
        self.board = board
        self.before_create = before_create
        self._states: dict[str, PrinterState] = {}
        self._defaults: tuple[int, bool] | None = None

    async def resume_after(self, printer: str) -> datetime | None:
        return self.state(printer).resume_after()

    async def emit(self, event: JobEvent) -> None:
        target = self.printers.get(event.printer)
        if target is None:
            log.error("[printer %s] isn't linked to 3D Print Log; skipping its jobs", event.printer)
            return
        state = self.state(event.printer)
        if isinstance(event, JobStarted):
            await self._started(target, state, event)
        else:
            await self._finished(target, state, event)

    def _report(self, printer: str) -> BoardReport | None:
        return self.board.printer(printer) if self.board is not None else None

    def state(self, printer: str) -> PrinterState:
        """The printer's state file, loaded once and kept."""
        if printer not in self._states:
            self._states[printer] = PrinterState.load(self.state_dir, printer)
        return self._states[printer]

    # -- start ---------------------------------------------------------------------------------

    async def _started(self, target: LoggedPrinter, state: PrinterState, event: JobStarted):
        job = state.jobs.get(event.job_id)
        if job is None:
            start = asdict(event)
            start["started_at"] = event.started_at.isoformat()
            job = state.jobs[event.job_id] = JobRecord(started_at=start["started_at"], start=start)
            state.save()
        if job.finished or job.print_id is not None:
            return  # a replay

        async def create() -> None:
            print_ = await self._create(target, state, event.printer, event.job_id, job)
            self._logged(
                event.printer,
                f"job {event.job_id} started: print {print_['id']}, {print_.get('title')!r}",
            )

        await self._attempt(event, create)

    async def _create(
        self, target: LoggedPrinter, state: PrinterState, printer: str, job_id: str, job: JobRecord
    ) -> dict[str, Any]:
        start = job.start
        if self.before_create is not None:
            try:
                await self.before_create(printer)
            except Exception:  # the print matters more than its link to the connection
                log.exception("[printer %s] couldn't register the connection", printer)
        view_status, allow_comments = await self._account_defaults()
        body: dict[str, Any] = {
            "printerId": target.printer_id,
            "title": title_from_file_name(start["file_name"]),
            "fileName": start["file_name"].replace("\\", "/").rsplit("/", 1)[-1],
            "startDate": start["started_at"],
            "status": PRINTING,
            "viewStatus": view_status,
            "allowComments": allow_comments,
            "externalSource": target.source,
            "externalId": f"{state.instance_id}:{job_id}",
            "connectionInstanceId": state.instance_id,
        }
        if start.get("estimated_duration_s"):
            body["estimatedPrintTimeInSeconds"] = round(start["estimated_duration_s"])
        if start.get("estimated_filament_g"):
            body["estimatedFilamentUsageMg"] = round(start["estimated_filament_g"] * 1000)
        row = _estimate_row(start)
        if row:
            body["filamentUsage"] = [row]

        print_, _ = await self.api.create_print(body)
        job.print_id = int(print_["id"])
        state.save()
        return print_

    async def _account_defaults(self) -> tuple[int, bool]:
        if self._defaults is None:
            settings = {
                s.get("userSettingTypeId"): str(s.get("value") or "")
                for s in await self.api.user_settings()
            }
            view_status = VIEW_STATUSES.get(
                settings.get(DEFAULT_VIEW_STATUS_SETTING, "").lower(), PRIVATE
            )
            allow_comments = settings.get(LAST_ALLOW_COMMENTS_SETTING, "").lower() == "true"
            self._defaults = (view_status, allow_comments)
        return self._defaults

    # -- end -----------------------------------------------------------------------------------

    async def _finished(self, target: LoggedPrinter, state: PrinterState, event: JobFinished):
        job = state.jobs.get(event.job_id)
        if job is None:
            log.warning(
                "[printer %s] job %s ended, but the bridge never saw it start; not logging it",
                event.printer, event.job_id,
            )  # fmt: skip
            return
        if job.finished:
            return  # a replay
        if job.ended_at is None:
            job.ended_at = event.finished_at.isoformat()
            state.save()

        async def complete() -> None:
            await self._complete(target, state, event, job)

        await self._attempt(event, complete)

    async def _complete(
        self, target: LoggedPrinter, state: PrinterState, event: JobFinished, job: JobRecord
    ) -> None:
        if job.print_id is None:
            print_: dict[str, Any] | None = await self._create(
                target, state, event.printer, event.job_id, job
            )
        else:
            print_ = await self.api.get_print(job.print_id)
        if print_ is None:
            return self._deleted(state, event, job)

        # A finished status already on the print was the user's call (say PartialSuccess), and
        # the measured duration and usage are recorded under it.
        status = print_.get("status")
        body: dict[str, Any] = {
            "status": status if status in FINISHED else STATUS_FOR[event.outcome],
            "endedAt": event.finished_at.isoformat(),
        }
        if event.duration_s is not None:
            body["printTimeInSeconds"] = round(event.duration_s)
        rows = _usage_rows(event, print_.get("filamentUsage") or [])
        if rows:
            body["filamentUsage"] = rows

        completed = await self.api.complete_print(int(print_["id"]), body)
        if completed is None:
            return self._deleted(state, event, job)

        if event.note:
            notes = (completed.get("notes") or "").rstrip()
            if event.note not in notes:
                await self.api.patch_print(
                    int(print_["id"]),
                    {"notes": f"{notes}\n\n{event.note}" if notes else event.note},
                )

        job.finished = True
        state.save()
        self._logged(
            event.printer, f"job {event.job_id} {event.outcome.value}: print {print_['id']}"
        )

    def _deleted(self, state: PrinterState, event: JobFinished, job: JobRecord) -> None:
        job.finished = True
        state.save()
        self._logged(
            event.printer,
            f"job {event.job_id} {event.outcome.value}: print {job.print_id} was deleted in "
            "3D Print Log, so it stays deleted",
        )

    def _logged(self, printer: str, what: str) -> None:
        log.info("[printer %s] %s", printer, what)
        report = self._report(printer)
        if report is not None:
            report.event(what)
            report.warn(API_TOPIC, None)

    def _not_logged(self, event: JobEvent, problem: str) -> None:
        report = self._report(event.printer)
        if report is not None:
            report.warn(API_TOPIC, f"job {event.job_id} isn't logged yet: {problem}")

    # -- errors --------------------------------------------------------------------------------

    async def _attempt(self, event: JobEvent, operation: Callable[[], Awaitable[None]]) -> None:
        """Runs `operation`, retrying while the API is unavailable.

        Gives up after the last retry; the job stays unfinished in the state file, so the next
        replay of its events (the adapter's next reconnect or restart) tries again.
        """
        for delay in (*self.retry_delays, None):
            try:
                await operation()
                return
            except AuthRejected:
                log.error(
                    "[printer %s] the 3D Print Log API rejected the API key, so job %s isn't "
                    "logged. Check api_key in [bridge], then restart the bridge.",
                    event.printer, event.job_id,
                )  # fmt: skip
                self._not_logged(event, "3D Print Log rejected the API key (api_key in [bridge])")
                return
            except ApiRejected as e:
                log.error("[printer %s] job %s isn't logged: %s", event.printer, event.job_id, e)
                self._not_logged(event, str(e))
                return
            except ApiUnavailable as e:
                if delay is None:
                    log.warning(
                        "[printer %s] couldn't log job %s: %s. It is retried when the bridge "
                        "next reconnects to the printer.",
                        event.printer, event.job_id, e,
                    )  # fmt: skip
                    self._not_logged(event, f"{e}. It is retried when the bridge reconnects.")
                    return
                await asyncio.sleep(delay)
            except Exception:
                # A bug, or an answer the bridge doesn't understand, must cost one job at most,
                # never the printer's whole connection.
                log.exception(
                    "[printer %s] couldn't log job %s; it is retried when the bridge next "
                    "reconnects to the printer",
                    event.printer, event.job_id,
                )  # fmt: skip
                self._not_logged(event, "an unexpected error; see the bridge's log")
                return


def _estimate_row(start: Mapping[str, Any]) -> dict[str, Any] | None:
    """The slicer's estimate as a usage row for slot 0, which completing fills in."""
    mm, grams = start.get("estimated_filament_mm"), start.get("estimated_filament_g")
    if not mm and not grams:
        return None
    row: dict[str, Any] = {"slot": 0, "estimatedSource": LENGTH if mm else WEIGHT}
    if mm:
        row["estimatedLengthInM"] = round(mm / 1000, 4)
    if grams:
        row["estimatedAmountMg"] = round(grams * 1000)
    return row


def _usage_rows(event: JobFinished, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per slot that fed filament, linked to the spool the user put on that slot's row."""
    usage = []
    for slot, mm in sorted(event.filament_mm.items()):
        if mm <= 0:
            continue
        row: dict[str, Any] = {"slot": slot, "lengthInM": round(mm / 1000, 4), "source": LENGTH}
        linked = next((r for r in rows if r.get("slot") == slot and r.get("filament")), None)
        if linked is not None:
            row["filamentId"] = linked["filament"]["id"]
        usage.append(row)
    return usage
