from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from printlog_bridge import __version__
from printlog_bridge.cli.main import main
from printlog_bridge.core.config import BridgeConfig, PrinterConfig
from printlog_bridge.core.outbox import Outbox
from printlog_bridge.core.state import JobRecord, PrinterState
from printlog_bridge.core.status import StatusBoard, format_status, read_status

NOW = datetime(2026, 10, 10, 9, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def board(tmp_path, clock=None) -> StatusBoard:
    return StatusBoard(tmp_path, clock=clock or Clock())


def test_a_new_board_says_the_bridge_is_running(tmp_path):
    board(tmp_path).start(["voron"])

    status = read_status(tmp_path)

    assert status["version"] == __version__
    assert status["started_at"] == NOW.isoformat()
    assert status["updated_at"] == NOW.isoformat()
    assert status["stopped_at"] is None
    assert status["printers"]["voron"]["connection"] == "starting"


def test_nothing_to_read_before_the_first_run(tmp_path):
    assert read_status(tmp_path) is None


def test_an_unreadable_file_reads_as_nothing(tmp_path):
    (tmp_path / "status.json").write_text("{not json", encoding="utf-8")

    assert read_status(tmp_path) is None


def test_connection_changes_are_recorded_with_when_they_happened(tmp_path):
    clock = Clock()
    b = board(tmp_path, clock)
    b.start(["voron"])
    report = b.printer("voron")

    report.connected()
    clock.now += timedelta(minutes=5)
    report.disconnected("can't reach Moonraker at http://voron:7125")

    voron = read_status(tmp_path)["printers"]["voron"]
    assert voron["connection"] == "disconnected"
    assert voron["connection_detail"] == "can't reach Moonraker at http://voron:7125"
    assert voron["connection_since"] == clock.now.isoformat()


def test_reconnecting_clears_the_reason_it_dropped(tmp_path):
    b = board(tmp_path)
    b.start(["voron"])
    report = b.printer("voron")

    report.disconnected("Moonraker closed the connection")
    report.connected()

    voron = read_status(tmp_path)["printers"]["voron"]
    assert voron["connection"] == "connected"
    assert voron["connection_detail"] is None


def test_staying_connected_keeps_the_time_it_connected(tmp_path):
    clock = Clock()
    b = board(tmp_path, clock)
    b.start(["voron"])
    report = b.printer("voron")

    report.connected()
    clock.now += timedelta(minutes=1)
    report.connected()

    assert read_status(tmp_path)["printers"]["voron"]["connection_since"] == NOW.isoformat()


def test_records_the_printer_state_and_the_last_event(tmp_path):
    b = board(tmp_path)
    b.start(["voron"])
    report = b.printer("voron")

    report.printer_state("ready")
    report.event("job 00000A started: print 17")

    voron = read_status(tmp_path)["printers"]["voron"]
    assert voron["printer_state"] == "ready"
    assert voron["last_event"] == "job 00000A started: print 17"
    assert voron["last_event_at"] == NOW.isoformat()


def test_a_warning_stays_until_its_topic_is_cleared(tmp_path):
    b = board(tmp_path)
    b.start(["voron"])
    report = b.printer("voron")

    report.warn("api", "the API rejected the key")
    report.warn("moonraker", "Moonraker's job history is off")
    report.warn("api", None)

    assert read_status(tmp_path)["printers"]["voron"]["warnings"] == {
        "moonraker": "Moonraker's job history is off"
    }


def test_records_each_heartbeat(tmp_path):
    b = board(tmp_path)
    b.start(["voron"])

    b.printer("voron").heartbeat()

    assert read_status(tmp_path)["printers"]["voron"]["last_heartbeat_at"] == NOW.isoformat()


def test_touch_shows_the_bridge_is_still_alive(tmp_path):
    clock = Clock()
    b = board(tmp_path, clock)
    b.start(["voron"])

    clock.now += timedelta(seconds=60)
    b.touch()

    assert read_status(tmp_path)["updated_at"] == clock.now.isoformat()


def test_stopping_is_recorded(tmp_path):
    b = board(tmp_path)
    b.start(["voron"])

    b.stopped()

    assert read_status(tmp_path)["stopped_at"] == NOW.isoformat()


def test_a_new_run_starts_from_a_clean_slate(tmp_path):
    first = board(tmp_path)
    first.start(["voron", "u1"])
    first.printer("voron").warn("api", "old news")
    first.stopped()

    board(tmp_path).start(["voron"])

    status = read_status(tmp_path)
    assert status["stopped_at"] is None
    assert list(status["printers"]) == ["voron"]
    assert status["printers"]["voron"]["warnings"] == {}


def test_the_file_is_plain_json(tmp_path):
    board(tmp_path).start(["voron"])

    assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))


def test_a_full_or_read_only_disk_never_stops_the_bridge(tmp_path, caplog):
    # A directory where the file goes makes every write fail, as a full disk would.
    (tmp_path / "status.json").mkdir()
    b = board(tmp_path)

    with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
        b.start(["voron"])
        b.printer("voron").connected()
        b.printer("voron").warn("api", "the API answered 503")
        b.touch()
        b.stopped()

    assert b.get("voron")["connection"] == "connected"
    assert caplog.text.count("couldn't write") == 1  # once, not on every change


def test_writes_again_once_the_disk_has_room(tmp_path, caplog):
    (tmp_path / "status.json").mkdir()
    b = board(tmp_path)
    with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
        b.start(["voron"])
        b.printer("voron").connected()
    (tmp_path / "status.json").rmdir()

    b.touch()

    assert read_status(tmp_path)["printers"]["voron"]["connection"] == "connected"


# -- printlog-bridge status ---------------------------------------------------------------------


class FakeAdapter:
    name = "fake"

    def describe(self) -> str:
        return "fake, remote http://voron:7125"


def config(tmp_path, *printers) -> BridgeConfig:
    return BridgeConfig(
        path=tmp_path / "printlog-bridge.cfg",
        api_url="https://api.example.test",
        api_key="k",
        state_dir=tmp_path,
        printers=[
            PrinterConfig(name, FakeAdapter(), printer_id=printer_id)
            for name, printer_id in printers or [("voron", 42)]
        ],
    )


def report(tmp_path, at: datetime, *printers):
    return format_status(config(tmp_path, *printers), now=at)


def test_status_before_the_first_run_says_how_to_start(tmp_path):
    text, code = report(tmp_path, NOW)

    assert code == 1
    assert "hasn't run yet" in text
    assert "printlog-bridge run" in text


def test_status_of_a_running_bridge(tmp_path):
    clock = Clock()
    b = board(tmp_path, clock)
    b.start(["voron"])
    voron = b.printer("voron")
    voron.connected()
    voron.printer_state("ready")
    voron.heartbeat()
    clock.now += timedelta(minutes=3)
    voron.event("job 00000A started: print 17, 'Benchy'")
    clock.now += timedelta(seconds=30)
    b.touch()

    text, code = report(tmp_path, clock.now + timedelta(seconds=10))

    assert code == 0
    assert "running since 2026-10-10 09:30 UTC" in text
    assert "Printer voron (printer 42 in 3D Print Log)" in text
    assert "Connection: connected for 3m 40s (fake, remote http://voron:7125)" in text
    assert "Printer state: ready" in text
    assert "Pairing: not paired; logging with the API key in [bridge]" in text
    assert "Last event: job 00000A started: print 17, 'Benchy' (40s ago)" in text
    assert "Heartbeat: 3m 40s ago" in text
    assert "Outbox: empty" in text
    assert "Warnings: none" in text


def test_status_lists_each_warning(tmp_path):
    b = board(tmp_path)
    b.start(["voron"])
    b.printer("voron").warn("api", "job 1 isn't logged yet: 503")
    b.printer("voron").warn("history", "Moonraker's job history is off")

    text, _ = report(tmp_path, NOW)

    assert (
        "Warnings:\n    - job 1 isn't logged yet: 503\n    - Moonraker's job history is off" in text
    )


def test_status_shows_why_the_printer_is_disconnected(tmp_path):
    clock = Clock()
    b = board(tmp_path, clock)
    b.start(["voron"])
    b.printer("voron").disconnected("can't reach Moonraker at http://voron:7125")
    clock.now += timedelta(seconds=90)
    b.touch()

    text, _ = report(tmp_path, clock.now)

    assert "Connection: disconnected for 1m 30s: can't reach Moonraker at http://voron:7125" in text


def test_status_counts_the_jobs_waiting_in_the_outbox(tmp_path):
    board(tmp_path).start(["voron"])
    outbox = Outbox.open(tmp_path, clock=lambda: NOW - timedelta(minutes=12))
    outbox.add("voron", "1", "start", {})
    outbox.add("voron", "1", "finish", {})  # the same print: one job
    outbox.add("voron", "2", "finish", {})
    outbox.add("u1", "3", "start", {})  # another printer's
    outbox.failed(outbox.heads()[0].seq, "the API answered 503 for /api/Prints")
    outbox.close()

    text, _ = report(tmp_path, NOW)

    assert (
        "Outbox: 2 jobs waiting to be logged, the oldest for 12m; last try: the API answered "
        "503 for /api/Prints" in text
    )


def test_status_counts_one_waiting_job(tmp_path):
    board(tmp_path).start(["voron"])
    outbox = Outbox.open(tmp_path, clock=lambda: NOW - timedelta(seconds=5))
    outbox.add("voron", "1", "start", {})
    outbox.close()

    text, _ = report(tmp_path, NOW)

    assert "Outbox: 1 job waiting to be logged, for 5s\n" in text


def test_status_ignores_jobs_the_state_file_alone_says_are_waiting(tmp_path):
    board(tmp_path).start(["voron"])
    state = PrinterState.load(tmp_path, "voron")
    state.jobs["1"] = JobRecord(started_at=NOW.isoformat())  # the outbox has nothing for it
    state.save()

    text, _ = report(tmp_path, NOW)

    assert "Outbox: empty" in text


def test_status_does_not_create_a_state_file(tmp_path):
    board(tmp_path).start(["voron"])

    report(tmp_path, NOW)

    assert not (tmp_path / "printers").exists()


def test_a_stale_heartbeat_says_the_app_shows_the_printer_offline(tmp_path):
    clock = Clock()
    b = board(tmp_path, clock)
    b.start(["voron"])
    b.printer("voron").heartbeat()
    clock.now += timedelta(minutes=20)
    b.touch()

    text, _ = report(tmp_path, clock.now)

    assert "Heartbeat: 20m ago; 3D Print Log shows the printer as offline" in text


def test_no_heartbeat_yet_says_so(tmp_path):
    board(tmp_path).start(["voron"])

    text, _ = report(tmp_path, NOW)

    assert "Heartbeat: none yet" in text


def test_a_stopped_bridge_says_so(tmp_path):
    clock = Clock()
    b = board(tmp_path, clock)
    b.start(["voron"])
    clock.now += timedelta(hours=1)
    b.stopped()

    text, code = report(tmp_path, clock.now + timedelta(minutes=5))

    assert code == 1
    assert "not running: stopped at 2026-10-10 10:30 UTC" in text
    assert "Connection: not running" in text


def test_a_bridge_that_stopped_checking_in_may_have_crashed(tmp_path):
    board(tmp_path).start(["voron"])

    text, code = report(tmp_path, NOW + timedelta(minutes=10))

    assert code == 1
    assert "not running: it last checked in 10m ago" in text


def test_a_printer_added_since_the_last_run_is_listed(tmp_path):
    board(tmp_path).start(["voron"])

    text, _ = report(tmp_path, NOW, ("voron", 42), ("u1", None))

    assert "Printer u1 (no printer_id yet)" in text
    assert "Connection: not watched by the running bridge; restart it" in text


class TestStatusCommand:
    def write_config(self, tmp_path):
        path = tmp_path / "printlog-bridge.cfg"
        path.write_text(
            f"[bridge]\napi_key = k\nstate_dir = {tmp_path / 'state'}\n"
            "[printer voron]\nprinter_id = 42\nurl = http://voron:7125\n",
            encoding="utf-8",
        )
        return path

    def test_reports_a_running_bridge_and_exits_zero(self, tmp_path, capsys):
        path = self.write_config(tmp_path)
        b = StatusBoard(tmp_path / "state")
        b.start(["voron"])
        b.printer("voron").connected()

        assert main(["status", "--config", str(path)]) == 0

        out = capsys.readouterr().out
        assert out.startswith(f"printlog-bridge {__version__} status\n")
        assert "Bridge: running" in out
        assert "moonraker, remote http://voron:7125" in out

    def test_exits_one_when_the_bridge_is_not_running(self, tmp_path, capsys):
        assert main(["status", "--config", str(self.write_config(tmp_path))]) == 1

        assert "hasn't run yet" in capsys.readouterr().out

    def test_a_bad_config_says_why_and_exits_two(self, tmp_path, capsys):
        assert main(["status", "--config", str(tmp_path / "missing.cfg")]) == 2

        assert "not found" in capsys.readouterr().err
