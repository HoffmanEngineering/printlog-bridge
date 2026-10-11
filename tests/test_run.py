from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Mapping
from pathlib import Path

from fake_moonraker import FakeMoonraker, eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.cli.main import configure_logging, main
from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.bridge import run_bridge
from printlog_bridge.core.config import BridgeConfig, PrinterConfig
from printlog_bridge.core.lifecycle import JobLogger, LoggedPrinter
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.status import read_status


class WatchingAdapter(Adapter):
    name = "watching"

    def __init__(self, printer: str) -> None:
        self.printer = printer
        self.started = asyncio.Event()
        self.cancelled = False
        self.sink = None
        self.report = None

    @classmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> WatchingAdapter:
        return cls(printer)

    def describe(self) -> str:
        return "watching"

    async def run(self, sink, report) -> None:
        self.sink = sink
        self.report = report
        report.connected()
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def check(self):  # pragma: no cover
        return []


def config_with(state_dir: Path, api_url: str, *adapters: WatchingAdapter) -> BridgeConfig:
    return BridgeConfig(
        path=Path("printlog-bridge.cfg"),
        api_url=api_url,
        api_key="key-123",
        state_dir=state_dir,
        printers=[PrinterConfig(a.printer, a, printer_id=42) for a in adapters],
    )


def running_bridge(tmp_path: Path, *adapters: WatchingAdapter, api=None, until=None, **kwargs):
    """Runs the bridge against the fake API until every adapter started and `until()` holds."""
    api = api or FakePrintLogApi()

    async def scenario():
        base = await api.start()
        try:
            task = asyncio.ensure_future(
                run_bridge(config_with(tmp_path, base, *adapters), **kwargs)
            )
            for adapter in adapters:
                await asyncio.wait_for(adapter.started.wait(), 5)
            if until is not None:
                await eventually(until)
            during = read_status(tmp_path)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return during
        finally:
            await api.close()

    return asyncio.run(scenario())


def test_runs_every_printer_until_cancelled(tmp_path):
    voron, u1 = WatchingAdapter("voron"), WatchingAdapter("u1")

    running_bridge(tmp_path, voron, u1)

    assert voron.cancelled and u1.cancelled
    assert voron.sink is not None


def test_logs_jobs_to_3d_print_log(tmp_path):
    voron = WatchingAdapter("voron")

    running_bridge(tmp_path, voron)
    sink = voron.sink

    assert isinstance(sink, JobLogger)
    assert sink.printers == {"voron": LoggedPrinter(printer_id=42, source="watching")}


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(f"[bridge]\nstate_dir = {tmp_path / 'state'}\n{text}", encoding="utf-8")
    return path


def test_run_without_an_api_key_says_where_to_get_one(tmp_path, capsys):
    config = write_config(tmp_path, "[printer voron]\nprinter_id = 42\nurl = http://voron:7125\n")

    assert main(["run", "--config", str(config)]) == 2

    err = capsys.readouterr().err
    assert "no API key" in err
    assert "https://www.3dprintlog.com/api-keys" in err


def test_run_without_a_printer_id_names_the_printer(tmp_path, capsys):
    config = write_config(
        tmp_path,
        "api_key = k\n[printer voron]\nprinter_id = 42\nurl = http://voron:7125\n"
        "[printer u1]\nurl = http://u1:7125\n",
    )

    assert main(["run", "--config", str(config)]) == 2

    err = capsys.readouterr().err
    assert "[printer u1] has no printer_id" in err
    assert "voron" not in err


def test_run_with_a_bad_config_says_why_and_fails(tmp_path, capsys):
    assert main(["run", "--config", str(tmp_path / "missing.cfg")]) == 2

    err = capsys.readouterr().err
    assert "not found" in err
    assert "printlog-bridge doctor" in err


def test_the_bridge_process_shows_up_in_moonraker(tmp_path):
    """The acceptance check for #3: a real `printlog-bridge run` identifies to Moonraker."""

    async def scenario():
        fake = FakeMoonraker()
        base = await fake.start()
        config = tmp_path / "printlog-bridge.cfg"
        config.write_text(
            # The heartbeat goes to a closed port, not to the real API.
            f"[bridge]\napi_url = http://127.0.0.1:9\napi_key = k\n"
            f"state_dir = {tmp_path / 'state'}\n"
            f"[printer u1]\nprinter_id = 42\nurl = {base}\n",
            encoding="utf-8",
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "printlog_bridge", "run", "--config", str(config),
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        try:
            await eventually(lambda: fake.subscriptions, timeout=20)
            return fake.identified[0]
        finally:
            process.kill()
            await process.wait()
            await fake.close()

    identity = asyncio.run(scenario())

    assert identity["client_name"] == "printlog-bridge"
    assert identity["type"] == "agent"


def test_registers_each_printers_connection(tmp_path):
    api = FakePrintLogApi()
    voron, u1 = WatchingAdapter("voron"), WatchingAdapter("u1")

    running_bridge(tmp_path, voron, u1, api=api, until=lambda: len(api.connections) == 2)

    instance_ids = {name: PrinterState.load(tmp_path, name).instance_id for name in ("voron", "u1")}
    assert {c["instanceId"]: c["displayName"] for c in api.connections.values()} == {
        instance_ids["voron"]: "printlog-bridge (voron)",
        instance_ids["u1"]: "printlog-bridge (u1)",
    }
    assert {c["kind"] for c in api.connections.values()} == {"watching"}


def test_sends_a_heartbeat_every_interval(tmp_path):
    api = FakePrintLogApi()

    running_bridge(
        tmp_path,
        WatchingAdapter("voron"),
        api=api,
        until=lambda: len(api.heartbeats) >= 3,
        heartbeat_interval=0.05,
    )


def test_keeps_the_status_file_up_to_date(tmp_path):
    api = FakePrintLogApi()
    voron = WatchingAdapter("voron")

    during = running_bridge(
        tmp_path,
        voron,
        api=api,
        until=lambda: (
            (read_status(tmp_path) or {})
            .get("printers", {})
            .get("voron", {})
            .get("last_heartbeat_at")
        ),
    )
    after = read_status(tmp_path)

    assert during["stopped_at"] is None
    assert during["printers"]["voron"]["connection"] == "connected"
    assert after["stopped_at"] is not None


def test_touches_the_status_file_while_nothing_happens(tmp_path):
    seen = set()

    def touched_twice():
        status = read_status(tmp_path)
        if status:
            seen.add(status["updated_at"])
        return len(seen) >= 3

    running_bridge(tmp_path, WatchingAdapter("voron"), until=touched_twice, touch_interval=0.05)


def test_run_also_logs_to_a_file_in_the_state_directory_for_support_bundles(tmp_path):
    handlers = configure_logging(tmp_path / "state")
    try:
        logging.getLogger("printlog_bridge.core.bridge").info("watching voron")
        for handler in handlers:
            handler.flush()
        text = (tmp_path / "state" / "bridge.log").read_text(encoding="utf-8")
    finally:
        root_logger = logging.getLogger()
        for handler in handlers:
            root_logger.removeHandler(handler)
            handler.close()

    assert "INFO watching voron" in text
