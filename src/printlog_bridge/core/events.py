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


@dataclass(frozen=True)
class JobFinished:
    printer: str
    job_id: str
    outcome: JobOutcome
    finished_at: datetime
    duration_s: float | None = None
    filament_mm: Mapping[int, float] = field(default_factory=dict)
    """Filament fed per slot (tool index), in millimetres."""


JobEvent = Union[JobStarted, JobFinished]


class EventSink(Protocol):
    async def emit(self, event: JobEvent) -> None: ...
