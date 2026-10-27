from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
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
        self._started: asyncio.Event | None = None
        self.cancelled = False
        self.sink = None
        self.report = None

    @property
    def started(self) -> asyncio.Event:
        # Made on first use, inside the test's loop: Python 3.9 binds an Event to the loop
        # current when it is created, and these adapters are made before asyncio.run.
        if self._started is None:
            self._started = asyncio.Event()
        return self._started

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


@pytest.fixture
def root_logging():
    """Takes away the handlers `run` adds to the root logger (it pairs in the log)."""
    before = list(logging.getLogger().handlers)
    yield
    for handler in logging.getLogger().handlers[:]:
        if handler not in before:
            logging.getLogger().removeHandler(handler)
            handler.close()


def test_run_without_credentials_says_where_to_get_them_when_it_cant_pair(
    tmp_path, capsys, root_logging
):
    # Nothing answers at the API's address, so pairing can't even ask whether it's on.
    config = write_config(
        tmp_path,
        "api_url = http://127.0.0.1:9\n[printer voron]\nprinter_id = 42\nurl = http://voron:7125\n",
    )

    assert main(["run", "--config", str(config)]) == 2

    err = capsys.readouterr().err
    assert "not paired with 3D Print Log yet" in err
    assert "printlog-bridge pair" in err
    assert "https://www.3dprintlog.com/api-keys" in err


async def _start_run(env: dict, *args: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "printlog_bridge", "run", *args,
        stderr=asyncio.subprocess.PIPE, env={**os.environ, **env},
    )  # fmt: skip


class Lines:
    """What a running bridge has written to stderr so far."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.text = ""
        self.task = asyncio.ensure_future(self._read(process))

    async def _read(self, process: asyncio.subprocess.Process) -> None:
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            self.text += line.decode(errors="replace")


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.kill()
        await process.wait()


def test_run_with_no_config_waits_for_printers_then_watches_them(tmp_path):
    """A new container: no config at all. The bridge (already holding an API key) writes a
    starter config, says what to add, stays up, and starts once a printer is added."""
    config, state = tmp_path / "printlog-bridge.cfg", tmp_path / "state"
    env = {
        "PRINTLOG_BRIDGE_CONFIG": str(config),
        "PRINTLOG_BRIDGE_STATE_DIR": str(state),
        "PRINTLOG_API_KEY": "k",
    }

    async def scenario():
        fake = FakeMoonraker()
        base = await fake.start()
        process = await _start_run(env)
        out = Lines(process)
        try:
            await eventually(lambda: "No printers to watch yet" in out.text, timeout=20)
            assert config.exists()
            status = read_status(state)
            assert status is not None and status["stopped_at"] is None
            assert status["setup_problem"]
            assert process.returncode is None
            config.write_text(
                # The heartbeat goes to a closed port, not to the real API.
                f"[bridge]\napi_url = http://127.0.0.1:9\n[printer u1]\nprinter_id = 42\n"
                f"url = {base}\n",
                encoding="utf-8",
            )
            await eventually(lambda: fake.subscriptions, timeout=20)
            return out.text
        finally:
            await _stop(process)
            await fake.close()

    text = asyncio.run(scenario())

    assert "starter config" in text
    assert "starting to watch u1" in text
    assert "Traceback" not in text


def test_run_without_a_printer_id_names_the_printer_and_waits(tmp_path):
    config = write_config(
        tmp_path,
        "api_key = k\n[printer voron]\nprinter_id = 42\nurl = http://voron:7125\n"
        "[printer u1]\nurl = http://u1:7125\n",
    )

    async def scenario():
        process = await _start_run({}, "--config", str(config))
        out = Lines(process)
        try:
            await eventually(lambda: "has no printer_id" in out.text, timeout=20)
            await asyncio.sleep(0.5)
            assert process.returncode is None
            return out.text
        finally:
            await _stop(process)

    text = asyncio.run(scenario())

    assert "[printer u1] has no printer_id" in text
    assert "[printer voron]" not in text


def test_status_and_pair_work_before_there_is_a_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PRINTLOG_BRIDGE_CONFIG", str(tmp_path / "printlog-bridge.cfg"))
    monkeypatch.setenv("PRINTLOG_BRIDGE_STATE_DIR", str(tmp_path / "state"))

    assert main(["status"]) == 1

    assert "hasn't run yet" in capsys.readouterr().out


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


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is how systemd stops a service")
def test_sigterm_stops_the_bridge_like_ctrl_c(tmp_path):
    """`systemctl stop` sends SIGTERM: the bridge records that it stopped and exits cleanly."""

    async def scenario():
        fake = FakeMoonraker()
        base = await fake.start()
        state = tmp_path / "state"
        config = tmp_path / "printlog-bridge.cfg"
        config.write_text(
            # The heartbeat goes to a closed port, not to the real API.
            f"[bridge]\napi_url = http://127.0.0.1:9\napi_key = k\nstate_dir = {state}\n"
            f"[printer u1]\nprinter_id = 42\nurl = {base}\n",
            encoding="utf-8",
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "printlog_bridge", "run", "--config", str(config),
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        try:
            await eventually(lambda: fake.subscriptions, timeout=20)
            process.terminate()
            _, err = await asyncio.wait_for(process.communicate(), 10)
            return process.returncode, err.decode(), read_status(state)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await fake.close()

    code, err, status = asyncio.run(scenario())

    assert code == 0, err
    assert status["stopped_at"] is not None
    assert "Traceback" not in err


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


def test_a_paired_bridge_with_no_printers_runs_and_starts_again_when_one_is_added(tmp_path):
    """#100: printers can come from the app, so a paired bridge with an empty config checks in
    instead of waiting; adding a [printer] to the file still starts it, with no restart."""
    from printlog_bridge.core.device import write_device_token

    config, state = tmp_path / "printlog-bridge.cfg", tmp_path / "state"
    env = {"PRINTLOG_BRIDGE_CONFIG": str(config), "PRINTLOG_BRIDGE_STATE_DIR": str(state)}

    async def scenario():
        api = FakePrintLogApi()
        url = await api.start()
        token = api.issue_device_token()
        write_device_token(state, token=token, api_url=url, grant_id=1, name="b",
                           scopes=sorted(api.grant_for(token)["scopes"]))  # fmt: skip
        config.write_text(f"[bridge]\napi_url = {url}\ndiscovery = false\n", encoding="utf-8")
        fake = FakeMoonraker()
        base = await fake.start()
        process = await _start_run(env)
        out = Lines(process)
        try:
            await eventually(lambda: api.bridges, timeout=20)
            status = read_status(state)
            assert status["stopped_at"] is None and "no printers yet" in status["setup_problem"]
            with config.open("a", encoding="utf-8") as f:
                f.write(f"[printer u1]\nprinter_id = 42\nurl = {base}\n")
            await eventually(lambda: fake.subscriptions, timeout=20)
            return out.text
        finally:
            await _stop(process)
            await fake.close()
            await api.close()

    text = asyncio.run(scenario())
    assert "starting again with it" in text
    assert "Traceback" not in text


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fetch(url: str) -> str:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.read().decode("utf-8")
    except OSError:
        return ""


def test_run_serves_the_local_page_with_the_code_to_approve_while_it_pairs(tmp_path):
    """A new container: the local page shows the pairing code (and its QR code) at once, and
    that the bridge is paired once it's approved."""
    from fake_auth0 import FakeAuth0

    port = _free_port()
    url = f"http://127.0.0.1:{port}/"

    async def scenario():
        auth0, api = FakeAuth0(interval=0.2), FakePrintLogApi()
        await auth0.start()
        api_url = await api.start()
        api.auth0, api.pairing = auth0, auth0.discovery()
        config = tmp_path / "printlog-bridge.cfg"
        config.write_text(f"[bridge]\napi_url = {api_url}\n", encoding="utf-8")
        env = {
            "PRINTLOG_BRIDGE_CONFIG": str(config),
            "PRINTLOG_BRIDGE_STATE_DIR": str(tmp_path / "state"),
            "PRINTLOG_BRIDGE_STATUS_PAGE": f"127.0.0.1:{port}",
            "PRINTLOG_API_KEY": "",
        }
        process = await _start_run(env)
        out = Lines(process)
        loop = asyncio.get_running_loop()

        async def until_page_has(text: str, timeout: float = 20) -> str:
            """The page once it says `text`, or as it was when `timeout` ran out."""
            deadline = loop.time() + timeout
            while True:
                body = await loop.run_in_executor(None, _fetch, url)
                if text in body or loop.time() > deadline:
                    return body
                await asyncio.sleep(0.2)

        try:
            await eventually(lambda: auth0.user_codes, timeout=20)
            code = auth0.user_codes[-1]
            pairing_page = await until_page_has(code)
            auth0.approve()
            paired_page = await until_page_has("Paired")
            return code, pairing_page, paired_page, out.text
        finally:
            await _stop(process)
            await api.close()
            await auth0.close()

    code, pairing_page, paired_page, text = asyncio.run(scenario())

    assert code in pairing_page, text
    assert "<svg " in pairing_page
    assert "Paired" in paired_page, text
    assert code not in paired_page
    assert f"local page (status, pairing, doctor): {url}" in text
    assert "Traceback" not in text
