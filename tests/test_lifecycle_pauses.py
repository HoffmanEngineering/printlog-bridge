"""Pauses on the print's timeline (api#343): the 30-second minimum, the outbox, the end."""

from __future__ import annotations

import asyncio
import contextlib
import functools
from datetime import timedelta

from fake_printlog_api import FakePrintLogApi
from test_job_logger import STARTED_AT, drain, finished, started
from test_job_logger import logging_to as _logging_to

from printlog_bridge.core.events import JobOutcome, JobPaused, JobResumed
from printlog_bridge.core.lifecycle import MIN_PAUSE, NO_TIMELINE_FOR

PAUSED_AT = STARTED_AT + timedelta(minutes=10)


def synced(test):
    """Runs an async test, as the rest of the suite runs its scenarios (`asyncio.run`)."""

    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


@contextlib.asynccontextmanager
async def logging_to(api, state_dir, **options):
    """A JobLogger sending to `api`, closed at the end like a bridge that stops."""
    async with _logging_to(api, state_dir, **options) as logger:
        try:
            yield logger
        finally:
            logger.close()


class Clock:
    """The bridge's clock, set by the test."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def paused(at=PAUSED_AT, job_id="00000A"):
    return JobPaused(printer="voron", job_id=job_id, at=at)


def resumed(after: timedelta, job_id="00000A"):
    return JobResumed(printer="voron", job_id=job_id, at=PAUSED_AT + after)


def sent(api: FakePrintLogApi):
    return [(e["type"], e["occurredAt"]) for _, events in api.timeline_posts for e in events]


@synced
async def test_a_pause_shorter_than_the_minimum_sends_nothing(tmp_path):
    api = FakePrintLogApi()
    async with logging_to(api, tmp_path, clock=Clock(PAUSED_AT)) as logger:
        for event in (started(), paused(), resumed(MIN_PAUSE - timedelta(seconds=1))):
            await logger.emit(event)
        await drain(logger)

    assert sent(api) == []


@synced
async def test_a_long_pause_sends_its_start_and_its_resume(tmp_path):
    api = FakePrintLogApi()
    async with logging_to(api, tmp_path, clock=Clock(PAUSED_AT)) as logger:
        for event in (started(), paused(), resumed(timedelta(minutes=6))):
            await logger.emit(event)
        await drain(logger)

    assert sent(api) == [
        ("pause", PAUSED_AT.isoformat()),
        ("resume", (PAUSED_AT + timedelta(minutes=6)).isoformat()),
    ]
    keys = [e["key"] for _, events in api.timeline_posts for e in events]
    assert all(":00000A:" in key for key in keys)
    assert len(set(keys)) == 2


@synced
async def test_a_pause_still_open_after_the_minimum_is_sent_without_waiting_for_its_resume(
    tmp_path,
):
    api = FakePrintLogApi()
    clock = Clock(PAUSED_AT)
    async with logging_to(api, tmp_path, clock=clock) as logger:
        await logger.emit(started())
        await logger.emit(paused())
        await drain(logger)
        assert sent(api) == []

        clock.now = PAUSED_AT + MIN_PAUSE
        await drain(logger)

    assert sent(api) == [("pause", PAUSED_AT.isoformat())]


@synced
async def test_a_pause_open_across_a_restart_keeps_its_start(tmp_path):
    api = FakePrintLogApi()
    async with logging_to(api, tmp_path, clock=Clock(PAUSED_AT)) as logger:
        await logger.emit(started())
        await logger.emit(paused())
        await drain(logger)

    api2 = FakePrintLogApi()
    api2.prints = api.prints
    async with logging_to(api2, tmp_path, clock=Clock(PAUSED_AT + timedelta(minutes=8))) as logger:
        # The adapter sees the job paused again after the restart, a little later.
        await logger.emit(paused(at=PAUSED_AT + timedelta(minutes=7)))
        await logger.emit(resumed(timedelta(minutes=8)))
        await drain(logger)

    assert sent(api2) == [
        ("pause", PAUSED_AT.isoformat()),
        ("resume", (PAUSED_AT + timedelta(minutes=8)).isoformat()),
    ]


@synced
async def test_a_job_ending_while_paused_sends_the_pause_and_adds_it_to_a_klipper_duration(
    tmp_path,
):
    api = FakePrintLogApi()
    end = PAUSED_AT + timedelta(minutes=5)
    async with logging_to(api, tmp_path, clock=Clock(PAUSED_AT)) as logger:
        for event in (
            started(),
            paused(),
            finished(finished_at=end, duration_s=1000, duration_excludes_pauses=True),
        ):
            await logger.emit(event)
        await drain(logger)

    assert sent(api) == [("pause", PAUSED_AT.isoformat())]
    completion = api.completions[-1]
    assert completion["printTimeInSeconds"] == 1000 + 300


@synced
async def test_paused_time_is_not_added_to_a_duration_that_already_includes_it(tmp_path):
    api = FakePrintLogApi()
    async with logging_to(api, tmp_path, clock=Clock(PAUSED_AT)) as logger:
        for event in (
            started(),
            paused(),
            resumed(timedelta(minutes=2)),
            finished(duration_s=3725.4),
        ):
            await logger.emit(event)
        await drain(logger)

    assert api.completions[-1]["printTimeInSeconds"] == 3725


@synced
async def test_a_failed_job_sends_its_failure_code(tmp_path):
    api = FakePrintLogApi()
    async with logging_to(api, tmp_path) as logger:
        for event in (
            started(),
            finished(
                outcome=JobOutcome.FAILED,
                failure_code="klippy_shutdown",
                failure_reason="Klipper shutdown",
            ),
        ):
            await logger.emit(event)
        await drain(logger)

    assert api.completions[-1]["failure"] == {
        "code": "klippy_shutdown",
        "message": "Klipper shutdown",
    }


@synced
async def test_a_completed_job_sends_no_failure(tmp_path):
    api = FakePrintLogApi()
    async with logging_to(api, tmp_path) as logger:
        for event in (started(), finished(failure_code="x")):
            await logger.emit(event)
        await drain(logger)

    assert "failure" not in api.completions[-1]


@synced
async def test_an_api_without_the_endpoint_stops_pauses_for_a_day_and_the_print_still_ends(
    tmp_path,
):
    api = FakePrintLogApi()
    api.timeline_route = False
    clock = Clock(PAUSED_AT)
    async with logging_to(api, tmp_path, clock=clock) as logger:
        for event in (started(), paused(), resumed(timedelta(minutes=2))):
            await logger.emit(event)
        await drain(logger)
        api.timeline_route = True
        clock.now = PAUSED_AT + NO_TIMELINE_FOR - timedelta(minutes=1)
        for event in (
            paused(at=PAUSED_AT + timedelta(minutes=5)),
            resumed(timedelta(minutes=7)),
            finished(),
        ):
            await logger.emit(event)
        await drain(logger)

    assert sent(api) == []
    assert len(api.completions) == 1


@synced
async def test_a_pause_for_a_job_never_started_is_ignored(tmp_path):
    api = FakePrintLogApi()
    async with logging_to(api, tmp_path, clock=Clock(PAUSED_AT)) as logger:
        await logger.emit(paused(job_id="unknown"))
        await logger.emit(resumed(timedelta(minutes=5), job_id="unknown"))
        await drain(logger)

    assert sent(api) == []
