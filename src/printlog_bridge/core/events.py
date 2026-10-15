"""What an adapter reports about a printer's jobs, and about the spools in its slots.

Adapters translate their printer's own vocabulary into these events, and everything downstream
(the outbox, the API client) only ever sees these.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, Union


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


@dataclass(frozen=True)
class JobPoint:
    """How far a running job has got, at the moment a spool changes in one of its slots (#44).

    The adapter takes it when it hears of the change, so the split falls where the change
    happened, however long 3D Print Log then takes to answer.
    """

    job_id: str
    fed_mm: Mapping[int, float] = field(default_factory=dict)
    """Filament fed per slot so far, in millimetres, on the same scale as `JobFinished`'s
    `filament_mm`: the length before this point is the old spool's, the rest the new one's."""
    progress: float | None = None
    """How far through the file the job is, 0 to 1."""
    layer: int | None = None
    """The layer it's on, when the slicer reports layers."""


@dataclass(frozen=True)
class FilamentChange:
    """A job was paused for a filament change and resumed (#44): a runout sensor found no
    filament, or filament was unloaded while it was paused (an M600). If no spool was assigned
    to the slot at that point, the print gets a note saying how to split its usage."""

    printer: str
    slot: int
    at: JobPoint
    """Where the job was paused."""
    reason: str
    """`runout` or `unload`."""

    @property
    def job_id(self) -> str:
        return self.at.job_id


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


@dataclass(frozen=True)
class SlicerMetadata:
    """The slicer's summary of a job's file, as the printer extracted it (Moonraker's metadata)."""

    slicer: str | None = None
    slicer_version: str | None = None
    layer_height_mm: float | None = None
    first_layer_height_mm: float | None = None
    nozzle_diameter_mm: float | None = None
    filament_name: str | None = None
    filament_type: str | None = None
    first_layer_nozzle_temperature_c: float | None = None
    first_layer_bed_temperature_c: float | None = None


@dataclass(frozen=True)
class SlicerFile:
    """What was read of a job's file for its slicer settings: never its toolpaths.

    As read, before scrubbing: the sink removes host and credential values before anything
    leaves the bridge (`core/slicer_settings.py`)."""

    header: str | None = None
    """The start of the file through `; HEADER_BLOCK_END` (PrusaSlicer: its first line)."""
    config_block: str | None = None
    """The `; key = value` settings the slicer wrote after the G-code."""


@dataclass(frozen=True)
class JobSlicerSettings:
    """The slicer settings of the job's file, reported after its start (#36).

    The sink calls `read` only when it still wants them, and never when they are turned off
    (`slicer_settings = false`), so an adapter must not read the file before then. `read`
    returns None when there's nothing to be had (not a G-code file, gone, unreadable), should
    give up on its own after about 10 s, and anything it raises counts as nothing read.
    `metadata` is sent either way."""

    printer: str
    job_id: str
    metadata: SlicerMetadata | None = None
    read: Callable[[], Awaitable[SlicerFile | None]] | None = field(
        default=None, compare=False, repr=False
    )


JobEvent = Union[JobStarted, JobFinished, JobImage, JobSlicerSettings, FilamentChange]


@dataclass(frozen=True)
class PastJob:
    """A finished job from the printer's own history, for `printlog-bridge backfill` (#8)."""

    start: JobStarted
    end: JobFinished
    """How it ended. `filament_mm` adds up to everything the job fed."""
    file_exists: bool = True
    """Whether the job's file is still on the printer."""


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
    """The user assigns a spool to a slot on the printer (`PRINTLOG_SET_SPOOL`, or a spool
    picked in Mainsail, Fluidd or KlipperScreen through Moonraker's Spoolman integration), or
    clears it.

    Exactly one of `filament_id`, `tag` and `spool_number` names the spool; with none, the slot
    is cleared.
    """

    printer: str
    slot: int
    """The slot (tool index), 0-based: T0 is slot 0."""
    filament_id: str | None = None
    """The spool's id in 3D Print Log."""
    tag: str | None = None
    """The UID of an RFID/NFC tag on the spool, in any common format (`04:A2:3F:11`)."""
    spool_number: int | None = None
    """The spool's number in 3D Print Log (`spoolNumber`), which is what Moonraker's Spoolman
    integration calls its spool id when it is pointed at 3D Print Log."""
    slot_count: int | None = None
    """How many slots (tools) the printer has, when the adapter knows."""
    during: JobPoint | None = None
    """Where the job running when the user gave the command was, so its usage is split there;
    None between jobs."""


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
    during: JobPoint | None = None
    """Where the job running when the reader saw this was; None between jobs."""


class SpoolSink(Protocol):
    async def spool_command(self, command: SpoolCommand) -> str:
        """Carries out the command. Returns what happened, in words for the printer's console.

        Never raises: a command that fails says why in the answer.
        """

    async def spool_reading(self, reading: SpoolReading) -> None:
        """Takes a reader's view of a slot. A tag a spool has assigns that spool to the slot."""

    def watch_loaded(
        self, printer: str, watcher: Callable[[Mapping[int, Mapping[str, Any]]], None]
    ) -> Callable[[], None]:
        """Tells `watcher` the printer's spool in each slot (3D Print Log's filament summary,
        `spoolNumber` included) once known, and after each change. Returns what stops that.

        Optional: adapters look it up with `getattr`, so a sink without it (a test's) is told
        nothing and needs no stub."""
        return lambda: None
