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
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from printlog_bridge.core.api_client import ApiClient, ApiRejected, ApiUnavailable, AuthRejected
from printlog_bridge.core.events import (
    ImageKind,
    JobEvent,
    JobFinished,
    JobImage,
    JobOutcome,
    JobStarted,
)
from printlog_bridge.core.images import EXTENSIONS, MAX_IMAGE_BYTES, ImageUnavailable, image_type
from printlog_bridge.core.outbox import Entry, Outbox, utc_now
from printlog_bridge.core.state import JobRecord, PrinterState, safe_file_name
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
OUTBOX_TOPIC = "outbox"
STORAGE_ERRORS = (OSError, sqlite3.Error)
"""A full or read-only disk, or an outbox another process holds locked past SQLite's timeout."""
START, FINISH = "start", "finish"
IMAGE_KINDS = frozenset(kind.value for kind in ImageKind)
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
        image_timeout: float = IMAGE_TIMEOUT_S,
    ) -> None:
        """`before_create(printer)` runs before each print is created. The bridge registers the
        printer's connection there, so the print links to it."""
        self.api = api
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
        queued, in order, once it can be, and `status` shows a warning meanwhile. A picture is
        skipped instead: it is fetched now or never.
        """
        if self._unqueued and not isinstance(event, JobImage):
            self._unqueued.append(event)  # nothing jumps ahead of what waits to be queued
            return
        if isinstance(event, JobImage) and any(
            (e.printer, e.job_id) == (event.printer, event.job_id) for e in self._unqueued
        ):
            # Queued now, it would go ahead of its job's start or end: a thumbnail before the
            # print exists is dropped, and a snapshot before the end misses its outcome.
            log.warning(
                "[printer %s] job %s's %s is skipped: the outbox can't be written",
                event.printer, event.job_id, event.kind.value,
            )  # fmt: skip
            return
        try:
            await self._emit(event)
        except STORAGE_ERRORS as e:
            self._storage_failed(e)
            if isinstance(event, JobImage):
                log.warning(
                    "[printer %s] job %s's %s is skipped: the outbox can't be written",
                    event.printer, event.job_id, event.kind.value,
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

    async def _fetch(self, event: JobImage) -> bytes | None:
        """The adapter's picture, or None when there's none, it fails, or it takes too long."""
        task = asyncio.ensure_future(event.fetch())
        try:
            # asyncio.wait, not wait_for, which can swallow a cancel before Python 3.12.
            done, _ = await asyncio.wait({task}, timeout=self.image_timeout)
        except asyncio.CancelledError:
            task.cancel()
            raise
        where = f"[printer {event.printer}] job {event.job_id}'s {event.kind.value}"
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
        image = entry.kind in IMAGE_KINDS
        try:
            if entry.kind == START:
                await self._send_start(entry)
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
            if entry.kind in IMAGE_KINDS:
                # Only the picture is lost; the print's own events are judged on their own.
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
            bool(payload.get("default")),
        )
        if added is None:
            log.info("%s: print %s was deleted in 3D Print Log", where, job.print_id)
            return
        self._logged(
            entry.printer, f"job {entry.job_id}: {entry.kind} added to print {job.print_id}"
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
        rows = _estimate_rows(start)
        if rows:
            body["filamentUsage"] = rows

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


def _usage_rows(event: JobFinished, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per slot that fed filament, linked to the spool in that slot when it's known.

    A slot with no spool still gets its row, with its slot and no `filamentId`. The API takes
    one row per spool, so a spool on two slots gets one row, on the first, with both lengths.
    """
    spools = _spools_by_slot(rows)
    usage: list[dict[str, Any]] = []
    mm_by_spool: dict[str, float] = {}
    for slot, mm in sorted(event.filament_mm.items()):
        if mm <= 0:
            continue
        spool = spools.get(slot)
        if spool is not None and spool in mm_by_spool:
            mm_by_spool[spool] += mm
            row = next(r for r in usage if r.get("filamentId") == spool)
            row["lengthInM"] = round(mm_by_spool[spool] / 1000, 4)
            continue
        row = {"slot": slot, "lengthInM": round(mm / 1000, 4), "source": LENGTH}
        if spool is not None:
            row["filamentId"] = spool
            mm_by_spool[spool] = mm
        usage.append(row)
    return usage


def _spools_by_slot(rows: Sequence[Mapping[str, Any]]) -> dict[int, str]:
    """The spool (filament id) in each slot, as the user linked it on the print's usage rows.

    The one place a slot's spool is decided: spool macros and the U1's RFID reader (#6) add the
    spool loaded in each slot here, with the user's choice on the print still winning.
    """
    spools: dict[int, str] = {}
    for row in rows:
        slot, filament = row.get("slot"), row.get("filament")
        if slot is not None and filament and slot not in spools:
            spools[slot] = filament["id"]
    return spools
