"""Turns job events into prints in 3D Print Log: created when a job starts, completed when it ends.

Each event goes into the outbox (`core/outbox.py`), committed to disk, before anything is sent,
and `run()` sends what the outbox holds: in order per print, retrying with a backoff that
doubles from 2 s to a minute, or as long as the API's `Retry-After` asks. An event leaves the
outbox only once the API has it, so neither an outage nor a power cut loses one.

Every call is safe to repeat. The print is created with an external ID (`<instance>:<job_id>`)
the API deduplicates on, the state file recognises a replayed event before anything is queued,
and completing only ever sets what the printer measured. What the user edits in the app while
the job runs (title, notes, a PartialSuccess status, the spool on a usage row) is kept.

Pictures (the slicer's thumbnail at the start, a webcam snapshot at the end) go through the
outbox too, in the print's order, so one is only added once its print exists. The image is
kept in `images/` in the state directory until it is sent. A picture never holds up its print:
one that can't be fetched in time is skipped, and one the API refuses, or keeps failing to take,
is dropped so the print's completion behind it goes through.

A print the user made themselves (the Slic3r uploader, the Cura plugin, or by hand) is adopted
rather than duplicated: before creating one, the bridge looks for an unclaimed print in Printing
on the same printer, made within `adopt_window`, for the same file, claims the newest, and logs
the job to it. In finish-only mode (`create_prints = false`) a job with nothing to adopt isn't
logged. See `_adopt`.

A spool assigned to a slot while the job runs (`Spools.on_swap`, #44) is kept in the job's state
with the point it was assigned at, and the slot's usage is split between the spools there when
the job ends. See `swaps.py`.

The slicer settings of the job's file (#36) go through the outbox too, once per job, after its
start: read from the printer when the adapter reports them, scrubbed (`slicer_settings.py`), and
sent once the print exists. Like a picture, they never hold up their print: an answer that says
they can't or needn't be recorded (400, 404, 409, 413) drops them, and so do too many failures.
A print adopted with settings of its own (the uploader's, the Cura plugin's) keeps them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from printlog_bridge.core.api_client import (
    ApiClient,
    ApiRejected,
    ApiUnavailable,
    AuthRejected,
    ClaimConflict,
)
from printlog_bridge.core.config import Adoption
from printlog_bridge.core.events import (
    FilamentChange,
    ImageKind,
    JobEvent,
    JobFinished,
    JobImage,
    JobOutcome,
    JobSlicerSettings,
    JobStarted,
)
from printlog_bridge.core.images import EXTENSIONS, MAX_IMAGE_BYTES, ImageUnavailable, image_type
from printlog_bridge.core.outbox import Entry, Outbox, utc_now
from printlog_bridge.core.slicer_settings import OUTBOX_KIND, request_body
from printlog_bridge.core.spools import Spools
from printlog_bridge.core.state import JobRecord, PrinterState, safe_file_name
from printlog_bridge.core.status import BoardReport, StatusBoard
from printlog_bridge.core.swaps import (
    SpoolSwap,
    before_swaps,
    change_hints,
    change_record,
    first_spools,
    is_split,
    on_slot,
    segments,
    swap_notes,
    swap_record,
)
from printlog_bridge.core.titles import same_file, title_from_file_name

log = logging.getLogger(__name__)
T = TypeVar("T")

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
OUTBOX_TOPIC = "outbox"
STORAGE_ERRORS = (OSError, sqlite3.Error)
"""A full or read-only disk, or an outbox another process holds locked past SQLite's timeout."""
START, FINISH = "start", "finish"
IMAGE_KINDS = frozenset(kind.value for kind in ImageKind)
SLICER_SETTINGS = OUTBOX_KIND
OPTIONAL_KINDS = IMAGE_KINDS | {SLICER_SETTINGS}
"""Entries that must never hold up their print: given up on after `IMAGE_MAX_ATTEMPTS`."""
NO_SLICER_SETTINGS = (405, 501)
"""What an API without the slicer settings endpoint (api#164) may answer it with. Its plain
404 can't be told from a print that's gone, so that only drops the one print's."""
NO_SLICER_SETTINGS_FOR = timedelta(hours=24)
"""How long none are read or sent after such an answer: it may as well be a maintenance page
as an API older than the endpoint, and either can change."""
IMAGES_DIR = "images"
IMAGE_TIMEOUT_S = 15.0
"""How long the bridge waits on an adapter for a picture. Adapters give up after 10 s."""
IMAGE_MAX_ATTEMPTS = 10
"""An upload that keeps failing while the API is reachable is given up after this many tries,
so the print's completion behind it isn't held up: pictures must never stop a print from being
logged. A try counts when the API answers with an error, or when the upload is cut off or times
out but a small request to the API right after goes through (a proxy that resets large bodies,
an uplink too slow for the file). A rate limited upload, or one made while the API can't be
reached at all, waits like any event: then nothing behind it could be sent either. Tries are
counted for this run of the bridge; a restart starts the count over."""
TOO_MANY_REQUESTS = 429
NO_LOOKUP = (400, 404, 405, 501)
"""What an API without the lookup for prints to adopt (api#165) answers it with: 400 when
`/api/Prints/claimable` falls through to `/api/Prints/{id}`."""


@dataclass(frozen=True)
class LoggedPrinter:
    printer_id: int
    """The printer in 3D Print Log."""
    source: str
    """The API's `externalSource`: the adapter's name, such as `moonraker`."""
    adoption: Adoption = Adoption()
    """Whether to adopt the user's own print for a job, and whether to create one otherwise."""
    slicer_settings: bool = True
    """Whether to read and send each job's slicer settings (`slicer_settings`)."""


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
        image_timeout: float = IMAGE_TIMEOUT_S,
        spools: Spools | None = None,
    ) -> None:
        """`before_create(printer)` runs before each print is created. The bridge registers the
        printer's connection there, so the print links to it; ApiUnavailable from it holds the
        print back for the next try, anything else is logged and the print goes ahead. With
        `spools`, each print's usage rows are linked to the spools loaded in their slots."""
        self.api = api
        self.spools = spools
        self.printers = dict(printers)
        self.state_dir = state_dir
        self.backoff = backoff
        self.board = board
        self.before_create = before_create
        self.image_timeout = image_timeout
        self.images_dir = state_dir / IMAGES_DIR
        self.outbox = Outbox.open(state_dir, clock=clock)
        self._outcomes: dict[str, tuple[str, JobOutcome]] = {}
        """Each printer's last job seen ending in this run, and how it ended, for whether its
        snapshot is the default. One per printer, and gone once the snapshot comes."""
        self._image_tries: dict[int, int] = {}
        """The counted failed tries of each image entry (see IMAGE_MAX_ATTEMPTS)."""
        self._remove_stray_images()
        self._states: dict[str, PrinterState] = {}
        self._defaults: tuple[int, bool] | None = None
        self._due: dict[int, float] = {}
        """When each entry that failed may be tried again, on the event loop's clock."""
        self._paused_until = 0.0
        """No entry is sent before this (the API's `Retry-After`, or a rejected key)."""
        self._wake = asyncio.Event()
        self._unqueued: list[JobEvent] = []
        """Job events the outbox couldn't take (a full disk), in order, until it can."""
        self._storage_failures = 0
        """Writes to the state directory that failed in a row."""
        self._no_lookup_logged = False
        self.clock = clock
        self._no_slicer_settings_until: datetime | None = None
        """The API answered that it has no slicer settings endpoint: none are read or sent until
        then (`NO_SLICER_SETTINGS_FOR`)."""

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
        """Queues the event in the outbox, on disk, unless it's a replay. Never waits on the API.

        When the state directory can't be written (a full disk), the event is kept in memory and
        queued, in order, once it can be, and `status` shows a warning meanwhile. A picture, or
        the slicer settings, is skipped instead: it is fetched now or never.
        """
        optional = isinstance(event, (JobImage, JobSlicerSettings))
        if self._unqueued and not optional:
            self._unqueued.append(event)  # nothing jumps ahead of what waits to be queued
            return
        if optional and any(
            (e.printer, e.job_id) == (event.printer, event.job_id) for e in self._unqueued
        ):
            # Queued now, it would go ahead of its job's start or end: a thumbnail before the
            # print exists is dropped, and a snapshot before the end misses its outcome.
            log.warning(
                "[printer %s] job %s's %s is skipped: the outbox can't be written",
                event.printer, event.job_id, _what(event),
            )  # fmt: skip
            return
        try:
            await self._emit(event)
        except STORAGE_ERRORS as e:
            self._storage_failed(e)
            if optional:
                log.warning(
                    "[printer %s] job %s's %s is skipped: the outbox can't be written",
                    event.printer, event.job_id, _what(event),
                )  # fmt: skip
            else:
                self._unqueued.append(event)
                self._wake.set()

    async def _emit(self, event: JobEvent) -> None:
        if event.printer not in self.printers:
            log.error("[printer %s] isn't linked to 3D Print Log; skipping its jobs", event.printer)
            return
        state = self.state(event.printer)
        if isinstance(event, JobImage):
            await self._emit_image(state, event)
        elif isinstance(event, JobSlicerSettings):
            await self._emit_slicer_settings(state, event)
        elif isinstance(event, FilamentChange):
            self._filament_changed(state, event)
        elif isinstance(event, JobStarted):
            job = state.jobs.get(event.job_id)
            if job is None:
                start = asdict(event)
                start["started_at"] = event.started_at.isoformat()
                start["estimated_filament_g_by_slot"] = {
                    str(slot): grams for slot, grams in event.estimated_filament_g_by_slot.items()
                }  # as JSON keeps it
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
            self._outcomes[event.printer] = (event.job_id, event.outcome)
            if job.finished:
                return  # a replay
            if job.ended_at is None:
                job.ended_at = event.finished_at.isoformat()
                state.save()
            self._queue(event.printer, event.job_id, FINISH, _finish_payload(event))

    def _filament_changed(self, state: PrinterState, event: FilamentChange) -> None:
        job = state.jobs.get(event.at.job_id)
        if job is None or job.finished:
            return
        record = change_record(event)
        if record in job.filament_changes:
            return  # a replay
        job.filament_changes.append(record)
        state.save()
        self._event(
            event.printer,
            f"job {event.at.job_id}: T{event.slot} was paused for a filament change "
            f"({event.reason})",
        )

    def spool_swapped(self, swap: SpoolSwap) -> bool:
        """Keeps a spool assigned to a slot while a job ran, to split the job's usage on that
        slot where it was assigned. Whether it will be: not for a job already logged as ended,
        or one the bridge isn't logging."""
        if swap.printer not in self.printers:
            return False
        state = self.state(swap.printer)
        job = state.jobs.get(swap.during.job_id)
        if job is None or job.finished or job.completion is not None:
            return False  # its end is already worked out
        record = swap_record(swap)
        job.swaps.append(record)
        try:
            state.save()
        except STORAGE_ERRORS as e:
            # Kept in memory, and written with the job's next save.
            self._storage_failed(e)
        if not is_split(record):
            return False  # before the slot fed anything: the spool it starts with
        self._event(
            swap.printer,
            f"job {swap.during.job_id}: T{swap.slot} usage split, "
            f"{swap.old_name or swap.old or 'no spool'} before, "
            f"{swap.new_name or swap.new or 'no spool'} after",
        )
        return True

    def _event(self, printer: str, what: str) -> None:
        log.info("[printer %s] %s", printer, what)
        report = self._report(printer)
        if report is not None:
            report.event(what)

    def _queue(self, printer: str, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        if self.outbox.add(printer, job_id, kind, payload):
            self._wake.set()

    async def _emit_image(self, state: PrinterState, event: JobImage) -> None:
        kind = event.kind.value
        outcome = None
        if event.kind is ImageKind.SNAPSHOT:
            last = self._outcomes.pop(event.printer, None)
            if last is not None and last[0] == event.job_id:
                outcome = last[1]
        job = state.jobs.get(event.job_id)
        if job is None or kind in job.images:
            return  # a job the bridge isn't logging, or a picture it already has
        if event.kind is ImageKind.THUMBNAIL and job.finished:
            return  # a job logged before (a history replay); its start is long gone
        data = await self._fetch(event)
        content_type = image_type(data) if data is not None else None
        if data is not None and content_type is None:
            log.info(
                "[printer %s] job %s's %s isn't an image 3D Print Log takes (jpeg, png, gif, "
                "webp, bmp); skipping it",
                event.printer, event.job_id, kind,
            )  # fmt: skip
        elif data is not None and len(data) > MAX_IMAGE_BYTES:
            log.info(
                "[printer %s] job %s's %s is over 10 MB; skipping it",
                event.printer, event.job_id, kind,
            )  # fmt: skip
            content_type = None
        if data is None or content_type is None:
            job.images[kind] = False
            state.save()
            return

        # The thumbnail is the print's picture, unless the print failed: then what the camera
        # saw tells more. Without a thumbnail, the snapshot is all there is.
        default = event.kind is ImageKind.THUMBNAIL or (
            outcome is JobOutcome.FAILED or not job.images.get(ImageKind.THUMBNAIL.value)
        )
        name = safe_file_name(f"{event.printer}-{event.job_id}-{kind}") + EXTENSIONS[content_type]
        self.images_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.images_dir / (name + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, self.images_dir / name)
        self._queue(
            event.printer,
            event.job_id,
            kind,
            {"file": name, "content_type": content_type, "default": default},
        )
        # Marked only once queued, so a crash before then fetches it again on the replay. The
        # outbox holds one entry per job and kind, and nothing is sent before this save.
        job.images[kind] = True
        state.save()

    async def _emit_slicer_settings(self, state: PrinterState, event: JobSlicerSettings) -> None:
        if not self.printers[event.printer].slicer_settings:
            return  # turned off: the file is never read
        job = state.jobs.get(event.job_id)
        if job is None or job.finished or job.slicer_settings or self._no_slicer_settings():
            return  # a job not logged, logged before (a replay), or read already
        file = None
        if event.read is not None:
            where = f"[printer {event.printer}] job {event.job_id}'s slicer settings"
            file = await self._wait_for(event.read, where)
        body = request_body(event.metadata, file)
        if body is not None:
            self._queue(event.printer, event.job_id, SLICER_SETTINGS, body)
        else:
            log.debug("[printer %s] job %s: no slicer settings", event.printer, event.job_id)
        # Marked only once queued, as a picture is.
        job.slicer_settings = True
        state.save()

    async def _fetch(self, event: JobImage) -> bytes | None:
        """The adapter's picture, or None when there's none, it fails, or it takes too long."""
        where = f"[printer {event.printer}] job {event.job_id}'s {event.kind.value}"
        return await self._wait_for(event.fetch, where)

    async def _wait_for(self, fetch: Callable[[], Awaitable[T]], where: str) -> T | None:
        """What the adapter's `fetch` gives, or None when it fails or takes too long."""
        task = asyncio.ensure_future(fetch())
        try:
            # asyncio.wait, not wait_for, which can swallow a cancel before Python 3.12.
            done, _ = await asyncio.wait({task}, timeout=self.image_timeout)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not done:
            task.cancel()
            log.info("%s took longer than %gs; skipping it", where, self.image_timeout)
            return None
        try:
            data = task.result()
        except ImageUnavailable as e:
            log.info("%s: %s; skipping it", where, e)
            return None
        except Exception as e:
            # Not the exception's text: it could hold a webcam URL with its password.
            log.warning("%s couldn't be fetched (%s); skipping it", where, type(e).__name__)
            return None
        if data is None:
            log.debug("%s: there is none", where)
        return data

    def _remove_stray_images(self) -> None:
        """Image files no outbox entry needs: left by a power cut between writing and queueing."""
        if not self.images_dir.is_dir():
            return
        wanted = {e.payload.get("file") for e in self.outbox.entries() if e.kind in IMAGE_KINDS}
        for path in self.images_dir.iterdir():
            if path.name not in wanted:
                _unlink(path)

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
        (or holds only entries for printers no longer in the config). When the state directory
        can't be written, returns when to try again rather than raising: the bridge keeps
        running, and nothing that was queued is lost.
        """
        try:
            wait = await self._send_due()
        except STORAGE_ERRORS as e:
            return self._storage_failed(e)
        if self._storage_failures:
            self._storage_recovered()
        return wait

    async def _send_due(self) -> float | None:
        loop = asyncio.get_running_loop()
        while self._unqueued:
            await self._emit(self._unqueued[0])
            self._unqueued.pop(0)
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
        image = entry.kind in OPTIONAL_KINDS  # given up on rather than holding up the print
        try:
            if entry.kind == START:
                await self._send_start(entry)
            elif entry.kind == SLICER_SETTINGS:
                await self._send_slicer_settings(entry)
            elif image:
                await self._send_image(entry)
            else:
                await self._send_finish(entry)
        except AuthRejected:
            problem = "3D Print Log rejected the API key (printlog-bridge doctor checks it)"
            log.error(
                "[printer %s] the 3D Print Log API rejected the API key, so job %s isn't "
                "logged yet. It stays in the outbox: check the key with `printlog-bridge "
                "doctor` (`printlog-bridge store-key` replaces it), then restart the bridge.",
                entry.printer, entry.job_id,
            )  # fmt: skip
            self.outbox.failed(entry.seq, problem)
            self._paused_until = loop.time() + AUTH_PAUSE_S
            self._not_logged(entry, problem)
        except ApiRejected as e:
            if image:
                return await self._image_failed(entry, e)
            # Sending it again won't change the answer. The job stays unfinished in the state
            # file, so a replay after the user fixes the cause (a restart) queues it again.
            log.error("[printer %s] job %s isn't logged: %s", entry.printer, entry.job_id, e)
            if entry.kind == FINISH:
                self._forget_completion(entry)
            self._done(entry)
            self._not_logged(entry, str(e))
        except ApiUnavailable as e:
            if image:
                return await self._image_failed(entry, e)
            self._retry_later(entry, str(e), e.retry_after)
        except Exception as e:
            if image:
                return await self._image_failed(entry, e)
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

    async def _image_failed(self, entry: Entry, error: Exception) -> None:
        """A picture that didn't go through. Only an API out of reach is worth waiting for."""
        where = f"[printer {entry.printer}] job {entry.job_id}'s {entry.kind}"
        if isinstance(error, ApiUnavailable):
            tries = self._image_tries.get(entry.seq, 0)
            if error.status != TOO_MANY_REQUESTS and (
                error.status is not None or await self._api_reachable()
            ):
                tries = self._image_tries[entry.seq] = tries + 1
            if tries < IMAGE_MAX_ATTEMPTS:
                self._retry_later(entry, str(error), error.retry_after)
                return
            log.warning("%s wasn't added to its print after %d tries: %s", where, tries, error)
        elif isinstance(error, ApiRejected):
            log.warning("%s wasn't added to its print: %s", where, error)
        else:
            log.warning("%s wasn't added to its print", where, exc_info=error)
        self._done(entry)

    def _forget_completion(self, entry: Entry) -> None:
        """A refused end is worked out again when it's next sent (after a restart, once the
        user has fixed the cause), rather than sent as it was refused."""
        state = self.state(entry.printer)
        job = state.jobs.get(entry.job_id)
        if job is not None and job.completion is not None:
            job.completion = None
            state.save()

    async def _api_reachable(self) -> bool:
        """Whether the API answers a small request, after an upload that got no answer."""
        try:
            await self.api.current_user()
        except AuthRejected:
            return True
        except Exception:
            return False
        return True

    def _done(self, entry: Entry) -> None:
        self.outbox.remove(entry.seq)
        self._due.pop(entry.seq, None)
        self._image_tries.pop(entry.seq, None)
        if entry.kind in IMAGE_KINDS:
            _unlink(self.images_dir / str(entry.payload.get("file")))

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
            self._image_tries.pop(entry.seq, None)
            if entry.kind in OPTIONAL_KINDS:
                # Only the picture (or the slicer settings) is lost; the print's own events are
                # judged on their own.
                if entry.kind in IMAGE_KINDS:
                    _unlink(self.images_dir / str(entry.payload.get("file")))
                continue
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
        if print_ is None:
            self._logged(
                entry.printer,
                f"job {entry.job_id} started: no print of yours to adopt, so it isn't logged "
                "(create_prints = false). The bridge looks again when it ends.",
            )
        elif not job.adopted:
            self._logged(
                entry.printer,
                f"job {entry.job_id} started: print {print_['id']}, {print_.get('title')!r}",
            )

    async def _send_image(self, entry: Entry) -> None:
        where = f"[printer {entry.printer}] job {entry.job_id}'s {entry.kind}"
        job = self.state(entry.printer).jobs.get(entry.job_id)
        if job is None or job.print_id is None:
            log.info("%s has no print to go on (the print wasn't logged); dropping it", where)
            return
        payload = entry.payload
        try:
            data = (self.images_dir / str(payload["file"])).read_bytes()
        except OSError:
            log.warning("%s is gone from %s; skipping it", where, self.images_dir)
            return
        content_type = str(payload["content_type"])
        added = await self.api.upload_print_image(
            job.print_id,
            data,
            content_type,
            entry.kind + EXTENSIONS.get(content_type, ""),
            # The picture the user gave their own print stays the one shown.
            bool(payload.get("default")) and not job.had_picture,
        )
        if added is None:
            log.info("%s: print %s was deleted in 3D Print Log", where, job.print_id)
            return
        self._logged(
            entry.printer, f"job {entry.job_id}: {entry.kind} added to print {job.print_id}"
        )

    async def _send_slicer_settings(self, entry: Entry) -> None:
        """Records the job's slicer settings on its print. An answer that sending again won't
        change ends it here; anything worth retrying is raised to `_send`."""
        where = f"[printer {entry.printer}] job {entry.job_id}'s slicer settings"
        job = self.state(entry.printer).jobs.get(entry.job_id)
        if job is None or job.print_id is None:
            log.info("%s have no print to go on (the print wasn't logged); dropping them", where)
            return
        if job.print_with_settings is not None and job.print_with_settings == job.print_id:
            log.info(
                "%s aren't sent: print %s already has the slicer's own (the Slic3r uploader's or "
                "the Cura plugin's)",
                where, job.print_id,
            )  # fmt: skip
            return
        if self._no_slicer_settings():
            return
        try:
            status = await self.api.post_slicer_settings(job.print_id, entry.payload)
        except ApiRejected as e:
            # The API's own words, which never quote what was sent.
            log.warning("%s weren't recorded: %s", where, e)
            return
        except ApiUnavailable as e:
            if e.status not in NO_SLICER_SETTINGS:
                raise
            self._no_slicer_settings_until = self.clock() + NO_SLICER_SETTINGS_FOR
            log.warning(
                "[printer %s] 3D Print Log doesn't take slicer settings (%s), so none are read "
                "or sent for %s; then the bridge tries again",
                entry.printer, e, f"{NO_SLICER_SETTINGS_FOR.total_seconds() / 3600:g} hours",
            )  # fmt: skip
            return
        if status == 409:
            log.info(
                "%s aren't recorded: print %s already has the slicer's own (the Slic3r "
                "uploader's or the Cura plugin's)",
                where, job.print_id,
            )  # fmt: skip
        elif status == 404:
            log.info(
                "%s weren't recorded: print %s is gone, or 3D Print Log doesn't take them yet",
                where, job.print_id,
            )  # fmt: skip
        else:
            self._logged(
                entry.printer, f"job {entry.job_id}: slicer settings added to print {job.print_id}"
            )

    def _no_slicer_settings(self) -> bool:
        until = self._no_slicer_settings_until
        return until is not None and self.clock() < until

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
    ) -> dict[str, Any] | None:
        """The job's print: one of the user's adopted, else a new one. None in finish-only mode
        when there's none to adopt."""
        start = job.start
        if self.before_create is not None:
            try:
                await self.before_create(printer)
            except ApiUnavailable:
                # Tried again with the print: created now, it would never link to the connection.
                raise
            except Exception:  # the print matters more than its link to the connection
                log.exception("[printer %s] couldn't register the connection", printer)
        if target.adoption.adopt or job.claiming is not None:
            adopted = await self._adopt(target, state, printer, job_id, job)
            if adopted is not None:
                return adopted
        if not target.adoption.create:
            return None
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
        if start.get("file_identity"):  # a start saved by an older bridge has none
            body["fileHash"] = start["file_identity"]
        if start.get("estimated_duration_s"):
            body["estimatedPrintTimeInSeconds"] = round(start["estimated_duration_s"])
        if start.get("estimated_filament_g"):
            body["estimatedFilamentUsageMg"] = round(start["estimated_filament_g"] * 1000)
        rows = _estimate_rows(start)
        single_tool = False
        current: dict[int, str] = {}
        if self.spools is not None:
            # A print created after a swap (3D Print Log was down until then) starts with the
            # spools the job started with, not the ones loaded now.
            current = await self.spools.current(printer)
            loaded = before_swaps(current, job.swaps)
            single_tool = (self.spools.slot_count(printer) or 1) <= 1
            _link_loaded(rows, loaded, single_tool=single_tool)
            notes = self.spools.mismatches(printer, [row["slot"] for row in rows] or [0])
            if notes:
                body["notes"] = "\n\n".join(notes)
        if rows:
            body["filamentUsage"] = rows

        print_, _ = await self.api.create_print(body)
        job.print_id = int(print_["id"])
        state.save()
        if single_tool and job.swaps:
            await self._restore_loaded(target, printer, current)
        return print_

    async def _restore_loaded(
        self, target: LoggedPrinter, printer: str, before: Mapping[int, str]
    ) -> None:
        """Puts back the spools that were loaded before a print was created with the ones its
        job started with: on a single-tool printer, creating a print loads its spools. Exactly
        what was loaded then, which may be a spool set since for the next job."""
        assert self.spools is not None
        try:
            now = await self.spools.current(printer)
            for slot, spool in sorted(before.items()):
                if now.get(slot) != spool:
                    await self.api.load_slot(target.printer_id, slot, spool, f"T{slot}")
            for slot in sorted(set(now) - set(before)):
                await self.api.unload_slot(target.printer_id, slot)
            if now != dict(before):
                await self.spools.refresh(printer)
        except Exception as e:  # the print matters more than what's loaded for the next
            log.warning("[printer %s] couldn't load its spools again: %s", printer, e)

    async def _adopt(
        self, target: LoggedPrinter, state: PrinterState, printer: str, job_id: str, job: JobRecord
    ) -> dict[str, Any] | None:
        """Claims the user's print for the job, when there's one to adopt, and returns it.

        A candidate is on the same printer, in Printing, claimed by no connector, created within
        `adopt_window` before the job started, and for the same file (`same_file`). The newest
        wins. The API filters on all but the file; they're checked again here all the same.
        """
        start = job.start
        claim = {
            "externalSource": target.source,
            "externalId": f"{state.instance_id}:{job_id}",
            "connectionInstanceId": state.instance_id,
            "startDate": start["started_at"],
        }
        if start.get("file_identity"):
            # Kept only when the print has none, so an OctoPrint SHA-1 already on it stays.
            claim["fileHash"] = start["file_identity"]
        if job.claiming is not None:
            # Claimed before, and the answer was lost: the same claim again returns the print.
            claimed = await self._claim(state, printer, job_id, job, job.claiming, claim)
            if claimed is not None:
                return claimed
            job.claiming = None
            state.save()

        started_at = datetime.fromisoformat(start["started_at"])
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        since = started_at - timedelta(seconds=target.adoption.window_s)
        try:
            found = await self.api.claimable_prints(target.printer_id, since)
        except (ApiUnavailable, ApiRejected) as e:
            if e.status not in NO_LOOKUP:
                raise
            if not self._no_lookup_logged:
                self._no_lookup_logged = True
                log.warning(
                    "[printer %s] 3D Print Log can't look up prints to adopt yet (%s), so "
                    "jobs get a print of their own",
                    printer, e,
                )  # fmt: skip
            return None
        candidates = sorted(
            (p for p in found if _adoptable(p, target.printer_id, since, start["file_name"])),
            key=_created,
            reverse=True,
        )
        if len(candidates) > 1:
            log.info(
                "[printer %s] job %s: %d prints could be adopted (%s); taking the newest",
                printer, job_id, len(candidates), ", ".join(str(p["id"]) for p in candidates),
            )  # fmt: skip
        for candidate in candidates:
            job.claiming = int(candidate["id"])
            if candidate.get("hasSlicerSettings"):
                job.print_with_settings = job.claiming
            state.save()
            claimed = await self._claim(state, printer, job_id, job, job.claiming, claim)
            if claimed is not None:
                return claimed
            log.info(
                "[printer %s] job %s: print %s was claimed, finished or deleted meanwhile",
                printer, job_id, candidate["id"],
            )  # fmt: skip
            job.claiming = None
            state.save()
        return None

    async def _claim(
        self,
        state: PrinterState,
        printer: str,
        job_id: str,
        job: JobRecord,
        print_id: int,
        body: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """The job's print once `print_id` is claimed for it, or None when it can't be (gone,
        claimed by another job, no longer in Printing), so the next candidate is tried.

        When the job is already logged as a print of its own (created before a power cut lost
        its id), that print is the job's, and nothing is adopted."""
        try:
            claimed = await self.api.claim_print(print_id, body)
        except ClaimConflict as e:
            if e.print_id is None:
                return None
            own = await self.api.get_print(e.print_id)
            if own is not None:
                job.print_id = int(own["id"])
                job.claiming = None
                state.save()
            return own
        except ApiRejected as e:
            log.info(
                "[printer %s] job %s: couldn't claim print %s: %s", printer, job_id, print_id, e
            )
            return None
        return self._adopted(state, printer, job_id, job, claimed) if claimed else None

    def _adopted(
        self, state: PrinterState, printer: str, job_id: str, job: JobRecord, print_: dict
    ) -> dict[str, Any]:
        job.print_id = int(print_["id"])
        job.claiming = None
        job.adopted = True
        job.had_picture = bool(print_.get("images"))
        state.save()
        title = print_.get("title")
        self._logged(printer, f"job {job_id}: adopted print {print_['id']} ({title!r})")
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
            if print_ is None:
                job.finished = True
                state.save()
                self._logged(
                    event.printer,
                    f"job {event.job_id} {event.outcome.value}: no print of yours to adopt, so "
                    "it isn't logged (create_prints = false)",
                )
                return
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
        completion = job.completion
        if completion is None:
            # With a swap, worked out once, from one look at the swaps, and sent the same way
            # every time: a retry reads the print back with a row per spool already on it (#44).
            loaded = await self.spools.current(event.printer) if self.spools is not None else {}
            slot_count = self.spools.slot_count(event.printer) if self.spools is not None else None
            single_tool = (slot_count or 1) <= 1 and all(slot == 0 for slot in event.filament_mm)
            swaps = list(job.swaps)
            rows_now = print_.get("filamentUsage") or []
            conflicts: list[str] = []
            usage = _usage_rows(event, rows_now, loaded, single_tool, swaps, conflicts)
            noted = "\n".join([*swap_notes(swaps, event.filament_mm), *conflicts])
            hints = change_hints(job.filament_changes, swaps)
            completion = {"usage": usage, "notes": [n for n in (event.note, noted, *hints) if n]}
            if swaps:
                job.completion = completion
                state.save()
        rows = completion["usage"]
        if rows:
            body["filamentUsage"] = rows

        completed = await self.api.complete_print(int(print_["id"]), body)
        if completed is None:
            return self._deleted(state, event, job)

        notes = (completed.get("notes") or "").rstrip()
        added = [n for n in completion["notes"] if n not in notes]
        if added:
            await self.api.patch_print(
                int(print_["id"]), {"notes": "\n\n".join([notes, *added] if notes else added)}
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

    def _storage_failed(self, error: Exception) -> float:
        """Logs and reports a failed write to the state directory. Returns when to try again."""
        self._storage_failures += 1
        delay = backoff_delay(self._storage_failures, *self.backoff)
        problem = (
            f"couldn't write the outbox in {self.state_dir} ({error}). Check the disk has room. "
            f"Jobs are kept until it can be written; the bridge tries again in {_seconds(delay)}."
        )
        level = logging.WARNING if self._storage_failures == 1 else logging.DEBUG
        log.log(level, "%s", problem)
        for printer in self.printers:
            report = self._report(printer)
            if report is not None:
                report.warn(OUTBOX_TOPIC, problem)
        return delay

    def _storage_recovered(self) -> None:
        log.info("the outbox in %s can be written again", self.state_dir)
        self._storage_failures = 0
        for printer in self.printers:
            report = self._report(printer)
            if report is not None:
                report.warn(OUTBOX_TOPIC, None)

    def _not_logged(self, entry: Entry, problem: str) -> None:
        report = self._report(entry.printer)
        if report is not None:
            report.warn(API_TOPIC, f"job {entry.job_id} isn't logged yet: {problem}")


def _adoptable(print_: Mapping[str, Any], printer_id: int, since: datetime, file_name: str) -> bool:
    """Whether the job for `file_name` may adopt the print. A print with no `fileName` (made by
    hand) matches on its title, which is what a person would have typed for the file."""
    if print_.get("id") is None or print_.get("printerId") != printer_id:
        return False
    if print_.get("status") != PRINTING or print_.get("externalId"):
        return False
    created = _created(print_)
    if created is not None and created < since:
        return False
    return same_file(print_.get("fileName") or print_.get("title") or "", file_name)


def _created(print_: Mapping[str, Any]) -> datetime | None:
    """When the print was made in 3D Print Log, or its start date when that isn't given."""
    for key in ("createdDate", "createdAt", "dateCreated", "startDate"):
        value = print_.get(key)
        if not value:
            continue
        try:
            when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return None


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


def _what(event: JobEvent) -> str:
    """What an optional event brings, for a log line: `thumbnail`, `slicer settings`..."""
    return event.kind.value if isinstance(event, JobImage) else "slicer settings"


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("couldn't remove %s: %s", path, e)


def _seconds(delay: float) -> str:
    return f"{delay:.0f}s" if delay >= 1 else f"{delay:.2f}s"


def _estimate_rows(start: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The slicer's estimate as usage rows, one per slot, which completing fills in.

    A file with a weight per tool gets a row for each tool it uses, by weight. Otherwise (one
    filament, or one tool used) the totals are one row, by length when the slicer gave one.
    """
    estimated = start.get("estimated_filament_g_by_slot") or {}
    by_slot = {int(slot): grams for slot, grams in estimated.items()}
    if len(by_slot) > 1:
        return [
            {"slot": slot, "estimatedAmountMg": round(grams * 1000), "estimatedSource": WEIGHT}
            for slot, grams in sorted(by_slot.items())
        ]
    mm, grams = start.get("estimated_filament_mm"), start.get("estimated_filament_g")
    if not mm and not grams:
        return []
    row: dict[str, Any] = {
        "slot": next(iter(by_slot), 0),
        "estimatedSource": LENGTH if mm else WEIGHT,
    }
    if mm:
        row["estimatedLengthInM"] = round(mm / 1000, 4)
    if grams:
        row["estimatedAmountMg"] = round(grams * 1000)
    return [row]


def _link_loaded(rows: list[dict[str, Any]], loaded: Mapping[int, str], single_tool: bool) -> None:
    """Links each estimate row to the spool loaded in its slot, once per spool.

    On a single-tool printer, creating a print makes its spools the printer's loaded ones, so
    a print with no estimate still gets a row for the loaded spool: without it, logging the
    print would unload the spool in 3D Print Log.

    A create names the spool as `filament: {id}`, the print's own shape: the API's create has
    no `filamentId` and drops it without an error (completing takes `filamentId`).
    """
    if not rows and single_tool and 0 in loaded:
        rows.append({"slot": 0})
    linked = set()
    for row in rows:
        spool = loaded.get(row["slot"])
        if spool is not None and spool not in linked:
            row["filament"] = {"id": spool}
            linked.add(spool)


def _usage_rows(
    event: JobFinished,
    rows: Sequence[Mapping[str, Any]],
    loaded: Mapping[int, str] | None = None,
    single_tool: bool = False,
    swaps: Sequence[Mapping[str, Any]] = (),
    notes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """One row per slot that fed filament, linked to the spool in that slot when it's known.

    A slot with no spool still gets its row, with its slot and no `filamentId`. The API takes
    one row per spool, so a spool on two slots gets one row, on the first, with both lengths.

    A slot whose spool changed while the job ran (`swaps`, #44) is split where it changed: one
    row per spool, the slot's first spool first, so the API's completion puts that one on the
    row the print started with (its estimate stays there) and adds a row for the next. The first
    spool is the one the slot started with, unless the print's row is linked to another the
    swaps don't explain (`first_spools`), which then keeps it, with a note added to `notes`.

    On a single-tool printer a row with no slot (an uploader's) is slot 0's, so the spool the
    user linked on it wins. The API's completion fills that row from slot 0's in the same way
    (on a printer with one slot, a row with no slot counts as slot 0, 3d-print-log-api#168).
    """
    if single_tool:
        rows = [{**row, "slot": 0} if row.get("slot") is None else row for row in rows]
    linked: dict[int, str] = {}
    for row in rows:
        slot, spool = row.get("slot"), (row.get("filament") or {}).get("id")
        if slot is not None and spool and slot not in linked:
            linked[slot] = str(spool)
    firsts, conflicts = first_spools(linked, swaps)
    if notes is not None:
        notes.extend(conflicts)
    rows = [
        {k: v for k, v in row.items() if k != "filament"} if row.get("slot") in firsts else row
        for row in rows
    ]
    spools = _spools_by_slot(rows, before_swaps(loaded or {}, swaps))
    for slot, first in firsts.items():
        spools.pop(slot, None)
        if first is not None:
            spools[slot] = first
    usage: list[dict[str, Any]] = []
    for slot, mm in sorted(event.filament_mm.items()):
        slot_swaps = [swap for swap in on_slot(swaps, slot) if is_split(swap)]
        pieces = [p for p in segments(mm, spools.get(slot), slot_swaps) if p[1] > 0]
        # In metres to 0.1 mm, the last piece taking what rounding left, so the slot's rows
        # add up to its rounded total exactly.
        metres = [round(length / 1000, 4) for _, length in pieces[:-1]]
        if pieces:
            metres.append(round(round(mm / 1000, 4) - sum(metres), 4))
        for (spool, _), m in zip(pieces, metres):
            # One row per spool, and one row per slot with no spool (the API matches those by
            # slot, so it takes only one each).
            key = _row_key({"slot": slot, "filamentId": spool})
            row = next((r for r in usage if _row_key(r) == key), None)
            if row is not None:
                row["lengthInM"] = round(row["lengthInM"] + m, 4)
                continue
            row = {"slot": slot, "lengthInM": m, "source": LENGTH}
            if spool is not None:
                row["filamentId"] = spool
            usage.append(row)
    return usage


def _row_key(row: Mapping[str, Any]) -> tuple[int, str | None] | str:
    spool = row.get("filamentId")
    return spool if spool is not None else (row["slot"], None)


def _spools_by_slot(rows: Sequence[Mapping[str, Any]], loaded: Mapping[int, str]) -> dict[int, str]:
    """The spool (filament id) in each slot: as linked on the print's usage rows, else the one
    loaded in the slot in 3D Print Log now.

    The one place a slot's spool is decided. The print's rows hold the spools loaded when it
    was created, and whatever the user linked since, which wins: spool macros and the U1's
    RFID reader (#6) only change what's loaded on the printer.
    """
    spools: dict[int, str] = {}
    for row in rows:
        slot, filament = row.get("slot"), row.get("filament")
        if slot is not None and filament and slot not in spools:
            spools[slot] = filament["id"]
    for slot, spool in loaded.items():
        if slot not in spools and spool not in spools.values():
            spools[slot] = spool
    return spools
