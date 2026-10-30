"""`adapters/anycubic/jobs.py` on its own: reports straight into the tracker, for the orders of
reports a reconnect, a restart and a resin end can bring."""

from __future__ import annotations

import asyncio

from fake_anycubic import job_block, resin_block
from test_prusalink_adapter import Sink

from printlog_bridge.adapters.anycubic.jobs import UNSEEN_END_NOTE, JobTracker
from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.state import MemoryState


def info(project):
    return {"type": "info", "data": {"state": "busy" if project else "free", "project": project}}


def partial_print(**fields):
    return {"type": "print", "state": "done", "data": fields}


def resin_print(state, **fields):
    return {"type": "print", "state": state, "data": resin_block(**fields)}


def idle():
    return {"type": "status", "state": "idle", "action": "free"}


def test_a_partial_print_report_first_after_a_reconnect_decides_nothing():
    sink = Sink()

    async def scenario():
        tracker = JobTracker("s1", sink, CurrentJob(MemoryState()))
        tracker.connected()
        await tracker.on_message("info", info(job_block(progress=40, curr_layer=60)))
        tracker.connected()  # a reconnect: a print report without the file comes first
        await tracker.on_message("print", partial_print(progress=41, curr_layer=61))
        await tracker.on_message("info", info(job_block(progress=41, curr_layer=61)))

    asyncio.run(scenario())
    assert [type(e) for e in sink.events] == [JobStarted]


def test_a_partial_print_report_first_after_a_restart_decides_nothing():
    state = MemoryState()
    first, second = Sink(), Sink()

    async def scenario():
        tracker = JobTracker("s1", first, CurrentJob(state))
        tracker.connected()
        await tracker.on_message("info", info(job_block(progress=40, curr_layer=60)))
        restarted = JobTracker("s1", second, CurrentJob(state))  # the bridge restarted
        restarted.connected()
        await restarted.on_message("print", partial_print(progress=42, curr_layer=62))
        await restarted.on_message("info", info(job_block(progress=42, curr_layer=62)))
        await restarted.on_message(
            "info", info(job_block(state="finished", print_status=2, progress=100))
        )

    asyncio.run(scenario())
    [start] = first.of(JobStarted)
    assert [e.job_id for e in second.of(JobStarted)] == [start.job_id]
    [done] = second.of(JobFinished)
    assert done.outcome is JobOutcome.COMPLETED and done.note is None


def test_a_resin_idle_at_the_last_layer_is_the_end():
    sink = Sink()

    async def scenario():
        tracker = JobTracker("p1", sink, CurrentJob(MemoryState()), resin=True)
        tracker.connected()
        await tracker.on_message("print", resin_print("printing", curr_layer=10, progress=3))
        await tracker.on_message("print", resin_print("printing", curr_layer=292, progress=100))
        await tracker.on_message("status", idle())  # before the `finished` print report

    asyncio.run(scenario())
    [done] = sink.of(JobFinished)
    assert done.outcome is JobOutcome.COMPLETED and done.note is None
    assert done.volume_ml == {0: 19.25}


def test_a_resin_idle_mid_print_waits_for_the_print_report():
    sink = Sink()

    async def scenario():
        tracker = JobTracker("p1", sink, CurrentJob(MemoryState()), resin=True)
        tracker.connected()
        await tracker.on_message("print", resin_print("printing", curr_layer=100, progress=34))
        await tracker.on_message("status", idle())
        assert not sink.of(JobFinished)
        await tracker.on_message("print", resin_print("failed", curr_layer=100, progress=34))

    asyncio.run(scenario())
    [done] = sink.of(JobFinished)
    assert done.outcome is JobOutcome.FAILED


def test_a_resin_idle_mid_print_without_a_print_report_ends_by_progress():
    sink = Sink()

    async def scenario():
        tracker = JobTracker("p1", sink, CurrentJob(MemoryState()), resin=True)
        tracker.connected()
        await tracker.on_message("print", resin_print("printing", curr_layer=100, progress=34))
        await tracker.on_message("status", idle())
        await tracker.on_message("status", idle())  # the next query: still idle, no report

    asyncio.run(scenario())
    [done] = sink.of(JobFinished)
    assert done.outcome is JobOutcome.CANCELLED and done.note == UNSEEN_END_NOTE
