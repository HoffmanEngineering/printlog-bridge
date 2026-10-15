"""What the bridge remembers about each printer between runs, in `state_dir`.

One JSON file per printer holds its connection's instance id, which makes every job's external
ID unique to this bridge and printer, and the jobs it has seen, so a replayed event is
recognised and a restart picks up where the last run stopped. An adapter that can find its
printer again after an address change keeps what it needs for that in `discovery`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KEEP_FINISHED_JOBS = 20
"""Finished jobs kept to recognise replays. Older ones are never replayed: see `resume_after`."""


@dataclass
class JobRecord:
    started_at: str
    """ISO 8601, from the printer's clock."""
    start: dict[str, Any] = field(default_factory=dict)
    """The start event's fields, which the outbox's start entry creates the print from."""
    print_id: int | None = None
    finished: bool = False
    ended_at: str | None = None
    """When the printer said the job ended, while that isn't logged yet (it's in the outbox)."""
    images: dict[str, bool] = field(default_factory=dict)
    """Each picture already fetched for the job (`thumbnail`, `snapshot`), and whether there
    was one to add, so a replayed job isn't fetched again."""
    slicer_settings: bool = False
    """The job's slicer settings were read and queued (or there were none), so a replayed job
    isn't read again (#36)."""
    print_with_settings: int | None = None
    """A print the lookup for prints to adopt said already has slicer settings (the Slic3r
    uploader's or the Cura plugin's). Adopted, it keeps them and the bridge's aren't sent."""
    claiming: int | None = None
    """The print being adopted, from just before the claim is sent until `print_id` is set, so
    a claim whose answer was lost is sent again rather than a second print created."""
    adopted: bool = False
    """The print was the user's (an uploader's), adopted rather than created by the bridge."""
    had_picture: bool = False
    """The adopted print already had a picture, which the thumbnail mustn't replace."""
    swaps: list[dict[str, Any]] = field(default_factory=list)
    """Each spool assigned to one of the job's slots while it ran, and where (`swaps.py`), so
    the slot's usage is split there when the job ends, even after a restart."""
    filament_changes: list[dict[str, Any]] = field(default_factory=list)
    """Each pause for a filament change (a runout, an unload) the job was resumed from."""
    completion: dict[str, Any] | None = None
    """The usage rows and notes worked out the first time the job's end was sent, sent the
    same way on every retry so a split slot is never counted twice."""


class PrinterState:
    def __init__(
        self,
        path: Path,
        instance_id: str,
        jobs: dict[str, JobRecord],
        discovery: dict[str, Any] | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        self.path = path
        self.read_only = read_only
        self.instance_id = instance_id
        self.jobs = jobs
        self.discovery: dict[str, Any] = discovery or {}
        """The printer's identity and where it was last found, for the adapter's own use."""

    @classmethod
    def load(cls, state_dir: Path, printer: str) -> PrinterState:
        path = state_dir / "printers" / f"{safe_file_name(printer)}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(JobRecord)}
            # Fields a newer bridge added are left out, so going back a version still works.
            jobs = {
                job_id: JobRecord(**{k: v for k, v in job.items() if k in known})
                for job_id, job in data["jobs"].items()
            }
            discovery = data.get("discovery")
            return cls(
                path, data["instance_id"], jobs, discovery if isinstance(discovery, dict) else {}
            )
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError) as e:
            broken = path.with_suffix(".broken")
            os.replace(path, broken)
            log.warning(
                "[printer %s] %s was unreadable (%s), so it was moved to %s and the bridge "
                "starts afresh. Jobs already logged may be logged again.",
                printer, path, e, broken,
            )  # fmt: skip
        state = cls(path, str(uuid.uuid4()), {})
        state.save()
        return state

    @classmethod
    def peek(cls, state_dir: Path, printer: str) -> PrinterState | None:
        """The printer's state as it is on disk, or None when there isn't a readable one.

        Unlike `load`, this never writes: a missing file isn't created and a broken one isn't
        moved aside, so `doctor` can look while the bridge is running. It holds only
        `instance_id` and `discovery`, and `save()` refuses it.
        """
        path = state_dir / "printers" / f"{safe_file_name(printer)}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            discovery = data.get("discovery")
            return cls(
                path,
                data["instance_id"],
                {},  # not read: saving it back would lose them, so it can't be saved
                discovery if isinstance(discovery, dict) else {},
                read_only=True,
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def resume_after(self) -> datetime | None:
        unfinished = [j.started_at for j in self.jobs.values() if not j.finished]
        candidates = unfinished or [j.started_at for j in self.jobs.values()]
        if not candidates:
            return None
        return datetime.fromisoformat(min(candidates) if unfinished else max(candidates))

    def save(self) -> None:
        if self.read_only:
            raise RuntimeError(f"{self.path} was read with peek(), which can't be saved")
        finished = sorted(
            (job_id for job_id, j in self.jobs.items() if j.finished),
            key=lambda job_id: self.jobs[job_id].started_at,
        )
        for job_id in finished[:-KEEP_FINISHED_JOBS]:
            del self.jobs[job_id]

        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "instance_id": self.instance_id,
            "jobs": {job_id: asdict(job) for job_id, job in self.jobs.items()},
        }
        if self.discovery:
            data["discovery"] = self.discovery
        # Write then rename, so a power cut leaves the old file or the new one, never half.
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)


def safe_file_name(name: str) -> str:
    """`name` with anything a file name shouldn't hold replaced by `_`."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)
