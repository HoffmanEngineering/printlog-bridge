"""The Bambu adapter: its config, `check()`, and `run()` against `tests/fake_bambu.py`, and the
whole bridge between the fake printer and the fake 3D Print Log API.

Built against the documented protocol (OpenBambuAPI, ha-bambulab, Bambu's wiki), not a recorded
printer: these need checking against a real one's trace (#17).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

import bridge_helpers as helpers
import pytest
from fake_bambu import (
    ACCESS_CODE,
    CANCELLED_ERROR,
    SERIAL,
    FakeBambu,
    FakeFtps,
    certificate,
    make_3mf,
)
from fake_moonraker import eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.adapters.bambu import BambuAdapter
from printlog_bridge.adapters.bambu.adapter import FIRMWARE_NAME
from printlog_bridge.core.checks import Status
from printlog_bridge.core.config import ConfigError
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.state import PrinterState

START = 1_793_000_000


@pytest.fixture
def cert(tmp_path_factory):
    return certificate(tmp_path_factory.mktemp("tls"))


def options(**more) -> dict[str, str]:
    return {"host": "127.0.0.1", "serial": SERIAL, "access_code": ACCESS_CODE, **more}


class TestConfig:
    def test_reads_its_options(self):
        adapter = BambuAdapter.from_config("x1c", options(ftps="false", mqtt_port="18883"))

        assert adapter.host == "127.0.0.1"
        assert adapter.serial == SERIAL
        assert adapter.access_code == ACCESS_CODE
        assert adapter.ftps is False
        assert adapter.mqtt_port == 18883
        assert adapter.describe() == f"bambu, 127.0.0.1 (serial {SERIAL})"
        assert ACCESS_CODE not in adapter.describe()
        assert ADAPTERS["bambu"] is BambuAdapter

    @pytest.mark.parametrize("missing", ["host", "serial", "access_code"])
    def test_each_of_host_serial_and_access_code_is_needed(self, missing):
        given = options()
        del given[missing]

        with pytest.raises(ConfigError, match=missing):
            BambuAdapter.from_config("x1c", given)

    @pytest.mark.parametrize(
        ("option", "value"),
        [
            ("serial", "01S 00C"),
            ("host", "http://printer/"),
            ("tls_fingerprint", "abc"),
            ("ftps", "maybe"),
            ("mqtt_port", "99999"),
        ],
    )
    def test_a_bad_value_is_refused(self, option, value):
        with pytest.raises(ConfigError, match=option):
            BambuAdapter.from_config("x1c", options(**{option: value}))

    def test_an_unknown_option_is_refused(self):
        with pytest.raises(ConfigError, match="url"):
            BambuAdapter.from_config("x1c", options(url="http://x"))


@contextlib.asynccontextmanager
async def printer(cert, *, files: dict[str, bytes] | None = None):
    broker = FakeBambu(*cert)
    await broker.start()
    ftps = FakeFtps(*cert)
    ftps.start()
    ftps.files.update(files or {})
    try:
        yield broker, ftps
    finally:
        await broker.close()
        ftps.close()


def adapter_for(broker: FakeBambu, ftps: FakeFtps, **more) -> BambuAdapter:
    return BambuAdapter.from_config(
        "x1c", options(mqtt_port=str(broker.port), ftps_port=str(ftps.port), **more)
    )


class TestCheck:
    def test_a_reachable_printer_with_its_files(self, cert):
        async def scenario():
            async with printer(cert) as (broker, ftps):
                return await adapter_for(broker, ftps).check()

        mqtt, files = asyncio.run(scenario())

        assert mqtt.status is Status.OK
        assert "firmware 01.08.02.00" in mqtt.detail
        assert "idle" in mqtt.detail
        assert files.status is Status.OK

    def test_a_refused_access_code_says_where_to_find_it_and_about_developer_mode(self, cert):
        async def scenario():
            async with printer(cert) as (broker, ftps):
                return await adapter_for(broker, ftps, access_code="99999999").check()

        [result] = asyncio.run(scenario())

        assert result.status is Status.FAIL
        assert "refused the access code" in result.detail
        assert "LAN Only" in result.detail
        assert "Developer Mode" in result.detail
        assert "99999999" not in result.detail

    def test_a_closed_port_is_a_failure_naming_developer_mode(self, cert):
        async def scenario():
            async with printer(cert) as (broker, ftps):
                port = broker.port
                await broker.close()
                adapter = adapter_for(broker, ftps)
                adapter.mqtt_port = port
                return await adapter.check()

        [result] = asyncio.run(scenario())

        assert result.status is Status.FAIL
        assert "Developer Mode" in result.detail

    def test_a_printer_that_sends_no_status_is_a_warning(self, cert, monkeypatch):
        monkeypatch.setattr("printlog_bridge.adapters.bambu.adapter.CHECK_TIMEOUT_S", 1.0)

        async def scenario():
            async with printer(cert) as (broker, ftps):
                broker.answer_pushall = False
                return await adapter_for(broker, ftps).check()

        mqtt, _ = asyncio.run(scenario())

        assert mqtt.status is Status.WARN
        assert "no status" in mqtt.detail

    def test_refused_files_are_a_warning(self, cert):
        async def scenario():
            async with printer(cert) as (broker, ftps):
                ftps.access_code = "something else"
                return await adapter_for(broker, ftps).check()

        mqtt, files = asyncio.run(scenario())

        assert mqtt.status is Status.OK
        assert files.status is Status.WARN
        assert "Developer Mode" in files.detail

    def test_a_changed_certificate_fails_without_sending_the_access_code(self, cert, tmp_path):
        state = PrinterState.load(tmp_path, "x1c")
        state.discovery["bambu_certificates"] = {"mqtt": "00:" * 31 + "00"}
        state.save()

        async def scenario():
            async with printer(cert) as (broker, ftps):
                adapter = adapter_for(broker, ftps)
                adapter.use_state(PrinterState.peek(tmp_path, "x1c"))
                return await adapter.check(), broker.connects

        [result], connects = asyncio.run(scenario())

        assert result.status is Status.FAIL
        assert "tls_fingerprint" in result.detail
        assert connects == []


class Sink:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)

    async def resume_after(self, printer):  # pragma: no cover
        return None

    def of(self, kind):
        return [e for e in self.events if isinstance(e, kind)]


class Report:
    def __init__(self) -> None:
        self.firmwares: list[tuple[str, str]] = []
        self.warnings: dict[str, str | None] = {}
        self.states: list[str] = []
        self.connects = 0
        self.disconnects: list[str] = []
        self.refused: list[tuple[str, ...]] = []

    def credentials_refused(self, fields) -> None:
        self.refused.append(tuple(fields))

    def connected(self) -> None:
        self.connects += 1

    def disconnected(self, reason: str) -> None:
        self.disconnects.append(reason)

    def printer_state(self, state: str) -> None:
        self.states.append(state)

    def warn(self, topic: str, message: str | None) -> None:
        self.warnings[topic] = message

    def moved(self, old, new, at=None) -> None:  # pragma: no cover
        pass

    def firmware(self, name: str, version: str) -> None:
        self.firmwares.append((name, version))

    def host_software(self, name, version) -> None:  # pragma: no cover
        raise AssertionError("a Bambu printer has no host software")

    def printer_model(self, model) -> None:  # pragma: no cover
        pass


def running(**fields):
    return {
        "gcode_state": "RUNNING",
        "subtask_name": "Benchy",
        "gcode_file": "/data/Metadata/plate_1.gcode",
        "gcode_start_time": str(START),
        "mc_percent": 10,
        **fields,
    }


class TestRun:
    def test_a_job_from_start_to_cancel_through_a_dropped_connection(self, cert, tmp_path):
        sink, report = Sink(), Report()

        async def scenario():
            async with printer(cert, files={"/cache/Benchy.3mf": make_3mf()}) as (broker, ftps):
                adapter = adapter_for(broker, ftps)
                adapter.use_state(PrinterState.load(tmp_path, "x1c"))
                task = asyncio.ensure_future(adapter.run(sink, report))
                try:
                    await eventually(lambda: broker.subscribed and report.states)
                    await broker.send(running())
                    await eventually(lambda: sink.of(JobStarted))
                    broker.drop()
                    broker.status.update(running(mc_percent=50))
                    await eventually(lambda: report.connects == 2 and len(sink.of(JobStarted)) == 2)
                    await broker.send({"print_error": CANCELLED_ERROR, "gcode_state": "FAILED"})
                    await eventually(lambda: sink.of(JobFinished))
                    return broker.requests_of("pushall"), ftps.retrieved
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        pushalls, retrieved = asyncio.run(scenario())

        assert report.firmwares[0] == (FIRMWARE_NAME, "01.08.02.00")
        assert len(pushalls) == 2  # one per connect
        assert retrieved == ["/cache/Benchy.3mf"]  # once, though it reconnected
        start = sink.of(JobStarted)[0]
        assert start.started_at == datetime.fromtimestamp(START, timezone.utc)
        assert start.estimated_filament_g == 12.34
        [finished] = sink.of(JobFinished)
        assert finished.outcome is JobOutcome.CANCELLED
        assert finished.filament_g == {0: pytest.approx(6.17)}
        assert finished.usage_estimated is True
        assert report.disconnects

    def test_a_refused_access_code_is_a_warning_on_status(self, cert, tmp_path):
        report = Report()

        async def scenario():
            async with printer(cert) as (broker, ftps):
                broker.refuse = 5
                adapter = adapter_for(broker, ftps)
                task = asyncio.ensure_future(adapter.run(Sink(), report))
                try:
                    await eventually(lambda: report.warnings.get("access"))
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        asyncio.run(scenario())

        assert "Developer Mode" in report.warnings["access"]
        assert report.connects == 0


@contextlib.asynccontextmanager
async def fakes(cert, files):
    api = FakePrintLogApi()
    api_url = await api.start()
    try:
        async with printer(cert, files=files) as (broker, ftps):
            yield broker, ftps, api, api_url
    finally:
        await api.close()


def write_config(tmp_path, broker, ftps, api_url) -> object:
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(
        f"[bridge]\napi_url = {api_url}\napi_key = key-123\nstate_dir = {tmp_path / 'state'}\n"
        f"[printer x1c]\nadapter = bambu\nprinter_id = 42\nhost = 127.0.0.1\nserial = {SERIAL}\n"
        f"access_code = {ACCESS_CODE}\nmqtt_port = {broker.port}\nftps_port = {ftps.port}\n",
        encoding="utf-8",
    )
    return path


def test_the_bridge_logs_a_bambu_print_with_the_slicers_grams_per_tray(cert, tmp_path):
    three = make_3mf({1: [(1, "PLA", 3.0, 9.0), (2, "PETG", 1.0, 3.0)]})

    async def scenario():
        async with fakes(cert, {"/Benchy.gcode.3mf": three}) as (broker, ftps, api, api_url):
            config = write_config(tmp_path, broker, ftps, api_url)
            async with helpers.bridge(
                config, connected=lambda: broker.subscribed, disconnected=lambda: True
            ):
                await broker.send(running(gcode_file="Benchy.gcode.3mf", ams_mapping=[0, 1]))
                await eventually(lambda: api.prints)
                await broker.send({"gcode_state": "FINISH", "mc_percent": 100})
                await eventually(lambda: api.prints[1]["status"] != 2)
                await eventually(lambda: api.connections)
            return api.prints[1], next(iter(api.connections.values()))

    done, connection = asyncio.run(scenario())

    assert done["title"] == "Benchy"
    assert done["status"] == 3
    rows = sorted(done["filamentUsage"], key=lambda r: r["slot"])
    assert [(r["slot"], r["amountMg"], r["actualIsEstimate"]) for r in rows] == [
        (1, 9000, True),
        (2, 3000, True),
    ]
    assert connection["kind"] == "bambu"
    assert ACCESS_CODE not in (tmp_path / "state").joinpath("status.json").read_text("utf-8")


def test_a_request_the_printer_turns_down_is_a_warning_about_developer_mode(cert):
    report = Report()

    async def scenario():
        async with printer(cert) as (broker, ftps):
            broker.answer_pushall = False
            task = asyncio.ensure_future(adapter_for(broker, ftps).run(Sink(), report))
            try:
                await eventually(lambda: broker.subscribed and broker.requests_of("pushall"))
                await broker.publish(
                    {"print": {"command": "pushall", "result": "failed", "reason": "verify failed"}}
                )
                await eventually(lambda: report.warnings.get("authorization"))
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())

    assert "verify failed" in report.warnings["authorization"]
    assert "Developer Mode" in report.warnings["authorization"]
