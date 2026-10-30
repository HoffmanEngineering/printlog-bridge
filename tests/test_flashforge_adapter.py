"""The FlashForge adapter (Adventurer 5M, 5M Pro, AD5X): config, `check()`, `run()` against
`tests/fake_flashforge.py`, what a trace keeps, discovery, `discover`, and the whole bridge
between the fake and the fake API. Built from community sources, not a recorded printer (#88)."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging

import bridge_helpers as helpers
import pytest
from aiohttp import web
from fake_flashforge import (
    CHECK_CODE,
    FIRMWARE,
    NAME,
    PNG,
    SERIAL,
    FakeDiscovery,
    FakeFlashForge,
    discovery_reply,
    legacy_reply,
)
from fake_moonraker import eventually
from fake_printlog_api import FakePrintLogApi
from test_prusalink_adapter import Report, Sink, running

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.adapters.flashforge import FlashForgeAdapter
from printlog_bridge.adapters.flashforge import discovery as ff_discovery
from printlog_bridge.adapters.flashforge.client import FlashForgeClient
from printlog_bridge.adapters.flashforge.discovery import (
    FlashForgeDiscovery,
    FoundFlashForge,
    ask,
    parse_reply,
    targets_for,
)
from printlog_bridge.cli.discover import format_flashforge
from printlog_bridge.cli.main import main
from printlog_bridge.core.adapter import NeedsCredentials
from printlog_bridge.core.checks import Status
from printlog_bridge.core.config import ConfigError
from printlog_bridge.core.discovery import Found
from printlog_bridge.core.events import JobFinished, JobImage, JobOutcome, JobStarted
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.trace import Trace, trace_files


def options(**more) -> dict[str, str]:
    return {"host": "127.0.0.1", "serial": SERIAL, "check_code": CHECK_CODE, **more}


class TestConfig:
    def test_reads_its_options(self):
        adapter = FlashForgeAdapter.from_config(
            "ad5m", options(host=" 192.168.1.60 ", poll_interval="2", idle_poll_interval="20")
        )
        assert adapter.base_url == "http://192.168.1.60:8898"
        assert (adapter.serial, adapter.check_code) == (SERIAL, CHECK_CODE)
        assert (adapter.poll_interval, adapter.idle_poll_interval) == (2.0, 20.0)
        assert adapter.files is True
        assert adapter.describe() == "flashforge, http://192.168.1.60:8898"
        assert CHECK_CODE not in adapter.describe()
        assert ADAPTERS["flashforge"] is FlashForgeAdapter
        assert FlashForgeAdapter.app_secrets == ("check_code",)

    def test_the_check_code_is_used_as_given(self):
        adapter = FlashForgeAdapter.from_config("ad5m", options(check_code=" ab12 "))
        assert adapter.check_code == " ab12 "

    @pytest.mark.parametrize(
        ("more", "message"),
        [
            ({"host": ""}, "needs host"),
            ({"host": "http://192.168.1.60"}, "host"),
            ({"serial": ""}, "needs serial"),
            ({"serial": "SN 1"}, "needs serial"),
            ({"check_code": "  "}, "needs check_code"),
            ({"port": "0"}, "port"),
            ({"poll_interval": "0.1"}, "poll_interval"),
            ({"idle_poll_interval": "1"}, "idle_poll_interval"),
            ({"files": "maybe"}, "files must be true or false"),
            ({"password": "x"}, "unknown option 'password'"),
        ],
    )
    def test_a_bad_value_is_refused(self, more, message):
        with pytest.raises(ConfigError, match=message):
            FlashForgeAdapter.from_config("ad5m", options(**more))

    def test_a_printer_from_the_app(self):
        made = FlashForgeAdapter.app_options(
            "192.168.1.60", 8898, SERIAL, {"check_code": CHECK_CODE}
        )
        assert made == {"host": "192.168.1.60", "serial": SERIAL, "check_code": CHECK_CODE}
        adapter = FlashForgeAdapter.from_config("app-3", made)
        assert adapter.watches(Found("flashforge", "192.168.1.99", 8898, serial=SERIAL.lower()))
        assert not adapter.watches(Found("flashforge", "192.168.1.60", 8898, serial="SNOTHER"))
        assert adapter.watches(Found("flashforge", "192.168.1.60", 8898))
        assert not adapter.watches(Found("bambu", "192.168.1.60", 8898, serial=SERIAL))
        assert adapter.found_entry() == Found("flashforge", "192.168.1.60", 8898, serial=SERIAL)

    def test_the_app_must_send_a_serial_and_a_check_code(self):
        with pytest.raises(NeedsCredentials) as missing:
            FlashForgeAdapter.app_options("192.168.1.60", 8898, SERIAL, {})
        assert missing.value.fields == ("check_code",)
        with pytest.raises(NeedsCredentials) as no_serial:
            FlashForgeAdapter.app_options("192.168.1.60", 8898, None, {"check_code": "x"})
        assert no_serial.value.fields == ()


@contextlib.asynccontextmanager
async def printer(model: str = "5M"):
    fake = await FakeFlashForge(model).start()
    try:
        yield fake
    finally:
        await fake.close()


def adapter_for(fake: FakeFlashForge, **more) -> FlashForgeAdapter:
    adapter = FlashForgeAdapter.from_config(
        "ad5m",
        options(port=str(fake.port), poll_interval="1", idle_poll_interval="1", **more),
    )
    adapter.poll_interval = adapter.idle_poll_interval = 0.05
    adapter.retry_s = 0.05
    adapter.probe_s = 0.5
    return adapter


async def closed_port() -> int:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


class TestCheck:
    def test_a_reachable_printer(self):
        async def scenario():
            async with printer("AD5X") as fake:
                return await adapter_for(fake).check()

        [result] = asyncio.run(scenario())
        assert result.status is Status.OK
        assert "AD5X firmware 1.1.3-1.0.8" in result.detail and "4 slots" in result.detail

    def test_a_wrong_check_code(self):
        async def scenario():
            async with printer() as fake:
                return await adapter_for(fake, check_code="00000000").check()

        [result] = asyncio.run(scenario())
        assert result.status is Status.FAIL and "check code" in result.detail
        assert "LAN-only" in result.detail and "00000000" not in result.detail

    def test_a_printer_that_isnt_there(self):
        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake)
            adapter.moonraker_port = adapter.tcp_port = await closed_port()
            return await adapter.check()

        [result] = asyncio.run(scenario())
        assert result.status is Status.FAIL and "can't reach http://127.0.0.1" in result.detail

    def test_firmware_without_the_http_api(self):
        async def scenario():
            async with printer() as fake:
                fake.answer = False
                legacy = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
                adapter = adapter_for(fake)
                adapter.moonraker_port = await closed_port()
                adapter.tcp_port = legacy.sockets[0].getsockname()[1]
                try:
                    return await adapter.check(), fake.requests
                finally:
                    legacy.close()

        [result], requests = asyncio.run(scenario())
        assert result.status is Status.FAIL and "update its firmware" in result.detail
        assert requests == [("POST", "/detail")]

    def test_a_klipper_mod_runs_moonraker(self):
        async def server_info(request):
            return web.json_response({"result": {"klippy_state": "ready"}})

        async def scenario():
            app = web.Application()
            app.router.add_get("/server/info", server_info)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            try:
                async with printer() as fake:
                    adapter = adapter_for(fake)
                adapter.moonraker_port = site._server.sockets[0].getsockname()[1]
                return await adapter.check()
            finally:
                await runner.cleanup()

        [result] = asyncio.run(scenario())
        assert result.status is Status.FAIL and "adapter = moonraker" in result.detail


def test_printer_details_come_from_the_status():
    async def scenario():
        async with printer("AD5X") as fake:
            return await adapter_for(fake).printer_details()

    details = asyncio.run(scenario())
    assert (details.name, details.make, details.model, details.slot_count) == (
        NAME, "FlashForge", "AD5X", 4,
    )  # fmt: skip


def test_the_client_refuses_anything_but_a_read():
    async def scenario():
        client = FlashForgeClient("http://127.0.0.1:9", serial=SERIAL, check_code=CHECK_CODE)
        try:
            for path in ("/control", "/printGcode", "/uploadGcode"):
                with pytest.raises(ValueError):
                    await client.post(path)
        finally:
            await client.close()

    asyncio.run(scenario())


class TestRun:
    def test_a_print_from_start_to_finish(self, tmp_path):
        sink, report = Sink(), Report()

        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake)
                adapter.use_state(PrinterState.load(tmp_path, "ad5m"))
                async with running(adapter, sink, report):
                    await eventually(lambda: report.connects)
                    fake.print_file("Benchy.gcode", grams=12.5, mm=4130.0)
                    await eventually(lambda: sink.of(JobStarted))
                    fake.progress(0.5, duration=1800)
                    await eventually(lambda: fake.paths("/detail") > 6)
                    fake.complete()
                    await eventually(lambda: sink.of(JobFinished))
                [image] = sink.of(JobImage)
                thumbnail = await image.fetch()
                return fake, thumbnail

        fake, thumbnail = asyncio.run(scenario())
        [start] = sink.of(JobStarted)
        assert start.file_name == "Benchy.gcode" and start.estimated_filament_g == 12.5
        [end] = sink.of(JobFinished)
        assert end.outcome is JobOutcome.COMPLETED
        assert end.filament_g == {0: 12.5} and end.usage_estimated is True
        assert thumbnail == PNG
        assert report.firmwares == [("FlashForge", FIRMWARE)]
        assert "printing" in report.states
        assert fake.methods == {"POST"}
        assert {p for _, p in fake.requests} <= {"/detail", "/gcodeList", "/gcodeThumb"}
        assert all(b["checkCode"] == CHECK_CODE for b in fake.bodies)

    def test_a_refused_check_code_is_reported(self):
        sink, report = Sink(), Report()

        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake, check_code="00000000")
                async with running(adapter, sink, report):
                    await eventually(lambda: report.refused)

        asyncio.run(scenario())
        assert report.refused == [("check_code",)]
        assert "check code" in report.warnings["access"]
        assert all("00000000" not in d for d in report.disconnects)

    def test_a_dropped_printer_carries_on_with_the_same_job(self, tmp_path):
        sink, report = Sink(), Report()

        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake)
                adapter.use_state(PrinterState.load(tmp_path, "ad5m"))
                async with running(adapter, sink, report):
                    fake.print_file("Cube.gcode")
                    await eventually(lambda: sink.of(JobStarted))
                    fake.answer = False
                    await eventually(lambda: report.disconnects)
                    fake.progress(0.6, duration=2000)
                    fake.answer = True
                    await eventually(lambda: report.connects >= 2)
                    fake.cancel()
                    await eventually(lambda: sink.of(JobFinished))

        asyncio.run(scenario())
        assert len({e.job_id for e in sink.of(JobStarted)}) == 1
        [end] = sink.of(JobFinished)
        assert end.outcome is JobOutcome.CANCELLED and end.filament_g == {0: 7.5}

    def test_stopping_always_stops_it(self):
        async def scenario():
            async with printer() as fake:
                adapter = adapter_for(fake)
                task = asyncio.ensure_future(adapter.run(Sink(), Report()))
                await eventually(lambda: fake.paths("/detail"))
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                return task.cancelled()

        assert asyncio.run(scenario())

    def test_an_ad5x_multicolor_print(self, tmp_path):
        sink, report = Sink(), Report()
        readings: list = []

        class Spools:
            async def spool_reading(self, reading):
                readings.append(reading)

            async def spool_command(self, command):  # pragma: no cover
                return ""

        async def scenario():
            async with printer("AD5X") as fake:
                adapter = adapter_for(fake)
                adapter.use_spools(Spools())
                async with running(adapter, sink, report):
                    await eventually(lambda: readings)
                    fake.print_file(
                        "Two color.3mf",
                        grams=10.0,
                        tools=[
                            {"toolId": 0, "slotId": 1, "materialName": "PLA",
                             "materialColor": "#FFFFFF", "filamentWeight": 8.0},
                            {"toolId": 1, "slotId": 2, "materialName": "PETG",
                             "materialColor": "#1A1A1A", "filamentWeight": 2.0},
                        ],
                    )  # fmt: skip
                    await eventually(lambda: sink.of(JobStarted))
                    fake.complete()
                    await eventually(lambda: sink.of(JobFinished))

        asyncio.run(scenario())
        [start] = sink.of(JobStarted)
        assert start.estimated_filament_g_by_slot == {0: 8.0, 1: 2.0}
        [end] = sink.of(JobFinished)
        assert end.filament_g == {0: 8.0, 1: 2.0}
        assert {(r.slot, r.material) for r in readings} == {(0, "PLA"), (1, "PETG"), (3, "PLA")}


def test_a_trace_keeps_neither_the_check_code_nor_the_printers_addresses(tmp_path):
    trace = Trace(tmp_path, "ad5m", secrets=[])

    async def scenario():
        async with printer() as fake:
            adapter = adapter_for(fake)
            adapter.use_trace(trace)
            sink, report = Sink(), Report()
            async with running(adapter, sink, report):
                fake.print_file("Benchy.gcode")
                await eventually(lambda: sink.of(JobStarted))
                await eventually(lambda: fake.paths("/detail") > 3)

    asyncio.run(scenario())
    trace.close()
    text = "".join(p.read_text(encoding="utf-8") for p in trace_files(tmp_path))
    assert '"printFileName": "Benchy.gcode"' in text and '"/detail"' in text
    for secret in (CHECK_CODE, SERIAL, "192.168.0.204", "88:A9:A7", "REG-FLASH", NAME):
        assert secret not in text, secret
    records = [json.loads(line) for line in text.splitlines()]
    assert records


@contextlib.asynccontextmanager
async def fakes():
    api = FakePrintLogApi()
    api_url = await api.start()
    try:
        async with printer() as fake:
            yield fake, api, api_url
    finally:
        await api.close()


def test_the_bridge_logs_an_adventurer_5m_print(tmp_path, caplog):
    async def scenario():
        async with fakes() as (fake, api, api_url):
            config = tmp_path / "printlog-bridge.cfg"
            config.write_text(
                f"[bridge]\napi_url = {api_url}\napi_key = key-123\n"
                f"state_dir = {tmp_path / 'state'}\ntrace = true\n"
                f"[printer ad5m]\nadapter = flashforge\nprinter_id = 42\nhost = 127.0.0.1\n"
                f"port = {fake.port}\nserial = {SERIAL}\ncheck_code = {CHECK_CODE}\n"
                "poll_interval = 1\nidle_poll_interval = 1\n",
                encoding="utf-8",
            )
            async with helpers.bridge(
                config, connected=lambda: fake.paths("/detail"), disconnected=lambda: True
            ):
                fake.print_file("Calibration Cube.gcode", grams=12.5, mm=4130.0)
                await eventually(lambda: api.prints, timeout=10)
                fake.progress(0.5, duration=1800)
                await asyncio.sleep(1.2)
                fake.cancel()
                await eventually(lambda: api.prints[1]["status"] != 2, timeout=10)
                await eventually(lambda: api.connections)
            return fake, api.prints[1], next(iter(api.connections.values())), api

    with caplog.at_level(logging.DEBUG):
        fake, done, connection, api = asyncio.run(scenario())
    assert done["title"] == "Calibration Cube"
    assert done["externalSource"] == "flashforge"
    assert connection["kind"] == "flashforge"
    assert fake.methods == {"POST"}
    bridge_log = [r.getMessage() for r in caplog.records if r.name != "aiohttp.access"]
    assert bridge_log and not any(CHECK_CODE in line for line in bridge_log)
    for path in (tmp_path / "state").rglob("*"):
        if path.is_file():
            assert CHECK_CODE.encode() not in path.read_bytes(), path


class TestDiscovery:
    def test_reads_a_printers_reply(self):
        found = parse_reply(discovery_reply(product=0x26), "192.168.1.60")
        assert found == FoundFlashForge("192.168.1.60", SERIAL, NAME, "AD5X", 8898)

    @pytest.mark.parametrize(
        "data", [b"", b"\x00" * 300, legacy_reply(), discovery_reply(serial="bad serial!")]
    )
    def test_ignores_anything_else(self, data):
        assert parse_reply(data, "192.168.1.60") is None

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
        assert [f.serial for f in found] == [SERIAL]
        assert asked == [b""]  # one empty datagram per target

    def test_where_it_asks(self):
        targets = targets_for([ipaddress.ip_network("192.168.1.0/24")])
        assert targets == [
            ("225.0.0.9", 19000),
            ("225.0.0.9", 8899),
            ("192.168.1.255", 48899),
            ("255.255.255.255", 48899),
        ]

    def test_a_scan_reports_the_printer_by_its_serial(self, monkeypatch):
        async def heard(targets, seconds=0):
            return [parse_reply(discovery_reply(), "192.168.1.60")]

        monkeypatch.setattr(ff_discovery, "ask", heard)
        hook = FlashForgeAdapter.discovery()
        assert isinstance(hook, FlashForgeDiscovery) and hook.kind == "flashforge"
        [found] = asyncio.run(hook.sweep([ipaddress.ip_network("192.168.1.0/24")]))
        assert found == Found(
            "flashforge", "192.168.1.60", 8898, model="Adventurer 5M", name=NAME, serial=SERIAL
        )
        assert found.cleaned() == found

    def test_lists_a_section_to_paste(self):
        text = format_flashforge([parse_reply(discovery_reply(), "192.168.1.60")], None, 3)
        assert f'192.168.1.60: Adventurer 5M, "{NAME}", serial {SERIAL}' in text
        assert "  adapter = flashforge\n" in text and f"  serial = {SERIAL}" in text
        assert "  check_code =" in text

    def test_says_when_a_printer_is_already_configured(self, tmp_path):
        from printlog_bridge.core.config import load_config

        config = tmp_path / "printlog-bridge.cfg"
        config.write_text(
            f"[printer ad5m]\nadapter = flashforge\nhost = 192.168.1.61\nserial = {SERIAL}\n"
            "check_code = x\n",
            encoding="utf-8",
        )
        loaded = load_config(config, adapters=ADAPTERS, env={}, home=tmp_path)
        text = format_flashforge([parse_reply(discovery_reply(), "192.168.1.60")], loaded, 3)
        assert "already in the config as [printer ad5m]" in text

    def test_says_when_none_answered_or_it_couldnt_ask(self):
        assert "No FlashForge printer answered" in format_flashforge([], None, 3)
        assert "Permission denied" in format_flashforge(None, None, 3, "Permission denied")

    def test_the_command_lists_a_printer_that_answered(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("PRINTLOG_BRIDGE_CONFIG", str(tmp_path / "missing.cfg"))

        async def heard(targets, seconds=0):
            assert ("127.0.6.3", 48899) in targets
            return [parse_reply(discovery_reply(), "192.168.1.60")]

        monkeypatch.setattr(ff_discovery, "ask", heard)
        args = ["discover", "--subnet", "127.0.6.0/30", "--port", "1", "--no-zeroconf",
                "--no-bambu", "--no-sdcp", "--no-anycubic"]  # fmt: skip
        code = main(args)
        out = capsys.readouterr().out
        assert code == 0
        assert "Found 1 FlashForge printer:" in out and "adapter = flashforge" in out
