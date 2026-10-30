"""A fake FlashForge Adventurer 5M / AD5X for the tests: the LAN HTTP API on port 8898 and the
UDP discovery answer.

Built from community sources, not a recorded printer (#88): GhostTypes/ff-5mp-api-py (MIT)
`flashforge/api/constants/endpoints.py`, `models/machine_info.py` and `discovery/discovery.py`,
and the idle AD5X answer in the facts of ff-5mp-api-ts's `MachineInfo.test.ts`. Each request is
a `POST` with `{"serialNumber", "checkCode"}`; each answer is `{"code": 0, "message": "Success",
...}`. A wrong serial or check code answers another `code` (ASSUMPTION: the code itself isn't
documented; 1 here). `printProgress` is 0 to 1, `printDuration` and `estimatedTime` seconds,
`estimatedRightWeight` the job's grams and `estimatedRightLen` its millimeters.

ASSUMPTION throughout: none of it has been checked against a real printer yet.
"""

from __future__ import annotations

import base64
import copy
import socket
import struct
import threading
from typing import Any

from aiohttp import web

SERIAL = "SNMOMC9900728"
CHECK_CODE = "8f3a2c71"
FIRMWARE = "2.7.5"
NAME = "Workshop 5M"
PNG = b"\x89PNG\r\n\x1a\n" + b"flashforge-thumbnail" * 50


def idle_detail(model: str = "5M") -> dict[str, Any]:
    """`/detail`'s `detail` of an idle printer: an Adventurer 5M, or an AD5X with its IFS."""
    detail: dict[str, Any] = {
        "autoShutdown": "close",
        "cameraStreamUrl": "http://192.168.0.204:8080/?action=stream",
        "cumulativeFilament": 120.5,
        "cumulativePrintTime": 4200,
        "errorCode": "",
        "estimatedLeftLen": 0,
        "estimatedLeftWeight": 0.0,
        "estimatedRightLen": 0,
        "estimatedRightWeight": 0.0,
        "estimatedTime": 0.0,
        "firmwareVersion": FIRMWARE,
        "flashRegisterCode": "REG-FLASH-1234",
        "ipAddr": "192.168.0.204",
        "lightStatus": "open",
        "location": "Group A",
        "macAddr": "88:A9:A7:9D:2A:70",
        "measure": "220X220X220",
        "name": NAME,
        "nozzleModel": "0.4mm",
        "pid": 35,
        "platTemp": 27.75,
        "polarRegisterCode": "REG-POLAR-5678",
        "printDuration": 0,
        "printFileName": "",
        "printLayer": 0,
        "printProgress": 0.0,
        "rightTemp": 25.0,
        "status": "ready",
        "targetPrintLayer": 0,
    }
    if model == "AD5X":
        detail.update(
            pid=38,
            firmwareVersion="1.1.3-1.0.8",
            hasMatlStation=True,
            matlStationInfo={
                "currentLoadSlot": 0,
                "currentSlot": 0,
                "slotCnt": 4,
                "stateAction": 0,
                "stateStep": 0,
                "slotInfos": [
                    {
                        "hasFilament": True,
                        "materialColor": "#FFFFFF",
                        "materialName": "PLA",
                        "slotId": 1,
                    },
                    {
                        "hasFilament": True,
                        "materialColor": "#1A1A1A",
                        "materialName": "PETG",
                        "slotId": 2,
                    },
                    {"hasFilament": False, "materialColor": "", "materialName": "?", "slotId": 3},
                    {
                        "hasFilament": True,
                        "materialColor": "#FF0000",
                        "materialName": "PLA",
                        "slotId": 4,
                    },
                ],
            },  # fmt: skip
            indepMatlInfo={
                "materialColor": "",
                "materialName": "?",
                "stateAction": 0,
                "stateStep": 0,
            },  # fmt: skip
        )
    return detail


class FakeFlashForge:
    def __init__(self, model: str = "5M") -> None:
        self.detail = idle_detail(model)
        self.model = model
        self.requests: list[tuple[str, str]] = []
        """(method, path) of every request."""
        self.bodies: list[dict[str, Any]] = []
        self.files: list[str] = []
        """`/gcodeList`'s names, newest first."""
        self.tools: dict[str, list[dict[str, Any]]] = {}
        """AD5X: `gcodeToolDatas` per file."""
        self.thumbnails: dict[str, bytes] = {}
        self.answer = True
        """False: answer 404 to everything (firmware without the HTTP API)."""
        self.host = "127.0.0.1"
        self.port = 0
        self._runner: web.AppRunner | None = None

    async def start(self) -> FakeFlashForge:
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return self

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def paths(self, path: str) -> int:
        return sum(1 for _, p in self.requests if p == path)

    @property
    def methods(self) -> set[str]:
        return {m for m, _ in self.requests}

    # -- the printer ----------------------------------------------------------------------

    def print_file(
        self,
        name: str,
        *,
        grams: float = 12.5,
        mm: float = 4130.0,
        seconds: float = 3600.0,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        self.files.insert(0, name)
        self.thumbnails[name] = PNG
        if tools is not None:
            self.tools[name] = tools
        self.detail.update(
            status="printing",
            printFileName=name,
            printProgress=0.01,
            printDuration=10,
            estimatedTime=seconds,
            estimatedRightWeight=grams,
            estimatedRightLen=mm,
            printLayer=1,
            targetPrintLayer=200,
        )

    def progress(self, fraction: float, duration: float | None = None) -> None:
        self.detail["printProgress"] = fraction
        self.detail["printLayer"] = int(200 * fraction)
        if duration is not None:
            self.detail["printDuration"] = duration

    def set_state(self, state: str) -> None:
        self.detail["status"] = state

    def complete(self) -> None:
        self.detail.update(status="completed", printProgress=1.0)

    def cancel(self) -> None:
        self.detail["status"] = "cancel"

    def fail(self, code: str = "E0015") -> None:
        self.detail.update(status="error", errorCode=code)

    def idle(self) -> None:
        self.detail.update(status="ready", printFileName="", printProgress=0.0, printDuration=0)

    # -- HTTP -----------------------------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.Response:
        path = "/" + request.match_info["path"]
        self.requests.append((request.method, path))
        if not self.answer:
            return web.Response(status=404, text="Not Found")
        if request.method != "POST":
            return web.Response(status=405)
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"code": 2, "message": "Bad request"})
        self.bodies.append(body)
        if body.get("serialNumber") != SERIAL or body.get("checkCode") != CHECK_CODE:
            return web.json_response({"code": 1, "message": "Error"})
        ok = {"code": 0, "message": "Success"}
        if path == "/detail":
            return web.json_response({**ok, "detail": copy.deepcopy(self.detail)})
        if path == "/product":
            return web.json_response(
                {
                    **ok,
                    "product": {
                        "chamberTempCtrlState": 0,
                        "externalFanCtrlState": 1,
                        "internalFanCtrlState": 1,
                        "lightCtrlState": 1,
                        "nozzleTempCtrlState": 1,
                        "platformTempCtrlState": 1,
                    },
                }
            )
        if path == "/gcodeList":
            answer: dict[str, Any] = {**ok, "gcodeList": self.files[:10]}
            if self.model == "AD5X":
                answer["gcodeListDetail"] = [
                    {
                        "gcodeFileName": name,
                        "printingTime": 3600,
                        "totalFilamentWeight": sum(
                            t["filamentWeight"] for t in self.tools.get(name, [])
                        ),
                        "useMatlStation": bool(self.tools.get(name)),
                        "gcodeToolCnt": len(self.tools.get(name, [])),
                        "gcodeToolDatas": self.tools.get(name, []),
                    }
                    for name in self.files[:10]
                ]
            return web.json_response(answer)
        if path == "/gcodeThumb":
            image = self.thumbnails.get(body.get("fileName", ""))
            if image is None:
                return web.json_response({"code": 1, "message": "File not found"})
            return web.json_response({**ok, "imageData": base64.b64encode(image).decode()})
        return web.Response(status=404, text="Not Found")


def discovery_reply(
    name: str = NAME, serial: str = SERIAL, product: int = 0x23, http_port: int = 8898
) -> bytes:
    """A 5M-series answer to discovery (276 bytes), as ff-5mp-api-py reads one."""
    data = bytearray(276)
    encoded = name.encode()[:0x83]
    data[: len(encoded)] = encoded
    struct.pack_into(">HHHHHH", data, 0x84, 8899, 0x2B71, product, 0, 0x5A02, http_port)
    encoded = serial.encode()[:127]
    data[0x92 : 0x92 + len(encoded)] = encoded
    return bytes(data)


def legacy_reply(name: str = "Adventurer 3") -> bytes:
    """An Adventurer 3's answer: 140 bytes, no serial."""
    data = bytearray(140)
    data[: len(name)] = name.encode()
    struct.pack_into(">HHHH", data, 0x84, 8899, 0x2B71, 0x0008, 0)
    return bytes(data)


class FakeDiscovery:
    """A printer answering any datagram on a UDP port, from 127.0.0.1. A blocking socket in a
    thread, since Windows' asyncio drops the empty datagram the bridge sends (a printer's Linux
    doesn't)."""

    def __init__(self, reply: bytes | None = None) -> None:
        self.reply = reply if reply is not None else discovery_reply()
        self.asked: list[bytes] = []
        self.repeat = 1
        self.port = 0
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    async def start(self) -> FakeDiscovery:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.05)
        self._sock, self.port = sock, sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            self.asked.append(data)
            for _ in range(self.repeat):
                self._sock.sendto(self.reply, addr)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
        if self._sock is not None:
            self._sock.close()
