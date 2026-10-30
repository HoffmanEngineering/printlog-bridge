"""The FlashForge job tracker on its own, fed `/detail` answers as `tests/fake_flashforge.py`
builds them (from community sources; none recorded yet, #88)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fake_flashforge import idle_detail

from printlog_bridge.adapters.flashforge.jobs import KEY, JobTracker
from printlog_bridge.adapters.flashforge.protocol import filament_by_slot, view_of
from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.events import (
    ImageKind,
    JobFinished,
    JobImage,
    JobOutcome,
    JobStarted,
    SpoolReading,
)
from printlog_bridge.core.state import MemoryState, PrinterState

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
FILE = "Benchy.gcode"


class Sink:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)

    def of(self, kind):
        return [e for e in self.events if isinstance(e, kind)]


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Files:
    def __init__(self, by_slot: dict[int, float] | None = None) -> None:
        self.by_slot = by_slot or {}
        self.asked: list[str] = []

    async def grams_by_slot(self, path: str) -> dict[int, float]:
        self.asked.append(path)
        return dict(self.by_slot)

    async def thumbnail(self, path: str) -> bytes | None:
        return b"\x89PNG\r\n\x1a\nthumb"


def detail(state: str = "printing", *, path: str = FILE, progress: float = 0.1,
           duration: float = 360, grams: float = 12.5, mm: float = 4130.0,
           model: str = "5M", **more) -> dict:  # fmt: skip
    d = idle_detail(model)
    d.update(
        status=state,
        printFileName=path,
        printProgress=progress,
        printDuration=duration,
        estimatedTime=3240,
        estimatedRightWeight=grams,
        estimatedRightLen=mm,
        **more,
    )
    return d


def tracker(state=None, files=None, clock=None, spools=None):
    sink = Sink()
    clock = clock or Clock()
    t = JobTracker(
        "ad5m", sink, CurrentJob(state or MemoryState(), clock=clock), files=files,
        clock=clock, spools=spools,
    )  # fmt: skip
    t.connected()
    return t, sink, clock


def run(coro):
    return asyncio.run(coro)


class TestView:
    def test_reads_the_status(self):
        view = view_of(detail(progress=0.42, layer=None))
        assert view.state == "printing" and view.under_way
        assert view.progress == 42.0
        assert view.duration_s == 360 and view.remaining_s == 3240
        assert (view.estimated_g, view.estimated_mm) == (12.5, 4130.0)
        assert view.model == "Adventurer 5M" and view.firmware == "2.7.5"

    def test_an_idle_printer_has_no_job(self):
        view = view_of(idle_detail())
        assert view.state == "ready" and view.path is None and not view.under_way

    def test_the_ad5x_ifs_slots(self):
        view = view_of(idle_detail("AD5X"))
        assert view.model == "AD5X" and view.slot_count == 4
        assert [(s.slot, s.loaded, s.material, s.color_hex) for s in view.slots] == [
            (0, True, "PLA", "#FFFFFF"),
            (1, True, "PETG", "#1A1A1A"),
            (2, False, None, None),
            (3, True, "PLA", "#FF0000"),
        ]

    def test_grams_by_slot_from_the_file_list(self):
        answer = {
            "gcodeListDetail": [
                {"gcodeFileName": "Other.gcode", "gcodeToolDatas": []},
                {
                    "gcodeFileName": "Two color.3mf",
                    "gcodeToolDatas": [
                        {"toolId": 0, "slotId": 1, "filamentWeight": 8.0},
                        {"toolId": 1, "slotId": 3, "filamentWeight": 2.5},
                    ],
                },
            ]
        }
        assert filament_by_slot(answer, "/data/Two color.3mf") == {0: 8.0, 2: 2.5}
        assert filament_by_slot(answer, "Missing.gcode") == {}
        assert filament_by_slot({"gcodeList": ["a"]}, "a") == {}


class TestLifecycle:
    def test_a_print_from_start_to_finish(self):
        t, sink, clock = tracker(files=Files())

        async def scenario():
            await t.on_detail(idle_detail())
            await t.on_detail(detail(progress=0.1, duration=360))
            clock.advance(3000)
            await t.on_detail(detail("paused", progress=0.8, duration=3000))
            await t.on_detail(detail(progress=0.95, duration=3300))
            await t.on_detail(detail("completed", progress=1.0, duration=3600))
            await t.on_detail(detail("completed", progress=1.0, duration=3600))
            await t.on_detail(idle_detail())

        run(scenario())
        [start] = sink.of(JobStarted)
        assert start.file_name == FILE
        assert start.started_at == T0 - timedelta(seconds=360)
        assert start.job_id == f"{int(start.started_at.timestamp())}-Benchy"
        assert start.estimated_filament_g == 12.5 and start.estimated_filament_mm == 4130.0
        assert start.estimated_duration_s == 3600
        [image] = sink.of(JobImage)
        assert image.kind is ImageKind.THUMBNAIL
        [end] = sink.of(JobFinished)
        assert end.outcome is JobOutcome.COMPLETED
        assert end.filament_g == {0: 12.5} and end.filament_mm == {0: 4130.0}
        assert end.usage_estimated is True
        assert t.job is None

    def test_a_cancel_is_charged_by_progress(self):
        t, sink, _ = tracker()

        async def scenario():
            await t.on_detail(detail(progress=0.4))
            await t.on_detail(detail("cancel", path="", progress=0.4))

        run(scenario())
        [end] = sink.of(JobFinished)
        assert end.outcome is JobOutcome.CANCELLED
        assert end.filament_g == {0: 5.0} and end.filament_mm == {0: 1652.0}
        assert end.usage_estimated is True

    def test_an_error_fails_it_with_the_code(self):
        t, sink, _ = tracker()

        async def scenario():
            await t.on_detail(detail(progress=0.2))
            await t.on_detail(detail("error", progress=0.2, errorCode="E0015"))

        run(scenario())
        [end] = sink.of(JobFinished)
        assert end.outcome is JobOutcome.FAILED and "E0015" in end.note

    def test_heating_and_busy_carry_the_job(self):
        t, sink, _ = tracker()

        async def scenario():
            await t.on_detail(detail(progress=0.2))
            await t.on_detail(detail("heating", path="", progress=0.2))
            await t.on_detail(detail("busy", progress=0.2))
            await t.on_detail(detail(progress=0.3, duration=500))

        run(scenario())
        assert len(sink.of(JobStarted)) == 1 and sink.of(JobFinished) == []

    def test_heating_before_the_first_layer_starts_nothing(self):
        t, sink, _ = tracker()
        run(t.on_detail(detail("heating", progress=0.0, duration=0)))
        assert sink.events == [] and t.active

    def test_the_estimate_is_taken_once_the_printer_has_it(self):
        t, sink, _ = tracker()

        async def scenario():
            await t.on_detail(detail(progress=0.01, grams=0.0, mm=0.0))
            await t.on_detail(detail(progress=0.5, grams=20.0, mm=6600.0))
            await t.on_detail(detail("completed", progress=1.0))

        run(scenario())
        [end] = sink.of(JobFinished)
        assert end.filament_g == {0: 20.0} and end.filament_mm == {0: 6600.0}

    def test_the_same_file_again_is_another_job(self):
        t, sink, clock = tracker()

        async def scenario():
            await t.on_detail(detail(progress=0.6, duration=2000))
            clock.advance(60)
            await t.on_detail(detail(progress=0.05, duration=100))

        run(scenario())
        assert len(sink.of(JobStarted)) == 2
        [end] = sink.of(JobFinished)
        assert end.outcome is JobOutcome.CANCELLED

    def test_another_file_ends_the_last_job_by_its_progress(self):
        t, sink, _ = tracker()

        async def scenario():
            await t.on_detail(detail(progress=0.995))
            await t.on_detail(detail(path="Next.gcode", progress=0.01, duration=20))

        run(scenario())
        [end] = sink.of(JobFinished)
        assert end.outcome is JobOutcome.COMPLETED and "didn't see" in end.note
        assert [s.file_name for s in sink.of(JobStarted)] == [FILE, "Next.gcode"]

    def test_a_late_status_after_the_end_starts_nothing(self):
        t, sink, _ = tracker()

        async def scenario():
            await t.on_detail(detail(progress=0.99, duration=3500))
            await t.on_detail(detail("completed", progress=1.0, duration=3600))
            await t.on_detail(detail(progress=1.0, duration=3600))
            await t.on_detail(detail("completed", progress=1.0, duration=3600))

        run(scenario())
        assert len(sink.of(JobStarted)) == 1 and len(sink.of(JobFinished)) == 1

    def test_the_end_state_at_connect_with_no_job_starts_nothing(self):
        t, sink, _ = tracker()
        run(t.on_detail(detail("completed", progress=1.0)))
        assert sink.events == []


class TestRestarts:
    def test_a_job_that_carried_on_while_the_bridge_was_away(self, tmp_path):
        state = PrinterState.load(tmp_path, "ad5m")
        t, sink, clock = tracker(state)
        run(t.on_detail(detail(progress=0.3, duration=1000)))
        assert KEY in state.discovery
        [start] = sink.of(JobStarted)

        again, sink2, _ = tracker(PrinterState.load(tmp_path, "ad5m"), clock=clock)
        clock.advance(600)
        run(again.on_detail(detail(progress=0.5, duration=1600)))
        assert sink2.of(JobStarted) == [start]  # the same start again, recognized by the sink
        run(again.on_detail(detail("completed", progress=1.0, duration=3600)))
        [end] = sink2.of(JobFinished)
        assert end.outcome is JobOutcome.COMPLETED and end.filament_g == {0: 12.5}

    def test_an_end_while_the_bridge_was_away_is_read_from_the_status(self, tmp_path):
        state = PrinterState.load(tmp_path, "ad5m")
        t, _, clock = tracker(state)
        run(t.on_detail(detail(progress=0.3)))

        again, sink, _ = tracker(PrinterState.load(tmp_path, "ad5m"), clock=clock)
        clock.advance(3600)
        run(again.on_detail(detail("cancel", progress=0.3)))
        [end] = sink.of(JobFinished)
        assert end.outcome is JobOutcome.CANCELLED
        assert end.finished_at == T0  # when the bridge last saw it
        assert "wasn't connected" in end.note
        assert end.filament_g == {0: 3.75}

    def test_an_idle_printer_after_a_restart_ends_it_by_progress(self, tmp_path):
        state = PrinterState.load(tmp_path, "ad5m")
        t, _, clock = tracker(state)
        run(t.on_detail(detail(progress=0.5)))

        again, sink, _ = tracker(PrinterState.load(tmp_path, "ad5m"), clock=clock)
        run(again.on_detail(idle_detail()))
        [end] = sink.of(JobFinished)
        assert end.outcome is JobOutcome.CANCELLED and "didn't see" in end.note
        assert KEY not in again.current.state.discovery
        run(again.on_detail(idle_detail()))
        assert len(sink.of(JobFinished)) == 1

    def test_a_reconnect_carries_on_with_the_same_job(self):
        t, sink, _ = tracker()

        async def scenario():
            await t.on_detail(detail(progress=0.2))
            t.connected()
            await t.on_detail(detail(progress=0.4, duration=1500))
            await t.on_detail(detail("completed", progress=1.0))

        run(scenario())
        assert len(sink.of(JobStarted)) == 1
        [end] = sink.of(JobFinished)
        assert end.outcome is JobOutcome.COMPLETED


class TestAd5x:
    def test_a_multicolor_print_splits_the_grams_by_slot(self):
        t, sink, _ = tracker(files=Files({0: 8.0, 2: 2.0}))

        async def scenario():
            await t.on_detail(detail(model="AD5X", progress=0.1, grams=10.0))
            await t.on_detail(detail("cancel", model="AD5X", progress=0.5, grams=10.0))

        run(scenario())
        [start] = sink.of(JobStarted)
        assert start.estimated_filament_g_by_slot == {0: 8.0, 2: 2.0}
        [end] = sink.of(JobFinished)
        assert end.filament_g == {0: 4.0, 2: 1.0}
        assert end.filament_mm == {}  # a length per slot isn't known
        assert end.usage_estimated is True

    def test_a_one_slot_print_goes_to_its_slot(self):
        t, sink, _ = tracker(files=Files({3: 9.0}))

        async def scenario():
            await t.on_detail(detail(model="AD5X", progress=0.1, grams=9.0, mm=3000.0))
            await t.on_detail(detail("completed", model="AD5X", progress=1.0, grams=9.0))

        run(scenario())
        [end] = sink.of(JobFinished)
        assert end.filament_g == {3: 9.0} and end.filament_mm == {3: 3000.0}

    def test_slots_are_reported_when_they_change(self):
        readings: list[SpoolReading] = []

        async def spools(reading):
            readings.append(reading)

        t, _, _ = tracker(spools=spools)

        async def scenario():
            await t.on_detail(idle_detail("AD5X"))
            await t.on_detail(idle_detail("AD5X"))
            changed = idle_detail("AD5X")
            changed["matlStationInfo"]["slotInfos"][1].update(hasFilament=False)
            await t.on_detail(changed)
            await asyncio.sleep(0)

        run(scenario())
        assert [(r.slot, r.material, r.color_hex, r.slot_count) for r in readings] == [
            (0, "PLA", "#FFFFFF", 4),
            (1, "PETG", "#1A1A1A", 4),
            (3, "PLA", "#FF0000", 4),
            (1, None, None, 4),  # taken out: forgets the slot's lookup
        ]
