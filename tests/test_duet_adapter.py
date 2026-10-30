"""The Duet adapter: its config, `check()`, the HTTP client and `run()` against
`tests/fake_duet.py` (standalone and SBC), discovery, and the whole bridge between the fake
board and the fake 3D Print Log API.

Built against Duet3D's documentation, not a recorded board: these need checking against a real
one's trace (#89).
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import bridge_helpers as helpers
import pytest
from fake_duet import BOARD, FIRMWARE, PASSWORD, PNG, FakeDuet
from fake_moonraker import eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.adapters.duet import DuetAdapter
from printlog_bridge.adapters.duet.client import DuetClient, Refused
from printlog_bridge.adapters.duet.discovery import DuetDiscovery
from printlog_bridge.core.checks import Status
from printlog_bridge.core.config import ConfigError
from printlog_bridge.core.discovery import Found
from printlog_bridge.core.events import JobFinished, JobImage, JobOutcome, JobStarted
from printlog_bridge.core.mdns import MdnsAnswer
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.trace import Trace


def options(**more) -> dict[str, str]:
    return {"host": "127.0.0.1", "password": PASSWORD, **more}


class TestConfig:
    def test_reads_its_options(self):
        adapter = DuetAdapter.from_config(
            "railcore",
            options(
                port="8080", poll_interval="2", idle_poll_interval="20", files="no"
            ),  # fmt: skip
        )
        assert adapter.base_url == "http://127.0.0.1:8080"
        assert adapter.poll_interval == 2.0 and adapter.idle_poll_interval == 20.0
        assert adapter.files is False
        assert adapter.describe() == "duet, http://127.0.0.1:8080"
        assert PASSWORD not in adapter.describe()
        assert ADAPTERS["duet"] is DuetAdapter

    def test_a_url_instead_of_a_host(self):
        adapter = DuetAdapter.from_config("jubilee", {"url": "http://duet3.local:81/"})
        assert (adapter.host, adapter.port, adapter.password) == ("duet3.local", 81, None)

    def test_the_password_is_optional_and_kept_as_given(self):
        assert DuetAdapter.from_config("d", {"host": "duet3.local"}).password is None
        assert DuetAdapter.from_config("d", {"host": "d", "password": "  "}).password is None
        assert DuetAdapter.from_config("d", {"host": "d", "password": " p "}).password == " p "

    @pytest.mark.parametrize(
        ("option", "value"),
        [
            ("host", "http://duet/"),
            ("url", "https://duet3.local"),
            ("url", "http://duet3.local/rr_model"),
            ("port", "0"),
            ("poll_interval", "0.1"),
            ("idle_poll_interval", "1"),
            ("files", "maybe"),
        ],
    )
    def test_a_bad_value_is_refused(self, option, value):
        given = {"host": "127.0.0.1"} if option != "url" else {}
        with pytest.raises(ConfigError, match=option if option != "idle_poll_interval" else "idle"):
            DuetAdapter.from_config("d", {**given, option: value})

    def test_url_and_host_together_or_neither_are_refused(self):
        with pytest.raises(ConfigError, match="not both"):
            DuetAdapter.from_config("d", {"host": "a", "url": "http://b"})
        with pytest.raises(ConfigError, match="needs url or host"):
            DuetAdapter.from_config("d", {})

    def test_an_unknown_option_is_refused(self):
        with pytest.raises(ConfigError, match="pasword"):
            DuetAdapter.from_config("d", options(pasword="x"))

    def test_app_options_take_the_password_only_when_there_is_one(self):
        assert DuetAdapter.app_options("192.168.1.88", None, None, {}) == {
            "host": "192.168.1.88",
            "port": "80",
        }
        assert (
            DuetAdapter.app_options("192.168.1.88", 80, None, {"password": "x"})["password"] == "x"
        )
        assert DuetAdapter.app_secrets == ("password",)


@contextlib.asynccontextmanager
async def board(**kwargs):
    fake = FakeDuet(**kwargs)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.close()


def adapter_for(fake: FakeDuet, poll: float = 0.05, idle: float = 0.05, **more) -> DuetAdapter:
    """With intervals shorter than the config allows, so the tests are quick."""
    given = options(port=str(fake.port), **more)
    if fake.password is None:
        given.pop("password")
    adapter = DuetAdapter.from_config("railcore", given)
    adapter.poll_interval, adapter.idle_poll_interval, adapter.retry_s = poll, idle, 0.05
    return adapter


class TestCheck:
    def test_a_reachable_board(self):
        async def scenario():
            async with board() as fake:
                result = await adapter_for(fake).check()
                return result, fake

        [result], fake = asyncio.run(scenario())
        assert result.status is Status.OK
        assert f"RepRapFirmware {FIRMWARE} on {BOARD}" in result.detail
        assert "idle" in result.detail and "SBC" not in result.detail
        assert fake.disconnects == 1  # it gives its session back

    def test_a_board_with_an_sbc(self):
        async def scenario():
            async with board(sbc=True) as fake:
                return await adapter_for(fake).check()

        [result] = asyncio.run(scenario())
        assert result.status is Status.OK
        assert "SBC mode (DuetSoftwareFramework 3.5.2)" in result.detail

    def test_a_wrong_password_says_where_the_right_one_is(self):
        for sbc in (False, True):

            async def scenario(sbc=sbc):
                async with board(sbc=sbc) as fake:
                    fake.password = "other"
                    return await adapter_for(fake).check()

            [result] = asyncio.run(scenario())
            assert result.status is Status.FAIL
            assert "M551" in result.detail
            assert PASSWORD not in result.detail

    def test_a_board_without_a_password_takes_none(self):
        async def scenario():
            async with board(password=None) as fake:
                result = await adapter_for(fake).check()
                return result, fake.queries

        [result], queries = asyncio.run(scenario())
        assert result.status is Status.OK
        assert "/rr_connect?password=reprap" in queries

    def test_a_board_that_isnt_there(self):
        async def scenario():
            async with board() as fake:
                adapter = adapter_for(fake)
            return await adapter.check()

        [result] = asyncio.run(scenario())
        assert result.status is Status.FAIL
        assert "127.0.0.1" in result.detail


class TestClient:
    def test_sends_the_session_key_and_reads_only_what_changed(self):
        async def scenario():
            async with board() as fake:
                client = DuetClient(f"http://127.0.0.1:{fake.port}", password=PASSWORD)
                try:
                    await client.connect()
                    await client.model()
                    full = len(fake.queries)
                    await client.model()
                    unchanged = fake.queries[full:]
                    fake.print_file("a.gcode")
                    mark = len(fake.queries)
                    model = await client.model()
                    changed = fake.queries[mark:]
                finally:
                    await client.close()
                return fake, unchanged, changed, model, client

        fake, unchanged, changed, model, client = asyncio.run(scenario())
        assert unchanged == ["/rr_model?key=&flags=d99fn"]
        assert sorted(q.split("&")[0] for q in changed[1:]) == [
            "/rr_model?key=job",
            "/rr_model?key=state",
        ]
        assert model["job"]["file"]["fileName"] == "0:/gcodes/a.gcode"
        assert model["state"]["status"] == "processing"
        keyed = [h for h in fake.headers if "X-Session-Key" in h]
        assert len(keyed) == len(fake.headers) - 1  # all but the connect
        assert fake.disconnects == 1 and client.session_key is None

    def test_a_lapsed_session_is_opened_again(self):
        async def scenario():
            async with board() as fake:
                client = DuetClient(f"http://127.0.0.1:{fake.port}", password=PASSWORD)
                try:
                    await client.connect()
                    await client.model()
                    fake.expire_sessions()
                    model = await client.model()
                finally:
                    await client.close()
                return client, model, fake

        client, model, fake = asyncio.run(scenario())
        assert client.reconnects == 1
        assert model["state"]["status"] == "idle"
        assert fake.requests_of("/rr_connect") == ["/rr_connect"] * 2

    def test_older_firmware_without_session_keys(self):
        async def scenario():
            async with board(session_keys=False) as fake:
                client = DuetClient(f"http://127.0.0.1:{fake.port}", password=PASSWORD)
                try:
                    await client.connect()
                    return await client.model(), client.session_key
                finally:
                    await client.close()

        model, key = asyncio.run(scenario())
        assert key is None and model["boards"][0]["firmwareVersion"] == FIRMWARE

    def test_a_long_array_is_read_in_pages(self):
        async def scenario():
            async with board(extruders=5) as fake:
                fake.page_size = 2
                client = DuetClient(f"http://127.0.0.1:{fake.port}", password=PASSWORD)
                try:
                    await client.connect()
                    return await client.model(), fake.queries
                finally:
                    await client.close()

        model, queries = asyncio.run(scenario())
        assert [t["number"] for t in model["tools"]] == [0, 1, 2, 3, 4]
        assert "/rr_model?key=tools&flags=d99vna4" in queries

    def test_a_wrong_password_is_refused(self):
        async def scenario():
            async with board() as fake:
                client = DuetClient(f"http://127.0.0.1:{fake.port}", password="nope")
                try:
                    await client.connect()
                finally:
                    await client.close()

        with pytest.raises(Refused):
            asyncio.run(scenario())

    def test_a_thumbnail_is_read_a_piece_at_a_time(self):
        async def scenario():
            async with board() as fake:
                path = fake.add_file("a.gcode")
                client = DuetClient(f"http://127.0.0.1:{fake.port}", password=PASSWORD)
                try:
                    await client.connect()
                    info = await client.file_info(path)
                    png = await client.thumbnail(path, info["thumbnails"][0])
                finally:
                    await client.close()
                return png, fake.requests_of("/rr_thumbnail")

        png, pieces = asyncio.run(scenario())
        assert png == PNG
        assert len(pieces) > 1


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
        self.firmwares: list = []
        self.hosts: list = []
        self.warnings: dict = {}
        self.states: list = []
        self.connects = 0
        self.disconnects: list = []
        self.refused: list = []

    def credentials_refused(self, fields) -> None:
        self.refused.append(tuple(fields))

    def connected(self) -> None:
        self.connects += 1

    def disconnected(self, reason: str) -> None:
        self.disconnects.append(reason)

    def printer_state(self, state: str) -> None:
        self.states.append(state)

    def warn(self, topic: str, message) -> None:
        self.warnings[topic] = message

    def firmware(self, name: str, version: str) -> None:
        self.firmwares.append((name, version))

    def host_software(self, name: str, version: str) -> None:
        self.hosts.append((name, version))

    def printer_model(self, model: str) -> None:  # pragma: no cover
        pass


@contextlib.asynccontextmanager
async def running(adapter, sink, report):
    task = asyncio.ensure_future(adapter.run(sink, report))
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)


class TestRun:
    @pytest.mark.parametrize("sbc", [False, True])
    def test_a_toolchanger_job_through_a_dropped_connection(self, tmp_path, sbc):
        sink, report = Sink(), Report()

        async def scenario():
            async with board(sbc=sbc, extruders=2, tools=[[1], [0]]) as fake:
                adapter = adapter_for(fake)
                adapter.use_state(PrinterState.load(tmp_path, "railcore"))
                async with running(adapter, sink, report):
                    await eventually(lambda: report.states)
                    fake.print_file("Tool change.gcode", filament=(300.0, 120.0))
                    await eventually(lambda: sink.of(JobStarted))
                    fake.extrude([100.0, 40.0])
                    fake.down = True
                    await eventually(lambda: report.disconnects)
                    fake.extrude([20.0, 60.0])
                    fake.down = False
                    await eventually(lambda: report.connects == 2)
                    fake.end()
                    await eventually(lambda: sink.of(JobFinished))
                    image = sink.of(JobImage)[0]
                    thumbnail = await image.fetch()
                return fake, thumbnail

        fake, thumbnail = asyncio.run(scenario())
        assert report.firmwares[0] == ("RepRapFirmware", FIRMWARE)
        assert report.hosts == ([("DuetSoftwareFramework", "3.5.2")] * 2 if sbc else [])
        start = sink.of(JobStarted)[0]
        assert start.file_name == "Tool change.gcode"
        assert start.estimated_filament_mm == 420.0
        [finished] = sink.of(JobFinished)
        assert finished.outcome is JobOutcome.COMPLETED
        # T0 drives extruder 1, T1 extruder 0.
        assert finished.filament_mm == {1: 120.0, 0: 100.0}
        assert finished.usage_estimated is False
        assert thumbnail == PNG
        assert fake.methods == {"GET"}
        assert fake.disconnects >= 1  # rr_disconnect (or /machine/disconnect) on stop

    def test_polls_faster_while_printing(self):
        report = Report()

        async def scenario():
            async with board() as fake:
                adapter = adapter_for(fake, poll=0.02, idle=0.5)
                async with running(adapter, Sink(), report):
                    await eventually(lambda: report.states)
                    await asyncio.sleep(0.4)
                    idle = len(fake.requests_of("/rr_model"))
                    fake.print_file("a.gcode")
                    await asyncio.sleep(0.75)
                    printing = len(fake.requests_of("/rr_model")) - idle
                return idle, printing

        idle, printing = asyncio.run(scenario())
        assert idle <= 9  # the first full read (seven keys and the live fields) and one poll
        assert printing >= 5

    def test_a_lapsed_session_isnt_a_disconnect(self):
        report = Report()

        async def scenario():
            async with board() as fake, running(adapter_for(fake), Sink(), report):
                await eventually(lambda: report.states)
                fake.expire_sessions()
                await eventually(lambda: len(fake.requests_of("/rr_connect")) == 2)
                await asyncio.sleep(0.2)

        asyncio.run(scenario())
        assert report.connects == 1 and report.disconnects == []

    def test_a_busy_board_is_waited_out(self):
        report = Report()

        async def scenario():
            async with board() as fake, running(adapter_for(fake), Sink(), report):
                await eventually(lambda: report.states)
                fake.busy = 2
                await eventually(lambda: fake.busy == 0)
                await asyncio.sleep(0.2)

        asyncio.run(scenario())
        assert report.disconnects == []

    def test_a_refused_password_is_a_warning_on_status(self):
        report = Report()

        async def scenario():
            async with board() as fake:
                fake.password = "other"
                async with running(adapter_for(fake), Sink(), report):
                    await eventually(lambda: report.warnings.get("access"))
                    await asyncio.sleep(0.2)
                return fake.requests_of("/rr_connect")

        connects = asyncio.run(scenario())
        assert "M551" in report.warnings["access"]
        assert report.refused == [("password",)]
        assert len(connects) == 1  # it waits five minutes before trying again

    def test_a_trace_keeps_neither_the_password_nor_the_session_nor_the_network(self, tmp_path):
        async def scenario():
            async with board() as fake:
                trace = Trace(tmp_path, "railcore")
                adapter = adapter_for(fake)
                adapter.use_trace(trace)
                sink, report = Sink(), Report()
                async with running(adapter, sink, report):
                    await eventually(lambda: report.states)
                    fake.print_file("a.gcode")
                    await eventually(lambda: sink.of(JobStarted))
                    await sink.of(JobImage)[0].fetch()
                trace.close()
                keys = list(fake.sessions) + [str(k) for k in fake.sessions]
                return keys

        asyncio.run(scenario())
        text = "".join(p.read_text("utf-8") for p in (tmp_path / "traces").glob("*.jsonl"))
        assert PASSWORD not in text
        assert "sessionKey" not in text
        assert "HomeWiFi" not in text and "02:00:00:12:34:56" not in text
        assert "do-not-trace" not in text
        assert "Railcore" in text  # the machine's name is kept
        lines = [json.loads(line) for line in text.splitlines()]
        assert any(m.get("msg", {}).get("params", {}) == {"object": "job", "flags": "d99vn"}
                   for m in lines if isinstance(m.get("msg"), dict))  # fmt: skip


class TestDiscovery:
    def answer(self, **txt) -> MdnsAnswer:
        return MdnsAnswer("_http._tcp.local", "railcore", "railcore.local", "192.168.1.88", 80,
                          txt)  # fmt: skip

    def test_a_duet_is_known_by_its_txt_record(self):
        found = asyncio.run(
            DuetDiscovery().from_mdns(
                self.answer(product="RepRapFirmware for Duet 3 MB6HC", version="3.5.2")
            )
        )
        assert found == Found("duet", "192.168.1.88", 80, model="Duet 3 MB6HC", name="railcore")
        assert found.cleaned() is not None

    def test_any_other_web_server_is_not(self):
        hook = DuetDiscovery()
        assert asyncio.run(hook.from_mdns(self.answer())) is None
        assert asyncio.run(hook.from_mdns(self.answer(product="Synology DSM"))) is None
        assert hook.mdns_services == ("_http._tcp.local",)

    def test_a_config_file_printer_watches_its_address(self):
        adapter = DuetAdapter.from_config("d", {"host": "192.168.1.88"})
        assert adapter.watches(Found("duet", "192.168.1.88", 80))
        assert not adapter.watches(Found("prusalink", "192.168.1.88", 80))
        assert adapter.found_entry() == Found("duet", "192.168.1.88", 80)


@contextlib.asynccontextmanager
async def fakes():
    api = FakePrintLogApi()
    api_url = await api.start()
    try:
        async with board() as fake:
            yield fake, api, api_url
    finally:
        await api.close()


def test_the_bridge_logs_a_duet_print_with_measured_filament(tmp_path):
    async def scenario():
        async with fakes() as (fake, api, api_url):
            config = tmp_path / "printlog-bridge.cfg"
            config.write_text(
                f"[bridge]\napi_url = {api_url}\napi_key = key-123\n"
                f"state_dir = {tmp_path / 'state'}\n"
                f"[printer railcore]\nadapter = duet\nprinter_id = 42\nhost = 127.0.0.1\n"
                f"port = {fake.port}\npassword = {PASSWORD}\npoll_interval = 1\n"
                "idle_poll_interval = 1\n",
                encoding="utf-8",
            )
            async with helpers.bridge(
                config,
                connected=lambda: fake.requests_of("/rr_model"),
                disconnected=lambda: True,
            ):
                fake.print_file("Benchy.gcode", duration=60)
                await eventually(lambda: api.prints, timeout=10)
                fake.extrude([3210.4])
                await asyncio.sleep(1.2)
                fake.end()
                await eventually(lambda: api.prints[1]["status"] != 2, timeout=10)
                await eventually(lambda: api.connections)
            return api.prints[1], next(iter(api.connections.values())), fake.methods

    done, connection, methods = asyncio.run(scenario())
    assert done["title"] == "Benchy"
    assert done["status"] == 3
    [row] = done["filamentUsage"]
    assert row["slot"] == 0
    assert row["lengthInM"] == pytest.approx(3.2104, abs=0.001)
    assert row["source"] == 2
    assert row["actualIsEstimate"] is False
    assert connection["kind"] == "duet"
    assert methods == {"GET"}
    assert PASSWORD not in (tmp_path / "state").joinpath("status.json").read_text("utf-8")


class TestDiscoverCommand:
    def test_lists_the_duets_that_answer_with_a_section(self, monkeypatch):
        from printlog_bridge.cli import discover as command

        async def ask(services, wait):
            assert services == ["_http._tcp.local"]
            return [
                MdnsAnswer("_http._tcp.local", "railcore", "railcore.local", "192.168.1.88", 80,
                           {"product": "RepRapFirmware for Duet 3 MB6HC"}),
                MdnsAnswer("_http._tcp.local", "nas", "nas.local", "192.168.1.5", 5000,
                           {"vendor": "Synology"}),
            ]  # fmt: skip

        monkeypatch.setattr(command.mdns, "ask", ask)
        found = asyncio.run(command.ask_duets())
        text = command.format_duet(found, None, 2)

        assert [f.address for f in found] == ["192.168.1.88"]
        assert "192.168.1.88: Duet 3 MB6HC" in text
        assert "adapter = duet" in text and "host = 192.168.1.88" in text
        assert "192.168.1.5" not in text

    def test_says_when_none_answered(self):
        from printlog_bridge.cli.discover import format_duet

        assert "No Duet answered" in format_duet([], None, 2)
        assert "Couldn't ask" in format_duet(None, None, 2, "no network")
