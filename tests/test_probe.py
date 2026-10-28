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
    REVIEW_LEAKS,
    REVIEW_TRACE,
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
    mdns_bad_names,
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
        assert rec.sent == []
        assert [e["kind"] for e in rec.events] == ["refused"]
        assert rec.refused[0][0] == "creality-ws"

    def test_never_asks_for_the_current_file(self):
        assert not any("reqGcodeFile" in r for r in allowlist.CREALITY_REQUESTS)
        with pytest.raises(NotAllowed):
            check("creality-ws", '{"method":"get","params":{"reqGcodeFile":1}}')

    @pytest.mark.parametrize(
        "names",
        [["_foo._tcp"], ["_x._tcp.local"] * 33, ["_" + "a" * 70 + "._tcp.local"]],
    )
    def test_refuses_mdns_queries_the_printer_could_make_us_send(self, names):
        with pytest.raises((NotAllowed, ValueError)):
            check("udp-mdns-query", mdns_query(names))

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


async def probe_fakes(
    *, refuse_mqtt: int = 0, extra=(), photon_flag=True, mdns_reply=mdns_answer, short=False
):
    http = await FakeHttp().start()
    creality = await FakeWebsocket("creality").start()
    sdcp = await FakeWebsocket("sdcp").start()
    target = await FakeWebsocket("creality").start()
    redirect = await FakeWebsocket("redirect", f"http://127.0.0.1:{target.port}/").start()
    mqtt = await FakeMqtt(refuse=refuse_mqtt, short_publish=short).start()
    photon = await FakeTcpText().start()
    mdns = await FakeUdp(mdns_reply).start()
    ssdp = await FakeUdp(lambda data: SSDP_REPLY).start()
    chitu = await FakeUdp(lambda data: SDCP_DISCOVERY_REPLY if data == b"M99999" else None).start()
    bambu_port = free_port(socket.SOCK_DGRAM)
    closed = free_port()
    port_list = (
        ports.Port(http.port, ports.MOONRAKER, "Moonraker", ports.MOONRAKER_PATHS),
        ports.Port(http.port, ports.HTTP, "Anycubic /info", ("/", "/info")),
        ports.Port(creality.port, ports.CREALITY_WS, "Creality"),
        ports.Port(redirect.port, ports.CREALITY_WS, "a websocket that redirects"),
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
            "127.0.0.1",
            WINDOW_S,
            port_list=port_list,
            network=network,
            extra_ports=extra,
            photon=photon_flag,
        )
        await announcing
    finally:
        for fake in (http, creality, sdcp, target, redirect, mqtt, photon, mdns, ssdp, chitu):
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
        "target": target,
        "redirect": redirect,
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
        answers = {e["request"]: e for e in events(trace, f"http:{fakes['http'].port}")}
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
        assert "serial numbers, MAC addresses, Wi-Fi, host and file names are hidden" in summary

    def test_redactor_without_ips(self):
        redactor = Redactor(ips=False)
        redactor.learn({"access_code": "12345678"})
        assert redactor.text("code 12345678 at 192.168.1.2") == f"code {REDACTED} at 192.168.1.2"


# -- the command -------------------------------------------------------------------------------


class TestCommand:
    @pytest.fixture
    def fake_run(self, monkeypatch):
        """Replaces the probe's network work with one recorded answer from 192.168.1.50."""
        state = {"interrupt": False}

        async def run(self):
            self.address = "192.168.1.50"
            self.port_rows = [{"port": 80, "kind": "http", "suspected": "web UI", "open": True}]
            self.rec.send("http-get", "http:80", "GET /info", request="/info")
            self.rec.receive(
                "http:80",
                request="/info",
                status=200,
                reason="OK",
                headers={},
                body={"size": 30, "text": json.dumps({"sn": SERIAL})},
            )
            if state["interrupt"]:
                raise KeyboardInterrupt

        monkeypatch.setattr(probe_run.Probe, "run", run)
        return state

    def test_writes_the_trace_and_prints_the_summary(self, tmp_path, fake_run, capsys):
        out = tmp_path / "trace.json"
        assert main(["probe", "192.168.1.50", "--seconds", "5", "--out", str(out), "--redact"]) == 0
        text = out.read_text(encoding="utf-8")
        written = json.loads(text)
        assert written["probe"]["redacted"] is True
        assert SERIAL not in text and "192.168.1.50" not in text
        printed = capsys.readouterr().out
        assert "redacted" in printed and str(out) in printed and SERIAL not in printed
        assert "tcp-connect" in printed  # the allow-list, before the run

    def test_says_when_the_file_is_not_redacted(self, tmp_path, fake_run, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert main(["probe", "10.1.2.3", "--seconds", "5"]) == 0
        files = list(tmp_path.glob("probe-10.1.2.3-*.json"))
        assert len(files) == 1
        assert "NOT redacted" in capsys.readouterr().out

    def test_ctrl_c_writes_what_was_recorded(self, tmp_path, fake_run, capsys):
        fake_run["interrupt"] = True
        out = tmp_path / "trace.json"
        assert main(["probe", "192.168.1.50", "--seconds", "5", "--out", str(out)]) == 130
        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["probe"]["interrupted"] is True
        assert any("Ctrl+C" in note for note in written["notes"])
        assert any(e.get("request") == "/info" for e in written["events"])
        assert "Stopped" in capsys.readouterr().out

    def test_rejects_a_window_out_of_range(self, capsys):
        assert main(["probe", "127.0.0.1", "--seconds", "1"]) == 2

    def test_an_unknown_host(self, capsys):
        assert main(["probe", "no-such-host.invalid", "--seconds", "5"]) == 2
        assert "can't resolve" in capsys.readouterr().err

    def test_a_resolver_that_never_answers(self, monkeypatch):
        async def hang(host):
            await asyncio.sleep(30)

        monkeypatch.setattr(probe_run, "_getaddrinfo", hang)
        monkeypatch.setattr(probe_run, "RESOLVE_TIMEOUT_S", 0.1)
        with pytest.raises(probe_run.ProbeError, match="no answer"):
            run(probe_run.resolve("printer.example"))


# -- what the adversarial review found ---------------------------------------------------------


class TestReviewFindings:
    def test_redact_leaves_none_of_the_reviewers_identifiers(self):
        out = redact_trace(json.loads(json.dumps(REVIEW_TRACE)), "192.168.1.50")
        text = json.dumps(out)
        for leak in REVIEW_LEAKS:
            assert leak not in text, leak
        for placeholder in ("<serial-", "<ssid-", "<host-", "<file-", "<mac-", "<ip-1>"):
            assert placeholder in text, placeholder
        assert "Photon Mono X 6K" in text and "V0.2.2" in text  # model and firmware stay
        assert "/status" in text and "anycubicCloud" in text  # the topic's shape stays

    def test_the_same_mac_in_every_form_is_one_placeholder(self):
        out = redact_trace({"a": "a4c1381f22e0", "b": "A4:C1:38:1F:22:E0", "c": "A4C1.381F.22E0"})
        assert out == {"a": "<mac-1>", "b": "<mac-1>", "c": "<mac-1>"}

    @pytest.mark.parametrize(
        "key",
        [
            "deviceSn",
            "printerSN",
            "wifiSsid",
            "wifi_name",
            "hostName",
            "host_name",
            "mac_address",
            "MainboardID",
            "cpu_serial",
        ],
    )
    def test_camel_case_and_suffixed_names(self, key):
        out = redact_trace({key: "Value-12345"})
        assert out[key] != "Value-12345"

    @pytest.mark.parametrize(
        "key", ["filename", "printFileName", "file_path", "path", "currentFile"]
    )
    def test_file_names(self, key):
        out = redact_trace({key: "/gcodes/Jane Doe gift.gcode", "other": "see Jane Doe gift.gcode"})
        assert "Jane" not in json.dumps(out)
        assert out[key].startswith("<file-")

    def test_sliced_files_anywhere(self):
        out = redact_trace({"m": "printing a.3mf then b.ctb, c.pwmx, d.goo; e.g. nothing"})
        assert out["m"] == "printing <file-1> then <file-2>, <file-3>, <file-4>; e.g. nothing"

    def test_a_password_in_a_url(self):
        out = redact_trace({"cam": "rtsp://admin:hunter22@printer.local/live"})
        assert out["cam"] == f"rtsp://{REDACTED}@printer.local/live"

    def test_the_summary_shows_no_raw_records_or_photon_answers(self):
        trace = json.loads(json.dumps(REVIEW_TRACE))
        trace["probe"].update(version="x", seconds=5, mode="m")
        trace["ports"] = [{"port": 6000, "kind": "photon", "suspected": "Photon", "open": True}]
        for event in trace["events"]:
            event["kind"] = "receive"
        trace["events"][0]["query"] = "sysinfo"
        trace.update(notes=[], sentCount=0)
        summary = probe_run.summarize(trace, None)
        for leak in REVIEW_LEAKS[:-2]:
            assert leak not in summary, leak
        assert "model Photon Mono X 6K, firmware V0.2.2" in summary
        assert "_http._tcp.local" in summary
        assert "Wi-Fi, host and file names are hidden" in summary

    def test_a_websocket_redirect_is_not_followed(self, probed):
        trace, fakes = probed
        assert fakes["redirect"].http == ["GET /"]
        assert fakes["target"].protocols == [] and fakes["target"].received == []
        channel = f"ws:{fakes['redirect'].port}/"
        refused = [e for e in trace["events"] if e["channel"] == channel and e.get("refused")]
        assert refused and refused[0]["status"] == 302

    def test_bad_service_names_from_the_printer_are_not_asked_for(self):
        trace, fakes = run(probe_fakes(mdns_reply=mdns_bad_names))
        for query in fakes["mdns"].received:
            check("udp-mdns-query", query)
        assert any(b"\x04_s39" in q for q in fakes["mdns"].received)  # 40 names, two queries
        assert not trace.get("refused")
        assert sum("not asked for" in n for n in trace["notes"]) == 2
        assert fakes["creality"].received  # the rest of the probe ran

    def test_a_refused_send_ends_only_its_task(self, monkeypatch):
        def refuse(payload):
            raise NotAllowed("refused for the test")

        monkeypatch.setitem(allowlist._CHECKS, "creality-ws", refuse)
        trace, fakes = run(probe_fakes())
        assert fakes["creality"].received == []
        assert {r["rule"] for r in trace["refused"]} == {"creality-ws"}
        assert fakes["sdcp"].received and fakes["mqtt"].packets  # everything else still ran
        assert any(
            e["kind"] == "error" and "allow-list refused" in e["error"] for e in trace["events"]
        )

    def test_port_6000_gets_nothing_without_photon(self):
        trace, fakes = run(probe_fakes(photon_flag=False))
        assert fakes["photon"].received == []
        assert any("--photon" in n for n in trace["notes"])

    def test_a_short_mqtt_publish(self):
        trace, fakes = run(probe_fakes(short=True))
        received = events(trace, f"mqtt:{fakes['mqtt'].port}")
        assert any("malformed" in e for e in received)
        assert any("topic" in e for e in received)  # and it went on reading
