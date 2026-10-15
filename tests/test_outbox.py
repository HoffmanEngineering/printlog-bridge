"""The outbox on its own: what it keeps, in what order, and what survives a restart."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from printlog_bridge.core.outbox import MAX_AGE, OUTBOX_FILE, Outbox, read_waiting

NOW = datetime(2026, 10, 10, 9, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def test_an_entry_survives_a_restart(tmp_path):
    outbox = Outbox.open(tmp_path)
    outbox.add("voron", "00000A", "start", {"file_name": "benchy.gcode"})
    outbox.close()  # or the power goes

    again = Outbox.open(tmp_path)
    [entry] = again.heads()
    again.close()

    assert (entry.printer, entry.job_id, entry.kind) == ("voron", "00000A", "start")
    assert entry.payload == {"file_name": "benchy.gcode"}
    assert entry.attempts == 0


def test_a_sent_entry_leaves_nothing_of_itself_in_the_file(tmp_path):
    """Slicer settings (#36) stay on the printer only until they're sent: SQLite would otherwise
    keep a deleted row's bytes in a free page, as some builds do by default."""
    outbox = Outbox.open(tmp_path)
    outbox.add("voron", "00000A", "slicer_settings", {"configBlock": "; a = CANARY-1\n" * 2000})
    outbox.add("voron", "00000B", "slicer_settings", {"configBlock": "; b = CANARY-2\n"})
    for entry in outbox.entries():
        outbox.remove(entry.seq)
    outbox.close()

    data = (tmp_path / OUTBOX_FILE).read_bytes()
    assert b"CANARY" not in data


def test_the_same_event_is_queued_once(tmp_path):
    outbox = Outbox.open(tmp_path)

    first = outbox.add("voron", "00000A", "start", {"n": 1})
    replayed = outbox.add("voron", "00000A", "start", {"n": 2})

    assert (first, replayed) == (True, False)
    [entry] = outbox.heads()
    assert entry.payload == {"n": 1}


def test_a_prints_next_entry_waits_for_the_one_before_it(tmp_path):
    outbox = Outbox.open(tmp_path)
    outbox.add("voron", "A", "start", {})
    outbox.add("voron", "B", "start", {})
    outbox.add("voron", "A", "finish", {})

    heads = [(e.job_id, e.kind) for e in outbox.heads()]
    assert heads == [("A", "start"), ("B", "start")]

    outbox.remove(outbox.heads()[0].seq)

    heads = [(e.job_id, e.kind) for e in outbox.heads()]
    assert heads == [("B", "start"), ("A", "finish")]  # still in the order they were queued


def test_prints_on_different_printers_are_kept_apart(tmp_path):
    outbox = Outbox.open(tmp_path)
    outbox.add("voron", "A", "start", {})
    outbox.add("u1", "A", "start", {})

    assert [e.printer for e in outbox.heads()] == ["voron", "u1"]


def test_a_failed_attempt_is_counted_and_remembered(tmp_path):
    outbox = Outbox.open(tmp_path)
    outbox.add("voron", "A", "start", {})
    seq = outbox.heads()[0].seq

    outbox.failed(seq, "the API answered 503")
    outbox.failed(seq, "can't reach the API")
    outbox.close()

    [entry] = Outbox.open(tmp_path).heads()
    assert entry.attempts == 2
    assert entry.last_error == "can't reach the API"


def test_entries_older_than_30_days_are_dropped(tmp_path):
    clock = Clock()
    outbox = Outbox.open(tmp_path, clock=clock)
    outbox.add("voron", "old", "start", {})
    clock.now += timedelta(days=20)
    outbox.add("voron", "new", "start", {})
    clock.now = NOW + MAX_AGE + timedelta(seconds=1)

    dropped = outbox.drop_expired()

    assert [e.job_id for e in dropped] == ["old"]
    assert [e.job_id for e in outbox.heads()] == ["new"]


def test_waiting_jobs_are_counted_per_printer_without_opening_for_writing(tmp_path):
    clock = Clock()
    outbox = Outbox.open(tmp_path, clock=clock)
    outbox.add("voron", "A", "start", {})
    outbox.add("voron", "A", "finish", {})
    clock.now += timedelta(minutes=5)
    outbox.add("voron", "B", "start", {})
    outbox.add("u1", "C", "start", {})
    outbox.failed(outbox.heads()[0].seq, "the API answered 503")
    outbox.close()

    waiting = read_waiting(tmp_path)

    assert waiting["voron"].jobs == 2
    assert waiting["voron"].since == NOW
    assert waiting["voron"].last_error == "the API answered 503"
    assert waiting["u1"].jobs == 1


def test_reading_waiting_jobs_never_creates_the_outbox(tmp_path):
    assert read_waiting(tmp_path) == {}
    assert not (tmp_path / OUTBOX_FILE).exists()


def test_an_unreadable_outbox_is_set_aside_and_a_new_one_started(tmp_path, caplog):
    (tmp_path / OUTBOX_FILE).write_bytes(b"this is not a database" * 100)

    with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
        outbox = Outbox.open(tmp_path)

    assert outbox.heads() == []
    assert (tmp_path / (OUTBOX_FILE + ".broken")).exists()
    assert "replayed" in caplog.text


def test_each_change_is_committed_before_it_returns(tmp_path):
    outbox = Outbox.open(tmp_path)
    outbox.add("voron", "A", "start", {})

    # Another connection, as after a power cut, sees it without the outbox being closed.
    other = sqlite3.connect(str(tmp_path / OUTBOX_FILE))
    try:
        assert other.execute("SELECT COUNT(*) FROM entries").fetchone() == (1,)
    finally:
        other.close()
        outbox.close()
