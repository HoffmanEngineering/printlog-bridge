"""Turns job events into prints in 3D Print Log: created when a job starts, completed when it ends.

Each event goes into the outbox (`core/outbox.py`), committed to disk, before anything is sent,
and `run()` sends what the outbox holds: in order per print, retrying with a backoff that
doubles from 2 s to a minute, or as long as the API's `Retry-After` asks. An event leaves the
outbox only once the API has it, so neither an outage nor a power cut loses one.

Every call is safe to repeat. The print is created with an external ID (`<instance>:<job_id>`)
the API deduplicates on, the state file recognises a replayed event before anything is queued,
and completing only ever sets what the printer measured. What the user edits in the app while
the job runs (title, notes, a PartialSuccess status, the spool on a usage row) is kept.
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
from printlog_bridge.core.outbox import Entry, Outbox, utc_now
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

BACKOFF_S = (2.0, 60.0)
"""The first retry's delay and the longest. The cap keeps a print within a minute or so of
reaching 3D Print Log once the internet is back."""
AUTH_PAUSE_S = 15 * 60
"""How long sending stops after the API rejects the key. Fixing it means a restart anyway."""
MAX_RETRY_AFTER_S = 60 * 60
"""A `Retry-After` longer than this is taken as this, in case the API ever asks for days."""
API_TOPIC = "api"
START, FINISH = "start", "finish"


@dataclass(frozen=True)
class LoggedPrinter:
    printer_id: int
    """The printer in 3D Print Log."""
    source: str
    """The API's `externalSource`: the adapter's name, such as `moonraker`."""


def backoff_delay(attempts: int, base: float, cap: float) -> float:
    """The wait after the `attempts`-th failed try: `base`, doubling each time, up to `cap`."""
    return min(cap, base * 2 ** max(0, attempts - 1))


class JobLogger:
    """The bridge's EventSink: queues each job's events, and sends them to the API."""

    def __init__(
        self,
        api: ApiClient,
        printers: Mapping[str, LoggedPrinter],
        state_dir: Path,
        backoff: tuple[float, float] = BACKOFF_S,
        board: StatusBoard | None = None,
        before_create: Callable[[str], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """`before_create(printer)` runs before each print is created. The bridge registers the
        printer's connection there, so the print links to it."""
        self.api = api
        self.printers = dict(printers)
        self.state_dir = state_dir
        self.backoff = backoff
        self.board = board
        self.before_create = before_create
        self.outbox = Outbox.open(state_dir, clock=clock)
        self._states: dict[str, PrinterState] = {}
        self._defaults: tuple[int, bool] | None = None
        self._due: dict[int, float] = {}
        """When each entry that failed may be tried again, on the event loop's clock."""
        self._paused_until = 0.0
        """No entry is sent before this (the API's `Retry-After`, or a rejected key)."""
        self._wake = asyncio.Event()

    def close(self) -> None:
        self.outbox.close()

    async def resume_after(self, printer: str) -> datetime | None:
        return self.state(printer).resume_after()

    def state(self, printer: str) -> PrinterState:
        """The printer's state file, loaded once and kept."""
        if printer not in self._states:
            self._states[printer] = PrinterState.load(self.state_dir, printer)
        return self._states[printer]

    def _report(self, printer: str) -> BoardReport | None:
        return self.board.printer(printer) if self.board is not None else None

    # -- queueing ------------------------------------------------------------------------------

    async def emit(self, event: JobEvent) -> None:
        """Queues the event in the outbox, on disk, unless it's a replay. Never waits on the API."""
        if event.printer not in self.printers:
            log.error("[printer %s] isn't linked to 3D Print Log; skipping its jobs", event.printer)
            return
        state = self.state(event.printer)
        if isinstance(event, JobStarted):
            job = state.jobs.get(event.job_id)
            if job is None:
                start = asdict(event)
                start["started_at"] = event.started_at.isoformat()
                job = state.jobs[event.job_id] = JobRecord(
                    started_at=start["started_at"], start=start
                )
                state.save()
            if job.finished or job.print_id is not None:
                return  # a replay
            self._queue(event.printer, event.job_id, START, job.start)
        else:
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
            self._queue(event.printer, event.job_id, FINISH, _finish_payload(event))

    def _queue(self, printer: str, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        if self.outbox.add(printer, job_id, kind, payload):
            self._wake.set()

    # -- sending -------------------------------------------------------------------------------

    async def run(self) -> None:
        """Sends what the outbox holds, as it comes and as retries fall due, until cancelled."""
        loop = asyncio.get_running_loop()
        while True:
            self._wake.clear()
            wait = await self.send_due()
            # A timer rather than asyncio.wait_for, which before Python 3.12 can swallow a
            # cancel that arrives as the wait ends (bpo-42130), and the bridge must stop.
            timer = loop.call_later(wait, self._wake.set) if wait is not None else None
            try:
                await self._wake.wait()
            finally:
                if timer is not None:
                    timer.cancel()

    async def send_due(self) -> float | None:
        """Sends every entry that may be sent now, in order.

        Returns the seconds until the next entry falls due, or None when the outbox is empty
        (or holds only entries for printers no longer in the config).
        """
        loop = asyncio.get_running_loop()
        self._drop_expired()
        while True:
            now = loop.time()
            heads = [e for e in self.outbox.heads() if e.printer in self.printers]
            if not heads:
                return None
            if now < self._paused_until:
                return self._paused_until - now
            due = [e for e in heads if self._due.get(e.seq, 0.0) <= now]
            if not due:
                return min(self._due[e.seq] for e in heads) - now
            await self._send(due[0])

    async def _send(self, entry: Entry) -> None:
        loop = asyncio.get_running_loop()
        try:
            if entry.kind == START:
                await self._send_start(entry)
            else:
                await self._send_finish(entry)
        except AuthRejected:
            problem = "3D Print Log rejected the API key (api_key in [bridge])"
            log.error(
                "[printer %s] the 3D Print Log API rejected the API key, so job %s isn't "
                "logged yet. It stays in the outbox: check api_key in [bridge], then restart "
                "the bridge.",
                entry.printer, entry.job_id,
            )  # fmt: skip
            self.outbox.failed(entry.seq, problem)
            self._paused_until = loop.time() + AUTH_PAUSE_S
            self._not_logged(entry, problem)
        except ApiRejected as e:
            # Sending it again won't change the answer. The job stays unfinished in the state
            # file, so a replay after the user fixes the cause (a restart) queues it again.
            log.error("[printer %s] job %s isn't logged: %s", entry.printer, entry.job_id, e)
            self._done(entry)
            self._not_logged(entry, str(e))
        except ApiUnavailable as e:
            self._retry_later(entry, str(e), e.retry_after)
        except Exception:
            # A bug, or an answer the bridge doesn't understand, must never stop the rest. The
            # traceback is logged once, not on every retry.
            if entry.attempts == 0:
                log.exception(
                    "[printer %s] couldn't log job %s; it stays in the outbox and is tried again",
                    entry.printer, entry.job_id,
                )  # fmt: skip
            self._retry_later(entry, "an unexpected error; see the bridge's log", None)
        else:
            self._done(entry)

    def _done(self, entry: Entry) -> None:
        self.outbox.remove(entry.seq)
        self._due.pop(entry.seq, None)

    def _retry_later(self, entry: Entry, problem: str, retry_after: float | None) -> None:
        loop = asyncio.get_running_loop()
        attempts = entry.attempts + 1
        self.outbox.failed(entry.seq, problem)
        # Never sooner than the backoff: a `Retry-After` of 0, or a date already past, would
        # otherwise resend at once, as fast as the API answers.
        delay = backoff_delay(attempts, *self.backoff)
        if retry_after is not None:
            pause = min(retry_after, MAX_RETRY_AFTER_S)
            self._paused_until = max(self._paused_until, loop.time() + pause)
            delay = max(delay, pause)
        self._due[entry.seq] = loop.time() + delay
        # The first failure is worth a warning; the rest of a long outage only fills the log.
        level = logging.WARNING if attempts == 1 else logging.DEBUG
        log.log(
            level,
            "[printer %s] couldn't log job %s yet: %s. It stays in the outbox and is tried "
            "again in %s.",
            entry.printer, entry.job_id, problem, _seconds(delay),
        )  # fmt: skip
        self._not_logged(entry, f"{problem}. The bridge tries again in {_seconds(delay)}.")

    def _drop_expired(self) -> None:
        for entry in self.outbox.drop_expired():
            log.warning(
                "[printer %s] gave up on job %s: its %s couldn't reach 3D Print Log in 30 days "
                "(queued %s; last error: %s)",
                entry.printer, entry.job_id, entry.kind, entry.queued_at.isoformat(),
                entry.last_error or "none",
            )  # fmt: skip
            self._due.pop(entry.seq, None)
            state = self.state(entry.printer)
            job = state.jobs.get(entry.job_id)
            if job is not None and not job.finished:
                job.finished = True  # done with it, so the printer's history isn't replayed
                state.save()  # from it forever

    async def _send_start(self, entry: Entry) -> None:
        state = self.state(entry.printer)
        job = state.jobs.get(entry.job_id)
        if job is None:  # the state file was lost or set aside since the event was queued
            job = state.jobs[entry.job_id] = JobRecord(
                started_at=entry.payload["started_at"], start=entry.payload
            )
            state.save()
        if job.finished or job.print_id is not None:
            return  # already sent, and the power went before the outbox heard
        print_ = await self._create(
            self.printers[entry.printer], state, entry.printer, entry.job_id, job
        )
        self._logged(
            entry.printer,
            f"job {entry.job_id} started: print {print_['id']}, {print_.get('title')!r}",
        )

    async def _send_finish(self, entry: Entry) -> None:
        state = self.state(entry.printer)
        job = state.jobs.get(entry.job_id)
        if job is None:
            log.warning(
                "[printer %s] job %s ended, but its start is no longer known; not logging it",
                entry.printer, entry.job_id,
            )  # fmt: skip
            return
        if job.finished:
            return
        event = _finish_event(entry)
        await self._complete(self.printers[entry.printer], state, event, job)

    # -- start ---------------------------------------------------------------------------------

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

    def _not_logged(self, entry: Entry, problem: str) -> None:
        report = self._report(entry.printer)
        if report is not None:
            report.warn(API_TOPIC, f"job {entry.job_id} isn't logged yet: {problem}")


def _finish_payload(event: JobFinished) -> dict[str, Any]:
    return {
        "outcome": event.outcome.value,
        "finished_at": event.finished_at.isoformat(),
        "duration_s": event.duration_s,
        "filament_mm": {str(slot): mm for slot, mm in event.filament_mm.items()},
        "note": event.note,
    }


def _finish_event(entry: Entry) -> JobFinished:
    payload = entry.payload
    return JobFinished(
        printer=entry.printer,
        job_id=entry.job_id,
        outcome=JobOutcome(payload["outcome"]),
        finished_at=datetime.fromisoformat(payload["finished_at"]),
        duration_s=payload.get("duration_s"),
        filament_mm={int(slot): mm for slot, mm in (payload.get("filament_mm") or {}).items()},
        note=payload.get("note"),
    )


def _seconds(delay: float) -> str:
    return f"{delay:.0f}s" if delay >= 1 else f"{delay:.2f}s"


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
