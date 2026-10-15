"""The outbox: every job event that changes something in 3D Print Log, kept until it is sent.

A SQLite file in the state directory (`outbox.sqlite3`). The job logger writes each event here,
committed to disk, before it calls the API, and removes it only once the API has it, so a
power cut or an outage loses nothing. Sending an entry twice is safe: the print is created
with an external ID the API deduplicates on, and completing it sets the same values again.

Entries are kept per print (printer and job) in the order they were queued, and a print's next
entry is only offered once the one before it is gone, so a print is never completed before it
is created. Entries older than `MAX_AGE` are dropped.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

OUTBOX_FILE = "outbox.sqlite3"
MAX_AGE = timedelta(days=30)
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    printer TEXT NOT NULL,
    job_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE (printer, job_id, kind)
)
"""
_COLUMNS = "seq, printer, job_id, kind, payload, queued_at, attempts, last_error"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Entry:
    seq: int
    printer: str
    job_id: str
    kind: str
    """`start`, `finish`, or a picture to add: `thumbnail` or `snapshot`."""
    payload: dict[str, Any]
    queued_at: datetime
    attempts: int
    last_error: str | None


@dataclass(frozen=True)
class Waiting:
    """One printer's part of the outbox, for `printlog-bridge status` and the heartbeat."""

    jobs: int
    entries: int
    """Events and pictures waiting, across those jobs."""
    since: datetime
    """When the oldest entry was queued."""
    last_error: str | None
    """Why the last failed attempt failed, if any did."""


class Outbox:
    def __init__(self, connection: sqlite3.Connection, clock: Callable[[], datetime]) -> None:
        self._db = connection
        self.clock = clock

    @classmethod
    def open(cls, state_dir: Path, clock: Callable[[], datetime] = utc_now) -> Outbox:
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / OUTBOX_FILE
        try:
            return cls(_connect(path), clock)
        except sqlite3.DatabaseError as e:
            broken = path.with_name(OUTBOX_FILE + ".broken")
            os.replace(path, broken)
            log.warning(
                "%s was unreadable (%s), so it was moved to %s and a new outbox started. Jobs "
                "the bridge hasn't finished logging are replayed from the printer's history.",
                path, e, broken,
            )  # fmt: skip
            return cls(_connect(path), clock)

    def close(self) -> None:
        self._db.close()

    def add(self, printer: str, job_id: str, kind: str, payload: dict[str, Any]) -> bool:
        """Queues an event, committed before it returns. False if it was already queued."""
        with self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO entries (printer, job_id, kind, payload, queued_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (printer, job_id, kind, json.dumps(payload), self.clock().isoformat()),
            )
        return cursor.rowcount == 1

    def heads(self) -> list[Entry]:
        """The oldest entry of each print, oldest first: the only ones that may be sent now."""
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM entries WHERE seq IN "
            "(SELECT MIN(seq) FROM entries GROUP BY printer, job_id) ORDER BY seq"
        ).fetchall()
        return [_entry(row) for row in rows]

    def entries(self) -> list[Entry]:
        """Every entry, oldest first."""
        rows = self._db.execute(f"SELECT {_COLUMNS} FROM entries ORDER BY seq").fetchall()
        return [_entry(row) for row in rows]

    def remove(self, seq: int) -> None:
        with self._db:
            self._db.execute("DELETE FROM entries WHERE seq = ?", (seq,))

    def failed(self, seq: int, error: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE entries SET attempts = attempts + 1, last_error = ? WHERE seq = ?",
                (error, seq),
            )

    def drop_expired(self) -> list[Entry]:
        """Removes and returns the entries queued more than `MAX_AGE` ago."""
        cutoff = self.clock() - MAX_AGE
        expired = [e for e in self.entries() if e.queued_at < cutoff]
        if expired:
            with self._db:
                self._db.executemany(
                    "DELETE FROM entries WHERE seq = ?", [(e.seq,) for e in expired]
                )
        return expired


def read_entries(state_dir: Path) -> list[Entry] | None:
    """Every entry, oldest first, read without writing anything (or creating the file).

    None when there is no outbox, or it can't be read."""
    path = state_dir / OUTBOX_FILE
    if not path.exists():
        return None
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = db.execute(f"SELECT {_COLUMNS} FROM entries ORDER BY seq").fetchall()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return [_entry(row) for row in rows]


def read_waiting(state_dir: Path) -> dict[str, Waiting]:
    """What each printer has waiting, read without writing anything (or creating the file)."""
    by_printer: dict[str, list[Entry]] = {}
    for entry in read_entries(state_dir) or []:
        by_printer.setdefault(entry.printer, []).append(entry)
    waiting = {}
    for printer, entries in by_printer.items():
        errors = [e.last_error for e in entries if e.last_error]
        waiting[printer] = Waiting(
            jobs=len({e.job_id for e in entries}),
            entries=len(entries),
            since=min(e.queued_at for e in entries),
            last_error=errors[-1] if errors else None,
        )
    return waiting


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    try:
        # The default rollback journal with synchronous=FULL: a commit is on disk when it returns.
        db.execute("PRAGMA synchronous = FULL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            log.warning(
                "%s was written by a newer printlog-bridge (schema %s); using it as is",
                path, version,
            )  # fmt: skip
        with db:
            db.execute(_SCHEMA)
            if version < SCHEMA_VERSION:
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        db.close()
        raise
    return db


def _entry(row: tuple[Any, ...]) -> Entry:
    seq, printer, job_id, kind, payload, queued_at, attempts, last_error = row
    return Entry(
        seq=seq,
        printer=printer,
        job_id=job_id,
        kind=kind,
        payload=json.loads(payload),
        queued_at=datetime.fromisoformat(queued_at),
        attempts=attempts,
        last_error=last_error,
    )
