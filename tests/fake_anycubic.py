"""A stand-in for an Anycubic printer in LAN mode: its HTTP handshake (`/info`, `/ctrl`) and its
MQTT broker over TLS, built from what a real Kobra S1 and Photon P1 Max answered
(`tests/fixtures/anycubic/`, recorded with `tests/integration/record_anycubic.py`).

What comes from the recordings: `/info`'s fields, `/ctrl`'s answer and what it opens to (with
fake values in place of the redacted ones), the topics, the `{"msgid"}` acknowledgement on
`.../response`, and the Kobra S1's idle answers to `info`, `multiColorBox` and `peripherie`.
What doesn't (no print has been recorded, and the P1 Max's broker was closed): the job block
(`project`, a `print` report) and the resin answers, from the sources `adapters/anycubic/jobs.py`
names. Replace them with a recorded print when there is one.

The broker checks the login `/ctrl` handed out (only the latest handshake's works) and, for a
resin printer, the client id. It records every request, and answers each read-only query as the
printer does; any other request is recorded in `refused` and gets no answer.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
import secrets
import ssl
import struct
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from aiohttp import web
from fake_bambu import _parse_connect, _publish, _read, certificate, server_context

from printlog_bridge.adapters.anycubic import aes
from printlog_bridge.adapters.anycubic.handshake import signature
from printlog_bridge.adapters.anycubic.protocol import READ_ONLY

FIXTURES = Path(__file__).parent / "fixtures" / "anycubic"
SERIAL = "8BC5-0000-1111-2222"
DEVICE_ID = "0123456789abcdef0123456789abcdef"
FIRMWARE = "2.5.6.4"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def recorded(name: str, kind: str, **match: Any) -> dict[str, Any]:
    """The first recorded event of `kind` (`http`, `opened`, `receive`) matching `match`."""
    for event in fixture(name):
        if event["kind"] != kind:
            continue
        if all(_get(event, k) == v for k, v in match.items()):
            return copy.deepcopy(event)
    raise KeyError(f"no {kind} {match} in {name}")


def _get(event: Mapping[str, Any], dotted: str) -> Any:
    value: Any = event
    for part in dotted.split("."):
        value = value.get(part) if isinstance(value, Mapping) else None
    return value


S1 = "kobra_s1_idle.json"
P1 = "photon_p1_max_handshake.json"


def job_block(name: str = "Benchy_PLA_0.2.gcode", **fields: Any) -> dict[str, Any]:
    """An FDM job block (`info.data.project`), as anycubic-lan's PROTOCOL.md captured one."""
    block: dict[str, Any] = {
        "remain_time": 42,
        "curr_layer": 3,
        "total_layers": 150,
        "supplies_usage": 120,
        "print_time": 7,
        "progress": 2,
        "state": "printing",
        "print_status": 1,
        "filename": f".3mf_temp/{name}",
        "pause": 0,
        "project_type": 1,
        "task_id": 614707220,
        "localtask": "b92a60d8-44d0-4b2a-916f-e5f476a07143",
        "task_settings": {"camera_timelapse": 0},
        "print_speed_mode": None,
    }
    block.update(fields)
    return block


def resin_block(name: str = "model.pwmb", **fields: Any) -> dict[str, Any]:
    """A resin `print` report's `data`, as hass-anycubic-photon-p1's protocol.md shows one."""
    block: dict[str, Any] = {
        "taskid": "0",
        "filename": name,
        "remain_time": 68,
        "model_hight": 14.6,
        "curr_layer": 0,
        "total_layers": 292,
        "supplies_usage": 19.25,
        "progress": 0,
        "z_thick": 0.05,
        "print_time": 0,
        "slicer": "ANYCUBIC-PC",
    }
    block.update(fields)
    return block


class FakeAnycubic:
    def __init__(self, cert: Path, key: Path, *, resin: bool = False) -> None:
        self.resin = resin
        self.context = server_context(cert, key)
        source = P1 if resin else S1
        info = recorded(source, "http", request="GET /info")["answer"]
        info.update({"cn": SERIAL, "ip": "127.0.0.1", "ctrlInfoUrl": "http://127.0.0.1/ctrl"})
        info.pop("usn", None)
        self.info = info
        self.opened = recorded(source, "opened")["value"]
        self.model_id = str(info["modelId"])
        self.device_id = DEVICE_ID
        self.ctrl_type = info["ctrlType"]
        self.project: dict[str, Any] | None = None
        """The job: FDM `info.data.project`, or the resin `print` report's `data`."""
        self.job_state = "printing"
        """The resin `print` report's envelope `state`."""
        self.last_project: dict[str, Any] | None = None
        self.login: tuple[str, str] | None = None
        self.client_id = "clientId" if resin else None
        self.refuse_mqtt = False
        self.broker_open = True
        self.handshakes = 0
        self.bad_signatures = 0
        self.connects: list[dict[str, Any]] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.refused: list[tuple[str, dict[str, Any]]] = []
        self.http_methods: list[tuple[str, str]] = []
        self.http_port = 0
        self.broker_port = 0
        self._token = ""
        self._runner: web.AppRunner | None = None
        self._server: asyncio.AbstractServer | None = None
        self._clients: list[tuple[asyncio.StreamWriter, set[str]]] = []

    # -- life -----------------------------------------------------------------------------

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self.context)
        self.broker_port = self._server.sockets[0].getsockname()[1]
        app = web.Application()
        app.router.add_get("/info", self._info)
        app.router.add_post("/ctrl", self._ctrl)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.http_port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return self.http_port

    async def close(self) -> None:
        self.drop()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._runner is not None:
            await self._runner.cleanup()

    async def close_broker(self) -> None:
        """Stops the broker, as the P1 Max's was (its port refused connections)."""
        self.drop()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def drop(self) -> None:
        for writer, _ in self._clients:
            writer.close()
        self._clients.clear()

    @property
    def subscribed(self) -> bool:
        return any(topics for _, topics in self._clients)

    def queries(self, kind: str) -> list[dict[str, Any]]:
        return [body for _, body in self.requests if body.get("type") == kind]

    # -- HTTP -----------------------------------------------------------------------------

    async def _info(self, request: web.Request) -> web.Response:
        self.http_methods.append(("GET", "/info"))
        self._token = secrets.token_hex(16)
        body = {**self.info, "ctrlType": self.ctrl_type, "token": self._token}
        return web.json_response(body)

    async def _ctrl(self, request: web.Request) -> web.Response:
        self.http_methods.append((request.method, "/ctrl"))
        query = {k: v[0] for k, v in parse_qs(request.query_string).items()}
        token = self._token
        expected = signature(token, query.get("ts", ""), query.get("nonce", "")) if token else ""
        if not token or query.get("sign") != expected or not query.get("did"):
            self.bad_signatures += 1
            return web.json_response({"code": 19002, "message": "failed"})
        self.handshakes += 1
        username, password = f"user{secrets.token_hex(3)}", secrets.token_hex(8)
        self.login = (username, password)
        opened = dict(self.opened)
        opened.update(
            {
                "broker": f"{'' if self.resin else 'mqtts://'}127.0.0.1:{self.broker_port}",
                "username": username,
                "password": password,
                "deviceId": self.device_id,
                "ip": "127.0.0.1",
            }
        )
        for key in ("devicecrt", "devicepk", "fileUploadUrl", "mac"):
            if key in opened:
                opened[key] = f"<fake {key}>"
        plain = json.dumps(opened).encode()
        pad = 16 - len(plain) % 16
        plain += bytes([pad]) * pad
        iv = secrets.token_hex(8)
        sealed = aes.encrypt_cbc(plain, token[16:32].encode(), iv.encode())
        self._token = ""  # one handshake per token
        return web.json_response(
            {
                "code": 200,
                "message": "success",
                "data": {"info": base64.b64encode(sealed).decode(), "token": iv},
            }
        )

    # -- the broker -----------------------------------------------------------------------

    @property
    def report_prefix(self) -> str:
        return f"anycubic/anycubicCloud/v1/printer/public/{self.model_id}/{self.device_id}"

    async def publish(self, kind: str, message: Mapping[str, Any]) -> None:
        topic = f"{self.report_prefix}/{kind}/report"
        await self._send_all(topic, json.dumps(message).encode())

    async def _send_all(self, topic: str, payload: bytes) -> None:
        packet = _publish(topic, payload)
        for writer, topics in list(self._clients):
            if any(t.endswith("#") and topic.startswith(t[:-1]) for t in topics):
                writer.write(packet)
                with contextlib.suppress(ConnectionError):
                    await writer.drain()

    async def push_info(self) -> None:
        await self.publish("info", self.info_report())

    async def push_print(self) -> None:
        await self.publish("print", self.print_report())

    async def push_status(self) -> None:
        await self.publish("status", self.status_report())

    def info_report(self) -> dict[str, Any]:
        report = recorded(S1, "receive", **{"payload.type": "info"})["payload"]
        data = report["data"]
        data["version"] = FIRMWARE
        data["state"] = "busy" if self.project else "free"
        data["project"] = copy.deepcopy(self.project)
        if self.last_project is not None:
            data["last_project"] = copy.deepcopy(self.last_project)
        return report

    def print_report(self) -> dict[str, Any]:
        envelope = {"type": "print", "action": "update", "msgid": secrets.token_hex(8),
                    "timestamp": 1, "code": 200, "msg": "done"}  # fmt: skip
        if self.resin:
            envelope["state"] = self.job_state
        else:
            envelope["state"] = "done"
        envelope["data"] = copy.deepcopy(self.project)
        return envelope

    def status_report(self) -> dict[str, Any]:
        busy = self.project is not None
        return {"type": "status", "action": "BUSY" if busy else "free",
                "state": "busy" if busy else "idle", "msgid": secrets.token_hex(8),
                "timestamp": 1, "code": 200, "data": None}  # fmt: skip

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        topics: set[str] = set()
        entry = (writer, topics)
        try:
            kind, body = await _read(reader)
            if kind != 1:
                return
            connect = _parse_connect(body)
            self.connects.append(connect)
            ok = (
                not self.refuse_mqtt
                and self.login is not None
                and (connect["username"], connect["password"]) == self.login
                and (self.client_id is None or connect["client_id"] == self.client_id)
            )
            writer.write(bytes([0x20, 2, 0, 0 if ok else 5]))
            await writer.drain()
            if not ok:
                return
            self._clients.append(entry)
            while True:
                kind, body = await _read(reader)
                if kind == 8:
                    packet_id = body[:2]
                    length = struct.unpack(">H", body[2:4])[0]
                    topics.add(body[4 : 4 + length].decode())
                    writer.write(bytes([0x90, 3]) + packet_id + bytes([0]))
                elif kind == 3:
                    length = struct.unpack(">H", body[:2])[0]
                    topic = body[2 : 2 + length].decode()
                    await self._request(topic, json.loads(body[2 + length :]))
                elif kind == 12:
                    writer.write(bytes([0xD0, 0]))
                elif kind == 14:
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError):
            pass
        finally:
            if entry in self._clients:
                self._clients.remove(entry)
            writer.close()

    async def _request(self, topic: str, body: dict[str, Any]) -> None:
        self.requests.append((topic, body))
        pair = (body.get("type"), body.get("action"))
        if pair not in READ_ONLY:
            self.refused.append((topic, body))
            return
        origin = "pc" if self.resin else "web"
        expected = f"anycubic/anycubicCloud/v1/{origin}/printer/{self.model_id}/{self.device_id}/"
        if not topic.startswith(expected):
            self.refused.append((topic, body))
            return
        await self._send_all(
            f"{self.report_prefix}/response", json.dumps({"msgid": body.get("msgid")}).encode()
        )
        kind = body["type"]
        if kind == "info" and not self.resin:
            await self.push_info()
        elif kind == "print" and self.project is not None:
            await self.push_print()
        elif kind == "multiColorBox":
            answer = recorded(S1, "receive", **{"payload.type": "multiColorBox"})["payload"]
            await self.publish(kind, answer)
        elif kind == "peripherie":
            answer = recorded(S1, "receive", **{"payload.type": "peripherie"})["payload"]
            await self.publish(kind, answer)
        elif kind == "status" and self.resin:
            await self.push_status()
        elif kind == "lanInfo" and self.resin:
            answer = {"type": "lanInfo", "action": "query", "state": "done", "code": 200,
                      "data": {"firmware_version": "1.0.3"}}  # fmt: skip
            await self.publish("status", answer)


__all__ = ["FakeAnycubic", "certificate", "job_block", "resin_block"]
