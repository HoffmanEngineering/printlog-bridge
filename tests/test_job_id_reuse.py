"""Moonraker hands a job id out again, and the bridge must still log each job as its own print.

Moonraker's history table declares `job_id INTEGER PRIMARY KEY ASC` with no AUTOINCREMENT, so
the next id is max(rowid)+1: deleting the newest history entry, or all of history, in Mainsail or
Fluidd (or re-imaging the printer's Pi) makes the next print reuse an id the bridge already
logged. FakeMoonraker numbers jobs the same way (`len(jobs) + 1`).

Bridges up to 0.2.1 keyed a job by that id alone, so the state file and the outbox of a running
install hold bare ids, and its prints carry them in their `externalId`. The upgrade tests start
from what such a bridge wrote: the same JobLogger, fed the bare ids its Moonraker adapter
reported (`old_bridge`).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from pathlib import Path

import bridge_helpers as helpers
from bridge_helpers import write_config
from fake_moonraker import FakeMoonraker, eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.adapters.moonraker import jobs as moonraker_jobs
from printlog_bridge.adapters.moonraker.job_ids import START_TOLERANCE_S, JobIds
from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.events import JobStarted
from printlog_bridge.core.lifecycle import JobLogger, LoggedPrinter
from printlog_bridge.core.state import KEEP_FINISHED_JOBS, JobRecord, PrinterState

T0 = 1_791_000_000.0  # 2026-10-03, on the printer's clock
HOUR = 3600.0


@contextlib.asynccontextmanager
async def fakes():
    moonraker, api = FakeMoonraker(), FakePrintLogApi()
    moonraker_url, api_url = await moonraker.start(), await api.start()
    try:
        yield moonraker, moonraker_url, api, api_url
    finally:
        await moonraker.close()
        await api.close()


@contextlib.asynccontextmanager
async def bridge(config_path: Path, moonraker: FakeMoonraker):
    async with helpers.bridge(
        config_path,
        connected=lambda: moonraker.client_count == 1,
        disconnected=lambda: moonraker.client_count == 0,
    ) as task:
        yield task


async def settle(seconds: float = 1.0) -> None:
    """Long enough for a connect's history replay, and anything it queues, to be sent."""
    await asyncio.sleep(seconds)


def writes(api: FakePrintLogApi) -> list[tuple[str, str]]:
    """The requests that create or change a print."""
    return [
        (method, path)
        for method, path in api.requests
        if method in ("POST", "PATCH") and path.startswith("/api/Prints")
    ]


def state_of(tmp_path: Path) -> PrinterState:
    return PrinterState.load(tmp_path / "state", "voron")


# -- what a bridge before job keys wrote -----------------------------------------------------------


def bare_start(job: dict) -> JobStarted:
    return moonraker_jobs.started("voron", job)  # keyed by the history id alone, as before


def bare_finish(job: dict):
    return moonraker_jobs.finished("voron", job)


async def old_bridge(
    tmp_path: Path, api: FakePrintLogApi, api_url: str, *events, drain_for: float = 1.0
) -> None:
    """Runs a JobLogger over the bridge's state directory with bare-id events, as a bridge up to
    0.2.1 did: its core was this one, and its Moonraker adapter reported `job["job_id"]`."""
    async with ApiClient(api_url, api.api_key) as client:
        logger = JobLogger(
            client,
            {"voron": LoggedPrinter(42, "moonraker")},
            tmp_path / "state",
            backoff=(0.01, 0.04),
        )
        try:
            for event in events:
                await logger.emit(event)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + drain_for
                while True:
                    wait = await logger.send_due()
                    if wait is None or loop.time() >= deadline:
                        break
                    await asyncio.sleep(min(wait, max(0.0, deadline - loop.time())))
        finally:
            logger.close()


# -- an id handed out again ------------------------------------------------------------------------


def test_a_print_after_deleting_the_newest_history_entry_is_logged(tmp_path):
    """The user deletes the last (say, cancelled) job from Mainsail's history; the next print
    gets the same job_id. It must be logged as its own print, and leave the first one alone."""

    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            async with bridge(write_config(tmp_path, moonraker_url, api_url), moonraker):
                first = await moonraker.start_job("test_cube.gcode", start_time=T0)
                await eventually(lambda: api.prints)
                await moonraker.finish_job(first, "cancelled")
                await eventually(lambda: api.prints[1]["status"] != 2)
                before = dict(api.prints[1])

                moonraker.jobs.remove(first)  # Mainsail: History > Delete
                second = await moonraker.start_job("real_part.gcode", start_time=T0 + HOUR)
                assert second["job_id"] == first["job_id"]
                await moonraker.finish_job(second, "completed", print_duration=7200.0)
                await eventually(lambda: len(api.prints) == 2 and api.prints[2]["status"] == 3)
                await settle(0.3)
            return before, dict(api.prints)

    before, prints = asyncio.run(scenario())

    assert [p["title"] for p in prints.values()] == ["Test Cube", "Real Part"]
    assert prints[1] == before
    assert prints[2]["printTimeInSeconds"] == 7200
    assert prints[1]["externalId"] != prints[2]["externalId"]


def test_a_print_after_clearing_history_does_not_overwrite_an_old_print(tmp_path):
    """After 21 prints the state file has pruned job 000001 (KEEP_FINISHED_JOBS = 20). The user
    clears Moonraker's history and ids start again at 000001. The new job must be a new print,
    and the print the first 000001 was logged as must not change."""

    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            async with bridge(write_config(tmp_path, moonraker_url, api_url), moonraker):
                for n in range(KEEP_FINISHED_JOBS + 1):
                    job = await moonraker.start_job(f"old_{n}.gcode", start_time=T0 + n * HOUR)
                    await eventually(lambda n=n: len(api.prints) == n + 1)
                    await moonraker.finish_job(job, "completed", print_duration=100.0 + n)
                    await eventually(lambda n=n: api.prints[n + 1]["status"] != 2)
                before = dict(api.prints[1])

                moonraker.jobs.clear()  # "Delete all" in Mainsail, or a re-imaged printer Pi
                new = await moonraker.start_job("brand_new.gcode", start_time=T0 + 30 * HOUR)
                assert new["job_id"] == "000001"
                await moonraker.finish_job(new, "cancelled", print_duration=5.0)
                await eventually(lambda: len(api.prints) == 22 and api.prints[22]["status"] == 4)
                await settle(0.3)
            return before, dict(api.prints)

    before, prints = asyncio.run(scenario())

    assert prints[1] == before
    assert prints[22]["title"] == "Brand New"
    assert prints[22]["printTimeInSeconds"] == 5


def test_a_replay_after_a_reconnect_stays_a_replay(tmp_path):
    """Same id, same start: the job Moonraker's history replays on every connect."""

    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            config = write_config(tmp_path, moonraker_url, api_url)
            async with bridge(config, moonraker):
                job = await moonraker.start_job("benchy.gcode", start_time=T0)
                await eventually(lambda: api.prints)
                await moonraker.finish_job(job)
                await eventually(lambda: api.prints[1]["status"] == 3)
                await settle(0.3)
                done = len(writes(api))
                await moonraker.drop()  # a Moonraker restart: the bridge reconnects and replays
                await eventually(lambda: moonraker.client_count == 1)
                await settle()
            async with bridge(config, moonraker):  # and a bridge restart
                await settle()
            return api, done

    api, done = asyncio.run(scenario())

    assert len(api.prints) == 1
    assert len(api.completions) == 1
    assert len(writes(api)) == done


def test_a_start_time_that_differs_slightly_between_event_and_history_is_the_same_job(tmp_path):
    """The websocket's `added` event and the history record are the same float in Moonraker, but
    the bridge must not depend on it: a difference across a whole second is still one job."""

    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            config = write_config(tmp_path, moonraker_url, api_url)
            async with bridge(config, moonraker):
                job = await moonraker.start_job(
                    "benchy.gcode", notify=False, start_time=T0 + 1.0001
                )
                announced = {**job, "start_time": T0 + 0.9998}  # the other side of a second
                await moonraker.notify(
                    "notify_history_changed", {"action": "added", "job": announced}
                )
                await eventually(lambda: api.prints)
                await moonraker.drop()
                await eventually(lambda: moonraker.client_count == 1)
                await settle(0.5)
                await moonraker.finish_job(job)  # announced with the history record's time
                await eventually(lambda: api.prints[1]["status"] == 3)
                await settle(0.3)
            return api

    api = asyncio.run(scenario())

    assert len(api.prints) == 1
    assert len(api.completions) == 1


# -- upgrading from bare ids -----------------------------------------------------------------------


def test_upgrade_with_finished_jobs_in_state_logs_none_of_them_again(tmp_path):
    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            old = []
            for n in range(3):
                job = await moonraker.start_job(
                    f"old_{n}.gcode", notify=False, start_time=T0 + n * HOUR
                )
                await moonraker.finish_job(job, notify=False)
                old.append(job)
            await old_bridge(
                tmp_path,
                api,
                api_url,
                *[e for job in old for e in (bare_start(job), bare_finish(job))],
            )
            assert [p["status"] for p in api.prints.values()] == [3, 3, 3]
            seeded = len(writes(api))

            async with bridge(write_config(tmp_path, moonraker_url, api_url), moonraker):
                await settle()
                replayed = len(writes(api))
                # And an id handed out again after the upgrade: the newest entry is deleted.
                moonraker.jobs.remove(old[-1])
                again = await moonraker.start_job("new.gcode", start_time=T0 + 10 * HOUR)
                await moonraker.finish_job(again, "cancelled")
                await eventually(lambda: len(api.prints) == 4 and api.prints[4]["status"] == 4)
            return api, seeded, replayed, again

    api, seeded, replayed, again = asyncio.run(scenario())
    instance = state_of(tmp_path).instance_id

    assert replayed == seeded  # nothing created or completed again
    assert [p["externalId"] for p in api.prints.values()] == [
        f"{instance}:000001",
        f"{instance}:000002",
        f"{instance}:000003",
        f"{instance}:000003@{int(T0 + 10 * HOUR)}",
    ]
    assert again["job_id"] == "000003"
    assert [p["status"] for p in api.prints.values()] == [3, 3, 3, 4]


def test_upgrade_with_a_job_running_finishes_its_print(tmp_path):
    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            job = await moonraker.start_job("long.gcode", notify=False, start_time=T0)
            await old_bridge(tmp_path, api, api_url, bare_start(job))
            assert api.prints[1]["status"] == 2

            async with bridge(write_config(tmp_path, moonraker_url, api_url), moonraker):
                await settle(0.5)
                await moonraker.finish_job(job, print_duration=5000.0)
                await eventually(lambda: api.prints[1]["status"] == 3)
                await settle(0.3)
            return api

    api = asyncio.run(scenario())

    assert len(api.prints) == 1
    assert api.prints[1]["printTimeInSeconds"] == 5000
    assert len(api.completions) == 1
    assert list(state_of(tmp_path).jobs) == ["000001"]


def test_upgrade_with_events_in_the_outbox_sends_them_under_their_old_id(tmp_path):
    """The old bridge queued a job's start and end while 3D Print Log was out of reach, and was
    upgraded before they went out."""

    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            job = await moonraker.start_job("offline.gcode", notify=False, start_time=T0)
            await moonraker.finish_job(job, notify=False, print_duration=900.0)
            api.offline = True
            await old_bridge(
                tmp_path, api, api_url, bare_start(job), bare_finish(job), drain_for=0.2
            )
            api.offline = False
            assert api.prints == {}

            async with bridge(write_config(tmp_path, moonraker_url, api_url), moonraker):
                await eventually(lambda: api.prints and api.prints[1]["status"] == 3)
                await settle()
            return api

    api = asyncio.run(scenario())
    instance = state_of(tmp_path).instance_id

    [print_] = api.prints.values()
    assert print_["externalId"] == f"{instance}:000001"
    assert print_["printTimeInSeconds"] == 900
    assert len(api.completions) == 1


def test_upgrade_never_duplicates_a_job_logged_before_but_gone_from_state(tmp_path):
    """The backstop: a job the old bridge logged, which its state file no longer holds, replayed
    after the upgrade (here, from an older unfinished job). It must keep the bare id its print
    was created with, so 3D Print Log finds that print rather than making a second one."""

    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            stuck = await moonraker.start_job("stuck.gcode", notify=False, start_time=T0)
            await moonraker.finish_job(stuck, notify=False)
            later = []
            for n in range(1, 6):
                job = await moonraker.start_job(
                    f"later_{n}.gcode", notify=False, start_time=T0 + n * HOUR
                )
                await moonraker.finish_job(job, notify=False)
                later.append(job)
            await old_bridge(
                tmp_path,
                api,
                api_url,
                bare_start(stuck),
                *[e for job in later for e in (bare_start(job), bare_finish(job))],
            )
            # The old bridge's state lost three of the finished jobs (pruned), and still has the
            # stuck one unfinished, so the next connect replays from it.
            state = state_of(tmp_path)
            for job in later[:3]:
                del state.jobs[job["job_id"]]
            state.save()
            count = len(api.prints)

            async with bridge(write_config(tmp_path, moonraker_url, api_url), moonraker):
                await eventually(lambda: api.prints[1]["status"] == 3)
                await settle()
            return api, count

    api, count = asyncio.run(scenario())
    instance = state_of(tmp_path).instance_id

    assert len(api.prints) == count == 6
    assert all("@" not in p["externalId"] for p in api.prints.values())
    assert {p["externalId"] for p in api.prints.values()} == {
        f"{instance}:{n:06X}" for n in range(1, 7)
    }


# -- a job left unfinished -------------------------------------------------------------------------


def test_a_job_whose_end_was_refused_stops_holding_the_replay_point(tmp_path):
    """Job 1's end is refused for good. Once KEEP_FINISHED_JOBS later jobs are logged, a
    reconnect must not replay the jobs the state file has pruned as new ones (thumbnail again,
    completed again)."""

    async def scenario():
        async with fakes() as (moonraker, moonraker_url, api, api_url):
            async with bridge(write_config(tmp_path, moonraker_url, api_url), moonraker):
                stuck = await moonraker.start_job("stuck.gcode", start_time=T0)
                await eventually(lambda: api.prints)
                api.refused_completions.add(1)
                await moonraker.finish_job(stuck)
                await eventually(lambda: api.completions)
                for n in range(1, KEEP_FINISHED_JOBS + 2):
                    job = await moonraker.start_job(f"later_{n}.gcode", start_time=T0 + n * HOUR)
                    await eventually(lambda n=n: len(api.prints) == n + 1)
                    await moonraker.finish_job(job)
                    await eventually(lambda n=n: api.prints[n + 1]["status"] == 3)
                await settle(0.3)
                done = (len(api.created), len(api.completions), len(writes(api)))
                await moonraker.drop()
                await eventually(lambda: moonraker.client_count == 1)
                await settle()
                after = (len(api.created), len(api.completions), len(writes(api)))
            return done, after, api

    done, after, api = asyncio.run(scenario())

    assert after == done
    assert len(api.prints) == KEEP_FINISHED_JOBS + 2


# -- the resolver ----------------------------------------------------------------------------------


def record(start: float, finished: bool = True) -> JobRecord:
    return JobRecord(
        started_at=datetime.fromtimestamp(start, timezone.utc).isoformat(), finished=finished
    )


def history(job_id: str, start: float) -> dict:
    return {"job_id": job_id, "start_time": start}


class TestJobIds:
    def test_a_job_is_keyed_by_its_id_and_its_start(self, tmp_path):
        ids = JobIds(PrinterState.load(tmp_path, "voron"))

        assert ids.key(history("000001", T0 + 0.75)) == f"000001@{int(T0)}"

    def test_without_a_state_file_every_job_gets_a_new_key(self):
        assert JobIds(None).key(history("00000A", T0)) == f"00000A@{int(T0)}"

    def test_a_job_in_state_keeps_its_key_within_the_tolerance(self, tmp_path):
        state = PrinterState.load(tmp_path, "voron")
        state.jobs[f"000001@{int(T0)}"] = record(T0 + 0.9999)

        ids = JobIds(state)

        assert ids.key(history("000001", T0 + 1.0001)) == f"000001@{int(T0)}"
        assert ids.key(history("000001", T0 + START_TOLERANCE_S + 2)) != f"000001@{int(T0)}"

    def test_a_bare_job_in_state_with_the_same_start_keeps_its_bare_key(self, tmp_path):
        state = PrinterState.load(tmp_path, "voron")
        state.jobs["000001"] = record(T0, finished=False)

        assert JobIds(state).key(history("000001", T0)) == "000001"

    def test_a_bare_job_in_state_with_another_start_is_another_job(self, tmp_path):
        state = PrinterState.load(tmp_path, "voron")
        state.jobs["000001"] = record(T0)

        assert JobIds(state).key(history("000001", T0 + HOUR)) == f"000001@{int(T0 + HOUR)}"

    def test_a_job_not_in_state_from_before_the_newest_bare_job_keeps_the_bare_id(self, tmp_path):
        state = PrinterState.load(tmp_path, "voron")
        state.jobs["000005"] = record(T0 + 5 * HOUR)
        state.jobs[f"000006@{int(T0 + 6 * HOUR)}"] = record(T0 + 6 * HOUR)
        ids = JobIds(state)

        assert ids.key(history("000003", T0 + 3 * HOUR)) == "000003"
        assert ids.key(history("000007", T0 + 7 * HOUR)) == f"000007@{int(T0 + 7 * HOUR)}"

    def test_the_same_job_gets_the_same_key_before_it_is_in_state(self, tmp_path):
        ids = JobIds(PrinterState.load(tmp_path, "voron"))

        first = ids.key(history("000001", T0 + 0.9999))

        assert ids.key(history("000001", T0 + 1.0001)) == first
