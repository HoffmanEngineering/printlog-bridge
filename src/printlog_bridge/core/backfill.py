"""`printlog-bridge backfill`: the jobs a printer ran before the bridge watched it (#8).

The adapter reads the printer's own history (`Adapter.past_jobs`), and each finished job is
created in 3D Print Log as an imported print: finished, with the job's external ID, its total
usage on one row, and "Imported from <history> history" in its notes.

An imported print is a record of the past, so it never touches inventory. Its usage row names no
spool, and the API takes `imported: true` to mean the printer's loaded spools are left alone too
(api `AddPrintDTO.Imported`). Historical usage would otherwise count against spools added since.

Running it twice, or while the bridge runs, logs no job twice:

- The external ID is the bridge's own (`<instance id>:<job id>`), so a job already logged live,
  or by an earlier backfill, is the API's existing print, which an imported create returns
  unchanged (200).
- Jobs the running bridge logs itself are left to it: any job its state file knows, and every job
  from where it resumes reading the printer's history (`PrinterState.resume_after`). Its state is
  only read, never written.

Requests go at most one a second (`REQUEST_INTERVAL_S`). The API's connector write budget refills
at that rate (api#151), and the bridge shares it. A 429 waits as long as `Retry-After` asks, and
an outage with a backoff, so a long backfill finishes on its own.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar

from printlog_bridge.core.api_client import ApiClient, ApiRejected, ApiUnavailable, AuthRejected
from printlog_bridge.core.events import PastJob
from printlog_bridge.core.lifecycle import (
    BACKOFF_S,
    LENGTH,
    MAX_RETRY_AFTER_S,
    STATUS_FOR,
    WEIGHT,
    LoggedPrinter,
    account_defaults,
    backoff_delay,
)
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.titles import title_from_file_name

log = logging.getLogger(__name__)

DEFAULT_DAYS = 90
REQUEST_INTERVAL_S = 1.0
"""The least time between two requests to the API."""
MAX_ATTEMPTS = 10
"""Tries of one request while the API is out of reach (not counting 429s) before stopping. With
the backoff, about five minutes."""
LIVE_MARGIN_S = 1.0
"""The margin the bridge's history replay keeps before its resume point (`jobs.py`)."""
INVENTORY_NOTE = (
    "Imported prints don't change your spools' remaining filament or what's loaded on the "
    "printer: their usage happened before, and would count against spools added since."
)

T = TypeVar("T")


@dataclass
class BackfillSummary:
    jobs: int = 0
    """Finished jobs in the history read."""
    created: int = 0
    present: int = 0
    """Already in 3D Print Log: logged live, or by an earlier backfill."""
    missing_file: int = 0
    """Skipped: the job's file is no longer on the printer."""
    live: int = 0
    """Left to the bridge, which logs them itself."""
    refused: int = 0
    """The API refused them (400); sending them again won't change that."""


class BackfillStopped(Exception):
    """The backfill can't go on. The message says why, and what to do."""


def since(days: int, now: datetime) -> datetime:
    """The start of a backfill of the last `days` days."""
    return now - timedelta(days=days)


class Backfill:
    def __init__(
        self,
        api: ApiClient,
        *,
        interval: float | None = None,
        backoff: tuple[float, float] = BACKOFF_S,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        say: Callable[[str], None] = print,
    ) -> None:
        self.api = api
        self.interval = REQUEST_INTERVAL_S if interval is None else interval
        self.backoff = backoff
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.clock = clock
        self.say = say
        """Where each job's line goes: the terminal."""
        self._last: float | None = None

    async def run(
        self,
        printer: str,
        target: LoggedPrinter,
        state: PrinterState,
        jobs: Sequence[PastJob],
        *,
        history: str,
    ) -> BackfillSummary:
        """Creates each of `jobs` (oldest first) that isn't in 3D Print Log yet.

        `state` is the printer's state file as the bridge left it, read only. `history` names
        where the jobs came from, for the note on each print ("Moonraker"). Raises
        BackfillStopped when it can't go on; running it again picks up where it stopped.
        """
        summary = BackfillSummary(jobs=len(jobs))
        resume = state.resume_after()
        live_from = resume - timedelta(seconds=LIVE_MARGIN_S) if resume is not None else None
        defaults: tuple[int, bool] | None = None
        for n, job in enumerate(jobs, 1):
            start = job.start
            title = title_from_file_name(start.file_name)
            what = f"[{n}/{len(jobs)}] {start.started_at:%Y-%m-%d} {title!r}"
            live = live_from is not None and start.started_at >= live_from
            if live or start.job_id in state.jobs:
                summary.live += 1
                self.say(f"{what}: left to the bridge, which logs it itself")
                continue
            if not job.file_exists:
                summary.missing_file += 1
                self.say(f"{what}: skipped, the file no longer exists on the printer")
                continue
            if defaults is None:
                try:
                    defaults = await self._call(lambda: account_defaults(self.api))
                except ApiRejected as e:
                    raise BackfillStopped(f"couldn't read your account's settings: {e}") from e
            body = imported_print(target, state, job, history, *defaults)
            try:
                print_, new = await self._call(lambda body=body: self.api.create_print(body))
            except ApiRejected as e:
                summary.refused += 1
                self.say(f"{what}: refused by 3D Print Log ({e})")
                log.warning("[printer %s] job %s wasn't backfilled: %s", printer, start.job_id, e)
                continue
            if new:
                summary.created += 1
                self.say(f"{what}: created print {print_.get('id')}")
            else:
                summary.present += 1
                self.say(f"{what}: already in 3D Print Log (print {print_.get('id')})")
        return summary

    async def _call(self, request: Callable[[], Awaitable[T]]) -> T:
        """`request()`, sent no sooner than `interval` after the last, and sent again while the
        API is rate limiting (429) or out of reach."""
        attempts = 0
        while True:
            await self._pace()
            try:
                return await request()
            except AuthRejected as e:
                raise BackfillStopped(
                    "3D Print Log rejected the API key. Check it with `printlog-bridge doctor` "
                    "(`printlog-bridge store-key` replaces it)."
                ) from e
            except ApiUnavailable as e:
                if e.status == 429:
                    wait = e.retry_after
                    if wait is None:
                        wait = backoff_delay(1, *self.backoff)
                else:
                    attempts += 1
                    if attempts >= self.max_attempts:
                        raise BackfillStopped(
                            f"{e}. Run it again once 3D Print Log can be reached: jobs already "
                            "backfilled aren't logged twice."
                        ) from e
                    wait = backoff_delay(attempts, *self.backoff)
                    if e.retry_after is not None:
                        wait = max(wait, e.retry_after)
                wait = min(wait, MAX_RETRY_AFTER_S)
                log.info("3D Print Log answered %s; trying again in %gs", e, wait)
                await self.sleep(wait)

    async def _pace(self) -> None:
        now = self.clock()
        if self._last is not None and now - self._last < self.interval:
            await self.sleep(self.interval - (now - self._last))
        self._last = self.clock()


def imported_print(
    target: LoggedPrinter,
    state: PrinterState,
    job: PastJob,
    history: str,
    view_status: int,
    allow_comments: bool,
) -> dict[str, Any]:
    """The create for a job from the printer's history: finished, imported, and with its total
    usage on one row that names no spool (and no slot: it isn't split by tool)."""
    start, end = job.start, job.end
    notes = [f"Imported from {history} history"]
    if end.note:
        notes.append(end.note)
    body: dict[str, Any] = {
        "printerId": target.printer_id,
        "title": title_from_file_name(start.file_name),
        "fileName": start.file_name.replace("\\", "/").rsplit("/", 1)[-1],
        "startDate": start.started_at.isoformat(),
        "status": STATUS_FOR[end.outcome],
        "viewStatus": view_status,
        "allowComments": allow_comments,
        "notes": "\n\n".join(notes),
        "externalSource": target.source,
        "externalId": f"{state.instance_id}:{start.job_id}",
        "connectionInstanceId": state.instance_id,
        "imported": True,
    }
    if end.duration_s:
        body["printTimeInSeconds"] = round(end.duration_s)
    if start.estimated_duration_s:
        body["estimatedPrintTimeInSeconds"] = round(start.estimated_duration_s)
    if start.estimated_filament_g:
        body["estimatedFilamentUsageMg"] = round(start.estimated_filament_g * 1000)
    used_mm = sum(mm for mm in end.filament_mm.values() if mm > 0)
    if used_mm > 0:
        row: dict[str, Any] = {"lengthInM": round(used_mm / 1000, 4), "source": LENGTH}
        if start.estimated_filament_mm:
            row["estimatedLengthInM"] = round(start.estimated_filament_mm / 1000, 4)
        if start.estimated_filament_g:
            row["estimatedAmountMg"] = round(start.estimated_filament_g * 1000)
        if start.estimated_filament_mm or start.estimated_filament_g:
            row["estimatedSource"] = LENGTH if start.estimated_filament_mm else WEIGHT
        body["filamentUsage"] = [row]
    return body


def format_summary(printer: str, summary: BackfillSummary, days: int) -> str:
    lines = [
        f"[printer {printer}] {summary.jobs} finished job{'' if summary.jobs == 1 else 's'} "
        f"in the last {days} days:",
        f"  {summary.created} created",
        f"  {summary.present} already in 3D Print Log",
        f"  {summary.missing_file} skipped: the file no longer exists on the printer",
    ]
    if summary.live:
        lines.append(f"  {summary.live} left to the bridge, which logs them itself")
    if summary.refused:
        lines.append(f"  {summary.refused} refused by 3D Print Log (see above)")
    lines.append(INVENTORY_NOTE)
    return "\n".join(lines)
