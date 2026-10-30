"""The SDCP adapter (Elegoo Centauri Carbon): config, `check()`, the websocket and its jobs
against `tests/fake_elegoo.py`, what a trace keeps, discovery by `M99999`, and the whole bridge
between the fake and the fake API. Built from the published spec, not a recorded printer (#94)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json

import bridge_helpers as helpers
import pytest
from fake_elegoo import (
    FIRMWARE,
    MAINBOARD,
    MODEL,
    NAME,
    FakeDiscovery,
    FakeElegoo,
    discovery_reply,
)
from fake_moonraker import eventually
from fake_printlog_api import FakePrintLogApi
from test_prusalink_adapter import Report, Sink, running

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.adapters.sdcp import SdcpAdapter
from printlog_bridge.adapters.sdcp import discovery as sdcp_discovery
from printlog_bridge.adapters.sdcp.discovery import (
    FoundSdcp,
    SdcpDiscovery,
    ask,
    parse_reply,
)
from printlog_bridge.cli.discover import format_sdcp
from printlog_bridge.cli.main import main
from printlog_bridge.core.checks import Status
from printlog_bridge.core.config import ConfigError
from printlog_bridge.core.discovery import Found
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.state import PrinterState


class TestConfig:
    def test_reads_its_options(self):
        adapter = SdcpAdapter.from_config("cc", {"host": " 192.168.1.77 "})
        assert (adapter.host, adapter.port) == ("192.168.1.77", 3030)
        assert adapter.describe() == "sdcp, 192.168.1.77"
        assert SdcpAdapter.from_config("cc", {"host": "cc.lan", "port": "3031"}).describe() == (
            "sdcp, cc.lan:3031"
        )
        assert ADAPTERS["sdcp"] is SdcpAdapter
        assert SdcpAdapter.app_secrets == ()

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({}, "needs host"),
            ({"host": "  "}, "needs host"),
            ({"host": "ws://192.168.1.77:3030"}, "host"),
            ({"host": "a b"}, "host"),
            ({"host": "192.168.1.77", "port": "70000"}, "port"),
            ({"host": "192.168.1.77", "port": "x"}, "port"),
            ({"host": "192.168.1.77", "password": "x"}, "unknown option 'password'"),
            ({"host": "192.168.1.77", "files": "maybe"}, "files must be true or false"),
            ({"host": "192.168.1.77", "http_port": "0"}, "http_port"),
        ],
    )
    def test_a_bad_value_is_refused(self, options, message):
        with pytest.raises(ConfigError, match=message):
            SdcpAdapter.from_config("cc", options)

    def test_a_printer_from_the_app_needs_no_credentials(self):
        made = SdcpAdapter.app_options("192.168.1.77", 3030, MAINBOARD, {})
        assert made == {"host": "192.168.1.77"}
        assert SdcpAdapter.app_options("192.168.1.77", 4000, None, {}) == {
            "host": "192.168.1.77",
            "port": "4000",
        }
        adapter = SdcpAdapter.from_config("app-3", made)
        assert adapter.watches(Found("sdcp", "192.168.1.77", 3030))
        assert not adapter.watches(Found("sdcp", "192.168.1.78", 3030))
        assert not adapter.watches(Found("bambu", "192.168.1.77", 3030))
        assert adapter.found_entry() == Found("sdcp", "192.168.1.77", 3030)

    def test_the_printer_is_known_by_its_mainboard_once_seen(self, tmp_path):
        adapter = SdcpAdapter.from_config("cc", {"host": "192.168.1.77"})
        state = PrinterState.load(tmp_path, "cc")
        state.discovery["sdcp_mainboard_id"] = MAINBOARD
        adapter.use_state(state)
        moved = Found("sdcp", "192.168.1.90", 3030, mainboard_id=MAINBOARD.upper())
        assert adapter.watches(moved)
        assert adapter.found_entry() == Found("sdcp", "192.168.1.77", 3030, mainboard_id=MAINBOARD)


@contextlib.asynccontextmanager
async def printer():
    fake = FakeElegoo()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.close()


def adapter_for(fake: FakeElegoo) -> SdcpAdapter:
    adapter = SdcpAdapter.from_config(
        "cc", {"host": fake.host, "port": str(fake.port), "http_port": str(fake.port)}
    )
    adapter.retry_s = 0.05
    adapter.ping_s = 0.1
    return adapter


class TestCheck:
    def test_a_reachable_printer(self):
        async def scenario():
            async with printer() as fake:
                return await adapter_for(fake).check(), fake.refused

        [result], refused = asyncio.run(scenario())
        assert result.status is Status.OK
        assert MODEL in result.detail and FIRMWARE.lstrip("V") in result.detail
        assert refused == []

    def test_a_printer_that_isnt_there(self):
        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake)
            return await adapter.check()

        [result] = asyncio.run(scenario())
        assert result.status is Status.FAIL and "127.0.0.1" in result.detail

    def test_something_that_doesnt_answer_like_sdcp(self):
        async def scenario():
            async with printer() as fake:
                fake.answer = False
                adapter = adapter_for(fake)
                adapter.answer_s = 0.3
                return await adapter.check()

        [result] = asyncio.run(scenario())
        assert result.status is Status.FAIL and "SDCP" in result.detail


def test_printer_details_come_from_its_attributes():
    async def scenario():
        async with printer() as fake:
            return await adapter_for(fake).printer_details()

    details = asyncio.run(scenario())
    assert (details.name, details.make, details.model) == (NAME, "Elegoo", MODEL)


class TestRun:
    def test_a_print_from_start_to_finish(self, tmp_path):
        sink, report = Sink(), Report()

        async def scenario():
            async with printer() as fake:
                fake.clock_offset = 120.0
                adapter = adapter_for(fake)
                adapter.use_state(PrinterState.load(tmp_path, "cc"))
                async with running(adapter, sink, report):
                    await eventually(lambda: report.states)
                    await fake.prepare()
                    await fake.print_file("/local/Benchy.gcode", ticks=30)
                    await eventually(lambda: sink.of(JobStarted))
                    await fake.progress(50)
                    await fake.pause()
                    await fake.resume()
                    await fake.complete()
                    await eventually(lambda: sink.of(JobFinished))
                    await asyncio.sleep(0.3)  # pings meanwhile; the end isn't sent twice
                return fake

        fake = asyncio.run(scenario())
        [start] = sink.of(JobStarted)
        assert start.file_name == "Benchy.gcode"
        assert start.estimated_duration_s == 3600
        [done] = sink.of(JobFinished)
        assert done.outcome is JobOutcome.COMPLETED
        assert report.firmwares == [("Elegoo", FIRMWARE.lstrip("V"))]
        assert report.hosts == []  # the bridge talks to the printer itself
        assert "printing" in report.states and "paused" in report.states
        # It only ever reads: status and attributes, and pings.
        assert fake.refused == []
        cmds = {m["Data"]["Cmd"] for m in fake.requests if isinstance(m, dict)}
        assert cmds == {0, 1}
        assert "ping" in fake.requests
        kept = PrinterState.load(tmp_path, "cc").discovery
        assert kept["sdcp_mainboard_id"] == MAINBOARD

    def test_requests_name_the_mainboard_once_known(self, tmp_path):
        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake)
                adapter.use_state(PrinterState.load(tmp_path, "cc"))
                report = Report()
                async with running(adapter, Sink(), report):
                    await eventually(lambda: report.connects)
                    await fake.drop()
                    await eventually(lambda: report.connects == 2)
                return [m for m in fake.requests if isinstance(m, dict)]

        requests = asyncio.run(scenario())
        first, last = requests[0], requests[-1]
        assert first["Data"]["MainboardID"] == "" and first["Topic"] == "sdcp/request/"
        assert last["Data"]["MainboardID"] == MAINBOARD
        assert last["Topic"] == f"sdcp/request/{MAINBOARD}"
        assert set(last["Data"]) == {"Cmd", "Data", "RequestID", "MainboardID", "TimeStamp",
                                     "From"}  # fmt: skip
        assert last["Data"]["Data"] == {}

    def test_pings_keep_a_quiet_connection_open(self):
        report = Report()

        async def scenario():
            async with printer() as fake:
                fake.idle_timeout = 0.4  # the printer drops a client that says nothing
                async with running(adapter_for(fake), Sink(), report):
                    await eventually(lambda: report.connects)
                    await asyncio.sleep(1.2)
                return fake

        fake = asyncio.run(scenario())
        assert report.connects == 1 and fake.connections == 1

    def test_stopping_always_stops_it(self):
        """A cancel that lands while a receive times out still stops `run` (on Python 3.9,
        aiohttp's `receive(timeout=...)` can turn that cancel into a TimeoutError)."""

        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake)
                adapter.ping_s = 0.001  # a receive times out every millisecond
                stuck = 0
                for _ in range(30):
                    report = Report()
                    task = asyncio.ensure_future(adapter.run(Sink(), report))
                    await eventually(lambda report=report: report.connects)
                    await asyncio.sleep(0.01)
                    task.cancel()
                    done, _ = await asyncio.wait({task}, timeout=3)
                    if not done:
                        stuck += 1
                        break
                return stuck

        assert asyncio.run(scenario()) == 0

    def test_a_dropped_socket_carries_on_with_the_same_job(self, tmp_path):
        sink, report = Sink(), Report()

        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake)
                adapter.use_state(PrinterState.load(tmp_path, "cc"))
                async with running(adapter, sink, report):
                    await eventually(lambda: report.states)
                    await fake.print_file(ticks=600, progress=20)
                    await eventually(lambda: sink.of(JobStarted))
                    await fake.drop()
                    await eventually(lambda: report.connects == 2)
                    await fake.progress(70)
                    await fake.cancel()
                    await eventually(lambda: sink.of(JobFinished))

        asyncio.run(scenario())
        assert len({s.job_id for s in sink.of(JobStarted)}) == 1
        assert "closed the connection" in report.disconnects[0]
        [done] = sink.of(JobFinished)
        assert done.outcome is JobOutcome.CANCELLED

    def test_a_print_that_ended_while_the_bridge_was_away(self, tmp_path):
        sink = Sink()

        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake)
                adapter.use_state(PrinterState.load(tmp_path, "cc"))
                report = Report()
                async with running(adapter, sink, report):
                    await eventually(lambda: report.states)
                    await fake.print_file(progress=40)
                    await eventually(lambda: sink.of(JobStarted))
                fake.pushes_on_change = False
                await fake.complete()
                again = adapter_for(fake)
                again.use_state(PrinterState.load(tmp_path, "cc"))
                async with running(again, sink, Report()):
                    await eventually(lambda: sink.of(JobFinished))

        asyncio.run(scenario())
        [done] = sink.of(JobFinished)
        assert done.outcome is JobOutcome.COMPLETED
        assert "wasn't connected" in done.note

    def test_no_answer_is_a_disconnection(self):
        report = Report()

        async def scenario():
            async with printer() as fake:
                fake.answer = False
                adapter = adapter_for(fake)
                adapter.answer_s = 0.2
                async with running(adapter, Sink(), report):
                    await eventually(lambda: report.disconnects)

        asyncio.run(scenario())
        assert report.connects == 0
        assert "SDCP" in report.disconnects[0]


def test_a_trace_keeps_only_what_the_bridge_reads(tmp_path):
    """Status, the attributes it uses and acknowledgements in full; of the attributes nothing
    else (the Wi-Fi network's name), and of any other message only its kind."""
    from printlog_bridge.core.trace import Trace, trace_files

    state = tmp_path / "state"
    trace = Trace(state, "cc", secrets=[])

    async def scenario():
        async with printer() as fake:
            adapter = adapter_for(fake)
            adapter.use_trace(trace)
            report = Report()
            async with running(adapter, Sink(), report):
                await eventually(lambda: report.firmwares)
                await fake.send(
                    json.dumps(
                        {
                            "Data": {"Data": {"Message": "SECRET-NOTICE", "Type": 1}},
                            "Topic": f"sdcp/notice/{MAINBOARD}",
                        }
                    )
                )
                await fake.send("not json SECRET-RAW")
                await asyncio.sleep(0.3)

    asyncio.run(scenario())
    trace.close()
    text = "".join(p.read_text(encoding="utf-8") for p in trace_files(state, "cc"))
    assert '"PrintInfo"' in text and '"Ack"' in text and FIRMWARE in text
    assert '"Cmd": 0' in text or '"Cmd":0' in text
    assert "notice" in text
    for secret in ("SECRET-HOME-WIFI", "SECRET-NOTICE", "SECRET-RAW", "aa:bb:cc:dd:ee:ff"):
        assert secret not in text


@contextlib.asynccontextmanager
async def fakes():
    api = FakePrintLogApi()
    api_url = await api.start()
    try:
        async with printer() as fake:
            yield fake, api, api_url
    finally:
        await api.close()


def test_the_bridge_logs_a_centauri_carbon_print(tmp_path):
    async def scenario():
        async with fakes() as (fake, api, api_url):
            config = tmp_path / "printlog-bridge.cfg"
            config.write_text(
                f"[bridge]\napi_url = {api_url}\napi_key = key-123\n"
                f"state_dir = {tmp_path / 'state'}\n"
                f"[printer cc]\nadapter = sdcp\nprinter_id = 42\nhost = {fake.host}\n"
                f"port = {fake.port}\nhttp_port = {fake.port}\n",
                encoding="utf-8",
            )
            async with helpers.bridge(
                config, connected=lambda: fake.sockets, disconnected=lambda: True
            ):
                await fake.print_file("/local/Calibration Cube.gcode")
                await eventually(lambda: api.prints, timeout=10)
                await fake.complete()
                await eventually(lambda: api.prints[1]["status"] != 2, timeout=10)
                await eventually(lambda: api.connections)
            return api.prints[1], next(iter(api.connections.values()))

    done, connection = asyncio.run(scenario())
    assert done["title"] == "Calibration Cube"
    assert done["status"] == 3
    assert done["externalSource"] == "sdcp"
    assert connection["kind"] == "sdcp"


class TestDiscovery:
    def test_reads_a_printers_reply(self):
        found = parse_reply(discovery_reply("192.168.1.77"), "192.168.1.77")
        assert found == FoundSdcp(
            address="192.168.1.77",
            mainboard_id=MAINBOARD,
            name=NAME,
            model=MODEL,
            firmware=FIRMWARE,
        )

    @pytest.mark.parametrize(
        "data",
        [
            b"M99999",
            b"\xff\xfe",
            b"[]",
            b'{"Id": "x", "Data": {}}',
            b'{"Data": {"MainboardID": 5}}',
        ],
    )
    def test_ignores_anything_else(self, data):
        assert parse_reply(data, "192.168.1.77") is None

    def test_the_address_is_where_it_answered_from(self):
        found = parse_reply(discovery_reply("10.9.9.9"), "192.168.1.77")
        assert found.address == "192.168.1.77"

    def test_asks_and_collects_the_answers(self):
        async def scenario():
            responder = await FakeDiscovery().start()
            responder.repeat = 2
            try:
                found = await ask([("127.0.0.1", responder.port)] * 2, seconds=0.3)
            finally:
                responder.close()
            return found, responder.asked

        found, asked = asyncio.run(scenario())
        assert [f.mainboard_id for f in found] == [MAINBOARD]  # once, though it answered twice
        assert asked == [b"M99999"]  # each target asked once

    def test_a_scan_reports_the_printer_where_the_adapter_connects(self, monkeypatch):
        async def heard(targets, seconds=0):
            assert ("192.168.1.255", 3000) in targets
            return [parse_reply(discovery_reply(), "192.168.1.77")]

        monkeypatch.setattr(sdcp_discovery, "ask", heard)
        hook = SdcpAdapter.discovery()
        assert isinstance(hook, SdcpDiscovery) and hook.kind == "sdcp"
        [found] = asyncio.run(hook.sweep([ipaddress.ip_network("192.168.1.0/24")]))
        assert found == Found(
            "sdcp", "192.168.1.77", 3030, model=MODEL, name=NAME, mainboard_id=MAINBOARD
        )
        assert found.cleaned() == found

    def test_lists_a_section_to_paste(self):
        found = [
            parse_reply(discovery_reply(), "192.168.1.77"),
            parse_reply(discovery_reply(), "192.168.1.77"),  # one printer, answered twice
        ]
        text = format_sdcp(found, None, 3)
        assert f'192.168.1.77: {MODEL}, "{NAME}", firmware {FIRMWARE}' in text
        assert text.count("192.168.1.77:") == 1
        assert "  [printer workshop-cc]\n  adapter = sdcp\n" in text
        assert "  host = 192.168.1.77" in text

    def test_says_when_a_printer_is_already_configured(self, tmp_path):
        from printlog_bridge.core.config import load_config

        config = tmp_path / "printlog-bridge.cfg"
        config.write_text("[printer cc]\nadapter = sdcp\nhost = 192.168.1.77\n", encoding="utf-8")
        loaded = load_config(config, adapters=ADAPTERS, env={}, home=tmp_path)
        text = format_sdcp([parse_reply(discovery_reply(), "192.168.1.77")], loaded, 3)
        assert "already in the config as [printer cc]" in text

    def test_says_when_none_answered_or_it_couldnt_ask(self):
        assert "No Elegoo printer answered" in format_sdcp([], None, 3)
        assert "Permission denied" in format_sdcp(None, None, 3, "Permission denied")

    def test_the_command_lists_a_printer_that_answered(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("PRINTLOG_BRIDGE_CONFIG", str(tmp_path / "missing.cfg"))

        async def heard(targets, seconds=0):
            assert ("127.0.6.3", 3000) in targets
            return [parse_reply(discovery_reply(), "192.168.1.77")]

        monkeypatch.setattr(sdcp_discovery, "ask", heard)
        args = ["discover", "--subnet", "127.0.6.0/30", "--port", "1", "--no-zeroconf",
                "--no-bambu", "--no-anycubic"]  # fmt: skip
        code = main(args)

        out = capsys.readouterr().out
        assert code == 0
        assert "Found 1 Elegoo printer:" in out and "adapter = sdcp" in out


class TestResinPrinters:
    """Elegoo's resin printers answer `M99999` too, but print through other states (#97)."""

    def resin(self):
        reply = json.loads(discovery_reply("192.168.1.80", board="resin-board"))
        reply["Data"]["MachineName"] = "ELEGOO Saturn 4 Ultra"
        return parse_reply(json.dumps(reply).encode(), "192.168.1.80")

    def test_a_scan_leaves_them_out(self, monkeypatch):
        async def heard(targets, seconds=0):
            return [parse_reply(discovery_reply(), "192.168.1.77"), self.resin()]

        monkeypatch.setattr(sdcp_discovery, "ask", heard)
        found = asyncio.run(SdcpDiscovery().sweep([ipaddress.ip_network("192.168.1.0/24")]))
        assert [f.address for f in found] == ["192.168.1.77"]

    def test_discover_names_them_without_a_section(self):
        text = format_sdcp([self.resin()], None, 3)
        assert "192.168.1.80: ELEGOO Saturn 4 Ultra" in text
        assert "resin" in text and "adapter = sdcp" not in text


@contextlib.asynccontextmanager
async def silent_server():
    """Accepts a connection and never answers, not even the websocket upgrade."""
    held = []

    async def hold(reader, writer):
        held.append(writer)
        await reader.read()  # until the client gives up

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        for writer in held:
            writer.close()
        server.close()
        await server.wait_closed()


class TestSilentHost:
    """A host that accepts the connection but never answers the upgrade."""

    def adapter(self, port: int) -> SdcpAdapter:
        adapter = SdcpAdapter.from_config("cc", {"host": "127.0.0.1", "port": str(port)})
        adapter.connect_s = 0.3
        adapter.retry_s = 0.05
        return adapter

    def test_check_gives_up(self):
        async def scenario():
            async with silent_server() as port:
                return await asyncio.wait_for(self.adapter(port).check(), 5)

        [result] = asyncio.run(scenario())
        assert result.status is Status.FAIL

    def test_printer_details_give_up(self):
        async def scenario():
            async with silent_server() as port:
                return await asyncio.wait_for(self.adapter(port).printer_details(), 5)

        assert asyncio.run(scenario()).name == "cc"

    def test_run_reports_it_and_tries_again(self):
        report = Report()

        async def scenario():
            async with silent_server() as port:  # noqa: SIM117 - the port comes from the first
                async with running(self.adapter(port), Sink(), report):
                    await eventually(lambda: len(report.disconnects) >= 2, timeout=5)

        asyncio.run(scenario())
        assert "no answer" in report.disconnects[0]
