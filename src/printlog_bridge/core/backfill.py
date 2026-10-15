"""`printlog-bridge backfill`: the jobs a printer ran before the bridge watched it (#8).

The adapter reads the printer's own history (`Adapter.past_jobs`), and each finished job is
created in 3D Print Log as an imported print: finished, with the job's external ID, its total
usage on one row, and "Imported from <history> history" in its notes.

An imported print is a record of the past, so it never touches inventory. Its usage row names no
spool, and the API takes `imported: true` to mean the printer's loaded spools are left alone too
(api `AddPrintDTO.Imported`). Historical usage would otherwise count against spools added since.

Running it twice, or while the bridge runs, logs no job twice:

- Before creating anything it lists the prints already logged on the printer around the jobs'
  dates (`GET /api/Prints/on-printer`). A job whose external ID (`<instance id>:<job id>`, the
  bridge's own) is there was logged live or by an earlier backfill. A print with no such ID, made
  with the Slic3r uploader, the Cura plugin or by hand, stands for the job of the same file
  (`same_file`) that ran within `adopt_window` of it, one job each. Neither is sent again.
- A job the lookup misses but the API has (dated outside the window) is still safe: an imported
  create of a used pair returns the existing print unchanged (200).
- Jobs the running bridge logs itself are left to it: any job its state file knows, and every job
  from where it resumes reading the printer's history (`PrinterState.resume_after`). Its state is
  only read, never written.

The lookup also tells an API that takes `imported` from an older one, which would ignore the flag
(unloading the printer's spool on every create, and replacing a live print's spool-linked usage
on a replay): there, the path falls through to `GET /api/Prints/{id}` and answers 400, and the
backfill stops before it writes anything.

Imported prints are private unless asked otherwise (`--visibility`), so hundreds of old prints
don't land in the public feed at once.

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
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from printlog_bridge.core.api_client import ApiClient, ApiRejected, ApiUnavailable, AuthRejected
from printlog_bridge.core.events import PastJob
from printlog_bridge.core.lifecycle import (
    BACKOFF_S,
    LENGTH,
    MAX_RETRY_AFTER_S,
    NO_LOOKUP,
    STATUS_FOR,
    VIEW_STATUSES,
    WEIGHT,
    LoggedPrinter,
    account_defaults,
    backoff_delay,
)
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.titles import same_file, title_from_file_name

log = logging.getLogger(__name__)

DEFAULT_DAYS = 90
MAX_DAYS = 3650
REQUEST_INTERVAL_S = 1.0
"""The least time between two requests to the API."""
MAX_ATTEMPTS = 10
"""Tries of one request while the API is out of reach (not counting 429s) before stopping. With
the backoff, about five minutes."""
DEADLINE_S = 4 * 60 * 60
"""The longest a backfill keeps trying, so an API that rate limits it for good can't keep it
running forever. 500 jobs take under 20 minutes."""
MAX_REFUSALS_IN_A_ROW = 3
"""Refusals in a row that stop a printer: a refusal that every job gets (a printer that isn't the
account's) shouldn't be sent 500 times."""
NOT_YOUR_PRINTER = "does not belong"
"""In the API's 400 for a printer that isn't the key owner's, which stops a printer at once."""
LOOKUP_PAGE = 500
LIVE = "left to the bridge, which logs it itself"
MISSING_FILE = "skipped, the file no longer exists on the printer"
VISIBILITIES = ("private", "unlisted", "public", "default")
"""`--visibility`: one for every imported print, or `default`, the account's."""
API_TOO_OLD = (
    "3D Print Log's API doesn't take imported prints yet, so nothing was backfilled (an older "
    "API would change your spools' loaded state and earlier prints). Try again once 3D Print Log "
    "has been updated."
)
LIVE_MARGIN_S = 1.0
"""The margin the bridge's history replay keeps before its resume point (`jobs.py`)."""
INVENTORY_NOTE = (
    "Imported prints don't change your spools' remaining filament or what's loaded on the "
    "printer: their usage happened before, and would count against spools added since."
)
DELETED_NOTE = (
    "A print you deleted in 3D Print Log comes back when the bridge no longer remembers its job "
    "(it keeps the last 20): delete it again, or backfill fewer days."
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


class PrinterRefused(Exception):
    """3D Print Log refuses this printer's jobs, so the rest of them aren't sent. The other
    printers can still be backfilled."""

    def __init__(self, message: str, summary: BackfillSummary) -> None:
        super().__init__(message)
        self.summary = summary


def since(days: int, now: datetime) -> datetime:
    """The start of a backfill of the last `days` days."""
    return now - timedelta(days=days)


class Backfill:
    def __init__(
        self,
        api: ApiClient,
        *,
        interval: float | None = None,
        visibility: str = "private",
        deadline: float = DEADLINE_S,
        backoff: tuple[float, float] = BACKOFF_S,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        say: Callable[[str], None] = print,
    ) -> None:
        self.api = api
        self.interval = REQUEST_INTERVAL_S if interval is None else interval
        if visibility not in VISIBILITIES:
            raise ValueError(f"visibility must be one of {', '.join(VISIBILITIES)}")
        self.visibility = visibility
        self.deadline = deadline
        self._started: float | None = None
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
        skipped: dict[str, str] = {}
        """Why each job not to be sent is skipped, by job id."""
        for job in jobs:
            start = job.start
            if start.job_id in state.jobs or (
                live_from is not None and start.started_at >= live_from
            ):
                skipped[start.job_id] = LIVE
            elif not job.file_exists:
                skipped[start.job_id] = MISSING_FILE
        todo = [job for job in jobs if job.start.job_id not in skipped]
        if todo:
            window = timedelta(seconds=target.adoption.window_s)
            logged = await self._logged(
                target,
                min(j.start.started_at for j in todo) - window,
                max(j.end.finished_at for j in todo) + window,
            )
            matcher = _Matcher(logged, target, state, window)
            view_status, allow_comments = await self._defaults()
        refused_in_a_row = 0
        for n, job in enumerate(jobs, 1):
            title = title_from_file_name(job.start.file_name)
            what = f"[{n}/{len(jobs)}] {job.start.started_at:%Y-%m-%d} {title!r}"
            why = skipped.get(job.start.job_id)
            if why is LIVE:
                summary.live += 1
            elif why is MISSING_FILE:
                summary.missing_file += 1
            if why is not None:
                self.say(f"{what}: {why}")
                continue
            existing = matcher.match(job)
            if existing is not None:
                summary.present += 1
                self.say(f"{what}: already in 3D Print Log (print {existing.get('id')})")
                continue
            body = imported_print(target, state, job, history, view_status, allow_comments)
            try:
                print_, new = await self._call(lambda body=body: self.api.create_print(body))
            except ApiRejected as e:
                summary.refused += 1
                refused_in_a_row += 1
                self.say(f"{what}: refused by 3D Print Log ({e})")
                log.warning(
                    "[printer %s] job %s wasn't backfilled: %s", printer, job.start.job_id, e
                )
                if NOT_YOUR_PRINTER in str(e):
                    raise PrinterRefused(str(e), summary) from e
                if refused_in_a_row >= MAX_REFUSALS_IN_A_ROW:
                    raise PrinterRefused(
                        f"3D Print Log refused {refused_in_a_row} jobs in a row ({e}), so the "
                        "rest weren't sent",
                        summary,
                    ) from e
                continue
            refused_in_a_row = 0
            if new:
                summary.created += 1
                self.say(f"{what}: created print {print_.get('id')}")
            else:
                summary.present += 1
                self.say(f"{what}: already in 3D Print Log (print {print_.get('id')})")
        return summary

    async def _logged(
        self, target: LoggedPrinter, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """The prints already logged on the printer between `start` and `end`. Stops the
        backfill on an API too old for imported prints, before anything is written."""
        prints: list[dict[str, Any]] = []
        while True:
            after = int(prints[-1]["id"]) if prints else None
            try:
                page = await self._call(
                    lambda after=after: self.api.prints_on_printer(
                        target.printer_id, start, end, after_id=after, limit=LOOKUP_PAGE
                    )
                )
            except (ApiRejected, ApiUnavailable) as e:
                if e.status in NO_LOOKUP:
                    raise BackfillStopped(API_TOO_OLD) from e
                raise BackfillStopped(f"couldn't look up the printer's prints: {e}") from e
            prints.extend(page)
            if len(page) < LOOKUP_PAGE:
                return prints

    async def _defaults(self) -> tuple[int, bool]:
        try:
            view_status, allow_comments = await self._call(lambda: account_defaults(self.api))
        except ApiRejected as e:
            raise BackfillStopped(f"couldn't read your account's settings: {e}") from e
        if self.visibility != "default":
            view_status = VIEW_STATUSES[self.visibility]
        return view_status, allow_comments

    async def _call(self, request: Callable[[], Awaitable[T]]) -> T:
        """`request()`, sent no sooner than `interval` after the last, and sent again while the
        API is rate limiting (429) or out of reach."""
        attempts = 0
        if self._started is None:
            self._started = self.clock()
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
                if self.clock() + wait - self._started > self.deadline:
                    raise BackfillStopped(
                        f"{e}, still, after {self.deadline / 3600:g} hours. Run it again later: "
                        "jobs already backfilled aren't logged twice."
                    ) from e
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
    lines.append(DELETED_NOTE)
    return "\n".join(lines)


class _Matcher:
    """Which of the prints already logged on the printer stands for a job, each for one job."""

    def __init__(
        self,
        prints: Sequence[dict[str, Any]],
        target: LoggedPrinter,
        state: PrinterState,
        window: timedelta,
    ) -> None:
        self.window = window
        self.own: dict[str, dict[str, Any]] = {}
        """The bridge's own prints on the printer, by external ID: each stands only for its job."""
        self.others: list[tuple[dict[str, Any], datetime]] = []
        for print_ in prints:
            external = print_.get("externalId") or ""
            if print_.get("externalSource") == target.source and external.startswith(
                f"{state.instance_id}:"
            ):
                self.own[external] = print_
                continue
            when = _date(print_.get("startDate") or print_.get("createdDate"))
            if when is not None:
                self.others.append((print_, when))
        self.prefix = f"{state.instance_id}:"

    def match(self, job: PastJob) -> dict[str, Any] | None:
        own = self.own.get(self.prefix + job.start.job_id)
        if own is not None:
            return own
        start, end = job.start.started_at, job.end.finished_at
        candidates = [
            (abs((when - start).total_seconds()), n)
            for n, (print_, when) in enumerate(self.others)
            if start - self.window <= when <= end + self.window
            and same_file(print_.get("fileName") or print_.get("title") or "", job.start.file_name)
        ]
        if not candidates:
            return None
        _, n = min(candidates)
        print_, _ = self.others.pop(n)
        return print_


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
