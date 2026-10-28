"""Stand-ins for the devices `printlog-bridge probe` talks to, each recording exactly what it
received, so the tests can hold it to the allow-list.

- `FakeHttp`: an HTTP server answering like Moonraker (and `/info` like an Anycubic printer).
- `FakeWebsocket`: Creality's stock websocket (a heart_beat and a status push on connect) or
  SDCP v3's at /websocket, on aiohttp.
- `FakeMqtt`: an MQTT 3.1.1 broker that takes anonymous clients (or refuses them with a code),
  and publishes a report after a SUBSCRIBE.
- `FakeUdp`: a UDP responder (mDNS, SSDP, Chitu/SDCP discovery) answering with a fixed reply.
- `FakeTcpText`: Anycubic Photon's TCP 6000, answering `sysinfo` and `getstatus`.

None of these is a recorded printer: their answers follow the community sources the probe's
allow-list cites (#17 has the real traces once Chris records them).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import struct
from typing import Any, Callable

from aiohttp import WSMsgType, web

from printlog_bridge.core.mdns import encode_name

SERIAL = "CRE1A2B3C4D5E6"
MAC = "a4:5e:60:12:34:56"
TOKEN = "s3cr3t-T0ken-value"
MAINBOARD = "f00dfacecafe0123"


def free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _serve(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]


class FakeHttp:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.port = 0
        self._runner: web.AppRunner | None = None

    async def start(self) -> FakeHttp:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner, self.port = await _serve(app)
        return self

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(f"{request.method} {request.raw_path}")
        path = request.path
        if path == "/server/info":
            return web.json_response(
                {"result": {"klippy_state": "ready", "moonraker_version": "v0.9.3"}}
            )
        if path == "/machine/system_info":
            return web.json_response(
                {
                    "result": {
                        "system_info": {
                            "cpu_info": {"serial_number": SERIAL},
                            "network": {"wlan0": {"mac_address": MAC}},
                        }
                    }
                }
            )
        if path == "/info":
            return web.json_response(
                {
                    "modelName": "Anycubic Kobra S1",
                    "cn": SERIAL,
                    "token": TOKEN,
                    "ctrlInfoUrl": "http://127.0.0.1:18910/ctrl",
                }
            )
        if path == "/":
            return web.Response(
                text="<html><title>Creality Hi</title></html>",
                content_type="text/html",
                headers={"Server": "nginx"},
            )
        if path.startswith("/printer/") or path.startswith("/server/"):
            return web.json_response({"result": {}})
        return web.Response(status=404)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


class FakeWebsocket:
    """`kind` is "creality" (path /, pushes a heart_beat and a status frame), "sdcp" (path
    /websocket, answers each request with a status message, and "pong" to "ping"), or
    "redirect" (path /, answers the handshake with a 302 to `location`)."""

    def __init__(self, kind: str, location: str = "") -> None:
        self.kind = kind
        self.location = location
        self.received: list[str] = []
        self.http: list[str] = []
        self.protocols: list[str] = []
        self.port = 0
        self._runner: web.AppRunner | None = None

    async def start(self) -> FakeWebsocket:
        app = web.Application()
        if self.kind == "redirect":
            app.router.add_get("/", self._redirect)
            self._runner, self.port = await _serve(app)
            return self
        path = "/" if self.kind == "creality" else "/websocket"
        app.router.add_get(path, self._ws)
        if self.kind == "sdcp":
            app.router.add_get("/", self._page)
        self._runner, self.port = await _serve(app)
        return self

    async def _redirect(self, request: web.Request) -> web.Response:
        self.http.append(f"{request.method} {request.raw_path}")
        raise web.HTTPFound(self.location)

    async def _page(self, request: web.Request) -> web.Response:
        self.http.append(f"{request.method} {request.raw_path}")
        return web.Response(text="<html><title>Elegoo</title></html>", content_type="text/html")

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(protocols=("wsslicer",) if self.kind == "creality" else ())
        await ws.prepare(request)
        self.protocols.append(ws.ws_protocol or "")
        if self.kind == "creality":
            await ws.send_str(json.dumps({"ModeCode": "heart_beat", "msg": "2026-10-27"}))
            await ws.send_str(
                json.dumps(
                    {
                        "nozzleTemp": "210.5",
                        "printProgress": 42,
                        "model": "Hi",
                        "hostname": "Creality-Hi",
                        "printFileName": "/usr/data/printer_data/benchy.gcode",
                    }
                )
            )
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            self.received.append(message.data)
            if self.kind == "sdcp":
                if message.data == "ping":
                    await ws.send_str("pong")
                else:
                    request_id = json.loads(message.data)["Data"]["RequestID"]
                    await ws.send_str(
                        json.dumps(
                            {
                                "Id": "brand",
                                "Topic": f"sdcp/response/{MAINBOARD}",
                                "Data": {
                                    "RequestID": request_id,
                                    "MainboardID": MAINBOARD,
                                    "Data": {"Ack": 0},
                                },
                            }
                        )
                    )
            elif message.data.startswith("{"):
                await ws.send_str(json.dumps({"reply": json.loads(message.data)["params"]}))
        return ws

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


def _mqtt_packet(first: int, body: bytes) -> bytes:
    out = bytearray([first])
    length = len(body)
    while True:
        byte, length = length % 128, length // 128
        out.append(byte | (0x80 if length else 0))
        if not length:
            break
    return bytes(out) + body


class FakeMqtt:
    """`short_publish`: after the SUBSCRIBE, also send a PUBLISH too short to hold a topic."""

    def __init__(self, refuse: int = 0, short_publish: bool = False) -> None:
        self.refuse = refuse
        self.short_publish = short_publish
        self.packets: list[bytes] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> FakeMqtt:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                first = (await reader.readexactly(1))[0]
                length, shift, header = 0, 0, bytes([first])
                while True:
                    byte = (await reader.readexactly(1))[0]
                    header += bytes([byte])
                    length |= (byte & 0x7F) << shift
                    shift += 7
                    if not byte & 0x80:
                        break
                body = await reader.readexactly(length)
                self.packets.append(header + body)
                kind = first >> 4
                if kind == 1:
                    writer.write(bytes([0x20, 2, 0, self.refuse]))
                    if self.refuse:
                        await writer.drain()
                        break
                elif kind == 8:
                    writer.write(_mqtt_packet(0x90, body[:2] + b"\x00"))
                    topic = f"device/{SERIAL}/report".encode()
                    report = json.dumps({"print": {"gcode_state": "RUNNING", "sn": SERIAL}})
                    if self.short_publish:
                        writer.write(_mqtt_packet(0x30, b"\x00"))
                    writer.write(
                        _mqtt_packet(0x30, struct.pack(">H", len(topic)) + topic + report.encode())
                    )
                elif kind == 12:
                    writer.write(bytes([0xD0, 0]))
                elif kind == 14:
                    break
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


class FakeUdp:
    """Answers each datagram with `reply(data)` (None: no answer), from 127.0.0.1."""

    def __init__(self, reply: Callable[[bytes], Any]) -> None:
        self.reply = reply
        self.received: list[bytes] = []
        self.port = 0
        self._transport: Any = None

    async def start(self) -> FakeUdp:
        fake = self

        class Protocol(asyncio.DatagramProtocol):
            def connection_made(self, transport) -> None:
                fake._transport = transport

            def datagram_received(self, data: bytes, addr) -> None:
                fake.received.append(data)
                answer = fake.reply(data)
                if answer:
                    fake._transport.sendto(answer, addr)

            def error_received(self, exc) -> None:
                pass

        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(Protocol, local_addr=("127.0.0.1", 0))
        self.port = self._transport.get_extra_info("sockname")[1]
        return self

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()


class FakeTcpText:
    """Anycubic Photon's TCP 6000: one query per connection, answered with `,end`."""

    def __init__(self) -> None:
        self.received: list[str] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> FakeTcpText:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def _client(self, reader, writer) -> None:
        with contextlib.suppress(ConnectionError, asyncio.TimeoutError):
            data = await asyncio.wait_for(reader.read(100), 3)
            text = data.decode()
            if text:  # an empty read is the probe's connect check
                self.received.append(text)
            if text == "sysinfo":
                writer.write(f"sysinfo,Photon Mono X,V0.2.2,{SERIAL},HomeWifi,end".encode())
            elif text == "getstatus":
                writer.write(b"getstatus,stop,end")
            await writer.drain()
        writer.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


# -- mDNS answers -------------------------------------------------------------------------------


def _record(name: str, kind: int, rdata: bytes, ttl: int = 120) -> bytes:
    return encode_name(name) + struct.pack("!HHIH", kind, 1, ttl, len(rdata)) + rdata


def mdns_answer(query: bytes) -> bytes | None:
    """Answers a query for `_services._dns-sd._udp.local` with `_creality._tcp.local`, and one
    for `_http._tcp.local` with an instance, its SRV, TXT (with a serial and MAC) and A."""
    records = []
    if b"\x09_services" in query:
        records.append(
            _record("_services._dns-sd._udp.local", 12, encode_name("_creality._tcp.local"))
        )
    if b"\x05_http" in query:
        instance = "Creality Hi._http._tcp.local"
        txt = b"".join(
            bytes([len(t)]) + t
            for t in (f"sn={SERIAL}".encode(), f"mac={MAC}".encode(), b"model=Hi")
        )
        records += [
            _record("_http._tcp.local", 12, encode_name(instance)),
            _record(instance, 33, struct.pack("!HHH", 0, 0, 80) + encode_name("creality.local")),
            _record(instance, 16, txt),
            _record("creality.local", 1, socket.inet_aton("127.0.0.1")),
        ]
    if not records:
        return None
    return struct.pack("!HHHHHH", 0, 0x8400, 0, len(records), 0, 0) + b"".join(records)


SSDP_REPLY = (
    "HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\nUSN: uuid:" + SERIAL + "::upnp:rootdevice\r\n"
    "SERVER: Linux UPnP/1.0 Creality/1.0\r\nLOCATION: http://127.0.0.1:80/desc.xml\r\n\r\n"
).encode()

SDCP_DISCOVERY_REPLY = json.dumps(
    {
        "Id": "979d4C788A4a78bC777A870F1A02867A",
        "Data": {
            "Name": "Saturn",
            "MachineName": "Saturn 4 Ultra",
            "BrandName": "ELEGOO",
            "MainboardIP": "127.0.0.1",
            "MainboardID": MAINBOARD,
            "ProtocolVersion": "V3.0.0",
            "FirmwareVersion": "V1.2.3",
        },
    }
).encode()

BAMBU_NOTIFY = (
    b"NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nServer: UPnP/1.0\r\nLocation: "
    b"127.0.0.1\r\nNT: urn:bambulab-com:device:3dprinter:1\r\nUSN: 01S00C123456789\r\n"
    b"DevModel.bambu.com: C12\r\nDevName.bambu.com: P1S\r\n\r\n"
)


def mdns_bad_names(query: bytes) -> bytes | None:
    """Lists service types a probe must not ask for (not `.local`, a label over 63 bytes) and
    more good ones than fit in one query."""
    if b"\x09_services" not in query:
        return None
    names = ["_foo._tcp", "_" + "a" * 70 + "._tcp.local"] + [f"_s{i}._tcp.local" for i in range(40)]
    records = [_record("_services._dns-sd._udp.local", 12, _name_bytes(n)) for n in names]
    return struct.pack("!HHHHHH", 0, 0x8400, 0, len(records), 0, 0) + b"".join(records)


def _name_bytes(name: str) -> bytes:
    """A DNS name, without encode_name's checks (a label may be too long, as a broken or
    hostile printer could send: 64 and up are still read as a length)."""
    out = b""
    for label in name.strip(".").split("."):
        raw = label.encode()
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


# -- what the adversarial review of #116 fed redact_trace -------------------------------------

REVIEW_SERIAL = "00001A9F00030034"
REVIEW_SSID = "MyHomeWiFi"
REVIEW_TRACE = {
    "probe": {"host": "creality-hi-a1b2c3.local", "address": "192.168.1.50"},
    "allowList": [],
    "events": [
        {
            "channel": "tcp:6000",
            "message": {
                "text": f"sysinfo,Photon Mono X 6K,V0.2.2,{REVIEW_SERIAL},{REVIEW_SSID},end"
            },
        },
        {
            "channel": "ws:9999/",
            "message": {
                "text": json.dumps(
                    {
                        "deviceSn": "F123456789ABCD",
                        "printerSN": "Z99887766",
                        "hostname": "K1C-7F3A",
                        "printFileName": "/usr/data/printer_data/gcodes/Jane_Doe_gift_box.gcode",
                        "wifiSsid": REVIEW_SSID,
                        "webrtcUrl": "rtsp://admin:hunter22@192.168.1.50/live",
                    }
                )
            },
        },
        {
            "channel": "mqtt:1883",
            "topic": "anycubic/anycubicCloud/v1/printer/public/20025/"
            "0a1b2c3d4e5f60718293a4b5c6d7e8f9/status",
            "message": {"text": "{}"},
        },
        {
            "channel": "udp:mdns",
            "records": [
                {
                    "name": "Creality-K1-a4c1381f22e0._http._tcp.local",
                    "type": "SRV",
                    "data": {"target": "K1-a4c1381f22e0.local"},
                }
            ],
        },
        {"channel": "x", "message": {"text": "mac a4c1381f22e0 / bssid A4C1.381F.22E0"}},
    ],
}
REVIEW_LEAKS = (
    REVIEW_SERIAL,
    REVIEW_SSID,
    "F123456789ABCD",
    "Z99887766",
    "K1C-7F3A",
    "Jane_Doe",
    "hunter22",
    "admin:",
    "0a1b2c3d4e5f60718293a4b5c6d7e8f9",
    "a4c1381f22e0",
    "A4C1.381F.22E0",
    "creality-hi-a1b2c3",
    "192.168.1.50",
)
