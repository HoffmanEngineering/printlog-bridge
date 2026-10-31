from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp import web
from fake_moonraker import eventually
from fake_printlog_api import SAS_SIGNATURE, FakePrintLogApi

from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.config import Adoption
from printlog_bridge.core.connections import Connection, Heartbeat
from printlog_bridge.core.events import (
    AttachmentKind,
    FileEstimate,
    ImageKind,
    JobAttachment,
    JobFinished,
    JobImage,
    JobOutcome,
    JobStarted,
    SpoolReading,
    UnplacedFilament,
)
from printlog_bridge.core.images import FileUnavailable
from printlog_bridge.core.lifecycle import (
    SPOOL_READ_HOLD,
    JobLogger,
    LoggedPrinter,
    backoff_delay,
)
from printlog_bridge.core.outbox import MAX_AGE, OUTBOX_FILE, Outbox, read_entries, read_waiting
from printlog_bridge.core.spools import Spools
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
    api: FakePrintLogApi,
    state_dir: Path,
    *,
    clock=None,
    backoff=FAST_BACKOFF,
    image_timeout: float | None = None,
    spools: bool = False,
    adoption: Adoption | None = None,
    slicer_settings: bool = True,
    max_attachment_bytes: int | None = None,
    **printers: int,
):
    """A JobLogger sending to `api`. With `spools`, it links each print to the loaded spools."""
    base = await api.start()
    options = {}
    if clock:
        options["clock"] = clock
    if image_timeout is not None:
        options["image_timeout"] = image_timeout
    if max_attachment_bytes is not None:
        options["max_attachment_bytes"] = max_attachment_bytes
    try:
        async with ApiClient(base, api.api_key) as client:
            targets = printers or {"voron": 42}
            if spools:
                options["spools"] = Spools(client, targets)
            yield JobLogger(
                client,
                {
                    name: LoggedPrinter(
                        printer_id=printer_id,
                        source="moonraker",
                        adoption=adoption or Adoption(),
                        slicer_settings=slicer_settings,
                    )
                    for name, printer_id in targets.items()
                },
                state_dir,
                backoff=backoff,
                **options,
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
    spools: bool = False,
    adoption: Adoption | None = None,
    slicer_settings: bool = True,
    image_timeout: float | None = None,
    max_attachment_bytes: int | None = None,
    **printers: int,
):
    """Emits `events` in order, sending each before the next, as a restart of the bridge would.

    A callable event is called with the API instead. The logger is closed at the end, like a
    bridge that stops, and returned.
    """

    async def scenario():
        async with logging_to(
            api,
            state_dir,
            backoff=backoff,
            spools=spools,
            adoption=adoption,
            slicer_settings=slicer_settings,
            image_timeout=image_timeout,
            max_attachment_bytes=max_attachment_bytes,
            **printers,
        ) as logger:
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

        assert api.requests == [("GET", "/api/Prints/claimable")]  # the start's lookup, failed

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
        assert api.requests == [("GET", "/api/Prints/claimable")]  # not sent again at once

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


def heartbeat_registration(state_dir: Path):
    """`report_run`'s `register`: the bridge's own hook, `Heartbeat.ensure_registered`."""

    def register(client, board):
        instance_id = PrinterState.load(state_dir, "voron").instance_id
        heartbeat = Heartbeat(client, [Connection("voron", instance_id, "moonraker", 42)], board)
        return heartbeat.ensure_registered

    return register


class TestConnection:
    def test_the_connection_is_registered_before_the_print_so_the_print_links_to_it(self, tmp_path):
        api = FakePrintLogApi()

        report_run(api, tmp_path, started(), register=heartbeat_registration(tmp_path))

        [print_] = api.prints.values()
        assert print_["connectionId"] == api.connections[print_["connectionInstanceId"]]["id"]

    def test_a_print_waits_for_a_registration_that_could_not_reach_3d_print_log(self, tmp_path):
        """3D Print Log coming back between the registration and the create (the end of an
        outage) must not log the print unlinked: it never links to the connection afterwards,
        and the connection itself would wait for the next heartbeat, minutes away."""
        api = FakePrintLogApi()
        api.fail_next(1)  # the registration's PUT; everything after it gets through

        report_run(api, tmp_path, started(), register=heartbeat_registration(tmp_path))

        [print_] = api.prints.values()
        assert print_["connectionId"] == api.connections[print_["connectionInstanceId"]]["id"]
        assert [r for r in api.requests if r[0] == "POST"] == [("POST", "/api/Prints")]

    def test_a_print_waits_for_its_connection_when_the_line_comes_back_mid_attempt(self, tmp_path):
        """The same, with the connection cut rather than answered with a 503."""
        api = FakePrintLogApi()
        api.offline = True
        register = heartbeat_registration(tmp_path)

        def register_then_reconnect(client, board):
            ensure_registered = register(client, board)

            async def hook(printer):
                try:
                    await ensure_registered(printer)
                finally:
                    api.offline = False  # back between the registration and the create

            return hook

        report_run(api, tmp_path, started(), register=register_then_reconnect)

        [print_] = api.prints.values()
        assert print_["connectionId"] == api.connections[print_["connectionInstanceId"]]["id"]

    def test_a_refused_registration_still_logs_the_print(self, tmp_path):
        """Refused is not unreachable: sending it again won't change the answer, so the print
        goes ahead unlinked rather than waiting for nothing."""
        api = FakePrintLogApi()
        api.fail_next(1, status=400)

        report_run(api, tmp_path, started(), register=heartbeat_registration(tmp_path))

        [print_] = api.prints.values()
        assert print_["connectionId"] is None

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


PNG = b"\x89PNG\r\n\x1a\n" + b"thumbnail" * 8
JPEG = b"\xff\xd8\xff\xe0" + b"snapshot" * 8


def image(
    kind: ImageKind = ImageKind.THUMBNAIL,
    data: bytes | None = PNG,
    job_id: str = "00000A",
    fetched: list | None = None,
    error: BaseException | None = None,
    hang: float = 0.0,
) -> JobImage:
    async def fetch():
        if fetched is not None:
            fetched.append(kind)
        if hang:
            await asyncio.sleep(hang)
        if error is not None:
            raise error
        return data

    return JobImage("voron", job_id, kind, fetch)


def snapshot(data: bytes | None = JPEG, **kwargs) -> JobImage:
    return image(ImageKind.SNAPSHOT, data, **kwargs)


class TestImages:
    def test_the_thumbnail_is_added_to_the_print_as_its_default_image(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), image())

        [print_] = api.prints.values()
        [added] = print_["images"]
        assert added["isDefault"]
        assert added["contentType"] == "image/png"
        assert api.default_image(print_["id"]) == PNG

    def test_a_failed_prints_snapshot_becomes_its_default_image(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), image(), finished(outcome=JobOutcome.FAILED), snapshot())

        [print_] = api.prints.values()
        assert len(print_["images"]) == 2
        assert print_["images"][1]["contentType"] == "image/jpeg"
        assert api.default_image(print_["id"]) == JPEG

    @pytest.mark.parametrize("outcome", [JobOutcome.COMPLETED, JobOutcome.CANCELLED])
    def test_otherwise_the_thumbnail_stays_the_default(self, tmp_path, outcome):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), image(), finished(outcome=outcome), snapshot())

        [print_] = api.prints.values()
        assert len(print_["images"]) == 2
        assert api.default_image(print_["id"]) == PNG

    def test_without_a_thumbnail_the_snapshot_is_the_default(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), image(data=None), finished(), snapshot())

        [print_] = api.prints.values()
        assert api.default_image(print_["id"]) == JPEG

    def test_an_image_is_fetched_and_added_once_however_often_it_is_replayed(self, tmp_path):
        api = FakePrintLogApi()
        fetched: list = []

        run(api, tmp_path, started(), image(fetched=fetched), image(fetched=fetched))
        run(api, tmp_path, started(), image(fetched=fetched))  # after a restart

        [print_] = api.prints.values()
        assert len(print_["images"]) == 1
        assert fetched == [ImageKind.THUMBNAIL]

    def test_a_job_that_was_logged_before_is_not_given_a_thumbnail_now(self, tmp_path):
        api = FakePrintLogApi()
        fetched: list = []

        run(api, tmp_path, started(), finished())
        run(api, tmp_path, started(), image(fetched=fetched), finished())  # history replayed

        assert fetched == []

    def test_an_image_for_a_job_never_seen_is_not_fetched(self, tmp_path):
        api = FakePrintLogApi()
        fetched: list = []

        run(api, tmp_path, image(fetched=fetched))

        assert fetched == []
        assert api.prints == {}

    @pytest.mark.parametrize(
        "failing",
        [
            image(data=None),
            image(data=b"qoif not a format the API takes"),
            image(error=RuntimeError("the adapter has a bug")),
            image(error=OSError("disk gone")),
        ],
        ids=["none", "not-an-image", "bug", "os-error"],
    )
    def test_an_image_that_cannot_be_had_never_stops_the_print(self, tmp_path, failing):
        api = FakePrintLogApi()

        logger = run(api, tmp_path, started(), failing, finished())

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert print_["images"] == []
        assert read_waiting(tmp_path) == {}
        assert logger.state("voron").jobs["00000A"].finished

    def test_a_fetch_that_hangs_is_given_up_on(self, tmp_path):
        api = FakePrintLogApi()

        async def scenario():
            async with logging_to(api, tmp_path, image_timeout=0.2) as logger:
                try:
                    loop = asyncio.get_running_loop()
                    began = loop.time()
                    await logger.emit(started())
                    await logger.emit(image(hang=30))
                    took = loop.time() - began
                    await logger.emit(finished())
                    await drain(logger)
                finally:
                    logger.close()
            return took

        took = asyncio.run(scenario())

        assert took < 2
        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert print_["images"] == []

    def test_an_image_the_api_refuses_is_dropped_and_the_print_still_completes(
        self, tmp_path, caplog
    ):
        api = FakePrintLogApi()
        api.max_images = 0

        with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
            run(api, tmp_path, started(), image(), finished())

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert read_waiting(tmp_path) == {}
        assert "Maximum of 0 images" in caplog.text
        assert not list((tmp_path / "images").iterdir())

    def test_an_upload_that_keeps_failing_is_given_up_so_the_print_completes(self, tmp_path):
        api = FakePrintLogApi()
        api.image_status = 500

        run(api, tmp_path, started(), image(), finished(), timeout=3)

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert read_waiting(tmp_path) == {}

    def test_an_upload_cut_off_while_the_api_answers_is_given_up_so_the_print_completes(
        self, tmp_path, caplog
    ):
        api = FakePrintLogApi()
        api.image_cut = True

        with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
            run(api, tmp_path, started(), image(), finished(), timeout=3)

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert print_["images"] == []
        assert read_waiting(tmp_path) == {}
        assert "wasn't added to its print after 10 tries" in caplog.text
        assert not list((tmp_path / "images").iterdir())

    def test_an_upload_made_while_the_api_is_out_of_reach_is_not_given_up(self, tmp_path):
        api = FakePrintLogApi()
        run(api, tmp_path, started())
        api.offline = True

        run(api, tmp_path, image(), finished(), timeout=3)  # many more than 10 tries
        api.offline = False
        run(api, tmp_path)

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert len(print_["images"]) == 1

    def test_an_image_lost_before_it_was_queued_is_fetched_again_on_a_replay(self, tmp_path):
        api = FakePrintLogApi()

        def power_cut(*_args):
            raise PowerCut

        async def scenario():
            async with logging_to(api, tmp_path) as logger:
                try:
                    await logger.emit(started())
                    await drain(logger)
                    logger._queue = power_cut  # between writing the file and queueing it
                    with pytest.raises(PowerCut):
                        await logger.emit(image())
                finally:
                    logger.close()

        asyncio.run(scenario())
        fetched: list = []
        run(api, tmp_path, started(), image(fetched=fetched), image(fetched=fetched))

        [print_] = api.prints.values()
        assert len(print_["images"]) == 1
        assert fetched == [ImageKind.THUMBNAIL]

    def test_outcomes_are_not_kept_for_jobs_without_a_snapshot(self, tmp_path):
        api = FakePrintLogApi()

        logger = run(
            api,
            tmp_path,
            *(e for n in range(5) for e in (started(f"0{n}"), finished(f"0{n}"))),
        )

        assert len(logger._outcomes) <= 1

    def test_a_rate_limited_upload_waits_rather_than_being_given_up(self, tmp_path):
        api = FakePrintLogApi()
        api.image_status = 429

        run(api, tmp_path, started(), image(), finished(), timeout=3)
        assert read_waiting(tmp_path)["voron"].jobs == 1  # the image, and the finish behind it
        api.image_status = None
        run(api, tmp_path)

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert len(print_["images"]) == 1

    def test_an_image_waits_in_the_outbox_while_the_api_is_down(self, tmp_path):
        api = FakePrintLogApi()
        api.offline = True

        run(api, tmp_path, started(), image(), finished(), snapshot(), timeout=0.05)
        assert read_waiting(tmp_path)["voron"].jobs == 1
        api.offline = False
        run(api, tmp_path)  # the bridge starts again, with the internet back

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert [i["contentType"] for i in print_["images"]] == ["image/png", "image/jpeg"]
        posts = [path for method, path in api.requests if method == "POST"]
        assert posts == [
            "/api/Prints",
            f"/api/Prints/{print_['id']}/image",
            f"/api/Prints/{print_['id']}/complete",
            f"/api/Prints/{print_['id']}/image",
        ]
        assert not list((tmp_path / "images").iterdir())

    def test_an_image_for_a_print_the_user_deleted_is_dropped(self, tmp_path):
        api = FakePrintLogApi()

        def delete(api):
            api.prints.clear()

        run(api, tmp_path, started(), delete, image(), finished())

        assert api.prints == {}
        assert read_waiting(tmp_path) == {}

    def test_an_image_file_gone_from_the_state_directory_is_skipped(self, tmp_path):
        api = FakePrintLogApi()
        api.offline = True
        run(api, tmp_path, started(), image(), timeout=0.05)
        for leftover in (tmp_path / "images").iterdir():
            leftover.unlink()
        api.offline = False

        run(api, tmp_path, finished())

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert print_["images"] == []

    def test_an_expired_image_does_not_stop_the_print_from_completing(self, tmp_path):
        api = FakePrintLogApi()
        now = [STARTED_AT]

        async def scenario():
            async with logging_to(api, tmp_path, clock=lambda: now[0]) as logger:
                try:
                    await logger.emit(started())
                    await drain(logger)
                    api.offline = True
                    await logger.emit(image())
                    await logger.send_due()
                    now[0] += MAX_AGE + timedelta(minutes=1)
                    await logger.emit(finished())
                    api.offline = False
                    await drain(logger)
                finally:
                    logger.close()

        asyncio.run(scenario())

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert print_["images"] == []


MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 2000
VIDEO = "timelapse_shelf_bracket.gcode_2026-10-15_12-00-00.mp4"


def timelapse(
    data: bytes | None = MP4, job_id: str = "00000A", fetched: list | None = None
) -> JobAttachment:
    async def fetch(path: Path, max_bytes: int) -> bool:
        if fetched is not None:
            fetched.append(max_bytes)
        if data is None:
            return False
        if len(data) > max_bytes:
            raise FileUnavailable(f"{VIDEO} is larger than {max_bytes} bytes")
        path.write_bytes(data)
        return True

    return JobAttachment("voron", job_id, AttachmentKind.TIMELAPSE, VIDEO, fetch)


def pro(api: FakePrintLogApi) -> FakePrintLogApi:
    api.pro = True
    return api


def attachment_requests(api: FakePrintLogApi) -> list:
    return [r for r in api.requests if "/files" in r[1]]


class TestAttachments:
    def test_the_video_is_attached_once_with_its_name_type_and_size(self, tmp_path):
        api = pro(FakePrintLogApi())
        fetched: list = []

        run(api, tmp_path, started(), finished(), timelapse(fetched=fetched), timelapse())
        run(api, tmp_path, timelapse(fetched=fetched))  # after a restart

        [attached] = api.attachments[1]
        assert attached["originalFileName"] == VIDEO
        assert attached["contentType"] == "video/mp4"
        assert attached["sizeBytes"] == len(MP4)
        assert api.attached(1) == [MP4]
        assert len(fetched) == 1
        assert list((tmp_path / "attachments").iterdir()) == []

    def test_it_goes_to_storage_without_the_api_key(self, tmp_path):
        api = pro(FakePrintLogApi())

        run(api, tmp_path, started(), finished(), timelapse())

        [put] = api.blob_puts
        assert "X-Api-Key" not in put and api.api_key not in str(put)
        assert put["x-ms-blob-type"] == "BlockBlob"
        assert put["Content-Type"] == "video/mp4"
        assert put["Content-Length"] == str(len(MP4))

    def test_without_pro_it_is_never_fetched_and_nothing_is_uploaded(self, tmp_path):
        api = FakePrintLogApi()
        fetched: list = []

        run(api, tmp_path, started(), finished(), timelapse(fetched=fetched))

        assert fetched == []
        assert attachment_requests(api) == []
        assert api.blob_puts == []

    def test_one_queued_while_the_api_was_down_waits_and_is_attached_once(self, tmp_path):
        api = pro(FakePrintLogApi())

        run(
            api, tmp_path, started(), finished(),
            lambda a: setattr(a, "offline", True), timelapse(), timeout=0.2,
        )  # fmt: skip
        assert not api.attachments
        api.offline = False
        run(api, tmp_path)  # the bridge starts again

        assert api.attached(1) == [MP4]

    def test_one_queued_before_pro_ended_is_not_uploaded(self, tmp_path):
        api = pro(FakePrintLogApi())

        run(
            api, tmp_path, started(), finished(),
            lambda a: setattr(a, "offline", True), timelapse(), timeout=0.2,
        )  # fmt: skip
        api.offline = False
        api.pro = False
        run(api, tmp_path)

        assert api.upload_requests == []
        assert api.blob_puts == []
        assert list((tmp_path / "attachments").iterdir()) == []

    def test_a_video_over_the_limit_is_skipped(self, tmp_path):
        api = pro(FakePrintLogApi())
        fetched: list = []

        run(
            api, tmp_path, started(), finished(), timelapse(fetched=fetched),
            max_attachment_bytes=len(MP4) - 1,
        )  # fmt: skip

        assert fetched == [len(MP4) - 1]
        assert attachment_requests(api) == []

    def test_a_file_that_is_not_an_mp4_is_skipped(self, tmp_path):
        api = pro(FakePrintLogApi())

        run(api, tmp_path, started(), finished(), timelapse(data=b"<html>not a video</html>"))

        assert attachment_requests(api) == []
        assert list((tmp_path / "attachments").iterdir()) == []

    def test_a_lost_answer_to_the_confirm_does_not_attach_it_twice(self, tmp_path):
        api = pro(FakePrintLogApi())
        api.confirm_cut = True

        run(api, tmp_path, started(), finished(), timelapse())

        assert len(api.confirms) == 1
        assert len(api.attachments[1]) == 1

    def test_one_storage_refuses_is_dropped_and_the_print_is_left_alone(self, tmp_path, caplog):
        api = pro(FakePrintLogApi())
        api.blob_status = 403

        with caplog.at_level(logging.INFO, logger="printlog_bridge"):
            run(api, tmp_path, started(), finished(), timelapse())

        assert api.confirms == []
        assert api.prints[1]["status"] == 3
        assert "storage refused the upload (403)" in caplog.text
        assert read_entries(tmp_path) == []

    def test_the_upload_urls_signature_is_never_logged(self, tmp_path, caplog):
        api = pro(FakePrintLogApi())
        api.blob_status = 500

        with caplog.at_level(logging.DEBUG, logger="printlog_bridge"):
            run(api, tmp_path, started(), finished(), timelapse(), timeout=1.0)

        assert "storage answered 500" in caplog.text
        assert SAS_SIGNATURE not in caplog.text

    def test_one_the_api_refuses_is_dropped(self, tmp_path, caplog):
        api = pro(FakePrintLogApi())
        api.max_files = 0

        with caplog.at_level(logging.INFO, logger="printlog_bridge"):
            run(api, tmp_path, started(), finished(), timelapse())

        assert "Maximum of 0 files per print" in caplog.text
        assert api.blob_puts == []
        assert read_entries(tmp_path) == []

    def test_another_prints_start_due_with_it_goes_first(self, tmp_path):
        api = pro(FakePrintLogApi())
        api.offline = True
        run(
            api, tmp_path, started(), finished(), timelapse(), started("00000B"), timeout=0.1,
        )  # fmt: skip
        api.offline = False

        run(api, tmp_path)  # the internet is back: everything is due at once

        sent = [path for method, path in api.requests if method == "POST" or "/files" in path]
        assert sent.index("/api/Prints", 1) < sent.index("/api/Prints/1/files")
        assert api.attached(1) == [MP4]


class BrokenDisk:
    """Makes the outbox's writes fail as a full disk (or a locked database) would, until fixed."""

    def __init__(self, monkeypatch, *methods: str) -> None:
        self.broken = True
        for method in methods:
            monkeypatch.setattr(Outbox, method, self._failing(getattr(Outbox, method)))

    def _failing(self, original):
        def method(outbox, *args, **kwargs):
            if self.broken:
                raise sqlite3.OperationalError("database or disk is full")
            return original(outbox, *args, **kwargs)

        return method

    def fix(self) -> None:
        self.broken = False


class TestWhenTheDiskFails:
    def test_an_event_that_cannot_be_queued_is_kept_and_queued_once_it_can(
        self, tmp_path, monkeypatch, caplog
    ):
        api = FakePrintLogApi()
        disk = BrokenDisk(monkeypatch, "add")
        seen = []

        def look_then_fix(api):
            seen.append(read_status(tmp_path)["printers"]["voron"])
            disk.fix()

        with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
            recovered = report_run(api, tmp_path, started(), finished(), look_then_fix)

        [broken] = seen
        assert "disk is full" in broken["warnings"]["outbox"]
        assert "couldn't write the outbox" in caplog.text
        [print_] = api.prints.values()
        assert print_["status"] == 3  # created, then completed, in order
        assert len(api.completions) == 1
        assert "outbox" not in recovered["warnings"]

    def test_a_failure_to_record_a_failed_send_does_not_stop_the_sender(
        self, tmp_path, monkeypatch
    ):
        api = FakePrintLogApi()
        api.offline = True
        disk = BrokenDisk(monkeypatch, "failed")

        async def scenario():
            async with logging_to(api, tmp_path) as logger:
                sender = asyncio.ensure_future(logger.run())
                try:
                    await logger.emit(started())
                    await asyncio.sleep(0.2)
                    assert not sender.done()  # still sending, not crashed
                    disk.fix()
                    api.offline = False
                    await eventually(lambda: api.prints)
                finally:
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender
                    logger.close()

        asyncio.run(scenario())

        assert len(api.prints) == 1

    def test_a_picture_that_cannot_be_queued_is_skipped(self, tmp_path, monkeypatch, caplog):
        api = FakePrintLogApi()
        run(api, tmp_path, started())
        BrokenDisk(monkeypatch, "add")

        with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
            run(api, tmp_path, image())

        [print_] = api.prints.values()
        assert print_["images"] == []
        assert "thumbnail" in caplog.text

    def test_a_picture_never_goes_ahead_of_its_jobs_event_waiting_to_be_queued(
        self, tmp_path, monkeypatch, caplog
    ):
        api = FakePrintLogApi()
        disk = BrokenDisk(monkeypatch, "add")
        fetched: list = []

        async def scenario():
            async with logging_to(api, tmp_path) as logger:
                try:
                    await logger.emit(started())  # kept in memory
                    disk.fix()  # a lock that clears, before the sender tries again
                    await logger.emit(image(fetched=fetched))
                    await drain(logger)
                finally:
                    logger.close()

        with caplog.at_level(logging.INFO, logger="printlog_bridge"):
            asyncio.run(scenario())

        [print_] = api.prints.values()
        assert print_["images"] == []
        assert fetched == []  # skipped, not queued ahead of the start and dropped
        assert "has no print to go on" not in caplog.text
        assert "thumbnail is skipped: the outbox can't be written" in caplog.text


# A four-colour print on a four-tool printer: the slicer's grams per tool, then what each fed.
FOUR_TOOL_ESTIMATE = {0: 30.2, 1: 9.8, 2: 3.6, 3: 1.5}
FOUR_TOOL_USAGE = {0: 10_120.0, 1: 3_280.0, 2: 1_160.0, 3: 500.0}


def four_tool_start(**kwargs) -> JobStarted:
    return started(
        estimated_filament_mm=15123.4,
        estimated_filament_g=45.1,
        estimated_filament_g_by_slot=FOUR_TOOL_ESTIMATE,
        **kwargs,
    )


def by_slot(print_) -> dict:
    return {row["slot"]: row for row in print_["filamentUsage"]}


class TestPerToolUsage:
    def test_the_slicers_estimate_is_one_row_per_tool(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, four_tool_start())

        [print_] = api.prints.values()
        rows = by_slot(print_)
        assert sorted(rows) == [0, 1, 2, 3]
        assert [rows[s]["estimatedAmountMg"] for s in range(4)] == [30200, 9800, 3600, 1500]
        assert {r["estimatedSource"] for r in rows.values()} == {1}  # Weight
        assert all(r["filament"] is None for r in rows.values())
        assert print_["estimatedFilamentUsageMg"] == 45100

    def test_a_multi_filament_file_using_one_tool_estimates_on_that_tool(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            started(
                estimated_filament_mm=2700.0,
                estimated_filament_g=8.2,
                estimated_filament_g_by_slot={2: 8.2},
            ),
        )

        [row] = api.prints[1]["filamentUsage"]
        assert row["slot"] == 2
        assert row["estimatedLengthInM"] == 2.7
        assert row["estimatedAmountMg"] == 8200

    def test_each_row_carries_the_note_on_what_its_slot_had_loaded(self, tmp_path):
        """As the Slic3r uploader writes it, so each spool's row can be told apart; a slot the
        printer said nothing about has no note, and completing keeps them."""
        api = FakePrintLogApi()
        notes = {0: "Slot 1 · White (#E2DEDB) · PLA SnapSpeed", 3: "Slot 4 · Pink (#D81B60) · PLA"}

        run(api, tmp_path, four_tool_start(slot_notes=notes), finished(filament_mm=FOUR_TOOL_USAGE))

        [created] = api.created
        assert [row.get("notes") for row in created["filamentUsage"]] == [
            notes[0],
            None,
            None,
            notes[3],
        ]
        rows = by_slot(api.prints[1])
        assert rows[0]["notes"] == notes[0]
        assert rows[3]["notes"] == notes[3]

    def test_each_tool_gets_its_own_row_and_they_add_up_to_the_total(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, four_tool_start(), finished(filament_mm=FOUR_TOOL_USAGE))

        rows = by_slot(api.prints[1])
        assert sorted(rows) == [0, 1, 2, 3]  # the estimate rows, filled in
        assert [rows[s]["lengthInM"] for s in range(4)] == [10.12, 3.28, 1.16, 0.5]
        assert [rows[s]["estimatedAmountMg"] for s in range(4)] == [30200, 9800, 3600, 1500]
        assert {r["source"] for r in rows.values()} == {2}  # Length
        total = sum(r["lengthInM"] for r in rows.values())
        assert total == pytest.approx(sum(FOUR_TOOL_USAGE.values()) / 1000, rel=0.01)

    def test_a_tool_with_no_spool_is_still_recorded_with_its_slot(self, tmp_path):
        api = FakePrintLogApi()

        def link_tool_1(api):
            by_slot(api.prints[1])[1]["filament"] = {"id": "spool-blue"}

        run(api, tmp_path, four_tool_start(), link_tool_1, finished(filament_mm=FOUR_TOOL_USAGE))

        [completion] = api.completions
        sent = {row["slot"]: row for row in completion["filamentUsage"]}
        assert sent[1]["filamentId"] == "spool-blue"
        for slot in (0, 2, 3):
            assert "filamentId" not in sent[slot]
            assert sent[slot]["lengthInM"] > 0
        rows = by_slot(api.prints[1])
        assert rows[1]["filament"] == {"id": "spool-blue"}
        assert rows[1]["lengthInM"] == 3.28
        assert rows[3]["filament"] is None
        assert rows[3]["lengthInM"] == 0.5

    def test_one_spool_linked_to_two_tools_is_one_row(self, tmp_path):
        """The API takes one row per spool, so two would be refused and the print never done."""
        api = FakePrintLogApi()

        def link_both(api):
            rows = by_slot(api.prints[1])
            rows[0]["filament"] = {"id": "spool-white"}
            rows[2]["filament"] = {"id": "spool-white"}

        run(api, tmp_path, four_tool_start(), link_both, finished(filament_mm=FOUR_TOOL_USAGE))

        [completion] = api.completions
        white = [r for r in completion["filamentUsage"] if r.get("filamentId") == "spool-white"]
        assert [(r["slot"], r["lengthInM"]) for r in white] == [(0, 11.28)]
        assert api.prints[1]["status"] == 3

    def test_a_tool_that_fed_nothing_sends_no_row(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), finished(filament_mm={0: 900.0, 1: 0.0}))

        [completion] = api.completions
        assert [r["slot"] for r in completion["filamentUsage"]] == [0]

    def test_the_estimate_per_tool_survives_a_restart(self, tmp_path):
        api = FakePrintLogApi()
        api.offline = True
        run(api, tmp_path, four_tool_start(), timeout=0.1)
        api.offline = False

        run(api, tmp_path)

        rows = by_slot(api.prints[1])
        assert [rows[s]["estimatedAmountMg"] for s in range(4)] == [30200, 9800, 3600, 1500]


class TestGramsAndEstimates:
    """Usage from a printer that reports grams, or only the slicer's estimate (#82)."""

    def test_grams_per_slot_are_sent_as_weight(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            four_tool_start(),
            finished(filament_mm={}, filament_g={0: 12.34, 1: 3.21}),
        )

        [completion] = api.completions
        assert completion["filamentUsage"] == [
            {"slot": 0, "amountMg": 12340, "source": 1},
            {"slot": 1, "amountMg": 3210, "source": 1},
        ]
        rows = by_slot(api.prints[1])
        assert (rows[0]["amountMg"], rows[0]["source"]) == (12340, 1)
        assert rows[0]["actualIsEstimate"] is False

    def test_an_estimate_is_marked_as_one_on_every_row(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            four_tool_start(),
            finished(filament_mm={}, filament_g={0: 12.34, 1: 3.21}, usage_estimated=True),
        )

        [completion] = api.completions
        assert [r["actualIsEstimate"] for r in completion["filamentUsage"]] == [True, True]
        assert all(r["actualIsEstimate"] for r in by_slot(api.prints[1]).values() if r["amountMg"])

    def test_measured_length_is_sent_as_it_always_was(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), finished())

        [completion] = api.completions
        assert completion["filamentUsage"] == [{"slot": 0, "lengthInM": 4.3215, "source": 2}]

    def test_a_slot_with_both_is_weighed_and_keeps_its_length(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), finished(filament_mm={0: 4321.5}, filament_g={0: 12.9}))

        [completion] = api.completions
        assert completion["filamentUsage"] == [
            {"slot": 0, "amountMg": 12900, "lengthInM": 4.3215, "source": 1}
        ]

    def test_slots_with_length_and_slots_with_grams_each_get_their_own(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), finished(filament_mm={0: 1000.0}, filament_g={1: 2.5}))

        [completion] = api.completions
        assert completion["filamentUsage"] == [
            {"slot": 0, "lengthInM": 1.0, "source": 2},
            {"slot": 1, "amountMg": 2500, "source": 1},
        ]

    def test_a_slot_that_weighed_nothing_sends_no_row(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(), finished(filament_mm={}, filament_g={0: 5.0, 1: 0.0}))

        [completion] = api.completions
        assert [r["slot"] for r in completion["filamentUsage"]] == [0]

    def test_grams_and_the_estimate_flag_survive_a_restart(self, tmp_path):
        api = FakePrintLogApi()
        run(api, tmp_path, started())
        api.offline = True
        run(
            api,
            tmp_path,
            finished(filament_mm={}, filament_g={0: 7.5}, usage_estimated=True),
            timeout=0.1,
        )
        api.offline = False

        run(api, tmp_path)

        [completion] = api.completions
        assert completion["filamentUsage"] == [
            {"slot": 0, "amountMg": 7500, "source": 1, "actualIsEstimate": True}
        ]


class TestLoadedSpools:
    """The spools loaded on the printer in 3D Print Log go on its prints' usage rows (#6)."""

    def test_the_spool_loaded_on_a_single_tool_printer_is_on_its_print(self, tmp_path):
        api = FakePrintLogApi()
        spool = api.add_spool()
        api.user_loads(42, None, spool)

        run(api, tmp_path, started(estimated_filament_mm=4400.0), finished(), spools=True)

        [row] = api.prints[1]["filamentUsage"]
        assert row["filament"] == {"id": spool}
        assert row["lengthInM"] == 4.3215
        assert api.slot_spools(42) == {None: spool}  # creating the print left it loaded

    def test_a_print_with_no_estimate_still_keeps_the_spool_loaded(self, tmp_path):
        api = FakePrintLogApi()
        spool = api.add_spool()
        api.user_loads(42, None, spool)

        run(api, tmp_path, started(), spools=True)

        assert api.slot_spools(42) == {None: spool}
        [row] = api.prints[1]["filamentUsage"]
        assert row["filament"] == {"id": spool}

    def test_a_printer_read_that_keeps_failing_holds_the_print_only_so_long(self, tmp_path):
        """The printer's GET is far heavier than the create, so it can keep failing while the
        create would work. The print waits for a read (#29), but only for SPOOL_READ_HOLD; then
        it's created with the spools read earlier, and says so."""
        api = FakePrintLogApi()
        spool = api.add_spool()
        api.user_loads(42, None, spool)
        now = [STARTED_AT]
        real = api._printer
        reads = {"n": 0}

        async def printer(request):
            reads["n"] += 1
            return await real(request) if reads["n"] == 1 else web.Response(status=503)

        api._printer = printer

        async def scenario():
            async with logging_to(api, tmp_path, clock=lambda: now[0], spools=True) as logger:
                try:
                    assert logger.spools is not None
                    await logger.spools.refresh("voron")  # the read at start worked
                    await logger.emit(started(estimated_filament_mm=4400.0))
                    await drain(logger, 0.2)
                    held = dict(api.prints)
                    now[0] += SPOOL_READ_HOLD - timedelta(minutes=1)
                    await drain(logger, 0.2)
                    still_held = dict(api.prints)
                    now[0] += timedelta(minutes=2)
                    await drain(logger, 0.5)
                finally:
                    logger.close()
                return held, still_held

        held, still_held = asyncio.run(scenario())

        assert held == {} and still_held == {}
        assert reads["n"] >= 3
        [print_] = api.prints.values()
        [row] = print_["filamentUsage"]
        assert row["filament"] == {"id": spool}
        assert "earlier read" in print_["notes"]

    def test_a_printer_read_the_bridge_cant_make_sense_of_doesnt_hold_the_print(self, tmp_path):
        api = FakePrintLogApi()
        spool = api.add_spool()
        api.user_loads(42, None, spool)
        real = api._printer
        reads = {"n": 0}

        async def printer(request):
            reads["n"] += 1
            if reads["n"] == 1:
                return await real(request)
            detail = api._printer_detail(42)
            detail["loadedFilaments"] = [{"slot": "T0", "filament": {"id": spool}}]
            return web.json_response(detail)

        api._printer = printer

        async def scenario():
            async with logging_to(api, tmp_path, spools=True) as logger:
                try:
                    assert logger.spools is not None
                    await logger.spools.refresh("voron")
                    await logger.emit(started(estimated_filament_mm=4400.0))
                    await logger.send_due()  # once, with no retry
                finally:
                    logger.close()

        asyncio.run(scenario())

        [print_] = api.prints.values()
        [row] = print_["filamentUsage"]
        assert row["filament"] == {"id": spool}
        assert "earlier read" in print_["notes"]

    def test_a_print_with_a_fresh_read_has_no_note_about_it(self, tmp_path):
        api = FakePrintLogApi()
        spool = api.add_spool()
        api.user_loads(42, None, spool)

        run(api, tmp_path, started(estimated_filament_mm=4400.0), spools=True)

        assert "earlier read" not in (api.prints[1].get("notes") or "")

    def test_each_tool_gets_the_spool_in_its_slot(self, tmp_path):
        api = FakePrintLogApi()
        api.slot_counts[42] = 4
        white, blue = api.add_spool("White"), api.add_spool("Blue")
        api.user_loads(42, 0, white)
        api.user_loads(42, 2, blue)

        run(api, tmp_path, four_tool_start(), finished(filament_mm=FOUR_TOOL_USAGE), spools=True)

        rows = by_slot(api.prints[1])
        assert rows[0]["filament"] == {"id": white}
        assert rows[2]["filament"] == {"id": blue}
        assert rows[1]["filament"] is None
        assert rows[2]["lengthInM"] == 1.16
        assert api.slot_spools(42) == {0: white, 2: blue}  # a multi-tool printer's are kept

    def test_a_spool_changed_in_the_app_between_prints_is_on_the_next_print(self, tmp_path):
        api = FakePrintLogApi()
        old, new = api.add_spool("Old"), api.add_spool("New")
        api.user_loads(42, 0, old)

        run(
            api,
            tmp_path,
            started("00000A", estimated_filament_mm=4400.0),
            finished("00000A"),
            lambda api: api.user_loads(42, 0, new),
            started("00000B", estimated_filament_mm=4400.0),
            finished("00000B"),
            spools=True,
        )

        assert [r["filament"] for r in api.prints[1]["filamentUsage"]] == [{"id": old}]
        assert [r["filament"] for r in api.prints[2]["filamentUsage"]] == [{"id": new}]

    def test_the_users_choice_on_the_print_wins(self, tmp_path):
        api = FakePrintLogApi()
        loaded, chosen = api.add_spool("Loaded"), api.add_spool("Chosen")
        api.user_loads(42, 0, loaded)

        def link_spool(api):
            api.prints[1]["filamentUsage"][0]["filament"] = {"id": chosen}

        run(
            api,
            tmp_path,
            started(estimated_filament_mm=4400.0),
            link_spool,
            finished(),
            spools=True,
        )

        [completion] = api.completions
        assert [r.get("filamentId") for r in completion["filamentUsage"]] == [chosen]

    def test_a_tool_the_estimate_missed_gets_its_slots_spool_at_the_end(self, tmp_path):
        api = FakePrintLogApi()
        api.slot_counts[42] = 4
        white, blue = api.add_spool("White"), api.add_spool("Blue")
        api.user_loads(42, 0, white)
        api.user_loads(42, 1, blue)

        run(
            api,
            tmp_path,
            started(estimated_filament_mm=4400.0),
            finished(filament_mm={0: 900.0, 1: 300.0}),
            spools=True,
        )

        rows = by_slot(api.prints[1])
        assert rows[1]["filament"] == {"id": blue}
        assert rows[1]["lengthInM"] == 0.3

    def test_spools_that_cant_be_read_dont_hold_up_the_print(self, tmp_path):
        api = FakePrintLogApi()
        spool = api.add_spool()
        api.user_loads(42, 0, spool)
        api.fail_next(1)  # the read of the printer's spools

        run(api, tmp_path, started(estimated_filament_mm=4400.0), spools=True)

        [print_] = api.prints.values()
        assert print_["status"] == 2

    def test_a_reader_that_disagrees_with_the_spool_adds_a_note(self, tmp_path):
        api = FakePrintLogApi()
        api.user_loads(42, 0, api.add_spool("Basic PLA", material="PLA"))

        async def scenario():
            async with logging_to(api, tmp_path, spools=True) as logger:
                await logger.spools.refresh_all()
                await logger.spools.spool_reading(SpoolReading("voron", 0, material="PETG"))
                await logger.emit(started(estimated_filament_mm=4400.0))
                await drain(logger)
                await logger.emit(finished())
                await drain(logger)
                logger.close()

        asyncio.run(scenario())

        [print_] = api.prints.values()
        assert "PETG" in print_["notes"]
        assert "Basic PLA" in print_["notes"]
        assert print_["status"] == 3  # logged all the same


FUECOCO_ESTIMATE = FileEstimate(
    duration_s=9597.0,
    filament_g=41.6,
    by_slot={0: 10.33, 1: 5.58, 2: 3.93},
    unplaced=[
        UnplacedFilament(
            0, 21.76, "#FF0000", "Sliced filament 1 · Red (#FF0000) · PLA: printer slot not known"
        )
    ],
    slot_colors={0: "#FFFFFF", 1: "#000000", 2: "#FFF014", 3: "#FF1E1E"},
)


def reading(estimate, calls: list):
    async def read():
        calls.append(1)
        if isinstance(estimate, Exception):
            raise estimate
        return estimate

    return read


class TestEstimateFromTheFile:
    """A job whose metadata has no estimate (a Creality Print file on a K3) gets one from its
    file before the print is created (printlog-bridge#128)."""

    def test_the_print_starts_with_the_files_duration_rows_and_notes(self, tmp_path):
        api, calls = FakePrintLogApi(), []
        notes = {0: "Slot 1 · White (#FFFFFF) · PLA"}

        run(
            api, tmp_path, started(slot_notes=notes, read_estimate=reading(FUECOCO_ESTIMATE, calls))
        )

        [created] = api.created
        assert calls == [1]
        assert created["estimatedPrintTimeInSeconds"] == 9597
        assert created["estimatedFilamentUsageMg"] == 41600
        rows = created["filamentUsage"]
        assert [(r.get("slot"), r["estimatedAmountMg"]) for r in rows] == [
            (0, 10330),
            (1, 5580),
            (2, 3930),
            (None, 21760),
        ]
        assert rows[0]["notes"] == notes[0]
        assert rows[3]["notes"] == "Sliced filament 1 · Red (#FF0000) · PLA: printer slot not known"
        assert rows[3]["estimatedSource"] == 1  # Weight
        assert "slot" not in rows[3]

    def test_with_slicer_settings_off_the_file_is_never_read(self, tmp_path):
        api, calls = FakePrintLogApi(), []

        run(
            api,
            tmp_path,
            started(read_estimate=reading(FUECOCO_ESTIMATE, calls)),
            slicer_settings=False,
        )

        assert calls == []
        [created] = api.created
        assert "estimatedPrintTimeInSeconds" not in created
        assert "filamentUsage" not in created

    def test_the_metadatas_estimate_wins(self, tmp_path):
        api, calls = FakePrintLogApi(), []

        run(
            api,
            tmp_path,
            started(
                estimated_duration_s=600.0,
                estimated_filament_g=3.0,
                estimated_filament_g_by_slot={0: 1.0, 1: 2.0},
                read_estimate=reading(FUECOCO_ESTIMATE, calls),
            ),
        )

        [created] = api.created
        assert created["estimatedPrintTimeInSeconds"] == 600
        assert [r["estimatedAmountMg"] for r in created["filamentUsage"]] == [1000, 2000]

    def test_a_start_seen_before_reads_nothing(self, tmp_path):
        api, calls = FakePrintLogApi(), []

        run(api, tmp_path, started())
        run(api, tmp_path, started(read_estimate=reading(FUECOCO_ESTIMATE, calls)))

        assert calls == []

    @pytest.mark.parametrize("answer", [None, OSError("gone")])
    def test_nothing_read_creates_the_print_as_before(self, tmp_path, answer):
        api, calls = FakePrintLogApi(), []

        run(api, tmp_path, started(read_estimate=reading(answer, calls)))

        [created] = api.created
        assert calls == [1]
        assert "filamentUsage" not in created


TWO_UNPLACED = FileEstimate(
    duration_s=9597.0,
    filament_g=41.6,
    by_slot={0: 10.33, 1: 5.58},
    unplaced=[
        UnplacedFilament(
            0, 21.76, "#FF0000", "Sliced filament 1 · Red (#FF0000) · PLA: printer slot not known"
        ),
        UnplacedFilament(
            2, 3.93, "#FFFF00", "Sliced filament 3 · Yellow (#FFFF00) · PLA: printer slot not known"
        ),
    ],
    slot_colors={0: "#FFFFFF", 1: "#000000", 2: "#FFF014", 3: "#FF1E1E"},
)
FED = {0: 3462.0, 1: 1870.0, 2: 1319.0, 3: 7295.0}


class TestPlaceholdersAtTheEnd:
    """An unplaced filament's row (no slot) is filled by the measured slot that fed it, named by
    `rowId`, so its estimate isn't counted again beside a new row (printlog-bridge#128)."""

    def test_one_placeholder_is_filled_by_the_one_slot_with_no_row(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            started(read_estimate=reading(FUECOCO_ESTIMATE, [])),
            finished(filament_mm=FED),
        )

        rows = api.prints[1]["filamentUsage"]
        assert len(rows) == 4
        red = next(r for r in rows if r["slot"] == 3)
        assert red["estimatedAmountMg"] == 21760
        assert red["lengthInM"] == 7.295
        assert red["notes"].startswith("Sliced filament 1 · Red")
        [completion] = api.completions
        assert [u.get("rowId") is not None for u in completion["filamentUsage"]] == [
            False,
            False,
            False,
            True,
        ]

    def test_several_are_paired_by_color(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            started(read_estimate=reading(TWO_UNPLACED, [])),
            finished(filament_mm=FED),
        )

        rows = {r["slot"]: r for r in api.prints[1]["filamentUsage"]}
        assert sorted(rows) == [0, 1, 2, 3]
        assert rows[3]["notes"].startswith("Sliced filament 1 · Red")
        assert rows[2]["notes"].startswith("Sliced filament 3 · Yellow")

    def test_a_placeholder_no_slot_is_left_for_gets_a_note(self, tmp_path):
        api = FakePrintLogApi()
        fed = {0: 3462.0, 1: 1870.0, 3: 8614.0}  # both sliced to the red slot

        run(
            api,
            tmp_path,
            started(read_estimate=reading(TWO_UNPLACED, [])),
            finished(filament_mm=fed),
        )

        rows = api.prints[1]["filamentUsage"]
        assert len(rows) == 4
        assert [r for r in rows if r["slot"] is None][0]["notes"].startswith("Sliced filament 3")
        assert (
            "Sliced filament 3's estimate wasn't matched to a measured slot. If the print's total "
            "looks high, delete its row."
        ) in api.prints[1]["notes"]

    def test_a_placeholder_the_user_linked_is_left_alone(self, tmp_path):
        api = FakePrintLogApi()

        def link(api):
            row = next(r for r in api.prints[1]["filamentUsage"] if r["slot"] is None)
            row["filament"] = {"id": "spool-red"}

        run(
            api,
            tmp_path,
            started(read_estimate=reading(FUECOCO_ESTIMATE, [])),
            link,
            finished(filament_mm=FED),
        )

        [completion] = api.completions
        assert not any(u.get("rowId") for u in completion["filamentUsage"])
        assert any(
            (r["filament"] or {}).get("id") == "spool-red" for r in api.prints[1]["filamentUsage"]
        )

    def test_a_start_with_nothing_unplaced_sends_no_row_ids(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, four_tool_start(), finished(filament_mm=FOUR_TOOL_USAGE))

        [completion] = api.completions
        assert not any("rowId" in u for u in completion["filamentUsage"])
