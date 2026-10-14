"""What an adapter reports about a printer's jobs, and about the spools in its slots.

Adapters translate their printer's own vocabulary into these events, and everything downstream
(the outbox, the API client) only ever sees these.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
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
    estimated_filament_g_by_slot: Mapping[int, float] = field(default_factory=dict)
    """The slicer's estimate per slot (tool index), in grams, when the file has one per tool.
    Empty for a file with a single filament, whose estimate is the totals above."""


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
    """For the print's notes: why it failed, such as "Klipper shutdown", and what else the
    user would want to know, such as objects they cancelled mid-print."""


class ImageKind(Enum):
    THUMBNAIL = "thumbnail"
    """The slicer's preview, from the job's file, reported when the job starts."""
    SNAPSHOT = "snapshot"
    """One frame from the printer's webcam, reported when the job ends."""


@dataclass(frozen=True)
class JobImage:
    """A picture of the job. The sink calls `fetch` only when it still wants this one.

    `fetch` returns the image's bytes, or None when there is none to be had (no thumbnail in
    the file, no webcam). It should give up on its own after about 10 s; the sink stops waiting
    after `IMAGE_TIMEOUT_S` regardless, and treats anything it raises as no image.
    """

    printer: str
    job_id: str
    kind: ImageKind
    fetch: Callable[[], Awaitable[bytes | None]] = field(compare=False, repr=False)


JobEvent = Union[JobStarted, JobFinished, JobImage]


class EventSink(Protocol):
    async def emit(self, event: JobEvent) -> None:
        """Handles one event. A replayed event (same printer and job_id) is recognised."""

    async def resume_after(self, printer: str) -> datetime | None:
        """Where to resume reading the printer's job history after a restart or reconnect.

        The `started_at` of the earliest job not yet logged as finished, or else of the latest
        job, so replaying every job that started at or after it misses nothing. None before the
        first job.
        """


@dataclass(frozen=True)
class SpoolCommand:
    """The user assigns a spool to a slot on the printer (`PRINTLOG_SET_SPOOL`), or clears it.

    Exactly one of `filament_id` and `tag` names the spool; with neither, the slot is cleared.
    """

    printer: str
    slot: int
    """The slot (tool index), 0-based: T0 is slot 0."""
    filament_id: str | None = None
    """The spool's id in 3D Print Log."""
    tag: str | None = None
    """The UID of an RFID/NFC tag on the spool, in any common format (`04:A2:3F:11`)."""
    slot_count: int | None = None
    """How many slots (tools) the printer has, when the adapter knows."""


@dataclass(frozen=True)
class SpoolReading:
    """What a reader on the printer says is in a slot, such as the Snapmaker U1's RFID reader."""

    printer: str
    slot: int
    tag: str | None = None
    """The tag's UID as hex, when the reader read one."""
    material: str | None = None
    """The material the tag says, such as `PLA` or `PETG-CF`."""
    color_hex: str | None = None
    """The tag's color, as `#RRGGBB`."""
    slot_count: int | None = None


class SpoolSink(Protocol):
    async def spool_command(self, command: SpoolCommand) -> str:
        """Carries out the command. Returns what happened, in words for the printer's console.

        Never raises: a command that fails says why in the answer.
        """

    async def spool_reading(self, reading: SpoolReading) -> None:
        """Takes a reader's view of a slot. A tag a spool has assigns that spool to the slot."""
