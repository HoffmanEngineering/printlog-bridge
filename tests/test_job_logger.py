from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fake_printlog_api import FakePrintLogApi

from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.lifecycle import JobLogger, LoggedPrinter

STARTED_AT = datetime(2026, 10, 10, 9, 30, tzinfo=timezone.utc)
FAST_RETRIES = (0.01, 0.01)


def started(job_id: str = "00000A", printer: str = "voron", **kwargs) -> JobStarted:
    kwargs.setdefault("file_name", "shelf_bracket_0.2mm_PLA_MK4_1h2m.gcode")
    kwargs.setdefault("started_at", STARTED_AT)
    return JobStarted(printer, job_id, **kwargs)


def finished(
    job_id: str = "00000A", outcome: JobOutcome = JobOutcome.COMPLETED, **kwargs
) -> JobFinished:
    kwargs.setdefault("printer", "voron")
    kwargs.setdefault("finished_at", STARTED_AT + timedelta(hours=1, minutes=5))
    kwargs.setdefault("duration_s", 3725.4)
    kwargs.setdefault("filament_mm", {0: 4321.5})
    return JobFinished(job_id=job_id, outcome=outcome, **kwargs)


@contextlib.asynccontextmanager
async def logging_to(api: FakePrintLogApi, state_dir: Path, **printers: int):
    base = await api.start()
    try:
        async with ApiClient(base, api.api_key) as client:
            yield JobLogger(
                client,
                {
                    name: LoggedPrinter(printer_id=printer_id, source="moonraker")
                    for name, printer_id in (printers or {"voron": 42}).items()
                },
                state_dir,
                retry_delays=FAST_RETRIES,
            )
    finally:
        await api.close()


def run(api: FakePrintLogApi, state_dir: Path, *events, **printers: int):
    """Emits `events` in order; a callable event is called with the API instead."""

    async def scenario():
        async with logging_to(api, state_dir, **printers) as logger:
            for event in events:
                if callable(event):
                    event(api)
                else:
                    await logger.emit(event)
            return logger

    return asyncio.run(scenario())


class TestJobStart:
    def test_creates_a_printing_print_for_the_configured_printer(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            started(
                file_name="parts/shelf_bracket_0.2mm_PLA_MK4_1h2m.gcode",
                estimated_duration_s=3900.6,
                estimated_filament_mm=4400.0,
                estimated_filament_g=13.27,
            ),
        )

        [print_] = api.prints.values()
        assert print_["printerId"] == 42
        assert print_["status"] == 2  # Printing
        assert print_["title"] == "Shelf Bracket"
        assert print_["fileName"] == "shelf_bracket_0.2mm_PLA_MK4_1h2m.gcode"
        assert print_["startDate"] == "2026-10-10T09:30:00+00:00"
        assert print_["estimatedPrintTimeInSeconds"] == 3901
        assert print_["estimatedFilamentUsageMg"] == 13270
        [row] = print_["filamentUsage"]
        assert row["slot"] == 0
        assert row["estimatedLengthInM"] == 4.4
        assert row["estimatedSource"] == 2  # Length

    def test_identifies_the_job_by_this_bridges_instance_and_moonrakers_job_id(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started("00000A"))
        run(api, tmp_path, started("00000B"))

        first, second = api.prints.values()
        assert first["externalSource"] == "moonraker"
        instance, job = first["externalId"].split(":")
        assert job == "00000A"
        assert second["externalId"] == f"{instance}:00000B"  # stable across restarts
        assert first["connectionInstanceId"] == instance

    def test_each_printer_has_its_own_instance(self, tmp_path):
        api = FakePrintLogApi(printers=(42, 43))

        run(api, tmp_path, started("00000A", "voron"), started("00000A", "u1"), voron=42, u1=43)

        voron, u1 = api.prints.values()
        assert (voron["printerId"], u1["printerId"]) == (42, 43)
        assert voron["externalId"] != u1["externalId"]

    def test_without_estimates_sends_no_usage_row(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started())

        [print_] = api.prints.values()
        assert print_["filamentUsage"] == []
        assert print_["estimatedPrintTimeInSeconds"] is None

    def test_uses_the_accounts_default_visibility_and_comments(self, tmp_path):
        api = FakePrintLogApi()
        api.settings = [
            {"userSettingTypeId": 1, "value": "Public"},
            {"userSettingTypeId": 3, "value": "true"},
        ]

        run(api, tmp_path, started())

        [print_] = api.prints.values()
        assert print_["viewStatus"] == 1
        assert print_["allowComments"] is True

    def test_is_private_without_comments_when_the_account_has_no_default(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started())

        [print_] = api.prints.values()
        assert print_["viewStatus"] == 3
        assert print_["allowComments"] is False

    def test_a_repeated_start_creates_nothing(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), started())
        run(api, tmp_path, started())  # and after a restart

        assert len(api.created) == 1


class TestJobEnd:
    def test_completes_the_print_with_its_measured_duration_and_usage(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(estimated_filament_mm=4400.0), finished())

        [print_] = api.prints.values()
        assert print_["status"] == 3  # Success
        assert print_["printTimeInSeconds"] == 3725
        [row] = print_["filamentUsage"]  # the estimate's row, now with the actual length
        assert row["estimatedLengthInM"] == 4.4
        assert row["lengthInM"] == 4.3215
        assert row["source"] == 2
        [completion] = api.completions
        assert completion["endedAt"] == "2026-10-10T10:35:00+00:00"

    def test_maps_each_outcome_to_a_status(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            started("1"), finished("1", JobOutcome.COMPLETED),
            started("2"), finished("2", JobOutcome.CANCELLED),
            started("3"), finished("3", JobOutcome.FAILED),
        )  # fmt: skip

        assert [p["status"] for p in api.prints.values()] == [3, 4, 5]

    def test_a_failure_adds_its_note_after_the_users_own(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            started(),
            lambda api: api.user_edits(1, notes="Watch the first layer"),
            finished(outcome=JobOutcome.FAILED, note="Klipper shutdown"),
        )

        assert api.prints[1]["notes"] == "Watch the first layer\n\nKlipper shutdown"

    def test_a_failure_note_on_a_print_without_notes(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), finished(outcome=JobOutcome.FAILED, note="Klipper error"))

        assert api.prints[1]["notes"] == "Klipper error"

    def test_keeps_a_status_the_user_already_chose(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            started(),
            lambda api: api.user_edits(1, status=6),  # PartialSuccess
            finished(),
        )

        assert api.prints[1]["status"] == 6
        assert api.prints[1]["printTimeInSeconds"] == 3725  # still recorded

    def test_never_overwrites_the_users_title(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            started(),
            lambda api: api.user_edits(1, title="Shelf bracket for Sam"),
            finished(),
        )

        assert api.prints[1]["title"] == "Shelf bracket for Sam"

    def test_usage_goes_to_the_spool_the_user_linked_while_it_printed(self, tmp_path):
        api = FakePrintLogApi()

        def link_spool(api):
            api.prints[1]["filamentUsage"][0]["filament"] = {"id": "spool-1"}

        run(api, tmp_path, started(estimated_filament_mm=4400.0), link_spool, finished())

        [row] = api.prints[1]["filamentUsage"]
        assert row["filament"] == {"id": "spool-1"}
        assert row["lengthInM"] == 4.3215

    def test_a_repeated_end_changes_nothing(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), finished(), finished())
        run(api, tmp_path, started(), finished())  # and after a restart

        assert len(api.created) == 1
        assert len(api.completions) == 1

    def test_a_print_the_user_deleted_is_not_logged_again(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), lambda api: api.prints.clear(), finished())

        assert api.prints == {}
        assert len(api.created) == 1

    def test_creates_the_print_first_when_its_start_was_never_logged(self, tmp_path):
        api = FakePrintLogApi()
        api.fail_next(3)  # the start's create, and both retries

        run(api, tmp_path, started(), finished())

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert print_["title"] == "Shelf Bracket"

    def test_an_end_without_a_start_is_ignored(self, tmp_path, caplog):
        api = FakePrintLogApi()

        with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
            run(api, tmp_path, finished("unknown"))

        assert api.requests == []
        assert "unknown" in caplog.text


class TestWhenTheApiIsDown:
    def test_retries_a_brief_outage(self, tmp_path):
        api = FakePrintLogApi()
        api.fail_next(2)

        run(api, tmp_path, started())

        assert len(api.prints) == 1

    def test_gives_up_on_a_long_outage_and_logs_the_job_on_its_next_replay(self, tmp_path, caplog):
        api = FakePrintLogApi()
        api.fail_next(3)

        with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
            run(api, tmp_path, started())
        assert api.prints == {}
        assert "503" in caplog.text

        run(api, tmp_path, started(), finished())

        [print_] = api.prints.values()
        assert print_["status"] == 3

    def test_does_not_retry_a_revoked_key(self, tmp_path, caplog):
        api = FakePrintLogApi(api_key="the-real-key")

        async def scenario():
            base = await api.start()
            try:
                async with ApiClient(base, "revoked") as client:
                    logger = JobLogger(
                        client,
                        {"voron": LoggedPrinter(42, "moonraker")},
                        tmp_path,
                        retry_delays=FAST_RETRIES,
                    )
                    await logger.emit(started())
            finally:
                await api.close()

        with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
            asyncio.run(scenario())

        assert len(api.requests) == 1
        assert "API key" in caplog.text

    def test_an_unexpected_answer_is_logged_and_the_job_retried_on_its_next_replay(
        self, tmp_path, caplog
    ):
        api = FakePrintLogApi()
        api.settings = None  # the API answers null where a list belongs

        with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
            run(api, tmp_path, started())
        assert "job 00000A" in caplog.text

        api.settings = []
        run(api, tmp_path, started())

        assert len(api.prints) == 1


class TestResumePoint:
    def test_is_unknown_before_the_first_job(self, tmp_path):
        logger = run(FakePrintLogApi(), tmp_path)

        assert asyncio.run(logger.resume_after("voron")) is None

    def test_is_the_earliest_job_not_yet_logged_as_finished(self, tmp_path):
        later = STARTED_AT + timedelta(hours=2)

        logger = run(
            FakePrintLogApi(), tmp_path, started("1"), started("2", started_at=later), finished("2")
        )

        assert asyncio.run(logger.resume_after("voron")) == STARTED_AT

    def test_is_the_latest_job_once_all_are_finished(self, tmp_path):
        later = STARTED_AT + timedelta(hours=2)

        logger = run(
            FakePrintLogApi(),
            tmp_path,
            started("1"), finished("1"), started("2", started_at=later), finished("2"),
        )  # fmt: skip

        assert asyncio.run(logger.resume_after("voron")) == later

    def test_survives_a_restart(self, tmp_path):
        run(FakePrintLogApi(), tmp_path, started("1"))

        logger = run(FakePrintLogApi(), tmp_path)

        assert asyncio.run(logger.resume_after("voron")) == STARTED_AT
