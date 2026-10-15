"""`printlog-bridge backfill` (#8): the printer's job history, logged as imported prints."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from fake_moonraker import FakeMoonraker
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.adapters.moonraker.adapter import MoonrakerAdapter
from printlog_bridge.cli.main import main
from printlog_bridge.core import backfill as backfill_module
from printlog_bridge.core.adapter import PrinterUnavailable
from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.backfill import (
    Backfill,
    BackfillStopped,
    BackfillSummary,
    format_summary,
)
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted, PastJob
from printlog_bridge.core.lifecycle import LoggedPrinter
from printlog_bridge.core.state import JobRecord, PrinterState

T0 = 1_788_000_000.0  # 2026-08-29, on the printer's clock
HOUR = 3600.0
TARGET = LoggedPrinter(printer_id=42, source="moonraker")


def at(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, timezone.utc)


def past(
    job_id: str,
    *,
    start: float = T0,
    outcome: JobOutcome = JobOutcome.COMPLETED,
    filament_mm: dict[int, float] | None = None,
    file_name: str = "benchy_PLA_1h.gcode",
    exists: bool = True,
    note: str | None = None,
) -> PastJob:
    return PastJob(
        start=JobStarted(
            printer="voron",
            job_id=job_id,
            file_name=file_name,
            started_at=at(start),
            estimated_duration_s=3000.4,
            estimated_filament_mm=1200.0,
            estimated_filament_g=3.6,
        ),
        end=JobFinished(
            printer="voron",
            job_id=job_id,
            outcome=outcome,
            finished_at=at(start + HOUR),
            duration_s=3300.6,
            filament_mm={0: 1234.5} if filament_mm is None else filament_mm,
            note=note,
        ),
        file_exists=exists,
    )


class FakeClock:
    """The backfill's clock and sleep: sleeping moves the clock on, without waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)


class TimedRequests(list):
    """The fake API's request log, with the backfill's clock at each request."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock
        self.times: list[float] = []

    def append(self, item) -> None:
        self.times.append(self.clock.now)
        super().append(item)


def run_backfill(tmp_path, jobs, *, api=None, state_jobs=None, clock=None, **kwargs):
    """Backfills `jobs` against the fake API. Returns the summary, the API, what was said, and
    the state the backfill saw."""
    api = api or FakePrintLogApi()
    clock = clock or FakeClock()
    said: list[str] = []
    state = PrinterState.load(tmp_path, "voron")
    state.jobs.update(state_jobs or {})
    state.save()

    async def scenario():
        base = await api.start()
        try:
            async with ApiClient(base, "key-123") as client:
                runner = Backfill(
                    client, sleep=clock.sleep, clock=clock.monotonic, say=said.append, **kwargs
                )
                return await runner.run("voron", TARGET, state, jobs, history="Moonraker")
        finally:
            await api.close()

    return asyncio.run(scenario()), api, said, state


class TestCreating:
    def test_each_finished_job_becomes_an_imported_print_with_its_external_id(self, tmp_path):
        summary, api, _, state = run_backfill(tmp_path, [past("00000A")])

        assert summary.created == 1
        [body] = api.created
        assert body["imported"] is True
        assert body["externalSource"] == "moonraker"
        assert body["externalId"] == f"{state.instance_id}:00000A"
        assert body["connectionInstanceId"] == state.instance_id
        assert body["printerId"] == 42
        assert body["title"] == "Benchy"
        assert body["fileName"] == "benchy_PLA_1h.gcode"
        assert body["startDate"] == at(T0).isoformat()
        assert body["status"] == 3
        assert body["printTimeInSeconds"] == 3301
        assert body["estimatedPrintTimeInSeconds"] == 3000
        assert body["estimatedFilamentUsageMg"] == 3600
        assert body["viewStatus"] == 3  # private, unless the account says otherwise
        assert body["allowComments"] is False

    def test_is_tagged_as_imported_in_its_notes(self, tmp_path):
        _, api, _, _ = run_backfill(tmp_path, [past("00000A")])

        assert api.created[0]["notes"] == "Imported from Moonraker history"

    def test_how_the_job_ended_sets_the_status_and_the_note(self, tmp_path):
        jobs = [
            past("00000A", outcome=JobOutcome.CANCELLED),
            past("00000B", start=T0 + HOUR, outcome=JobOutcome.FAILED, note="Klipper shutdown"),
        ]
        _, api, _, _ = run_backfill(tmp_path, jobs)

        cancelled, failed = api.created
        assert cancelled["status"] == 4
        assert failed["status"] == 5
        assert failed["notes"] == "Imported from Moonraker history\n\nKlipper shutdown"

    def test_usage_is_the_jobs_total_on_one_row_with_no_slot_and_no_spool(self, tmp_path):
        _, api, _, _ = run_backfill(tmp_path, [past("00000A", filament_mm={0: 600.0, 2: 400.0})])

        [row] = api.created[0]["filamentUsage"]
        assert row == {
            "lengthInM": 1.0,
            "source": 2,
            "estimatedLengthInM": 1.2,
            "estimatedAmountMg": 3600,
            "estimatedSource": 2,
        }

    def test_a_job_that_fed_no_filament_gets_no_usage_row(self, tmp_path):
        _, api, _, _ = run_backfill(tmp_path, [past("00000A", filament_mm={0: 0.0})])

        assert "filamentUsage" not in api.created[0]

    def test_uses_the_accounts_default_visibility(self, tmp_path):
        api = FakePrintLogApi()
        api.settings = [
            {"userSettingTypeId": 1, "value": "Public"},
            {"userSettingTypeId": 3, "value": "true"},
        ]
        _, api, _, _ = run_backfill(tmp_path, [past("00000A")], api=api)

        assert api.created[0]["viewStatus"] == 1
        assert api.created[0]["allowComments"] is True

    def test_says_what_it_did_with_each_job(self, tmp_path):
        jobs = [past("00000A"), past("00000B", start=T0 + HOUR, exists=False)]
        _, _, said, _ = run_backfill(tmp_path, jobs)

        assert said[0].startswith("[1/2] ")
        assert "Benchy" in said[0] and "created" in said[0]
        assert said[1].startswith("[2/2] ")
        assert "file no longer exists" in said[1]


class TestNoDuplicates:
    def test_running_it_twice_creates_no_duplicates(self, tmp_path):
        jobs = [past(f"00000{n}", start=T0 + n * HOUR) for n in range(1, 4)]
        api = FakePrintLogApi()
        first, api, _, _ = run_backfill(tmp_path, jobs, api=api)
        second, api, _, _ = run_backfill(tmp_path, jobs, api=api)

        assert (first.created, first.present) == (3, 0)
        assert (second.created, second.present) == (0, 3)
        assert len(api.prints) == 3

    def test_a_job_the_bridge_logged_live_is_left_as_it_is(self, tmp_path):
        # Logged live with its spool, then dropped from the state file (it keeps the last 20).
        api = FakePrintLogApi()
        spool = api.add_spool()
        state = PrinterState.load(tmp_path, "voron")
        live = api.user_creates(
            "Benchy, renamed",
            status=6,
            externalSource="moonraker",
            externalId=f"{state.instance_id}:00000A",
            usage=[{"slot": 0, "filament": {"id": spool}, "amountMg": 22_000}],
        )
        before = dict(api.prints[live])

        summary, api, _, _ = run_backfill(tmp_path, [past("00000A")], api=api)

        assert (summary.created, summary.present) == (0, 1)
        assert api.prints[live] == before
        assert len(api.prints) == 1


class TestInventory:
    def test_the_spool_loaded_on_the_printer_stays_loaded(self, tmp_path):
        # Any other create on a single-tool printer makes the print's spools (none) the loaded
        # ones, which would unload the spool in the printer today.
        api = FakePrintLogApi()
        spool = api.add_spool()
        api.user_loads(42, 0, spool)

        _, api, _, _ = run_backfill(tmp_path, [past("00000A")], api=api)

        assert api.slot_spools(42) == {0: spool}
        [row] = next(iter(api.prints.values()))["filamentUsage"]
        assert row["filament"] is None

    def test_the_summary_says_inventory_is_left_alone(self):
        text = format_summary("voron", BackfillSummary(jobs=1, created=1), days=90)

        assert "don't change your spools' remaining filament" in text


class TestSkipping:
    def test_a_job_whose_file_no_longer_exists_is_skipped(self, tmp_path):
        summary, api, _, _ = run_backfill(tmp_path, [past("00000A", exists=False)])

        assert summary.missing_file == 1
        assert api.created == []

    def test_jobs_the_running_bridge_logs_itself_are_left_to_it(self, tmp_path):
        # The bridge knows 00000C and is still logging it: it replays everything from there.
        state_jobs = {
            "00000C": JobRecord(started_at=at(T0 + 2 * HOUR).isoformat()),
            "00000B": JobRecord(started_at=at(T0 + HOUR).isoformat(), finished=True, print_id=9),
        }
        jobs = [
            past("00000A"),
            past("00000B", start=T0 + HOUR),
            past("00000C", start=T0 + 2 * HOUR),
            past("00000D", start=T0 + 3 * HOUR),
        ]
        summary, api, said, _ = run_backfill(tmp_path, jobs, state_jobs=state_jobs)

        assert [b["externalId"].split(":")[1] for b in api.created] == ["00000A"]
        assert summary.live == 3
        assert "left to the bridge" in said[1]

    def test_without_any_job_logged_live_every_job_is_backfilled(self, tmp_path):
        jobs = [past("00000A"), past("00000B", start=T0 + HOUR)]
        summary, _, _, _ = run_backfill(tmp_path, jobs)

        assert summary.created == 2


class TestPacing:
    def test_sends_at_most_one_request_per_second(self, tmp_path):
        clock = FakeClock()
        api = FakePrintLogApi()
        api.requests = TimedRequests(clock)
        jobs = [past(f"0000{n:02X}", start=T0 + n * HOUR) for n in range(1, 6)]

        run_backfill(tmp_path, jobs, api=api, clock=clock)

        times = api.requests.times
        assert len(times) == 6  # the account's defaults, then one create per job
        assert all(b - a >= 1.0 for a, b in zip(times, times[1:]))

    def test_a_429_is_waited_out_as_long_as_it_asks_and_the_job_retried(self, tmp_path):
        clock = FakeClock()
        api = FakePrintLogApi()
        api.fail_next(1, status=429, headers={"Retry-After": "7"})

        summary, api, _, _ = run_backfill(tmp_path, [past("00000A")], api=api, clock=clock)

        assert summary.created == 1
        assert max(clock.sleeps) >= 7

    def test_an_outage_is_waited_out_with_a_growing_backoff(self, tmp_path):
        clock = FakeClock()
        api = FakePrintLogApi()
        api.fail_next(4, status=503)

        summary, _, _, _ = run_backfill(tmp_path, [past("00000A")], api=api, clock=clock)

        assert summary.created == 1
        assert [s for s in clock.sleeps if s > 1] == [2.0, 4.0, 8.0, 16.0]

    def test_backfilling_500_jobs_completes_without_manual_retries(self, tmp_path):
        class Flaky(FakePrintLogApi):
            """Rate limits now and then, and has the odd bad moment."""

            count = 0

            @web.middleware
            async def _middleware(self, request, handler):
                self.count += 1
                if self.count % 37 == 0:
                    return web.Response(status=429, headers={"Retry-After": "3"})
                if self.count % 101 == 0:
                    return web.Response(status=503)
                return await super()._middleware(request, handler)

        jobs = [past(f"{n:06X}", start=T0 + n * HOUR) for n in range(1, 501)]
        summary, api, said, _ = run_backfill(tmp_path, jobs, api=Flaky())

        assert summary.created == 500
        assert len(api.prints) == 500
        assert said[-1].startswith("[500/500] ")


class TestStopping:
    def test_a_job_the_api_refuses_is_counted_and_the_rest_go_on(self, tmp_path):
        api = FakePrintLogApi(printers=(7,))  # printer 42 isn't the account's: every create 400s
        jobs = [past("00000A"), past("00000B", start=T0 + HOUR)]

        summary, api, said, _ = run_backfill(tmp_path, jobs, api=api)

        assert summary.refused == 2
        assert len(api.created) == 2
        assert all("refused" in line for line in said)

    def test_a_rejected_key_stops_it(self, tmp_path):
        api = FakePrintLogApi(api_key="another-key")

        with pytest.raises(BackfillStopped, match="rejected the API key"):
            run_backfill(tmp_path, [past("00000A")], api=api)

    def test_account_settings_it_cant_read_stop_it(self, tmp_path):
        api = FakePrintLogApi()
        api.fail_next(1, status=403)

        with pytest.raises(BackfillStopped, match="403"):
            run_backfill(tmp_path, [past("00000A")], api=api)

        assert api.created == []

    def test_an_api_out_of_reach_too_long_stops_it_saying_to_run_it_again(self, tmp_path):
        api = FakePrintLogApi()
        api.fail_next(50, status=503)

        with pytest.raises(BackfillStopped, match="Run it again"):
            run_backfill(tmp_path, [past("00000A")], api=api, max_attempts=3)


class TestMoonrakerHistory:
    @staticmethod
    def past_jobs(fake: FakeMoonraker, since: datetime):
        async def scenario():
            base = await fake.start()
            try:
                adapter = MoonrakerAdapter("voron", url=base, socket=None)
                return await adapter.past_jobs(since)
            finally:
                await fake.close()

        return asyncio.run(scenario())

    @staticmethod
    async def add(fake, n, *, status="completed", exists=True, used=1500.0):
        job = await fake.start_job(f"part{n}.gcode", notify=False, start_time=T0 + n * HOUR)
        if status != "in_progress":
            await fake.finish_job(job, status, filament_used=used, notify=False)
        job["exists"] = exists
        return job

    def test_reads_every_finished_job_since_the_date_page_by_page_oldest_first(self):
        fake = FakeMoonraker()

        async def fill():
            for n in range(1, 131):
                await self.add(fake, n)
            await self.add(fake, 131, status="in_progress")

        asyncio.run(fill())

        jobs = self.past_jobs(fake, at(T0 + 10.5 * HOUR))

        assert [j.start.file_name for j in jobs] == [f"part{n}.gcode" for n in range(11, 131)]
        assert jobs[0].start.started_at == at(T0 + 11 * HOUR)
        assert jobs[0].end.outcome is JobOutcome.COMPLETED
        assert sum(jobs[0].end.filament_mm.values()) == pytest.approx(1500.0)
        assert all(j.file_exists for j in jobs)

    def test_says_which_files_no_longer_exist_and_how_jobs_ended(self):
        fake = FakeMoonraker()

        async def fill():
            await self.add(fake, 1, exists=False)
            await self.add(fake, 2, status="klippy_shutdown")

        asyncio.run(fill())

        gone, failed = self.past_jobs(fake, at(T0))

        assert not gone.file_exists
        assert failed.end.outcome is JobOutcome.FAILED
        assert failed.end.note == "Klipper shutdown"

    def test_without_history_says_how_to_turn_it_on(self):
        with pytest.raises(PrinterUnavailable, match=r"\[history\]"):
            self.past_jobs(FakeMoonraker(history=False), at(T0))

    def test_a_printer_out_of_reach_says_where_it_looked(self):
        adapter = MoonrakerAdapter("voron", url="http://127.0.0.1:9", socket=None)

        with pytest.raises(PrinterUnavailable, match="127.0.0.1:9"):
            asyncio.run(adapter.past_jobs(at(T0)))


class TestCommand:
    @staticmethod
    def write_config(tmp_path, api_url: str, moonraker_url: str):
        path = tmp_path / "printlog-bridge.cfg"
        path.write_text(
            f"[bridge]\napi_url = {api_url}\napi_key = key-123\nstate_dir = {tmp_path / 'state'}\n"
            f"[printer voron]\nadapter = moonraker\nurl = {moonraker_url}\nprinter_id = 42\n"
            "discovery = false\n",
            encoding="utf-8",
        )
        return path

    def backfill(self, tmp_path, monkeypatch, args, *, with_state=True, fill=None):
        monkeypatch.setattr(backfill_module, "REQUEST_INTERVAL_S", 0.0)
        api, fake = FakePrintLogApi(), FakeMoonraker()
        if with_state:
            PrinterState.load(tmp_path / "state", "voron")
        exit_code = {}

        async def scenario():
            api_url, moonraker_url = await api.start(), await fake.start()
            try:
                if fill is not None:
                    await fill(fake)
                path = self.write_config(tmp_path, api_url, moonraker_url)
                loop = asyncio.get_running_loop()
                # main() runs its own event loop, so run it off this one.
                exit_code["value"] = await loop.run_in_executor(
                    None, main, ["backfill", *args, "--config", str(path)]
                )
            finally:
                await fake.close()
                await api.close()

        asyncio.run(scenario())
        return exit_code["value"], api

    def test_backfills_the_last_days_and_prints_a_summary(self, tmp_path, monkeypatch, capsys):
        now = datetime.now(timezone.utc).timestamp()

        async def fill(fake):
            for n, days_ago in enumerate((120, 30, 2), 1):
                job = await fake.start_job(
                    f"part{n}.gcode", notify=False, start_time=now - days_ago * 86400
                )
                await fake.finish_job(job, notify=False)

        exit_code, api = self.backfill(tmp_path, monkeypatch, ["--days", "90"], fill=fill)

        out = capsys.readouterr().out
        assert exit_code == 0
        assert [b["fileName"] for b in api.created] == ["part2.gcode", "part3.gcode"]
        assert "[1/2]" in out and "[2/2]" in out
        assert "2 created" in out
        assert "remaining filament" in out

    def test_needs_the_bridge_to_have_run_once(self, tmp_path, monkeypatch, capsys):
        exit_code, api = self.backfill(tmp_path, monkeypatch, [], with_state=False)

        assert exit_code == 2
        assert "printlog-bridge run" in capsys.readouterr().err
        assert api.created == []
        assert not (tmp_path / "state" / "printers").exists()

    def test_days_must_be_a_positive_number(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exited:
            main(["backfill", "--days", "0"])

        assert exited.value.code == 2

    def test_defaults_to_90_days(self, tmp_path, monkeypatch, capsys):
        now = datetime.now(timezone.utc).timestamp()

        async def fill(fake):
            for n, days_ago in enumerate((91, 89), 1):
                job = await fake.start_job(
                    f"part{n}.gcode", notify=False, start_time=now - days_ago * 86400
                )
                await fake.finish_job(job, notify=False)

        _, api = self.backfill(tmp_path, monkeypatch, [], fill=fill)

        assert [b["fileName"] for b in api.created] == ["part2.gcode"]


def test_a_since_date_is_this_many_days_back():
    now = at(T0)

    assert backfill_module.since(90, now) == now - timedelta(days=90)
