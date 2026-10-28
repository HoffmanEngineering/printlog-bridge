"""The running job of a printer that keeps no job history (#82), so a restart or a reconnect can
still tell what became of it.

Moonraker keeps a history the adapter replays from `resume_after`. PrusaLink, Bambu's local API
and OctoPrint's core say only what is printing now. An adapter for one of those keeps the job in
the printer's state (`state.discovery["current_job"]`) with `CurrentJob`: `started` when it
reports the start, `seen` while it runs, `ended` once it has reported the end itself. After each
connect it calls `reconcile` with the job the printer is running now, and emits what it returns:

- the same job still running: its start again, which the sink recognizes;
- another job, or none: the remembered job's start and a `JobFinished`. The outcome is the
  printer's when it says (the last job's state), otherwise `COMPLETED`; the end is the printer's
  when it says, otherwise when the bridge last saw it running, with a note saying so. A job
  guessed complete carries the slicer's estimate as its usage, marked estimated.

A job that started and ended entirely while the bridge was away is never seen, and isn't logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from printlog_bridge.core.events import JobEvent, JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.health import utc_now
from printlog_bridge.core.state import PrinterState

log = logging.getLogger(__name__)

KEY = "current_job"
"""Where in `state.discovery` the job is kept."""
SEEN_SAVE_INTERVAL = timedelta(minutes=1)
"""How often `seen` writes the state file: the end of a job missed is placed this close."""
UNSEEN_END_NOTE = (
    "The bridge wasn't connected when this job ended, so it is logged as finished, ending when "
    "the bridge last saw it running, with the slicer's estimate as its usage. Check its status "
    "and times."
)
UNSEEN_TIME_NOTE = (
    "The bridge wasn't connected when this job ended, so its end is when the bridge last saw it "
    "running."
)


class CurrentJob:
    def __init__(self, state: PrinterState, clock: Callable[[], datetime] = utc_now) -> None:
        self.state = state
        self.clock = clock

    @property
    def job(self) -> JobStarted | None:
        """The remembered job's start, or None."""
        entry = self.state.discovery.get(KEY)
        try:
            return _start_event(entry["start"])  # type: ignore[index]
        except (TypeError, KeyError, ValueError):
            return None

    @property
    def last_seen(self) -> datetime | None:
        """When the bridge last saw the remembered job running."""
        entry = self.state.discovery.get(KEY)
        try:
            return datetime.fromisoformat(entry["seen_at"])  # type: ignore[index]
        except (TypeError, KeyError, ValueError):
            return None

    def started(self, event: JobStarted) -> None:
        """Remembers `event`'s job as the one running, in place of any other."""
        self.state.discovery[KEY] = {
            "start": _start_payload(event),
            "seen_at": self.clock().isoformat(),
        }
        self.state.save()

    def seen(self) -> None:
        """The remembered job is still running. Written at most once a minute."""
        entry = self.state.discovery.get(KEY)
        if not isinstance(entry, dict):
            return
        now = self.clock()
        last = self.last_seen
        if last is not None and now - last < SEEN_SAVE_INTERVAL:
            return
        entry["seen_at"] = now.isoformat()
        self.state.save()

    def ended(self) -> None:
        """Forgets the job, once the adapter has reported its end itself."""
        if self.state.discovery.pop(KEY, None) is not None:
            self.state.save()

    def reconcile(
        self,
        running: str | None,
        outcome: JobOutcome | None = None,
        finished_at: datetime | None = None,
    ) -> list[JobEvent]:
        """What to emit after a connect, given the `job_id` the printer is running now (None
        when it's idle), and what it says of how its last job ended, when it says."""
        start = self.job
        if start is None:
            if self.state.discovery.pop(KEY, None) is not None:
                self.state.save()  # broken: nothing can be made of it
            return []
        if running == start.job_id:
            self.seen()
            return [start]
        ended = finished_at or self.last_seen or self.clock()
        complete = outcome is None or outcome is JobOutcome.COMPLETED
        grams, mm, ml = _estimate(start) if complete else ({}, {}, {})
        if outcome is None:
            note: str | None = UNSEEN_END_NOTE
        else:
            note = None if finished_at is not None else UNSEEN_TIME_NOTE
        finished = JobFinished(
            printer=start.printer,
            job_id=start.job_id,
            outcome=outcome or JobOutcome.COMPLETED,
            finished_at=ended,
            duration_s=max(0.0, (ended - start.started_at).total_seconds()),
            filament_mm=mm,
            filament_g=grams,
            usage_estimated=bool(grams or mm or ml),
            note=note,
            volume_ml=ml,
        )
        log.info(
            "[printer %s] job %s ended while the bridge wasn't connected: logged as %s",
            start.printer, start.job_id, finished.outcome.value,
        )  # fmt: skip
        self.ended()
        return [start, finished]


def _estimate(
    start: JobStarted,
) -> tuple[dict[int, float], dict[int, float], dict[int, float]]:
    """The slicer's estimate as usage (grams, millimeters, milliliters): grams per slot when
    the file has them, else the totals on slot 0."""
    if start.estimated_filament_g_by_slot:
        return dict(start.estimated_filament_g_by_slot), {}, {}
    grams = {0: start.estimated_filament_g} if start.estimated_filament_g else {}
    mm = {0: start.estimated_filament_mm} if start.estimated_filament_mm else {}
    ml = {0: start.estimated_volume_ml} if start.estimated_volume_ml else {}
    return grams, mm, ml


def _start_payload(event: JobStarted) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "printer": event.printer,
        "job_id": event.job_id,
        "file_name": event.file_name,
        "started_at": event.started_at.isoformat(),
        "estimated_duration_s": event.estimated_duration_s,
        "estimated_filament_mm": event.estimated_filament_mm,
        "estimated_filament_g": event.estimated_filament_g,
        "estimated_filament_g_by_slot": {
            str(slot): grams for slot, grams in event.estimated_filament_g_by_slot.items()
        },
    }
    if event.estimated_volume_ml is not None:  # only then: a filament job's is as before #97
        payload["estimated_volume_ml"] = event.estimated_volume_ml
    return payload


def _start_event(payload: Mapping[str, Any]) -> JobStarted:
    for name in ("printer", "job_id", "file_name", "started_at"):
        if not isinstance(payload[name], str):
            raise ValueError(f"{name} isn't text")
    return JobStarted(
        printer=payload["printer"],
        job_id=payload["job_id"],
        file_name=payload["file_name"],
        started_at=datetime.fromisoformat(payload["started_at"]),
        estimated_duration_s=payload.get("estimated_duration_s"),
        estimated_filament_mm=payload.get("estimated_filament_mm"),
        estimated_filament_g=payload.get("estimated_filament_g"),
        estimated_filament_g_by_slot={
            int(slot): grams
            for slot, grams in (payload.get("estimated_filament_g_by_slot") or {}).items()
        },
        estimated_volume_ml=payload.get("estimated_volume_ml"),
    )
