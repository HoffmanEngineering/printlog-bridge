"""`printlog-bridge probe`: what it records, that it sends nothing outside its allow-list, and
how it redacts."""

from __future__ import annotations

import asyncio
import json
import socket

import pytest
from fake_probe_targets import (
    BAMBU_NOTIFY,
    MAC,
    MAINBOARD,
    SDCP_DISCOVERY_REPLY,
    SERIAL,
    SSDP_REPLY,
    TOKEN,
    FakeHttp,
    FakeMqtt,
    FakeTcpText,
    FakeUdp,
    FakeWebsocket,
    free_port,
    mdns_answer,
)

from printlog_bridge.cli.main import main
from printlog_bridge.probe import allowlist, ports, record, services
from printlog_bridge.probe import run as probe_run
from printlog_bridge.probe.allowlist import NotAllowed, check
from printlog_bridge.probe.discovery import Network, mdns_query
from printlog_bridge.probe.redact import REDACTED, Redactor, redact_trace

pytestmark = pytest.mark.timeout(60)

WINDOW_S = 4.0


# -- the allow-list ----------------------------------------------------------------------------


class TestAllowList:
    def test_allows_the_documented_read_only_requests(self):
        for request in allowlist.CREALITY_REQUESTS:
            check("creality-ws", request)
        check("creality-ws", "ok")
        check("sdcp-ws", services.sdcp_request(MAINBOARD, 0))
        check("sdcp-ws", services.sdcp_request(MAINBOARD, 1))
        check("sdcp-ws", "ping")
        check("mqtt", services.mqtt_connect_packet("plb-probe-1"))
        check("mqtt", services.mqtt_subscribe_packet())
        check("mqtt", services.PINGREQ)
        check("mqtt", services.DISCONNECT)
        check("udp-mdns-query", mdns_query(allowlist.MDNS_SERVICES))
        check("udp-ssdp-msearch", allowlist.SSDP_MSEARCH)
        check("udp-chitu-discovery", b"M99999")
        check("photon-query", "sysinfo")
        for port in ports.PORTS:
            for path in port.paths:
                check("http-get", f"GET {path}")

    @pytest.mark.parametrize(
        "text",
        [
            '{"method":"set","params":{"pause":1}}',
            '{"method":"set","params":{"stop":1}}',
            '{"method":"set","params":{"nozzleTempControl":200}}',
            '{"method":"set","params":{"gcodeCmd":"G28"}}',
            '{"method":"get","params":{"ReqPrinterPara":1,"stop":1}}',
            "OK",
            "G28",
        ],
    )
    def test_refuses_creality_commands(self, text):
        with pytest.raises(NotAllowed):
            check("creality-ws", text)

    @pytest.mark.parametrize("cmd", [128, 129, 130, 131, 258, 255])
    def test_refuses_sdcp_commands_other_than_status_and_attributes(self, cmd):
        message = json.loads(services.sdcp_request(MAINBOARD, 0))
        message["Data"]["Cmd"] = cmd
        with pytest.raises(NotAllowed):
            check("sdcp-ws", json.dumps(message))

    def test_refuses_sdcp_requests_with_data(self):
        message = json.loads(services.sdcp_request(MAINBOARD, 0))
        message["Data"]["Data"] = {"Filename": "x.goo"}
        with pytest.raises(NotAllowed):
            check("sdcp-ws", json.dumps(message))

    def test_refuses_mqtt_publish_credentials_and_other_topics(self):
        publish = b"\x30\x0c\x00\x05topic" + b"hello"
        with pytest.raises(NotAllowed):
            check("mqtt", publish)
        with_credentials = bytearray(services.mqtt_connect_packet("x"))
        with_credentials[9] = 0xC2  # user name and password flags
        with pytest.raises(NotAllowed):
            check("mqtt", bytes(with_credentials))
        other_topic = b"\x82\x0b\x00\x01\x00\x06device\x00"
        with pytest.raises(NotAllowed):
            check("mqtt", other_topic)

    @pytest.mark.parametrize(
        "request_line",
        [
            "POST /",
            "GET /printer/print/start",
            "GET /machine/reboot",
            "PUT /api/v1/files",
            "DELETE /api/v1/job",
            "GET /printer/gcode/script?script=G28",
            "POST /ctrl",
            "GET /api/v1/job",
        ],
    )
    def test_refuses_http_other_than_get_of_listed_paths(self, request_line):
        with pytest.raises(NotAllowed):
            check("http-get", request_line)

    def test_refuses_anything_on_unknown_rules_and_other_udp(self):
        with pytest.raises(NotAllowed):
            check("telnet", "x")
        with pytest.raises(NotAllowed):
            check("udp-chitu-discovery", b"M4000")
        with pytest.raises(NotAllowed):
            check("photon-query", "gostop,end")
        with pytest.raises(NotAllowed):
            check("tcp-connect", b"hello")

    def test_refuses_an_mdns_message_with_answers(self):
        packet = bytearray(mdns_query(["_http._tcp.local"]))
        packet[7] = 1  # one answer
        with pytest.raises(NotAllowed):
            check("udp-mdns-query", bytes(packet))

    def test_the_recorder_checks_before_recording(self):
        rec = record.Recorder()
        with pytest.raises(NotAllowed):
            rec.send("creality-ws", "ws:9999/", '{"method":"set","params":{"stop":1}}')
        assert rec.sent == [] and rec.events == []

    def test_help_lists_every_rule(self, capsys):
        with pytest.raises(SystemExit):
            main(["probe", "--help"])
        out = capsys.readouterr().out
        for rule in allowlist.RULES:
            assert rule.id in out
        assert "9999" in out and "18910" in out


# -- the whole probe, against fakes ------------------------------------------------------------


def run(scenario):
    return asyncio.run(scenario)


async def probe_fakes(*, refuse_mqtt: int = 0, extra=()):
    http = await FakeHttp().start()
    creality = await FakeWebsocket("creality").start()
    sdcp = await FakeWebsocket("sdcp").start()
    mqtt = await FakeMqtt(refuse=refuse_mqtt).start()
    photon = await FakeTcpText().start()
    mdns = await FakeUdp(mdns_answer).start()
    ssdp = await FakeUdp(lambda data: SSDP_REPLY).start()
    chitu = await FakeUdp(lambda data: SDCP_DISCOVERY_REPLY if data == b"M99999" else None).start()
    bambu_port = free_port(socket.SOCK_DGRAM)
    closed = free_port()
    port_list = (
        ports.Port(http.port, ports.MOONRAKER, "Moonraker", ports.MOONRAKER_PATHS),
        ports.Port(http.port, ports.HTTP, "Anycubic /info", ("/", "/info")),
        ports.Port(creality.port, ports.CREALITY_WS, "Creality"),
        ports.Port(sdcp.port, ports.SDCP, "SDCP"),
        ports.Port(mqtt.port, ports.MQTT, "MQTT"),
        ports.Port(photon.port, ports.PHOTON, "Photon"),
        ports.Port(closed, ports.TCP, "nothing"),
    )
    network = Network(
        mdns_group=("127.0.0.1", mdns.port),
        mdns_port=mdns.port,
        ssdp_group=("127.0.0.1", ssdp.port),
        ssdp_port=ssdp.port,
        listen=(("bambu", bambu_port, None),),
        chitu_port=chitu.port,
        broadcast="127.0.0.1",
    )

    async def announce():
        await asyncio.sleep(0.5)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(BAMBU_NOTIFY, ("127.0.0.1", bambu_port))

    try:
        announcing = asyncio.ensure_future(announce())
        trace = await probe_run.probe(
            "127.0.0.1", WINDOW_S, port_list=port_list, network=network, extra_ports=extra
        )
        await announcing
    finally:
        for fake in (http, creality, sdcp, mqtt, photon, mdns, ssdp, chitu):
            await fake.close()
    fakes = {
        "http": http,
        "creality": creality,
        "sdcp": sdcp,
        "mqtt": mqtt,
        "photon": photon,
        "mdns": mdns,
        "ssdp": ssdp,
        "chitu": chitu,
        "closed": closed,
    }
    return trace, fakes


@pytest.fixture(scope="module")
def probed():
    return run(probe_fakes())


def events(trace, channel, kind="receive"):
    return [e for e in trace["events"] if e["channel"] == channel and e["kind"] == kind]


class TestWhatIsSent:
    def test_every_fake_received_only_allow_listed_requests(self, probed):
        _, fakes = probed
        for line in fakes["http"].requests + fakes["sdcp"].http:
            check("http-get", line)
        for text in fakes["creality"].received:
            check("creality-ws", text)
        for text in fakes["sdcp"].received:
            check("sdcp-ws", text)
        for packet in fakes["mqtt"].packets:
            check("mqtt", packet)
        for text in fakes["photon"].received:
            check("photon-query", text)
        for data in fakes["mdns"].received:
            check("udp-mdns-query", data)
        for data in fakes["ssdp"].received:
            check("udp-ssdp-msearch", data)
        for data in fakes["chitu"].received:
            check("udp-chitu-discovery", data)

    def test_exactly_these_requests(self, probed):
        _, fakes = probed
        assert sorted(set(fakes["http"].requests)) == sorted(
            {f"GET {p}" for p in ports.MOONRAKER_PATHS} | {"GET /", "GET /info"}
        )
        assert fakes["creality"].protocols == ["wsslicer"]
        assert fakes["creality"].received == [*allowlist.CREALITY_REQUESTS, "ok"]
        assert [json.loads(t)["Data"]["Cmd"] for t in fakes["sdcp"].received] == [0, 1]
        assert {json.loads(t)["Data"]["MainboardID"] for t in fakes["sdcp"].received} == {MAINBOARD}
        assert [p[0] for p in fakes["mqtt"].packets] == [0x10, 0x82, 0xE0]
        assert fakes["photon"].received == ["sysinfo", "getstatus"]
        assert set(fakes["chitu"].received) == {b"M99999"}
        assert set(fakes["ssdp"].received) == {allowlist.SSDP_MSEARCH}

    def test_follows_up_on_service_types_the_printer_lists(self, probed):
        _, fakes = probed
        assert any(b"\x09_creality" in q for q in fakes["mdns"].received)

    def test_the_trace_lists_each_send_with_its_rule(self, probed):
        trace, _ = probed
        sends = [e for e in trace["events"] if e["kind"] == "send"]
        assert sends and all(e["rule"] in allowlist.RULE_IDS for e in sends)
        assert trace["sentCount"] == len(sends)
        assert [r["id"] for r in trace["allowList"]] == [r.id for r in allowlist.RULES]


class TestWhatIsRecorded:
    def test_ports(self, probed):
        trace, fakes = probed
        rows = {(r["port"], r["kind"]): r for r in trace["ports"]}
        assert rows[(fakes["http"].port, ports.MOONRAKER)]["open"] is True
        assert rows[(fakes["closed"], ports.TCP)]["open"] is False
        assert all("suspected" in r for r in trace["ports"])

    def test_http_answers_with_status_headers_and_body(self, probed):
        trace, fakes = probed
        answers = {e["path"]: e for e in events(trace, f"http:{fakes['http'].port}")}
        assert answers["/server/info"]["status"] == 200
        assert json.loads(answers["/server/info"]["body"]["text"])["result"]["klippy_state"] == (
            "ready"
        )
        assert answers["/"]["headers"]["Server"] == "nginx"
        assert TOKEN in answers["/info"]["body"]["text"]  # raw mode keeps it; see redaction

    def test_websocket_messages_pushed_and_answered(self, probed):
        trace, fakes = probed
        texts = [
            e["message"]["text"]
            for e in events(trace, f"ws:{fakes['creality'].port}/")
            if "message" in e
        ]
        assert any("heart_beat" in t for t in texts)
        assert any("printProgress" in t for t in texts)
        assert any("ReqPrinterPara" in t for t in texts)
        sdcp = [e for e in events(trace, f"ws:{fakes['sdcp'].port}/websocket") if "message" in e]
        assert len(sdcp) == 2

    def test_mqtt_messages(self, probed):
        trace, fakes = probed
        received = events(trace, f"mqtt:{fakes['mqtt'].port}")
        assert received[0]["connack"] == 0
        published = [e for e in received if "topic" in e]
        assert published[0]["topic"] == f"device/{SERIAL}/report"
        assert "RUNNING" in published[0]["message"]["text"]

    def test_discovery(self, probed):
        trace, _ = probed
        mdns = events(trace, "udp:mdns")
        records = [r for e in mdns for r in e.get("records", [])]
        assert {"type": "SRV", "name": "Creality Hi._http._tcp.local"}.items() <= {
            **next(r for r in records if r["type"] == "SRV")
        }.items()
        assert any(r["type"] == "TXT" and f"sn={SERIAL}" in r["data"] for r in records)
        assert SERIAL in events(trace, "udp:ssdp")[0]["message"]["text"]
        chitu = events(trace, "udp:chitu")
        assert chitu[0]["sdcp"] is True and MAINBOARD in chitu[0]["message"]["text"]
        assert "bambulab" in events(trace, "udp:bambu")[0]["message"]["text"]

    def test_photon_answers(self, probed):
        trace, fakes = probed
        answers = {
            e["query"]: e["message"]["text"]
            for e in events(trace, f"tcp:{fakes['photon'].port}")
            if "query" in e
        }
        assert answers["getstatus"] == "getstatus,stop,end"

    def test_a_broker_that_wants_credentials_is_left_alone(self):
        trace, fakes = run(probe_fakes(refuse_mqtt=5))
        received = events(trace, f"mqtt:{fakes['mqtt'].port}")
        assert received[0]["connack"] == 5
        assert "credentials required" in received[0]["meaning"]
        assert [p[0] for p in fakes["mqtt"].packets] == [0x10]


class TestBodiesAreCapped:
    def test_payload_cut(self):
        out = record.payload(b"x" * 100, cap=10)
        assert out == {"size": 100, "text": "x" * 10, "truncated": True}
        assert "base64" in record.payload(bytes(range(256)))

    def test_messages_per_channel_are_counted_past_the_cap(self, monkeypatch):
        monkeypatch.setattr(record, "MAX_PER_CHANNEL", 2)
        rec = record.Recorder()
        for _ in range(5):
            rec.receive("ws:1/", message={})
        assert len(rec.events) == 2 and rec.dropped["ws:1/"] == 3 and rec.received("ws:1/") == 5


# -- redaction ---------------------------------------------------------------------------------


class TestRedaction:
    def test_the_file_redacts_identifiers_credentials_and_addresses(self, probed):
        trace, _ = probed
        redacted = probe_run.finish(json.loads(json.dumps(trace)), redact=True)
        text = json.dumps(redacted)
        for secret in (SERIAL, MAC, TOKEN, MAINBOARD, "01S00C123456789", "127.0.0.1"):
            assert secret not in text, secret
        assert "<serial-1>" in text and "<ip-1>" in text and REDACTED in text
        assert redacted["probe"]["redacted"] is True
        assert "redacted" in redacted["probe"]["mode"]
        # The allow-list is the probe's own text, kept as is.
        assert redacted["allowList"] == trace["allowList"]

    def test_placeholders_are_stable(self):
        trace = {
            "a": {"serial": "ABCDEF123"},
            "b": "seen ABCDEF123 again, mac AA:BB:CC:00:11:22",
            "c": "aa-bb-cc-00-11-22 at 192.168.1.20 and 192.168.1.20",
        }
        out = redact_trace(trace)
        assert out["a"]["serial"] == "<serial-1>"
        assert out["b"] == "seen <serial-1> again, mac <mac-1>"
        assert out["c"] == "<mac-1> at <ip-1> and <ip-1>"

    def test_keeps_firmware_versions_and_special_addresses(self):
        out = redact_trace({"v": "firmware 1.3.3.46, group 224.0.0.251, 0.0.0.0, 127.0.0.1"})
        assert out["v"] == "firmware 1.3.3.46, group 224.0.0.251, 0.0.0.0, 127.0.0.1"

    def test_learns_from_json_in_strings_and_from_header_text(self):
        trace = {
            "body": json.dumps({"result": {"cpu_info": {"serial_number": "10000000abcdef"}}}),
            "ssdp": "USN: uuid:2bd3c9f0-1111::upnp:rootdevice\r\nServer: x",
            "chitu": "ok MAC:00:1A:2B:3C:4D:5E IP:10.0.0.5 ID:2e7f00182c15",
            "other": "10000000abcdef appears here too",
            "headers": {"Set-Cookie": "session=abc123"},
            "base64": "AAAA",
        }
        out = redact_trace(trace)
        dumped = json.dumps(out)
        for value in (
            "10000000abcdef",
            "2bd3c9f0-1111",
            "00:1A:2B:3C:4D:5E",
            "10.0.0.5",
            "2e7f00182c15",
            "abc123",
        ):
            assert value not in dumped, value
        assert out["headers"]["Set-Cookie"] == REDACTED
        assert out["base64"].startswith("<binary")

    def test_summary_hides_identifiers_but_keeps_addresses(self, probed):
        trace, _ = probed
        summary = probe_run.summarize(probe_run.finish(json.loads(json.dumps(trace)), False), None)
        for secret in (SERIAL, MAC, TOKEN):
            assert secret not in summary, secret
        assert "127.0.0.1" in summary
        assert "Creality Hi" in summary  # the page title
        assert "credentials, serial numbers and MAC addresses hidden" in summary

    def test_redactor_without_ips(self):
        redactor = Redactor(ips=False)
        redactor.learn({"access_code": "12345678"})
        assert redactor.text("code 12345678 at 192.168.1.2") == f"code {REDACTED} at 192.168.1.2"


# -- the command -------------------------------------------------------------------------------


class TestCommand:
    def test_writes_the_trace_and_prints_the_summary(self, tmp_path, monkeypatch, capsys):
        async def fake_probe(host, seconds, **options):
            return {
                "probe": {
                    "version": "x",
                    "host": host,
                    "address": "192.168.1.50",
                    "seconds": seconds,
                    "redacted": False,
                },
                "allowList": allowlist.to_json(),
                "ports": [
                    {"port": 9999, "kind": "creality-ws", "suspected": "Creality", "open": True}
                ],
                "events": [
                    {
                        "channel": "http:80",
                        "kind": "receive",
                        "path": "/info",
                        "status": 200,
                        "headers": {},
                        "body": {"size": 30, "text": json.dumps({"sn": SERIAL})},
                    }
                ],
                "notes": [],
                "sentCount": 1,
            }

        monkeypatch.setattr(probe_run, "probe", fake_probe)
        out = tmp_path / "trace.json"
        assert main(["probe", "192.168.1.50", "--seconds", "5", "--out", str(out), "--redact"]) == 0
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["probe"]["redacted"] is True
        assert SERIAL not in out.read_text(encoding="utf-8")
        assert "192.168.1.50" not in out.read_text(encoding="utf-8")
        printed = capsys.readouterr().out
        assert "redacted" in printed and str(out) in printed and SERIAL not in printed

    def test_says_when_the_file_is_not_redacted(self, tmp_path, monkeypatch, capsys):
        async def fake_probe(host, seconds, **options):
            return {
                "probe": {"version": "x", "host": host, "address": host, "seconds": seconds},
                "allowList": [],
                "ports": [],
                "events": [],
                "notes": [],
                "sentCount": 0,
            }

        monkeypatch.setattr(probe_run, "probe", fake_probe)
        monkeypatch.chdir(tmp_path)
        assert main(["probe", "10.1.2.3", "--seconds", "5"]) == 0
        files = list(tmp_path.glob("probe-10.1.2.3-*.json"))
        assert len(files) == 1
        assert "NOT redacted" in capsys.readouterr().out

    def test_rejects_a_window_out_of_range(self, capsys):
        assert main(["probe", "127.0.0.1", "--seconds", "1"]) == 2

    def test_an_unknown_host(self, capsys):
        assert main(["probe", "no-such-host.invalid", "--seconds", "5"]) == 2
        assert "can't resolve" in capsys.readouterr().err
