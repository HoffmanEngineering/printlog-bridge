"""The key the bridge knows a Moonraker job by: its history id and its start.

Moonraker numbers its history with SQLite's rowid (`job_id INTEGER PRIMARY KEY ASC`, no
AUTOINCREMENT, shown as six hex digits), so the next id is the highest one plus one. Deleting
the newest entry, or all of history, in Mainsail or Fluidd, or setting the printer's Pi up again,
makes the next print reuse an id the bridge has already logged. Keyed by the id alone, that print
was taken for a replay and never logged, or, once the old job had left the state file, logged to
the old job's print in 3D Print Log, which deduplicates on the external ID. So a job's key is
`<job_id>@<start, whole seconds>`, and its print's external ID is `<instance>:<key>`.

Bridges up to 0.2.1 keyed jobs by the bare id, and the state file and outbox of an install they
ran hold those keys, as do the external IDs of the prints they logged. Such a job keeps its bare
key, so it is neither logged again as a new print nor left unfinished:

- a job the state file holds, under its bare id or a key of this kind, with the same start
  (within `START_TOLERANCE_S`), keeps that key;
- a job the state file doesn't hold that started no later than the newest job it holds under a
  bare id may have been logged under the bare id, and keeps it. Only an older bridge writes bare
  keys, so a state file with none (a new install, or once they're all pruned) gets new keys only.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from printlog_bridge.core.state import PrinterState

SEPARATOR = "@"
START_TOLERANCE_S = 2.0
"""How far apart two starts of the same id may be and still be one job. Moonraker announces and
stores the same float, so this only absorbs rounding; a reused id comes at least a whole print
later."""


class JobIds:
    def __init__(self, state: PrinterState | None) -> None:
        """`state` is the printer's state file, the one the job logger keeps its jobs in. Without
        one, every job gets a key of the new kind."""
        self.state = state
        self._seen: dict[str, tuple[float, str]] = {}
        """The latest start and key of each history id this run, for a job the state file
        doesn't hold yet (its start couldn't be written, or the printer isn't logged)."""

    def key(self, job: Mapping[str, Any]) -> str:
        """The key of the history entry `job` (it needs `job_id` and `start_time`)."""
        history_id = str(job["job_id"])
        start = float(job["start_time"])
        seen = self._seen.get(history_id)
        if seen is not None and abs(seen[0] - start) <= START_TOLERANCE_S:
            return seen[1]
        key = self._known(history_id, start)
        if key is None:
            key = history_id if self._logged_bare(start) else _new_key(history_id, start)
        self._seen[history_id] = (start, key)
        return key

    def _known(self, history_id: str, start: float) -> str | None:
        """The key the state file holds this job under, if it does."""
        if self.state is None:
            return None
        best: tuple[float, str] | None = None
        for key, record in self.state.jobs.items():
            if key != history_id and not key.startswith(history_id + SEPARATOR):
                continue
            apart = _timestamp(record.started_at)
            if apart is None:
                continue
            distance = abs(apart - start)
            if distance <= START_TOLERANCE_S and (best is None or distance < best[0]):
                best = (distance, key)
        return best[1] if best is not None else None

    def _logged_bare(self, start: float) -> bool:
        """Whether a job not in the state file may have been logged by an older bridge, under
        its bare id: it started no later than the newest job the file holds under a bare id."""
        if self.state is None:
            return False
        bare = [
            _timestamp(record.started_at)
            for key, record in self.state.jobs.items()
            if SEPARATOR not in key
        ]
        newest = max((t for t in bare if t is not None), default=None)
        return newest is not None and start <= newest + START_TOLERANCE_S


def _new_key(history_id: str, start: float) -> str:
    return f"{history_id}{SEPARATOR}{int(start)}"


def _timestamp(iso: str) -> float | None:
    try:
        when = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
