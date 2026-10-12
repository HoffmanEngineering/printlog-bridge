from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.connections import Connection, Heartbeat
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.lifecycle import JobLogger, LoggedPrinter, backoff_delay
from printlog_bridge.core.outbox import MAX_AGE, OUTBOX_FILE, read_waiting
from printlog_bridge.core.state import JobRecord, PrinterState
from printlog_bridge.core.status import StatusBoard, read_status

STARTED_AT = datetime(2026, 10, 10, 9, 30, tzinfo=timezone.utc)
FAST_BACKOFF = (0.01, 0.04)


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
async def logging_to(
    api: FakePrintLogApi, state_dir: Path, *, clock=None, backoff=FAST_BACKOFF, **printers: int
):
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
                backoff=backoff,
                **({"clock": clock} if clock else {}),
            )
    finally:
        await api.close()


async def drain(logger: JobLogger, timeout: float = 1.0) -> None:
    """Sends what the outbox holds, retrying, until it is empty or `timeout` runs out."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        wait = await logger.send_due()
        if wait is None or loop.time() >= deadline:
            return
        await asyncio.sleep(min(wait, max(0.0, deadline - loop.time())))


def run(
    api: FakePrintLogApi,
    state_dir: Path,
    *events,
    timeout: float = 1.0,
    backoff=FAST_BACKOFF,
    **printers: int,
):
    """Emits `events` in order, sending each before the next, as a restart of the bridge would.

    A callable event is called with the API instead. The logger is closed at the end, like a
    bridge that stops, and returned.
    """

    async def scenario():
        async with logging_to(api, state_dir, backoff=backoff, **printers) as logger:
            try:
                await drain(logger, timeout)  # what the last run left in the outbox
                for event in events:
                    if callable(event):
                        event(api)
                    else:
                        await logger.emit(event)
                        await drain(logger, timeout)
            finally:
                logger.close()
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

        # A state file from before the outbox: the start was seen, and the API was down.
        state = PrinterState.load(tmp_path, "voron")
        start = started()
        state.jobs[start.job_id] = JobRecord(
            started_at=start.started_at.isoformat(),
            start={**asdict(start), "started_at": start.started_at.isoformat()},
        )
        state.save()

        run(api, tmp_path, finished())

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

    def test_a_print_made_while_offline_is_logged_once_the_api_is_back(self, tmp_path, caplog):
        api = FakePrintLogApi()
        api.offline = True

        with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
            run(api, tmp_path, started(), finished(), timeout=0.1)
        assert api.prints == {}
        assert "job 00000A" in caplog.text

        api.offline = False
        run(api, tmp_path)  # the bridge restarts; the printer has nothing new to say

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert print_["printTimeInSeconds"] == 3725
        assert len(api.completions) == 1

    def test_a_print_is_created_before_it_is_completed(self, tmp_path):
        api = FakePrintLogApi()
        api.offline = True
        run(api, tmp_path, started("1"), started("2"), finished("2"), finished("1"), timeout=0.1)
        api.offline = False
        api.requests.clear()

        run(api, tmp_path)

        writes = [(m, p) for m, p in api.requests if m == "POST"]
        assert writes == [
            ("POST", "/api/Prints"),
            ("POST", "/api/Prints"),
            ("POST", "/api/Prints/2/complete"),
            ("POST", "/api/Prints/1/complete"),
        ]
        assert [p["status"] for p in api.prints.values()] == [3, 3]

    def test_nothing_more_is_sent_for_a_print_while_its_start_waits_to_be_retried(self, tmp_path):
        api = FakePrintLogApi()

        async def scenario():
            async with logging_to(api, tmp_path, backoff=(10, 10)) as logger:
                try:
                    api.fail_next(1)
                    await logger.emit(started())
                    await logger.emit(finished())
                    await logger.send_due()
                finally:
                    logger.close()

        asyncio.run(scenario())

        assert api.requests == [("GET", "/api/Users/me/user-settings")]  # the start's, failed

    def test_one_print_the_api_keeps_failing_does_not_hold_up_the_others(self, tmp_path):
        api = FakePrintLogApi()
        api.fail_next(1)  # only the first job's first try

        run(api, tmp_path, started("1"), started("2"), timeout=0, backoff=(10, 10))

        assert [p["externalId"].split(":")[1] for p in api.prints.values()] == ["2"]

    def test_waits_as_long_as_the_api_asks(self, tmp_path):
        api = FakePrintLogApi()

        async def scenario():
            async with logging_to(api, tmp_path) as logger:
                try:
                    api.fail_next(1, status=429, headers={"Retry-After": "30"})
                    await logger.emit(started())
                    wait = await logger.send_due()
                    sent = len(api.requests)
                    again = await logger.send_due()
                    return wait, again, len(api.requests) - sent
                finally:
                    logger.close()

        wait, again, sent_meanwhile = asyncio.run(scenario())

        assert 29 < wait <= 30
        assert 29 < again <= 30
        assert sent_meanwhile == 0

    @pytest.mark.parametrize("retry_after", ["0", "Thu, 01 Jan 1970 00:00:00 GMT"])
    def test_a_retry_after_of_zero_still_backs_off(self, tmp_path, retry_after):
        api = FakePrintLogApi()

        async def scenario():
            async with logging_to(api, tmp_path, backoff=(10, 10)) as logger:
                try:
                    api.fail_next(1, status=503, headers={"Retry-After": retry_after})
                    await logger.emit(started())
                    return await logger.send_due()
                finally:
                    logger.close()

        wait = asyncio.run(scenario())

        assert 9 < wait <= 10
        assert api.requests == [("GET", "/api/Users/me/user-settings")]  # not sent again at once

    def test_does_not_retry_a_revoked_key_but_keeps_the_job(self, tmp_path, caplog):
        api = FakePrintLogApi(api_key="the-real-key")

        async def scenario():
            base = await api.start()
            try:
                async with ApiClient(base, "revoked") as client:
                    logger = JobLogger(
                        client,
                        {"voron": LoggedPrinter(42, "moonraker")},
                        tmp_path,
                        backoff=FAST_BACKOFF,
                    )
                    try:
                        await logger.emit(started())
                        await drain(logger, 0.2)
                    finally:
                        logger.close()
            finally:
                await api.close()

        with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
            asyncio.run(scenario())

        assert len(api.requests) == 1
        assert "API key" in caplog.text
        api.api_key = "revoked"  # say the user fixes the key, then restarts the bridge
        run(api, tmp_path)
        assert len(api.prints) == 1

    def test_a_refused_job_leaves_the_outbox(self, tmp_path, caplog):
        api = FakePrintLogApi(printers=(7,))

        with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
            run(api, tmp_path, started())

        assert "does not belong" in caplog.text
        assert len(api.created) == 1
        assert read_waiting(tmp_path) == {}

    def test_an_unexpected_answer_is_logged_and_the_job_retried(self, tmp_path, caplog):
        api = FakePrintLogApi()
        api.settings = None  # the API answers null where a list belongs

        with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
            run(api, tmp_path, started(), timeout=0.1)
        assert "job 00000A" in caplog.text

        api.settings = []
        run(api, tmp_path)

        assert len(api.prints) == 1


def test_backoff_doubles_from_its_base_to_its_cap():
    assert [backoff_delay(n, 2, 60) for n in range(1, 9)] == [2, 4, 8, 16, 32, 60, 60, 60]


class TestOldEntries:
    def test_an_entry_unsent_for_30_days_is_dropped_with_a_warning(self, tmp_path, caplog):
        api = FakePrintLogApi()
        api.offline = True
        now = [STARTED_AT]

        async def scenario():
            async with logging_to(api, tmp_path, clock=lambda: now[0]) as logger:
                try:
                    await logger.emit(started())
                    await logger.send_due()
                    now[0] += MAX_AGE + timedelta(minutes=1)
                    api.offline = False
                    with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
                        await logger.send_due()
                    await logger.emit(started())  # the printer replays it
                    await drain(logger, 0.1)
                finally:
                    logger.close()
                return logger

        logger = asyncio.run(scenario())

        assert api.prints == {}
        assert "30 days" in caplog.text
        assert "00000A" in caplog.text
        assert read_waiting(tmp_path) == {}
        assert logger.state("voron").jobs["00000A"].finished  # so it is never replayed again


class PowerCut(BaseException):
    """The bridge's process dies on the spot."""


def cut_power_after(monkeypatch, method: str) -> None:
    """The API gets the call, and the power goes before the bridge hears the answer."""
    original = getattr(ApiClient, method)

    async def cut(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        monkeypatch.setattr(ApiClient, method, original)
        raise PowerCut

    monkeypatch.setattr(ApiClient, method, cut)


class TestPowerLoss:
    def test_during_the_create_the_print_is_not_duplicated(self, tmp_path, monkeypatch):
        api = FakePrintLogApi()
        cut_power_after(monkeypatch, "create_print")

        with pytest.raises(PowerCut):
            run(api, tmp_path, started())
        run(api, tmp_path)  # the bridge starts again

        assert len(api.prints) == 1
        assert len(api.created) == 2  # sent twice; the API recognised its external ID
        assert read_waiting(tmp_path) == {}

    def test_during_the_completion_it_is_completed_once_with_one_note(self, tmp_path, monkeypatch):
        api = FakePrintLogApi()
        run(api, tmp_path, started(estimated_filament_mm=4400.0))
        cut_power_after(monkeypatch, "complete_print")

        with pytest.raises(PowerCut):
            run(api, tmp_path, finished(outcome=JobOutcome.FAILED, note="Klipper shutdown"))
        run(api, tmp_path)

        [print_] = api.prints.values()
        assert print_["status"] == 5
        assert print_["notes"] == "Klipper shutdown"
        [row] = print_["filamentUsage"]
        assert row["lengthInM"] == 4.3215
        assert read_waiting(tmp_path) == {}

    def test_after_the_event_but_before_any_send_it_is_still_sent(self, tmp_path):
        api = FakePrintLogApi()

        async def scenario():
            async with logging_to(api, tmp_path) as logger:
                await logger.emit(started())
                logger.close()  # and nothing else: no send

        asyncio.run(scenario())
        assert api.prints == {}

        run(api, tmp_path)

        assert len(api.prints) == 1

    def test_a_state_file_from_before_the_outbox_still_logs_its_job_once(self, tmp_path):
        api = FakePrintLogApi()
        state = PrinterState.load(tmp_path, "voron")
        state.jobs["00000A"] = JobRecord(
            started_at=STARTED_AT.isoformat(),
            start={**asdict(started()), "started_at": STARTED_AT.isoformat()},
            print_id=None,
            ended_at=finished().finished_at.isoformat(),
        )
        state.save()
        assert not (tmp_path / OUTBOX_FILE).exists()

        run(api, tmp_path, started(), finished(), started(), finished())  # the replay

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert len(api.completions) == 1


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


def report_run(api: FakePrintLogApi, state_dir: Path, *events, register=None):
    """`run`, with a status board and, optionally, a connection registered before each create."""

    async def scenario():
        board = StatusBoard(state_dir)
        board.start(["voron"])
        base = await api.start()
        try:
            async with ApiClient(base, api.api_key) as client:
                hook = None
                if register:
                    hook = register(client, board)
                logger = JobLogger(
                    client,
                    {"voron": LoggedPrinter(42, "moonraker")},
                    state_dir,
                    backoff=FAST_BACKOFF,
                    board=board,
                    before_create=hook,
                )
                try:
                    for event in events:
                        if callable(event):
                            event(api)
                        else:
                            await logger.emit(event)
                            await drain(logger, 0.1)
                    await drain(logger, 0.5)
                finally:
                    logger.close()
        finally:
            await api.close()
        return read_status(state_dir)["printers"]["voron"]

    return asyncio.run(scenario())


class TestConnection:
    def test_the_connection_is_registered_before_the_print_so_the_print_links_to_it(self, tmp_path):
        api = FakePrintLogApi()

        def register(client, board):
            instance_id = PrinterState.load(tmp_path, "voron").instance_id
            heartbeat = Heartbeat(
                client, [Connection("voron", instance_id, "moonraker", 42)], board
            )
            return heartbeat.ensure_registered

        report_run(api, tmp_path, started(), register=register)

        [print_] = api.prints.values()
        assert print_["connectionId"] == api.connections[print_["connectionInstanceId"]]["id"]

    def test_a_failed_registration_still_logs_the_print(self, tmp_path):
        api = FakePrintLogApi()

        def register(client, board):
            async def refuse(printer):
                raise AssertionError("before_create must not be able to stop a print")

            return refuse

        report_run(api, tmp_path, started(), register=register)

        assert len(api.prints) == 1


class TestStatusReport:
    def test_the_last_event_names_the_job_and_its_print(self, tmp_path):
        api = FakePrintLogApi()

        printing = report_run(api, tmp_path, started())
        done = report_run(api, tmp_path, finished())

        assert printing["last_event"] == "job 00000A started: print 1, 'Shelf Bracket'"
        assert done["last_event"] == "job 00000A completed: print 1"

    def test_a_job_that_could_not_be_logged_is_a_warning_until_one_is(self, tmp_path):
        api = FakePrintLogApi()
        api.offline = True
        seen = []

        def back_online(api):
            seen.append(read_status(tmp_path)["printers"]["voron"])
            api.offline = False

        recovered = report_run(api, tmp_path, started(), back_online)

        [failed] = seen

        assert "job 00000A" in failed["warnings"]["api"]
        assert "can't reach" in failed["warnings"]["api"]
        assert "tries again" in failed["warnings"]["api"]
        assert recovered["warnings"] == {}

    def test_a_refused_job_is_a_warning(self, tmp_path):
        api = FakePrintLogApi(printers=(7,))

        status = report_run(api, tmp_path, started())

        assert "does not belong" in status["warnings"]["api"]


class TestWaitingJobs:
    def test_a_job_that_ended_but_was_not_logged_is_marked_as_ended(self, tmp_path):
        api = FakePrintLogApi()
        api.offline = True

        run(api, tmp_path, started(), finished(), timeout=0.05)

        [job] = PrinterState.load(tmp_path, "voron").jobs.values()
        assert not job.finished
        assert job.ended_at == finished().finished_at.isoformat()
        assert read_waiting(tmp_path)["voron"].jobs == 1
