"""What an adapter reports about a printer's jobs.

Adapters translate their printer's own vocabulary into these events, and everything downstream
(the outbox, the API client) only ever sees these.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, Union


class JobOutcome(Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class JobStarted:
    printer: str
    """The `[printer <name>]` this job ran on."""
    job_id: str
    """Stable for the job's lifetime and unique per printer, so a replay is recognised."""
    file_name: str
    started_at: datetime
    estimated_duration_s: float | None = None
    estimated_filament_mm: float | None = None
    """The slicer's estimate of filament fed, in millimetres."""
    estimated_filament_g: float | None = None


@dataclass(frozen=True)
class JobFinished:
    printer: str
    job_id: str
    outcome: JobOutcome
    finished_at: datetime
    duration_s: float | None = None
    filament_mm: Mapping[int, float] = field(default_factory=dict)
    """Filament fed per slot (tool index), in millimetres."""
    note: str | None = None
    """Why it failed, in words for the print's notes, such as "Klipper shutdown"."""


JobEvent = Union[JobStarted, JobFinished]


class EventSink(Protocol):
    async def emit(self, event: JobEvent) -> None:
        """Handles one event. A replayed event (same printer and job_id) is recognised."""

    async def resume_after(self, printer: str) -> datetime | None:
        """Where to resume reading the printer's job history after a restart or reconnect.

        The `started_at` of the earliest job not yet logged as finished, or else of the latest
        job, so replaying every job that started at or after it misses nothing. None before the
        first job.
        """
