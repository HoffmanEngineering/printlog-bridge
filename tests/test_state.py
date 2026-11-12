from __future__ import annotations

import logging

from printlog_bridge.core.state import KEEP_FINISHED_JOBS, JobRecord, PrinterState


def finished_job(n: int) -> JobRecord:
    return JobRecord(
        started_at=f"2026-10-{1 + n // 24:02d}T{n % 24:02d}:00:00+00:00", finished=True
    )


def test_keeps_its_instance_id_and_jobs_across_restarts(tmp_path):
    state = PrinterState.load(tmp_path, "voron")
    state.jobs["00000A"] = JobRecord(started_at="2026-10-10T09:30:00+00:00", print_id=9)
    state.save()

    again = PrinterState.load(tmp_path, "voron")

    assert again.instance_id == state.instance_id
    assert again.jobs == state.jobs


def test_keeps_only_the_latest_finished_jobs_but_every_unfinished_one(tmp_path):
    state = PrinterState.load(tmp_path, "voron")
    for n in range(KEEP_FINISHED_JOBS + 5):
        state.jobs[f"job{n}"] = finished_job(n)
    state.jobs["running"] = JobRecord(started_at="2026-09-01T00:00:00+00:00")
    state.save()

    kept = PrinterState.load(tmp_path, "voron").jobs

    assert len(kept) == KEEP_FINISHED_JOBS + 1
    assert "running" in kept
    assert "job0" not in kept
    assert f"job{KEEP_FINISHED_JOBS + 4}" in kept


def test_a_printer_name_is_made_safe_for_a_file_name(tmp_path):
    state = PrinterState.load(tmp_path, "../my voron")

    assert state.path.parent == tmp_path / "printers"
    assert state.path.name == ".._my_voron.json"


def test_an_unreadable_file_is_set_aside_and_the_bridge_starts_afresh(tmp_path, caplog):
    path = PrinterState.load(tmp_path, "voron").path
    path.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
        state = PrinterState.load(tmp_path, "voron")

    assert state.jobs == {}
    assert path.with_suffix(".broken").read_text(encoding="utf-8") == "{not json"
    assert "may be logged again" in caplog.text


def test_the_resume_point_is_the_earliest_unfinished_job(tmp_path):
    state = PrinterState.load(tmp_path, "voron")
    for n in range(1, KEEP_FINISHED_JOBS):
        state.jobs[f"job{n}"] = finished_job(n)
    state.jobs["stuck"] = JobRecord(started_at=finished_job(0).started_at)

    assert state.resume_after().isoformat() == finished_job(0).started_at


def test_a_job_unfinished_behind_a_full_set_of_finished_ones_no_longer_holds_the_resume_point(
    tmp_path,
):
    """Its start or end was refused for good: held there, a reconnect would replay jobs the
    state file has pruned, as new ones."""
    state = PrinterState.load(tmp_path, "voron")
    state.jobs["stuck"] = JobRecord(started_at=finished_job(0).started_at)
    for n in range(1, KEEP_FINISHED_JOBS + 2):
        state.jobs[f"job{n}"] = finished_job(n)
    state.jobs["running"] = JobRecord(started_at=finished_job(KEEP_FINISHED_JOBS + 5).started_at)
    state.save()

    state = PrinterState.load(tmp_path, "voron")

    assert "stuck" in state.jobs  # kept: its outbox entries may still need it
    assert state.resume_after().isoformat() == finished_job(KEEP_FINISHED_JOBS + 5).started_at
    del state.jobs["running"]
    assert state.resume_after().isoformat() == finished_job(KEEP_FINISHED_JOBS + 1).started_at
