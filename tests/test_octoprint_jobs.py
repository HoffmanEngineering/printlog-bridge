"""The OctoPrint job tracker on its own, fed statuses and events as the socket brings them.

OctoPrint 1.11.8 keeps the print chart's marks for `temperature.cutoff` minutes (30 by default,
`printer/standard.py`'s `DataHistory`): each new mark drops those older than that. So a print
over 30 minutes loses its start mark at the next pause, resume, done or cancel, and
`progress.printTime` leaves out time paused (`comm.py`). These tests drop marks as that does.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from printlog_bridge.adapters.octoprint.adapter import _work
from printlog_bridge.adapters.octoprint.files import from_analysis
from printlog_bridge.adapters.octoprint.jobs import JobTracker
from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.state import MemoryState, PrinterState

SOCKET = Path(__file__).parent / "fixtures" / "octoprint" / "socket.jsonl"
T0 = 1793151018.0
"""OctoPrint's clock: when the print starts."""


class Sink:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)

    def of(self, kind):
        return [e for e in self.events if isinstance(e, kind)]


class Files:
    async def read(self, job):
        return from_analysis(job)


class Clock:
    """The bridge's clock, `skew` seconds behind OctoPrint's."""

    def __init__(self, skew: float = 0.0) -> None:
        self.now = T0
        self.skew = skew

    def __call__(self) -> datetime:
        return datetime.fromtimestamp(self.now - self.skew, timezone.utc)


def status(
    t: float,
    *,
    state: str = "Printing",
    completion: float | None = None,
    print_time: float | None = None,
    marks: list | None = None,
    path: str = "a.gcode",
    error: bool = False,
    closed: bool = False,
    filepos: float | None = None,
) -> dict:
    flags = {
        "printing": state in ("Starting", "Printing", "Finishing", "Cancelling"),
        "paused": state == "Paused",
        "finishing": state == "Finishing",
        "cancelling": state == "Cancelling",
        "operational": not error,
        "error": error,
        "closedOrError": error or closed,
    }
    return {
        "state": {"text": state, "flags": flags},
        "job": {
            "file": {"origin": "local", "path": path, "display": path, "name": path},
            "estimatedPrintTime": 7200,
            "filament": {"tool0": {"length": 10000.0, "volume": 24.0}},
        },
        "progress": {"completion": completion, "printTime": print_time, "filepos": filepos},
        "serverTime": t,
        "markings": marks or [],
    }


def tracker(state=None, clock=None) -> tuple[JobTracker, Sink]:
    sink = Sink()
    t = JobTracker(
        "p", sink, CurrentJob(state or MemoryState()), files=Files(), clock=clock or Clock()
    )
    t.connected()
    return t, sink


START = {"type": "print", "label": "Start", "time": T0}


def test_the_recorded_socket_with_the_start_mark_dropped_by_the_done_mark():
    """A print over 30 minutes: the Done mark's append drops its Start mark, and a status in
    Finishing (`printing` still true) comes after `PrintDone`. One job, not two."""
    lines = SOCKET.read_text(encoding="utf-8").splitlines()
    messages = [json.loads(line)["msg"] for line in lines if '"msg"' in line]
    first_start = 1793151018.7954702
    done = False
    pruned = []
    for message in messages:
        if done and ("current" in message or "history" in message):
            key = "current" if "current" in message else "history"
            inner = dict(message[key])
            inner["markings"] = [m for m in inner["markings"] if m["time"] != first_start]
            message = {key: inner}
        if message.get("event", {}).get("type") == "PrintDone":
            done = True
        pruned.append(message)
    t, sink = tracker()

    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        for message in [*pruned, None]:
            queue.put_nowait(message)
        await _work(queue, t)

    asyncio.run(scenario())
    assert len(sink.of(JobStarted)) == 2
    done_job, cancelled = sink.of(JobFinished)
    assert done_job.outcome is JobOutcome.COMPLETED and done_job.note is None
    assert cancelled.outcome is JobOutcome.CANCELLED


def test_a_status_still_finishing_after_print_done_starts_nothing():
    t, sink = tracker()

    async def scenario():
        await t.on_status(status(T0 + 5, completion=1.0, print_time=5, marks=[START]))
        await t.on_status(status(T0 + 2400, completion=100.0, print_time=2400, marks=[START]))
        await t.on_event("PrintDone", {"path": "a.gcode", "time": 2400.0})
        later = [{"type": "done", "time": T0 + 2401}]  # the start mark is gone
        await t.on_status(status(T0 + 2401, state="Finishing", completion=100.0, marks=later))
        stale = status(T0 + 2402, completion=100.0, print_time=2402, marks=later)
        await t.on_status(stale)  # `printing` without `finishing`: still the ended job
        await t.on_status(status(T0 + 2403, state="Operational", marks=later))

    asyncio.run(scenario())
    assert len(sink.of(JobStarted)) == 1
    [finished] = sink.of(JobFinished)
    assert finished.outcome is JobOutcome.COMPLETED


def test_the_same_file_started_again_is_a_new_job():
    t, sink = tracker()

    async def scenario():
        await t.on_status(status(T0 + 5, completion=1.0, print_time=5, marks=[START]))
        await t.on_event("PrintDone", {"path": "a.gcode", "time": 60.0})
        await t.on_status(status(T0 + 61, state="Operational", marks=[START]))
        await t.on_event("PrintStarted", {"path": "a.gcode"})
        again = [START, {"type": "done", "time": T0 + 60}, {"type": "print", "time": T0 + 70}]
        await t.on_status(status(T0 + 71, completion=0.5, print_time=1, marks=again))

    asyncio.run(scenario())
    first, second = sink.of(JobStarted)
    assert first.job_id != second.job_id
    assert second.started_at.timestamp() == T0 + 70


def pause_after_30_minutes(t: JobTracker, *, between=None):
    """A print paused at 40 minutes for 5: the pause mark drops the start mark, and
    `printTime` leaves the pause out. `between(t)` runs while it's paused."""

    async def scenario():
        await t.on_status(status(T0 + 60, completion=1.0, print_time=60, marks=[START]))
        await t.on_status(status(T0 + 2400, completion=40.0, print_time=2400, marks=[START]))
        pause = [{"type": "pause", "time": T0 + 2400}]
        await t.on_status(
            status(T0 + 2700, state="Paused", completion=40.0, print_time=2400, marks=pause)
        )
        if between is not None:
            t2 = await between(t)
        else:
            t2 = t
        resume = [*pause, {"type": "resume", "time": T0 + 2700}]
        await t2.on_status(status(T0 + 2760, completion=41.0, print_time=2460, marks=resume))
        await t2.on_event("PrintDone", {"path": "a.gcode", "time": 7500.0})
        return t2

    return scenario()


def test_a_pause_after_30_minutes_keeps_the_job():
    t, sink = tracker()
    asyncio.run(pause_after_30_minutes(t))
    [start] = sink.of(JobStarted)
    [finished] = sink.of(JobFinished)
    assert finished.job_id == start.job_id and finished.outcome is JobOutcome.COMPLETED


def test_a_pause_after_30_minutes_keeps_the_job_across_a_reconnect():
    t, sink = tracker()

    async def reconnect(t):
        t.connected()
        pause = [{"type": "pause", "time": T0 + 2400}]
        await t.on_status(
            status(T0 + 2750, state="Paused", completion=40.0, print_time=2400, marks=pause)
        )
        return t

    asyncio.run(pause_after_30_minutes(t, between=reconnect))
    assert {s.job_id for s in sink.of(JobStarted)} == {sink.of(JobStarted)[0].job_id}
    [finished] = sink.of(JobFinished)
    assert finished.outcome is JobOutcome.COMPLETED


def test_a_pause_after_30_minutes_keeps_the_job_across_a_bridge_restart(tmp_path):
    state = PrinterState.load(tmp_path, "p")
    t, sink = tracker(state)

    async def restart(_old):
        t2 = JobTracker(
            "p", sink, CurrentJob(PrinterState.load(tmp_path, "p")), files=Files(), clock=Clock()
        )
        t2.connected()
        pause = [{"type": "pause", "time": T0 + 2400}]
        await t2.on_status(
            status(T0 + 2750, state="Paused", completion=40.0, print_time=2400, marks=pause)
        )
        return t2

    asyncio.run(pause_after_30_minutes(t, between=restart))
    assert len({s.job_id for s in sink.of(JobStarted)}) == 1
    [finished] = sink.of(JobFinished)
    assert finished.outcome is JobOutcome.COMPLETED


@pytest.mark.parametrize("restart_bridge", [False, True])
def test_a_print_that_died_with_octoprint_is_cancelled_at_its_progress(tmp_path, restart_bridge):
    """OctoPrint restarted (or lost power) at 20%: its marks went with it."""
    state = PrinterState.load(tmp_path, "p")
    t, sink = tracker(state)

    async def scenario():
        await t.on_status(status(T0 + 60, completion=1.0, print_time=60, marks=[START]))
        await t.on_status(status(T0 + 600, completion=20.0, print_time=600, marks=[START]))
        if restart_bridge:
            state.save()
            t2, _ = tracker(PrinterState.load(tmp_path, "p"))
            t2.sink = sink
        else:
            t2 = t
            t2.connected()
        connected = [{"type": "connected", "time": T0 + 690}]
        await t2.on_status(status(T0 + 700, state="Operational", marks=connected))

    asyncio.run(scenario())
    [finished] = sink.of(JobFinished)
    assert finished.outcome is JobOutcome.CANCELLED
    assert finished.filament_mm == {0: 2000.0}
    assert finished.usage_estimated is True
    assert "didn't see this job end" in finished.note


def test_a_printer_in_error_after_a_missed_end_is_a_failure():
    t, sink = tracker()

    async def scenario():
        await t.on_status(status(T0 + 60, completion=30.0, print_time=60, marks=[START]))
        t.connected()
        await t.on_status(status(T0 + 900, state="Offline after error", error=True))

    asyncio.run(scenario())
    [finished] = sink.of(JobFinished)
    assert finished.outcome is JobOutcome.FAILED


def test_a_cancel_found_from_the_marks_charges_what_was_printed():
    t, sink = tracker()

    async def scenario():
        await t.on_status(status(T0 + 60, completion=1.0, print_time=60, marks=[START]))
        await t.on_status(status(T0 + 900, completion=25.0, print_time=900, marks=[START]))
        t.connected()
        marks = [START, {"type": "cancel", "time": T0 + 1000}]
        await t.on_status(status(T0 + 1100, state="Operational", marks=marks))

    asyncio.run(scenario())
    [finished] = sink.of(JobFinished)
    assert finished.outcome is JobOutcome.CANCELLED
    assert finished.filament_mm == {0: 2500.0}
    assert finished.finished_at.timestamp() == T0 + 1000


@pytest.mark.parametrize("skew", [0.0, 3600.0, -3600.0])
def test_times_are_on_one_clock(skew):
    """The bridge's clock off by an hour from OctoPrint's: the end is never before the start,
    and the end less the start is the duration."""
    clock = Clock(skew)
    t, sink = tracker(clock=clock)

    async def scenario():
        clock.now = T0 + 60
        await t.on_status(status(T0 + 60, completion=1.0, print_time=60, marks=[START]))
        clock.now = T0 + 300
        await t.on_status(status(T0 + 300, completion=10.0, print_time=300, marks=[START]))
        await t.on_event("PrintCancelled", {"path": "a.gcode", "progress": 10})

    asyncio.run(scenario())
    [start] = sink.of(JobStarted)
    [finished] = sink.of(JobFinished)
    assert finished.finished_at >= start.started_at
    elapsed = (finished.finished_at - start.started_at).total_seconds()
    assert elapsed == pytest.approx(finished.duration_s) == pytest.approx(300.0)
    assert start.started_at.timestamp() == T0


@pytest.mark.parametrize("event", ["PrintDone", "PrintCancelled"])
def test_a_long_pause_does_not_end_the_print_early(event):
    """OctoPrint's event `time` leaves out the time paused (comm.py's getPrintTime): the end is
    when the event came, on OctoPrint's clock, not the start plus that."""
    clock = Clock()
    t, sink = tracker(clock=clock)

    async def scenario():
        clock.now = T0 + 60
        await t.on_status(status(T0 + 60, completion=1.0, print_time=60, marks=[START]))
        clock.now = T0 + 11000
        pause = [START, {"type": "pause", "time": T0 + 600}]
        await t.on_status(
            status(T0 + 11000, state="Paused", completion=50.0, print_time=10400, marks=pause)
        )
        clock.now = T0 + 11990
        resume = [*pause, {"type": "resume", "time": T0 + 11400}]
        await t.on_status(status(T0 + 11990, completion=99.0, print_time=1190, marks=resume))
        clock.now = T0 + 12000
        await t.on_event(event, {"path": "a.gcode", "time": 1200.0, "progress": 99})

    asyncio.run(scenario())
    [finished] = sink.of(JobFinished)
    assert finished.finished_at.timestamp() == pytest.approx(T0 + 12000)
    assert finished.duration_s == pytest.approx(12000)


AFTER_A = [START, {"type": "done", "label": "Done", "time": T0 + 600}]
B_MARK = {"type": "print", "label": "Start", "time": T0 + 700}


@pytest.mark.parametrize("path", ["b.gcode", "a.gcode"])
@pytest.mark.parametrize("first_state", ["Starting", "Printing"])
def test_a_new_print_before_its_start_mark_is_one_job(path, first_state):
    """While OctoPrint is starting a print, `printing` is already true but its start mark comes
    later (with `PrintStarted`): the marks still show the last print, started and done."""
    clock = Clock()
    t, sink = tracker(clock=clock)

    async def scenario():
        clock.now = T0 + 60
        await t.on_status(status(T0 + 60, completion=1.0, print_time=60, marks=[START]))
        clock.now = T0 + 600
        await t.on_event("PrintDone", {"path": "a.gcode", "time": 540.0})
        await t.on_status(status(T0 + 601, state="Operational", marks=AFTER_A))
        clock.now = T0 + 700
        await t.on_event("PrintStarted", {"path": path})
        await t.on_status(
            status(T0 + 700, state=first_state, completion=0.0, print_time=0, marks=AFTER_A,
                   path=path)
        )  # fmt: skip
        clock.now = T0 + 702
        await t.on_status(
            status(T0 + 702, completion=0.1, print_time=2, marks=[*AFTER_A, B_MARK], path=path)
        )
        clock.now = T0 + 900
        await t.on_status(
            status(T0 + 900, completion=10.0, print_time=200, marks=[*AFTER_A, B_MARK], path=path)
        )

    asyncio.run(scenario())
    first, second = sink.of(JobStarted)
    assert first.started_at.timestamp() == T0
    assert second.file_name == path and second.job_id != first.job_id
    assert abs(second.started_at.timestamp() - (T0 + 700)) <= 2
    [finished] = sink.of(JobFinished)  # a's, and nothing made up for b
    assert finished.job_id == first.job_id


def test_octoprint_restarted_under_a_print_is_not_a_failure():
    """After a restart OctoPrint has no printer connection yet: Offline, with `closedOrError`
    but not `error` (standard.py's is_closed_or_error). That's no error of the print's."""
    t, sink = tracker()

    async def scenario():
        await t.on_status(status(T0 + 60, completion=1.0, print_time=60, marks=[START]))
        await t.on_status(status(T0 + 600, completion=20.0, print_time=600, marks=[START]))
        t.connected()
        await t.on_status(status(T0 + 700, state="Offline", closed=True))

    asyncio.run(scenario())
    [finished] = sink.of(JobFinished)
    assert finished.outcome is JobOutcome.CANCELLED
    assert finished.filament_mm == {0: 2000.0}
    assert "error" not in finished.note


def test_an_error_event_seen_makes_a_closed_printer_a_failure():
    t, sink = tracker()

    async def scenario():
        await t.on_status(status(T0 + 60, completion=30.0, print_time=60, marks=[START]))
        await t.on_event("Error", {"error": "Heater decoupled"})
        await t.on_status(status(T0 + 90, state="Offline", closed=True))

    asyncio.run(scenario())
    [finished] = sink.of(JobFinished)
    assert finished.outcome is JobOutcome.FAILED and "Heater decoupled" in finished.note


def test_the_same_file_reprinted_while_the_bridge_was_away_is_a_new_job(tmp_path):
    """The kept job reached byte 50,000; the bridge comes back to the same file at byte 1,000,
    with no start mark left to tell (a pause more than 30 minutes after it started)."""
    state = PrinterState.load(tmp_path, "p")
    t, sink = tracker(state)

    async def scenario():
        await t.on_status(
            status(T0 + 60, completion=1.0, print_time=60, marks=[START], filepos=600)
        )
        await t.on_status(
            status(T0 + 3000, completion=50.0, print_time=3000, marks=[START], filepos=50_000)
        )
        state.save()
        t2, _ = tracker(PrinterState.load(tmp_path, "p"))
        t2.sink = sink
        pause = [{"type": "pause", "time": T0 + 9000}]
        await t2.on_status(
            status(T0 + 9500, state="Paused", completion=1.0, print_time=3000, marks=pause,
                   filepos=1_000)
        )  # fmt: skip

    asyncio.run(scenario())
    first, again, second = sink.of(JobStarted)
    assert again.job_id == first.job_id and second.job_id != first.job_id
    [finished] = sink.of(JobFinished)
    assert finished.job_id == first.job_id
    assert finished.outcome is JobOutcome.CANCELLED and finished.filament_mm == {0: 5000.0}


def test_a_restart_keeps_the_gcode_span(tmp_path):
    """After a bridge restart a cancel is still charged by the G-code sent, not by bytes."""
    from printlog_bridge.adapters.common.slicer_meta import JobFile

    class SpanFiles:
        async def read(self, job):
            return JobFile(mm={0: 10000.0}, gcode_span=(200_000, 300_000))

    state = PrinterState.load(tmp_path, "p")
    sink = Sink()
    t = JobTracker("p", sink, CurrentJob(state), files=SpanFiles(), clock=Clock())
    t.connected()

    async def scenario():
        await t.on_status(
            status(T0 + 60, completion=50.0, print_time=60, marks=[START], filepos=150_000)
        )
        state.save()
        t2 = JobTracker(
            "p", sink, CurrentJob(PrinterState.load(tmp_path, "p")), files=SpanFiles(),
            clock=Clock(),
        )  # fmt: skip
        t2.connected()
        await t2.on_status(
            status(T0 + 600, completion=62.5, print_time=600, marks=[START], filepos=250_000)
        )
        await t2.on_event("PrintCancelled", {"path": "a.gcode"})

    asyncio.run(scenario())
    [finished] = sink.of(JobFinished)
    assert finished.filament_mm == {0: 5000.0}


def test_progress_leaves_out_the_header_and_thumbnails():
    """OctoPrint's `completion` counts the file's bytes, thumbnails and settings included: a
    cancel is charged by how much of the G-code itself was sent."""
    from printlog_bridge.adapters.common.slicer_meta import JobFile

    class SpanFiles:
        async def read(self, job):
            return JobFile(mm={0: 10000.0}, gcode_span=(200_000, 300_000))

    sink = Sink()
    t = JobTracker("p", sink, CurrentJob(MemoryState()), files=SpanFiles(), clock=Clock())
    t.connected()

    async def scenario():
        first = status(T0 + 60, completion=50.0, print_time=60, marks=[START])
        first["progress"]["filepos"] = 150_000  # still in the thumbnails
        await t.on_status(first)
        later = status(T0 + 600, completion=62.5, print_time=600, marks=[START])
        later["progress"]["filepos"] = 250_000  # half the G-code
        await t.on_status(later)
        await t.on_event("PrintCancelled", {"path": "a.gcode"})

    asyncio.run(scenario())
    [finished] = sink.of(JobFinished)
    assert finished.filament_mm == {0: 5000.0}


def test_the_gcode_span_of_a_file():
    from printlog_bridge.adapters.octoprint.files import gcode_span

    head = b"; generated by PrusaSlicer\n; thumbnail begin 1x1 4\n; AAAA\n; thumbnail end\n\nG28\n"
    body = b"G1 X1 E1\n" * 10
    tail_text = b"; filament used [mm] = 10\n; prusaslicer_config = end\n"
    whole = head + body + tail_text
    assert gcode_span(whole, b"", 0) == (len(head) - 4, len(head) + len(body))
    start = len(whole) - 40
    assert gcode_span(head, whole[start:], start) is None  # the tail shows no G-code
    start = len(head) + 5
    assert gcode_span(head, whole[start:], start) == (len(head) - 4, len(head) + len(body))
