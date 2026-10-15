from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fake_moonraker import eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge import __version__
from printlog_bridge.core.api_client import ApiClient, ApiUnavailable
from printlog_bridge.core.connections import Connection, Heartbeat
from printlog_bridge.core.health import ProblemLog
from printlog_bridge.core.outbox import Outbox
from printlog_bridge.core.status import StatusBoard, read_status

VORON = Connection(printer="voron", instance_id="inst-voron", kind="moonraker", printer_id=42)
U1 = Connection(printer="u1", instance_id="inst-u1", kind="moonraker", printer_id=43)
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

NOTHING_KNOWN = {
    "mode": None,
    "klipperVersion": None,
    "moonrakerVersion": None,
    "printerModel": None,
    "outboxDepth": 0,
    "outboxOldestAgeSeconds": None,
    "lastError": None,
    "lastErrorAt": None,
    "errorCount": 0,
    "snapshots": None,
    "slicerSettings": False,
}
"""The health fields (#37) of a printer the bridge knows nothing about yet."""


@contextlib.asynccontextmanager
async def heartbeat(api: FakePrintLogApi, tmp_path, *connections: Connection, **kwargs):
    base = await api.start()
    board = StatusBoard(tmp_path)
    board.start([c.printer for c in connections])
    try:
        async with ApiClient(base, api.api_key) as client:
            yield Heartbeat(client, connections, board, **kwargs)
    finally:
        await api.close()


def run(scenario):
    return asyncio.run(scenario())


def test_registers_each_printer_as_its_own_connection(tmp_path):
    api = FakePrintLogApi(printers=(42, 43))

    async def scenario():
        async with heartbeat(api, tmp_path, VORON, U1) as beat:
            await beat.beat_all()

    run(scenario)

    assert api.heartbeats == [
        (
            "inst-voron",
            {
                "kind": "moonraker",
                "displayName": "printlog-bridge (voron)",
                "agentVersion": __version__,
                "printerId": 42,
                **NOTHING_KNOWN,
            },
        ),
        (
            "inst-u1",
            {
                "kind": "moonraker",
                "displayName": "printlog-bridge (u1)",
                "agentVersion": __version__,
                "printerId": 43,
                **NOTHING_KNOWN,
            },
        ),
    ]
    assert set(api.connections) == {"inst-voron", "inst-u1"}


def test_a_long_printer_name_is_cut_to_the_apis_limit(tmp_path):
    api = FakePrintLogApi()
    long = Connection(printer="v" * 150, instance_id="i", kind="moonraker", printer_id=42)

    async def scenario():
        async with heartbeat(api, tmp_path, long) as beat:
            await beat.beat_all()

    run(scenario)

    [(_, body)] = api.heartbeats
    assert len(body["displayName"]) == 100


def test_beats_again_every_interval(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON, interval=0.05) as beat:
            task = asyncio.ensure_future(beat.run())
            try:
                await eventually(lambda: len(api.heartbeats) >= 3)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    run(scenario)


def test_records_each_heartbeat_for_status(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            await beat.beat_all()

    run(scenario)

    voron = read_status(tmp_path)["printers"]["voron"]
    assert voron["last_heartbeat_at"] is not None
    assert voron["warnings"] == {}


def test_an_outage_is_a_warning_that_clears_when_the_api_is_back(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            api.fail_next(1)
            await beat.beat_all()
            during = read_status(tmp_path)["printers"]["voron"]
            await beat.beat_all()
            return during, read_status(tmp_path)["printers"]["voron"]

    during, after = run(scenario)

    assert during["last_heartbeat_at"] is None
    assert "503" in during["warnings"]["heartbeat"]
    assert after["warnings"] == {}


def test_a_rejected_key_says_what_to_do(tmp_path, caplog):
    api = FakePrintLogApi(api_key="the-real-key")

    async def scenario():
        base = await api.start()
        board = StatusBoard(tmp_path)
        board.start(["voron"])
        try:
            async with ApiClient(base, "a-wrong-key") as client:
                await Heartbeat(client, [VORON], board).beat_all()
        finally:
            await api.close()

    with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
        run(scenario)

    warning = read_status(tmp_path)["printers"]["voron"]["warnings"]["heartbeat"]
    assert "printlog-bridge doctor" in warning
    assert any("printlog-bridge doctor" in r.getMessage() for r in caplog.records)


def test_a_printer_that_is_not_the_accounts_is_a_warning(tmp_path):
    api = FakePrintLogApi(printers=(7,))

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            await beat.beat_all()

    run(scenario)

    warning = read_status(tmp_path)["printers"]["voron"]["warnings"]["heartbeat"]
    assert "not yours" in warning
    assert "printer_id" in warning


def test_ensure_registered_registers_only_once(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            await beat.ensure_registered("voron")
            await beat.ensure_registered("voron")

    run(scenario)

    assert len(api.heartbeats) == 1


def test_ensure_registered_raises_when_3d_print_log_is_unavailable(tmp_path):
    """So the print it runs before waits for the API rather than being created unlinked."""
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            api.fail_next(1)
            with pytest.raises(ApiUnavailable):
                await beat.ensure_registered("voron")

    run(scenario)

    assert api.connections == {}


def test_ensure_registered_does_not_raise_when_the_connection_is_refused(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            api.fail_next(1, status=400)
            await beat.ensure_registered("voron")

    run(scenario)

    assert api.connections == {}


def test_ensure_registered_tries_again_after_a_failure(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            api.fail_next(1)
            with pytest.raises(ApiUnavailable):
                await beat.ensure_registered("voron")
            await beat.ensure_registered("voron")

    run(scenario)

    assert [m for m, _ in api.requests] == ["PUT", "PUT"]
    assert "inst-voron" in api.connections


def test_ensure_registered_ignores_a_printer_it_does_not_know(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            await beat.ensure_registered("nope")

    run(scenario)

    assert api.heartbeats == []


class TestUpdateNotice:
    """The check-in answer says which bridge is the latest, so Docker and remote-mode users hear
    about an update without the bridge calling anyone else (#39)."""

    def beat(self, api, tmp_path, *connections, times=1, **kwargs):
        async def scenario():
            async with heartbeat(api, tmp_path, *connections, **kwargs) as beat:
                for _ in range(times):
                    await beat.beat_all()

        run(scenario)

    def notices(self, caplog):
        return [r.getMessage() for r in caplog.records if "is available" in r.getMessage()]

    def test_an_older_bridge_records_the_newer_version_for_status(self, tmp_path):
        api = FakePrintLogApi()
        api.latest_agent_version = "999.0.0"

        self.beat(api, tmp_path, VORON)

        assert read_status(tmp_path)["latest_version"] == "999.0.0"

    def test_an_older_bridge_logs_the_notice_once_per_version(self, tmp_path, caplog):
        api = FakePrintLogApi(printers=(42, 43))
        api.latest_agent_version = "999.0.0"

        with caplog.at_level(logging.INFO, logger="printlog_bridge"):
            self.beat(api, tmp_path, VORON, U1, times=3)

        [notice] = self.notices(caplog)
        assert notice.startswith(f"printlog-bridge 999.0.0 is available (this is {__version__})")

    def test_a_newer_release_is_logged_again(self, tmp_path, caplog):
        api = FakePrintLogApi()

        async def scenario():
            async with heartbeat(api, tmp_path, VORON) as beat:
                api.latest_agent_version = "998.0.0"
                await beat.beat_all()
                await beat.beat_all()
                api.latest_agent_version = "999.0.0"
                await beat.beat_all()

        with caplog.at_level(logging.INFO, logger="printlog_bridge"):
            run(scenario)

        assert [n.split(" is ")[0] for n in self.notices(caplog)] == [
            "printlog-bridge 998.0.0",
            "printlog-bridge 999.0.0",
        ]
        assert read_status(tmp_path)["latest_version"] == "999.0.0"

    def test_the_current_bridge_says_nothing(self, tmp_path, caplog):
        api = FakePrintLogApi()
        api.latest_agent_version = __version__

        with caplog.at_level(logging.INFO, logger="printlog_bridge"):
            self.beat(api, tmp_path, VORON)

        assert self.notices(caplog) == []
        assert read_status(tmp_path)["latest_version"] == __version__

    def test_an_api_that_does_not_know_says_nothing(self, tmp_path, caplog):
        api = FakePrintLogApi()

        with caplog.at_level(logging.INFO, logger="printlog_bridge"):
            self.beat(api, tmp_path, VORON)

        assert self.notices(caplog) == []
        assert read_status(tmp_path)["latest_version"] is None

    def test_update_check_off_keeps_it_quiet(self, tmp_path, caplog):
        api = FakePrintLogApi()
        api.latest_agent_version = "999.0.0"

        with caplog.at_level(logging.INFO, logger="printlog_bridge"):
            self.beat(api, tmp_path, VORON, update_check=False)

        assert self.notices(caplog) == []
        assert read_status(tmp_path)["latest_version"] is None


# The printer's health in the check-in (#37). tests/test_privacy.py checks what it never holds.


def test_sends_the_printers_software_and_model_once_known(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            report = beat.board.printer("voron")
            report.software("klipper", "v0.12.0-123-gabcdef")
            report.software("moonraker", "v0.9.3-45")
            report.printer_model("InfiMech EX")
            await beat.beat_all()

    run(scenario)

    [(_, body)] = api.heartbeats
    assert body["klipperVersion"] == "v0.12.0-123-gabcdef"
    assert body["moonrakerVersion"] == "v0.9.3-45"
    assert body["printerModel"] == "InfiMech EX"


def test_cuts_the_software_and_model_to_the_apis_limits(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            report = beat.board.printer("voron")
            report.software("klipper", "v" * 80)
            report.printer_model("m" * 150)
            await beat.beat_all()

    run(scenario)

    [(_, body)] = api.heartbeats
    assert len(body["klipperVersion"]) == 50
    assert len(body["printerModel"]) == 100


def test_sends_the_mode_and_which_extras_are_on(tmp_path):
    api = FakePrintLogApi()
    remote = Connection(
        printer="voron",
        instance_id="inst-voron",
        kind="moonraker",
        printer_id=42,
        mode="remote",
        snapshots=False,
        slicer_settings=True,
    )

    async def scenario():
        async with heartbeat(api, tmp_path, remote) as beat:
            await beat.beat_all()

    run(scenario)

    [(_, body)] = api.heartbeats
    assert (body["mode"], body["snapshots"], body["slicerSettings"]) == ("remote", False, True)


def test_sends_what_waits_in_the_outbox_and_for_how_long(tmp_path):
    api = FakePrintLogApi()
    outbox = Outbox.open(tmp_path, clock=lambda: NOW - timedelta(minutes=10))
    outbox.add("voron", "job-1", "start", {})
    outbox.add("voron", "job-1", "thumbnail", {})
    outbox.add("u1", "job-9", "start", {})
    outbox.close()
    outbox = Outbox.open(tmp_path, clock=lambda: NOW - timedelta(minutes=4))
    outbox.add("voron", "job-2", "start", {})
    outbox.close()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON, clock=lambda: NOW) as beat:
            await beat.beat_all()

    run(scenario)

    [(_, body)] = api.heartbeats
    assert body["outboxDepth"] == 3
    assert body["outboxOldestAgeSeconds"] == 600


def problem(printer: str, message: str, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord("printlog_bridge.x", level, __file__, 1, "[printer %s] %s",
                             (printer, message), None)  # fmt: skip


def test_sends_the_last_problem_and_how_many_since_the_last_check_in(tmp_path):
    api = FakePrintLogApi()
    problems = ProblemLog(["voron"], clock=lambda: NOW)

    async def scenario():
        async with heartbeat(api, tmp_path, VORON, problems=problems) as beat:
            problems.handle(problem("voron", "couldn't subscribe: gone"))
            problems.handle(problem("voron", "Moonraker at http://u1.local:7125 closed"))
            await beat.beat_all()
            await beat.beat_all()

    run(scenario)

    [(_, first), (_, second)] = api.heartbeats
    assert first["errorCount"] == 2
    assert first["lastError"] == "Moonraker at <url> closed"
    assert first["lastErrorAt"] == "2026-10-15T12:00:00+00:00"
    assert second["errorCount"] == 0
    assert second["lastError"] == "Moonraker at <url> closed"


def test_a_check_in_that_fails_keeps_its_problems_for_the_next(tmp_path):
    api = FakePrintLogApi()
    problems = ProblemLog(["voron"], clock=lambda: NOW)
    logger = logging.getLogger("printlog_bridge")
    logger.addHandler(problems)

    async def scenario():
        async with heartbeat(api, tmp_path, VORON, problems=problems) as beat:
            problems.handle(problem("voron", "couldn't subscribe: gone"))
            api.fail_next(1)
            await beat.beat_all()  # fails, and says so in the log
            await beat.beat_all()

    try:
        run(scenario)
    finally:
        logger.removeHandler(problems)

    [(_, body)] = api.heartbeats
    assert body["errorCount"] == 2
    assert body["lastError"].startswith("couldn't send the heartbeat")
