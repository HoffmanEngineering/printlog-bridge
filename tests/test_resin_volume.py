"""Resin usage in milliliters (#97): the vat is slot 0, and its usage is always the slicer's
estimate, sent by volume (`source: 3`). Prints from filament printers stay exactly as they were.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fake_printlog_api import FakePrintLogApi
from test_job_logger import STARTED_AT, by_slot, finished, run, started

from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.lifecycle import _finish_event, _finish_payload, _usage_rows
from printlog_bridge.core.outbox import Entry
from printlog_bridge.core.state import PrinterState

VOLUME = 3


def resin_start(**kwargs) -> JobStarted:
    kwargs.setdefault("file_name", "miniature_bust.ctb")
    return started(estimated_volume_ml=40.0, **kwargs)


def resin_end(volume_ml: float = 42.5, **kwargs) -> JobFinished:
    return finished(filament_mm={}, volume_ml={0: volume_ml}, **kwargs)


class TestResinEstimate:
    def test_the_slicers_volume_is_the_estimate_row_on_the_vat(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, resin_start())

        [body] = api.created
        assert body["filamentUsage"] == [
            {"slot": 0, "estimatedVolumeMl": 40.0, "estimatedSource": VOLUME}
        ]
        assert "estimatedFilamentUsageMg" not in body

    def test_the_bottle_loaded_on_the_printer_is_linked(self, tmp_path):
        api = FakePrintLogApi()
        bottle = api.add_spool()
        api.user_loads(42, None, bottle)

        run(api, tmp_path, resin_start(), spools=True)

        [body] = api.created
        assert body["filamentUsage"] == [
            {
                "slot": 0,
                "estimatedVolumeMl": 40.0,
                "estimatedSource": VOLUME,
                "filament": {"id": bottle},
            }
        ]

    def test_the_estimate_survives_a_restart(self, tmp_path):
        api = FakePrintLogApi()
        api.offline = True
        run(api, tmp_path, resin_start(), timeout=0.1)
        api.offline = False

        run(api, tmp_path)

        [row] = api.prints[1]["filamentUsage"]
        assert (row["estimatedVolumeMl"], row["estimatedSource"]) == (40.0, VOLUME)


class TestResinUsage:
    def test_volume_alone_is_a_usage_row_marked_estimated(self, tmp_path):
        """A slot with only a volume isn't "no usage", and resin is always an estimate."""
        api = FakePrintLogApi()

        run(api, tmp_path, resin_start(), resin_end())

        [completion] = api.completions
        assert completion["filamentUsage"] == [
            {"slot": 0, "volumeMl": 42.5, "source": VOLUME, "actualIsEstimate": True}
        ]
        [row] = api.prints[1]["filamentUsage"]
        assert (row["volumeMl"], row["source"], row["actualIsEstimate"]) == (42.5, VOLUME, True)

    def test_the_loaded_bottle_is_charged(self, tmp_path):
        api = FakePrintLogApi()
        bottle = api.add_spool()
        api.user_loads(42, None, bottle)

        run(api, tmp_path, resin_start(), resin_end(usage_estimated=True), spools=True)

        [completion] = api.completions
        assert completion["filamentUsage"] == [
            {
                "slot": 0,
                "volumeMl": 42.5,
                "source": VOLUME,
                "actualIsEstimate": True,
                "filamentId": bottle,
            }
        ]

    def test_no_volume_and_nothing_else_sends_no_rows(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(file_name="calibration.ctb"), resin_end(0.0))

        [completion] = api.completions
        assert "filamentUsage" not in completion
        assert api.prints[1]["status"] == 3

    def test_volume_survives_a_restart(self, tmp_path):
        api = FakePrintLogApi()
        run(api, tmp_path, resin_start())
        api.offline = True
        run(api, tmp_path, resin_end(), timeout=0.1)
        api.offline = False

        run(api, tmp_path)

        [completion] = api.completions
        assert completion["filamentUsage"] == [
            {"slot": 0, "volumeMl": 42.5, "source": VOLUME, "actualIsEstimate": True}
        ]

    def test_volume_is_rounded_to_a_hundredth(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, resin_start(), resin_end(12.345678))

        [completion] = api.completions
        assert completion["filamentUsage"][0]["volumeMl"] == 12.35


class TestResinUsageRows:
    def test_a_bottle_swapped_mid_print_splits_by_progress(self):
        end = JobFinished(
            "elegoo", "1", JobOutcome.COMPLETED, datetime(2026, 10, 14, tzinfo=timezone.utc),
            volume_ml={0: 50.0},
        )  # fmt: skip
        swaps = [{"slot": 0, "fed_mm": 30.0, "old": "a", "new": "b", "progress": 0.4}]

        usage = _usage_rows(end, [], {0: "b"}, True, swaps)

        assert usage == [
            {"slot": 0, "volumeMl": 20.0, "source": VOLUME, "actualIsEstimate": True,
             "filamentId": "a"},
            {"slot": 0, "volumeMl": 30.0, "source": VOLUME, "actualIsEstimate": True,
             "filamentId": "b"},
        ]  # fmt: skip

    def test_one_bottle_on_two_slots_adds_its_volumes(self):
        end = JobFinished(
            "x", "1", JobOutcome.COMPLETED, datetime(2026, 10, 14, tzinfo=timezone.utc),
            volume_ml={0: 10.0, 1: 2.5},
        )  # fmt: skip
        rows = [{"slot": 0, "filament": {"id": "a"}}, {"slot": 1, "filament": {"id": "a"}}]

        usage = _usage_rows(end, rows, {})

        assert usage == [
            {"slot": 0, "volumeMl": 12.5, "source": VOLUME, "actualIsEstimate": True,
             "filamentId": "a"}
        ]  # fmt: skip


class TestFilamentPrintsAreUnchanged:
    """What a Moonraker, Bambu, PrusaLink or OctoPrint job sends, and what the bridge keeps of
    it, byte for byte as before #97."""

    def test_the_create_body_for_a_filament_job(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            started(estimated_duration_s=3720.0, estimated_filament_mm=4400.0,
                    estimated_filament_g=13.1),
        )  # fmt: skip

        [body] = api.created
        assert body["filamentUsage"] == [
            {"slot": 0, "estimatedSource": 2, "estimatedLengthInM": 4.4, "estimatedAmountMg": 13100}
        ]
        assert body["estimatedFilamentUsageMg"] == 13100
        assert body["estimatedPrintTimeInSeconds"] == 3720
        assert not any("olume" in key for key in json.dumps(body).split('"'))

    def test_the_completion_for_a_filament_job(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            started(),
            finished(filament_mm={0: 1000.0}, filament_g={0: 3.0, 1: 2.5}, usage_estimated=True),
        )

        [completion] = api.completions
        assert completion["filamentUsage"] == [
            {"slot": 0, "amountMg": 3000, "lengthInM": 1.0, "source": 1, "actualIsEstimate": True},
            {"slot": 1, "amountMg": 2500, "source": 1, "actualIsEstimate": True},
        ]

    def test_the_finish_entry_keeps_its_keys(self):
        payload = _finish_payload(finished(filament_g={0: 3.0}, usage_estimated=True))

        assert list(payload) == [
            "outcome", "finished_at", "duration_s", "filament_mm", "filament_g",
            "usage_estimated", "note",
        ]  # fmt: skip

    def test_the_start_kept_in_state_has_no_volume_key(self, tmp_path):
        api = FakePrintLogApi()

        logger = run(api, tmp_path, started(estimated_filament_mm=4400.0))

        start = logger.state("voron").jobs["00000A"].start
        assert "estimated_volume_ml" not in start

    def test_a_remembered_job_keeps_its_keys(self, tmp_path):
        job = CurrentJob(PrinterState.load(tmp_path, "mk4"))
        job.started(started(printer="mk4", estimated_filament_mm=4400.0))

        saved = PrinterState.load(tmp_path, "mk4").discovery["current_job"]
        assert "estimated_volume_ml" not in json.dumps(saved)


def finish_entry(payload: dict) -> Entry:
    return Entry(1, "voron", "00000A", "finish", payload, STARTED_AT, 0, None)


class TestEntriesFromTheCurrentRelease:
    def test_a_finish_entry_without_volume_reads_as_none(self):
        entry = finish_entry(
            {
                "outcome": "completed", "finished_at": STARTED_AT.isoformat(), "duration_s": 60.0,
                "filament_mm": {"0": 100.0}, "filament_g": {}, "usage_estimated": False,
                "note": None,
            }
        )  # fmt: skip

        event = _finish_event(entry)

        assert event.volume_ml == {}
        assert event.filament_mm == {0: 100.0}

    def test_a_finish_entry_with_volume_round_trips(self):
        event = resin_end()
        entry = finish_entry(json.loads(json.dumps(_finish_payload(event))))

        assert _finish_event(entry).volume_ml == {0: 42.5}


class TestRememberedResinJob:
    def test_a_resin_job_gone_while_away_is_charged_its_estimate(self, tmp_path):
        CurrentJob(PrinterState.load(tmp_path, "elegoo")).started(
            resin_start(printer="elegoo", job_id="job-1")
        )

        replayed, end = CurrentJob(PrinterState.load(tmp_path, "elegoo")).reconcile(None)

        assert replayed == resin_start(printer="elegoo", job_id="job-1")
        assert end.volume_ml == {0: 40.0}
        assert end.usage_estimated is True
        assert end.filament_mm == {} and end.filament_g == {}


def test_rows_for_resin_go_on_the_vat_only(tmp_path):
    api = FakePrintLogApi()

    run(api, tmp_path, resin_start(), resin_end())

    assert sorted(by_slot(api.prints[1])) == [0]
