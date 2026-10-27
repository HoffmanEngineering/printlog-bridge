"""The PrusaLink adapter: its config, `check()`, and `run()` against `tests/fake_prusalink.py`,
and the whole bridge between the fake printer and the fake 3D Print Log API.

Built against Prusa's published API and Buddy firmware's sources, not a recorded printer: these
need checking against a real one's trace (#17).
"""

from __future__ import annotations

import asyncio
import contextlib

import bridge_helpers as helpers
import pytest
from fake_moonraker import eventually
from fake_printlog_api import FakePrintLogApi
from fake_prusalink import FIRMWARE, PASSWORD, FakePrusaLink, make_bgcode

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.adapters.prusalink import PrusaLinkAdapter
from printlog_bridge.core.checks import Status
from printlog_bridge.core.config import ConfigError
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.state import PrinterState

XL = {
    "printer_model": "XL5",
    "filament used [g]": "10.00, 0.00, 2.50, 0.00, 1.25",
    "filament used [mm]": "3300.0, 0.0, 800.0, 0.0, 400.0",
    "estimated printing time (normal mode)": "2h 0m 0s",
}


def options(**more) -> dict[str, str]:
    return {"host": "127.0.0.1", "password": PASSWORD, **more}


class TestConfig:
    def test_reads_its_options(self):
        adapter = PrusaLinkAdapter.from_config(
            "mk4s", options(port="8080", poll_interval="2", idle_poll_interval="20", files="no")
        )

        assert adapter.base_url == "http://127.0.0.1:8080"
        assert adapter.username == "maker"
        assert adapter.poll_interval == 2.0
        assert adapter.idle_poll_interval == 20.0
        assert adapter.files is False
        assert adapter.describe() == "prusalink, http://127.0.0.1:8080"
        assert PASSWORD not in adapter.describe()
        assert ADAPTERS["prusalink"] is PrusaLinkAdapter

    def test_an_api_key_instead_of_a_password(self):
        adapter = PrusaLinkAdapter.from_config("mini", {"host": "mini.local", "api_key": "k3y"})
        assert adapter.api_key == "k3y" and adapter.password is None

    def test_a_password_or_an_api_key_is_needed(self):
        with pytest.raises(ConfigError, match="password"):
            PrusaLinkAdapter.from_config("mk4s", {"host": "127.0.0.1"})

    @pytest.mark.parametrize(
        ("option", "value"),
        [
            ("host", "http://mk4/"),
            ("port", "0"),
            ("poll_interval", "0.1"),
            ("poll_interval", "soon"),
            ("idle_poll_interval", "1"),
            ("files", "maybe"),
        ],
    )
    def test_a_bad_value_is_refused(self, option, value):
        with pytest.raises(ConfigError, match=option):
            PrusaLinkAdapter.from_config("mk4s", options(**{option: value}))

    def test_an_unknown_option_is_refused(self):
        with pytest.raises(ConfigError, match="pasword"):
            PrusaLinkAdapter.from_config("mk4s", options(pasword="x"))


@contextlib.asynccontextmanager
async def printer():
    fake = FakePrusaLink()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.close()


def adapter_for(fake: FakePrusaLink, poll: float = 0.05, idle: float = 0.05) -> PrusaLinkAdapter:
    """With intervals shorter than the config allows, so the tests are quick."""
    adapter = PrusaLinkAdapter.from_config("mk4s", options(port=str(fake.port)))
    adapter.poll_interval = poll
    adapter.idle_poll_interval = idle
    adapter.retry_s = 0.05
    return adapter


class TestCheck:
    def test_a_reachable_printer(self):
        async def scenario():
            async with printer() as fake:
                return await adapter_for(fake).check()

        [result] = asyncio.run(scenario())

        assert result.status is Status.OK
        assert "Prusa-Firmware-Buddy " + FIRMWARE in result.detail
        assert "idle" in result.detail

    def test_a_wrong_password_says_where_the_right_one_is(self):
        async def scenario():
            async with printer() as fake:
                fake.password = "other"
                return await adapter_for(fake).check()

        [result] = asyncio.run(scenario())

        assert result.status is Status.FAIL
        assert "Settings > Network > PrusaLink" in result.detail
        assert PASSWORD not in result.detail

    def test_a_printer_that_isnt_there(self):
        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake)
            return await adapter.check()

        [result] = asyncio.run(scenario())

        assert result.status is Status.FAIL
        assert "127.0.0.1" in result.detail

    def test_firmware_without_the_v1_api_is_told_to_update(self, monkeypatch):
        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake)
                adapter.status_path = "/api/v1/nothing"
                return await adapter.check()

        [result] = asyncio.run(scenario())

        assert result.status is Status.FAIL
        assert "firmware" in result.detail


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
        self.hosts: list[tuple[str, str]] = []
        self.models: list[str] = []
        self.warnings: dict[str, str | None] = {}
        self.states: list[str] = []
        self.connects = 0
        self.disconnects: list[str] = []

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

    def host_software(self, name: str, version: str) -> None:
        self.hosts.append((name, version))

    def printer_model(self, model: str) -> None:
        self.models.append(model)


@contextlib.asynccontextmanager
async def running(adapter, sink, report):
    task = asyncio.ensure_future(adapter.run(sink, report))
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestRun:
    def test_an_xl_job_through_a_dropped_connection(self, tmp_path):
        sink, report = Sink(), Report()

        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake)
                adapter.use_state(PrinterState.load(tmp_path, "xl"))
                async with running(adapter, sink, report):
                    await eventually(lambda: report.states)
                    fake.print_file(
                        12,
                        "Toolchanger.bgcode",
                        make_bgcode(printer=XL, print_meta=XL),
                        progress=1.0,
                    )
                    await eventually(lambda: sink.of(JobStarted))
                    fake.down = True
                    await eventually(lambda: report.disconnects)
                    fake.job["progress"] = 50.0  # type: ignore[index]
                    fake.down = False
                    await eventually(lambda: report.connects == 2)
                    await eventually(lambda: len(sink.of(JobStarted)) == 2)
                    fake.end("STOPPED")
                    await eventually(lambda: sink.of(JobFinished))
                return fake

        fake = asyncio.run(scenario())

        assert report.firmwares[0] == ("Prusa-Firmware-Buddy", FIRMWARE)
        assert report.hosts[0] == ("PrusaLink", "2.0.0")
        assert report.models == ["XL5"]
        assert fake.downloads == ["Toolchanger.bgcode"]  # once, though it reconnected
        start = sink.of(JobStarted)[0]
        assert start.job_id == "12-Toolchanger"
        assert start.estimated_filament_g_by_slot == {0: 10.0, 2: 2.5, 4: 1.25}
        [finished] = sink.of(JobFinished)
        assert finished.outcome is JobOutcome.CANCELLED
        assert finished.filament_g == {0: 5.0, 2: 1.25, 4: 0.625}
        assert finished.usage_estimated is True
        assert fake.requests_of("/api/v1/job") == ["/api/v1/job"]

    def test_polls_faster_while_printing(self, tmp_path):
        report = Report()

        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake, poll=0.02, idle=0.5)
                async with running(adapter, Sink(), report):
                    await eventually(lambda: report.states)
                    await asyncio.sleep(0.4)
                    idle = len(fake.requests_of("/api/v1/status"))
                    fake.print_file(3, "A.bgcode", make_bgcode())
                    await asyncio.sleep(0.75)  # the idle wait ends, then fast polls
                    printing = len(fake.requests_of("/api/v1/status")) - idle
                return idle, printing

        idle, printing = asyncio.run(scenario())

        assert idle <= 2
        assert printing >= 5

    def test_a_refused_password_is_a_warning_on_status(self):
        report = Report()

        async def scenario():
            async with printer() as fake:
                fake.password = "other"
                async with running(adapter_for(fake), Sink(), report):
                    await eventually(lambda: report.warnings.get("access"))
                return fake.unauthorized

        unauthorized = asyncio.run(scenario())

        assert "Settings > Network > PrusaLink" in report.warnings["access"]
        assert report.connects == 0
        assert unauthorized <= 3  # it waits before trying again


@contextlib.asynccontextmanager
async def fakes():
    api = FakePrintLogApi()
    api_url = await api.start()
    try:
        async with printer() as fake:
            yield fake, api, api_url
    finally:
        await api.close()


def write_config(tmp_path, fake, api_url):
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(
        f"[bridge]\napi_url = {api_url}\napi_key = key-123\nstate_dir = {tmp_path / 'state'}\n"
        f"[printer mk4s]\nadapter = prusalink\nprinter_id = 42\nhost = 127.0.0.1\n"
        f"port = {fake.port}\npassword = {PASSWORD}\npoll_interval = 1\nidle_poll_interval = 1\n",
        encoding="utf-8",
    )
    return path


def test_the_bridge_logs_a_prusalink_print_with_the_slicers_grams(tmp_path):
    async def scenario():
        async with fakes() as (fake, api, api_url):
            config = write_config(tmp_path, fake, api_url)
            async with helpers.bridge(
                config,
                connected=lambda: fake.requests_of("/api/v1/status"),
                disconnected=lambda: True,
            ):
                fake.print_file(5, "Benchy.bgcode", make_bgcode(), time_printing=60)
                await eventually(lambda: api.prints, timeout=10)
                fake.end("FINISHED")
                await eventually(lambda: api.prints[1]["status"] != 2, timeout=10)
                await eventually(lambda: api.connections)
            return api.prints[1], next(iter(api.connections.values())), fake.methods

    done, connection, methods = asyncio.run(scenario())

    assert done["title"] == "Benchy"
    assert done["status"] == 3
    [row] = done["filamentUsage"]
    assert (row["slot"], row["amountMg"], row["actualIsEstimate"]) == (0, 12340, True)
    assert connection["kind"] == "prusalink"
    assert methods == {"GET"}  # nothing is ever sent to the printer
    assert PASSWORD not in (tmp_path / "state").joinpath("status.json").read_text("utf-8")
