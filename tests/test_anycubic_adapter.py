"""The Anycubic LAN-mode adapter: config, the handshake, `check()`, `run` for an FDM and a resin
printer against `tests/fake_anycubic.py`, what a trace keeps, discovery from the recorded SSDP
answers, and the whole bridge between the fake and the fake API."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import timedelta

import bridge_helpers as helpers
import pytest
from fake_anycubic import FIXTURES, SERIAL, FakeAnycubic, certificate, job_block, resin_block
from fake_moonraker import eventually
from fake_printlog_api import FakePrintLogApi
from test_prusalink_adapter import Report, Sink, running

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.adapters.anycubic import AnycubicAdapter, protocol
from printlog_bridge.adapters.anycubic import handshake as hs
from printlog_bridge.adapters.anycubic.discovery import (
    AnycubicDiscovery,
    parse_reply,
    targets_for,
)
from printlog_bridge.core.checks import Status
from printlog_bridge.core.config import ConfigError
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted
from printlog_bridge.core.state import PrinterState


@pytest.fixture(scope="module")
def cert(tmp_path_factory):
    return certificate(tmp_path_factory.mktemp("tls"), "anycubic")


@contextlib.asynccontextmanager
async def printer(cert, *, resin=False):
    fake = FakeAnycubic(*cert, resin=resin)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.close()


def adapter_for(fake: FakeAnycubic) -> AnycubicAdapter:
    adapter = AnycubicAdapter.from_config("s1", {"host": "127.0.0.1", "port": str(fake.http_port)})
    adapter.retry_s = 0.05
    adapter.query_s = 0.1
    adapter.connect_s = 3.0
    return adapter


class TestConfig:
    def test_reads_its_options(self):
        adapter = AnycubicAdapter.from_config("s1", {"host": " 192.168.1.88 "})
        assert (adapter.host, adapter.port, adapter.mqtt_port) == ("192.168.1.88", 18910, None)
        assert adapter.describe() == "anycubic, 192.168.1.88"
        other = AnycubicAdapter.from_config(
            "s1", {"host": "s1.lan", "port": "18911", "mqtt_port": "9884"}
        )
        assert other.describe() == "anycubic, s1.lan:18911" and other.mqtt_port == 9884
        assert ADAPTERS["anycubic"] is AnycubicAdapter
        assert AnycubicAdapter.app_secrets == ()

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({}, "needs host"),
            ({"host": "http://192.168.1.88"}, "host"),
            ({"host": "192.168.1.88", "port": "0"}, "port"),
            ({"host": "192.168.1.88", "mqtt_port": "x"}, "mqtt_port"),
            ({"host": "192.168.1.88", "password": "x"}, "unknown option 'password'"),
        ],
    )
    def test_a_bad_value_is_refused(self, options, message):
        with pytest.raises(ConfigError, match=message):
            AnycubicAdapter.from_config("s1", options)

    def test_a_printer_from_the_app_needs_only_its_address(self):
        assert AnycubicAdapter.app_options("192.168.1.88", 18910, "x", {}) == {
            "host": "192.168.1.88"
        }
        assert AnycubicAdapter.app_options("192.168.1.88", 18911, None, {})["port"] == "18911"


class TestHandshake:
    def test_the_signature_is_md5_of_the_keyed_token_time_and_nonce(self):
        import hashlib

        token = "0123456789abcdef" + "fedcba9876543210"
        keyed = hashlib.md5(b"0123456789abcdef").hexdigest()
        assert (
            hs.signature(token, "1700000000000", "abc123")
            == hashlib.md5((keyed + "1700000000000abc123").encode()).hexdigest()
        )

    def test_the_recorded_info_answers_parse(self):
        for name, resin, mode in (("kobra_s1_idle.json", False, "lan"),
                                  ("photon_p1_max_handshake.json", True, "net")):  # fmt: skip
            events = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
            body = dict(events[0]["answer"], token="t" * 32)
            info = hs.parse_info(body)
            assert info.resin is resin and info.ctrl_type == mode
            assert "token" not in hs.traced_info(body) and "cn" not in hs.traced_info(body)

    def test_lan_mode_off_is_said_so(self):
        with pytest.raises(hs.LanModeOff, match="LAN mode"):
            hs.parse_info({"token": "t" * 32, "modelId": "20025", "ctrlType": "cloud"})

    @pytest.mark.parametrize(
        "answer",
        [
            {"code": 19002, "message": "failed"},
            {"code": 200, "data": {"token": "x" * 16}},
            {"code": 200, "data": {"info": "!!!", "token": "x" * 16}},
            "not an object",
        ],
    )
    def test_a_bad_ctrl_answer_is_a_handshake_error(self, answer):
        with pytest.raises(hs.HandshakeError):
            hs.open_credentials("t" * 32, answer)

    def test_both_paddings_and_both_broker_forms_open(self):
        from printlog_bridge.adapters.anycubic import aes

        token = "a" * 16 + "0123456789abcdef"
        for padding, broker, port in ((b"pkcs", "mqtts://10.0.0.5:9883", 9883),
                                      (b"zeros", "10.0.0.5:8883", 8883)):  # fmt: skip
            plain = json.dumps({"broker": broker, "username": "u", "password": "p",
                                "deviceId": "d", "clientId": "clientId"}).encode()  # fmt: skip
            fill = 16 - len(plain) % 16
            plain += bytes([fill]) * fill if padding == b"pkcs" else b"\x00" * fill
            sealed = aes.encrypt_cbc(plain, token[16:].encode(), b"ivivivivivivivi\x00")
            import base64

            answer = {"code": 200, "data": {"info": base64.b64encode(sealed).decode(),
                                            "token": "ivivivivivivivi"}}  # fmt: skip
            opened = hs.open_credentials(token, answer)
            assert (opened.broker_host, opened.broker_port) == ("10.0.0.5", port)
            assert opened.client_id == "clientId"
            assert "p" not in repr(opened).replace("port", "")

    def test_only_read_only_queries_can_be_sent(self):
        topics = protocol.Topics("20025", "dev")
        with pytest.raises(protocol.NotReadOnly):
            topics.request(protocol.Query("print", "stop"))
        with pytest.raises(protocol.NotReadOnly):
            topics.request(protocol.Query("light", "control"))
        topic, text = topics.request(protocol.Query("info", "query"))
        assert topic == "anycubic/anycubicCloud/v1/web/printer/20025/dev/info"
        assert json.loads(text)["data"] is None
        resin = protocol.Topics("132", "dev", resin=True)
        topic, _ = resin.request(protocol.Query("lanInfo", "query"))
        assert topic == "anycubic/anycubicCloud/v1/pc/printer/132/dev/status"


class TestCheck:
    def test_a_kobra_s1(self, cert):
        async def scenario():
            async with printer(cert) as fake:
                return await adapter_for(fake).check(), fake

        [result], fake = asyncio.run(scenario())
        assert result.status is Status.OK, result.detail
        assert "Anycubic Kobra S1" in result.detail and "firmware 2.5.6.4" in result.detail
        assert fake.refused == [] and fake.bad_signatures == 0

    def test_a_photon_p1_max_whose_broker_is_closed(self, cert):
        async def scenario():
            async with printer(cert, resin=True) as fake:
                fake.broker_open = False
                await fake.close_broker()
                return await adapter_for(fake).check()

        [result] = asyncio.run(scenario())
        assert result.status is Status.FAIL and "LAN mode" in result.detail

    def test_a_printer_that_isnt_there(self, cert):
        async def scenario():
            async with printer(cert) as fake:
                adapter = adapter_for(fake)
            return await adapter.check()

        [result] = asyncio.run(scenario())
        assert result.status is Status.FAIL and "127.0.0.1" in result.detail

    def test_lan_mode_off(self, cert):
        async def scenario():
            async with printer(cert) as fake:
                fake.ctrl_type = "cloud"
                return await adapter_for(fake).check()

        [result] = asyncio.run(scenario())
        assert result.status is Status.FAIL and "LAN mode is off" in result.detail


class TestRunFdm:
    def test_a_print_from_start_to_finish(self, cert, tmp_path):
        sink, report = Sink(), Report()

        async def scenario():
            async with printer(cert) as fake:
                adapter = adapter_for(fake)
                adapter.use_state(PrinterState.load(tmp_path, "s1"))
                async with running(adapter, sink, report):
                    await eventually(lambda: report.firmwares)
                    fake.project = job_block(state="preheating", print_status=6, print_time=0)
                    await fake.push_info()
                    await asyncio.sleep(0.3)
                    assert not sink.of(JobStarted)  # the start routine starts nothing
                    fake.project = job_block()
                    await eventually(lambda: sink.of(JobStarted))
                    fake.project = job_block(progress=60, curr_layer=90, supplies_usage=2400.5)
                    await fake.push_print()
                    fake.project = job_block(state="paused", pause=1, progress=61)
                    await fake.push_info()
                    fake.project = job_block(state="finished", print_status=2, progress=100,
                                             supplies_usage=4100.2)  # fmt: skip
                    await eventually(lambda: sink.of(JobFinished))
                    fake.last_project, fake.project = fake.project, None
                    await asyncio.sleep(0.3)  # idle reports after: the end isn't sent twice
                return fake

        fake = asyncio.run(scenario())
        [start] = sink.of(JobStarted)
        assert start.file_name == "Benchy_PLA_0.2.gcode"
        assert start.estimated_duration_s == (7 + 42) * 60
        assert start.estimated_volume_ml is None
        [done] = sink.of(JobFinished)
        assert done.outcome is JobOutcome.COMPLETED
        assert done.filament_mm == {0: 4100.2} and done.usage_estimated
        assert done.volume_ml == {}
        assert report.firmwares[0] == ("Anycubic", "2.5.6.4")
        assert report.hosts == []
        assert "free" in report.states and "busy" in report.states
        # Only the read-only queries, on the web topic, and a fresh handshake.
        assert fake.refused == []
        kinds = {(b["type"], b["action"]) for _, b in fake.requests}
        assert kinds <= protocol.READ_ONLY and ("info", "query") in kinds
        assert fake.http_methods[:2] == [("GET", "/info"), ("POST", "/ctrl")]
        kept = PrinterState.load(tmp_path, "s1").discovery
        assert kept["anycubic_identity"]["serial"] == SERIAL
        assert "anycubic_job" not in kept and "current_job" not in kept

    def test_a_cancel(self, cert, tmp_path):
        sink, report = Sink(), Report()

        async def scenario():
            async with printer(cert) as fake:
                adapter = adapter_for(fake)
                async with running(adapter, sink, report):
                    fake.project = job_block()
                    await eventually(lambda: sink.of(JobStarted))
                    fake.project = job_block(state="stoped", print_status=3, progress=20,
                                             supplies_usage=300.0)  # fmt: skip
                    await eventually(lambda: sink.of(JobFinished))

        asyncio.run(scenario())
        [done] = sink.of(JobFinished)
        assert done.outcome is JobOutcome.CANCELLED and done.filament_mm == {0: 300.0}

    def test_a_job_that_ended_while_the_bridge_was_away(self, cert, tmp_path):
        """The bridge restarts: the kept job is replayed, and `last_project` says it was
        cancelled."""

        async def scenario():
            async with printer(cert) as fake:
                first, second = Sink(), Sink()
                adapter = adapter_for(fake)
                adapter.use_state(PrinterState.load(tmp_path, "s1"))
                fake.project = job_block(progress=30)
                async with running(adapter, first, Report()):
                    await eventually(lambda: first.of(JobStarted))
                fake.last_project = job_block(state="stoped", print_status=3, progress=31)
                fake.project = None
                adapter = adapter_for(fake)
                adapter.use_state(PrinterState.load(tmp_path, "s1"))
                async with running(adapter, second, Report()):
                    await eventually(lambda: second.of(JobFinished))
                return first, second

        first, second = asyncio.run(scenario())
        [start] = first.of(JobStarted)
        assert [e.job_id for e in second.of(JobStarted)] == [start.job_id]
        [done] = second.of(JobFinished)
        assert done.outcome is JobOutcome.CANCELLED and "wasn't connected" in done.note

    def test_a_job_gone_without_word_is_judged_by_its_progress(self, cert, tmp_path):
        sink = Sink()

        async def scenario():
            async with printer(cert) as fake:
                adapter = adapter_for(fake)
                async with running(adapter, sink, Report()):
                    fake.project = job_block(progress=99.5)
                    await eventually(lambda: sink.of(JobStarted))
                    fake.project = None
                    await eventually(lambda: sink.of(JobFinished))

        asyncio.run(scenario())
        [done] = sink.of(JobFinished)
        assert done.outcome is JobOutcome.COMPLETED and "didn't see this job end" in done.note

    def test_a_reconnect_keeps_the_job(self, cert, tmp_path):
        sink, report = Sink(), Report()

        async def scenario():
            async with printer(cert) as fake:
                adapter = adapter_for(fake)
                async with running(adapter, sink, report):
                    fake.project = job_block()
                    await eventually(lambda: sink.of(JobStarted))
                    fake.drop()
                    await eventually(lambda: report.connects >= 2)
                    await eventually(lambda: fake.handshakes >= 2)
                    fake.project = job_block(state="finished", print_status=2, progress=100)
                    await eventually(lambda: sink.of(JobFinished))

        asyncio.run(scenario())
        assert len({e.job_id for e in sink.of(JobStarted)}) == 1
        [done] = sink.of(JobFinished)
        assert done.outcome is JobOutcome.COMPLETED

    def test_a_refused_login_is_retried_with_a_new_handshake(self, cert):
        report = Report()

        async def scenario():
            async with printer(cert) as fake:
                fake.refuse_mqtt = True
                adapter = adapter_for(fake)
                async with running(adapter, Sink(), report):
                    await eventually(lambda: fake.handshakes >= 2)
                    fake.refuse_mqtt = False
                    await eventually(lambda: report.connects)

        asyncio.run(scenario())
        assert any("refused the login" in r for r in report.disconnects)


class TestRunResin:
    def test_a_resin_print_logs_its_volume(self, cert, tmp_path):
        sink, report = Sink(), Report()

        async def scenario():
            async with printer(cert, resin=True) as fake:
                adapter = adapter_for(fake)
                async with running(adapter, sink, report):
                    await eventually(lambda: report.firmwares)
                    fake.job_state = "monitoring"
                    fake.project = resin_block()
                    await fake.push_print()
                    await asyncio.sleep(0.3)
                    fake.job_state = "printing"
                    fake.project = resin_block(curr_layer=10, progress=3)
                    await eventually(lambda: sink.of(JobStarted))
                    fake.job_state = "finished"
                    fake.project = resin_block(curr_layer=292, progress=100)
                    await eventually(lambda: sink.of(JobFinished))
                return fake

        fake = asyncio.run(scenario())
        [start] = sink.of(JobStarted)
        assert start.file_name == "model.pwmb" and start.estimated_volume_ml == 19.25
        [done] = sink.of(JobFinished)
        assert done.outcome is JobOutcome.COMPLETED
        assert done.volume_ml == {0: 19.25} and done.filament_mm == {}
        assert report.firmwares[0] == ("Anycubic", "1.0.3")
        # Photon Workshop's topic and client id; only read-only queries.
        assert fake.refused == []
        assert all("/pc/printer/" in topic for topic, _ in fake.requests)
        assert {c["client_id"] for c in fake.connects} == {"clientId"}

    def test_a_cancelled_resin_print_is_charged_by_its_layers(self, cert):
        sink = Sink()

        async def scenario():
            async with printer(cert, resin=True) as fake:
                adapter = adapter_for(fake)
                async with running(adapter, sink, Report()):
                    fake.project = resin_block(curr_layer=10)
                    await eventually(lambda: sink.of(JobStarted))
                    fake.project = resin_block(curr_layer=73)
                    await fake.push_print()
                    await asyncio.sleep(0.2)
                    fake.project = None  # idle, the job gone: 73 of 292 layers, cancelled
                    await eventually(lambda: sink.of(JobFinished))

        asyncio.run(scenario())
        [done] = sink.of(JobFinished)
        assert done.outcome is JobOutcome.CANCELLED
        assert done.volume_ml == {0: round(19.25 * 73 / 292, 2)}


def test_a_trace_keeps_no_secret(cert, tmp_path):
    from printlog_bridge.core.trace import Trace, trace_files

    state = tmp_path / "state"
    trace = Trace(state, "s1", secrets=[])

    async def scenario():
        async with printer(cert) as fake:
            adapter = adapter_for(fake)
            adapter.use_trace(trace)
            report = Report()
            async with running(adapter, Sink(), report):
                await eventually(lambda: report.firmwares)
                fake.project = job_block()
                await asyncio.sleep(0.3)
            return fake

    fake = asyncio.run(scenario())
    trace.close()
    text = "".join(p.read_text(encoding="utf-8") for p in trace_files(state, "s1"))
    assert '"info"' in text and '"project"' in text
    for secret in (*fake.login, fake.device_id, "gcode_upload", SERIAL):
        assert secret not in text


class TestDiscovery:
    def test_reads_the_recorded_answers(self):
        answers = json.loads((FIXTURES / "ssdp_answers.json").read_text(encoding="utf-8"))
        found = [parse_reply(a["answer"].encode("latin-1"), "192.168.4.9") for a in answers]
        by_kind = {f.resin: f for f in found if f is not None}
        assert by_kind[False].model == "Anycubic Kobra S1"
        assert by_kind[False].model_id == "20025"
        assert by_kind[True].model == "Anycubic Photon P1 Max"
        for f in by_kind.values():
            assert f.lan_mode and f.serial and f.serial.startswith("<serial")
            assert "token" not in repr(f)

    def test_ignores_other_ssdp_answers(self):
        reply = b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\nLOCATION: http://x/\r\n\r\n"
        assert parse_reply(reply, "192.168.1.5") is None

    def test_a_scan_asks_the_group_and_each_broadcast(self):
        import ipaddress

        targets = targets_for([ipaddress.ip_network("192.168.4.0/24")])
        assert ("239.255.255.250", 1900) in targets and ("192.168.4.255", 1900) in targets
        assert AnycubicDiscovery.kind == "anycubic"
        assert isinstance(AnycubicAdapter.discovery(), AnycubicDiscovery)


@contextlib.asynccontextmanager
async def fakes(cert):
    api = FakePrintLogApi()
    api_url = await api.start()
    try:
        async with printer(cert) as fake:
            yield fake, api, api_url
    finally:
        await api.close()


def test_the_bridge_logs_a_kobra_s1_print(cert, tmp_path):
    async def scenario():
        async with fakes(cert) as (fake, api, api_url):
            config = tmp_path / "printlog-bridge.cfg"
            config.write_text(
                f"[bridge]\napi_url = {api_url}\napi_key = key-123\n"
                f"state_dir = {tmp_path / 'state'}\n"
                f"[printer s1]\nadapter = anycubic\nprinter_id = 42\nhost = 127.0.0.1\n"
                f"port = {fake.http_port}\n",
                encoding="utf-8",
            )
            async with helpers.bridge(
                config, connected=lambda: fake.subscribed, disconnected=lambda: True
            ):
                fake.project = job_block(name="Calibration Cube.gcode")
                await fake.push_info()
                await eventually(lambda: api.prints, timeout=10)
                fake.project = job_block(name="Calibration Cube.gcode", state="finished",
                                         print_status=2, progress=100)  # fmt: skip
                await fake.push_info()
                await eventually(lambda: api.prints[1]["status"] != 2, timeout=10)
                await eventually(lambda: api.connections)
            return api.prints[1], next(iter(api.connections.values()))

    done, connection = asyncio.run(scenario())
    assert done["title"] == "Calibration Cube"
    assert done["status"] == 3
    assert done["externalSource"] == "anycubic"
    assert connection["kind"] == "anycubic"


def test_started_at_is_now_less_the_print_time(cert):
    sink = Sink()

    async def scenario():
        async with printer(cert) as fake, running(adapter_for(fake), sink, Report()):
            fake.project = job_block(print_time=30)
            await eventually(lambda: sink.of(JobStarted))

    asyncio.run(scenario())
    [start] = sink.of(JobStarted)
    from printlog_bridge.core.health import utc_now

    assert abs((utc_now() - start.started_at) - timedelta(minutes=30)) < timedelta(seconds=30)
