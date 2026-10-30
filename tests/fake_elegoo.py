"""A fake Elegoo Centauri Carbon for the tests: SDCP v3 over its websocket, and the `M99999`
discovery reply on UDP.

**Not checked against a real printer** (#94): built from the published SDCP v3.0.0 spec
(cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0, "Device Discovery", "Topic", "Heartbeat",
"Status Information", "Request for status refresh" and "attributes"), the Centauri Carbon's
own print states (elegooofficial/CentauriCarbon, `print_stats_c.h`; elegooofficial/elegoo-link,
`elegoo_fdm_cc_message_adapter.cpp`) and what community write-ups recorded from one
(WalkerFrederick/sdcp-centauri-carbon, firmware 1.1.29). Replace it with a recorded trace
(`trace = true`) when there is one.

- `ws://<host>:3030/websocket`, no authentication. A client's text `ping` is answered `pong`.
  Silence from a client for `idle_timeout` seconds closes its socket (reported as 60 s).
- A request is `{"Id", "Data": {"Cmd", "Data", "RequestID", "MainboardID", "TimeStamp",
  "From"}, "Topic": "sdcp/request/<MainboardID>"}`. Cmd 0 is answered with an `Ack` on
  `sdcp/response/<id>` and the status pushed again; Cmd 1 likewise with the attributes. Any
  other command is recorded in `refused` and never acted on: the bridge must only read.
- Pushes: `{"Status": {...}, "MainboardID", "TimeStamp", "Topic": "sdcp/status/<id>"}` and
  `{"Attributes": {...}, ..., "Topic": "sdcp/attributes/<id>"}`, on the printer's clock (epoch
  seconds). Nothing is pushed on connect until the client asks.
- `GET /downloadFile<path>` serves `files` (on the websocket's port here; port 80 on a real
  printer, as reported by bjan/pycentauri's `docs/PROTOCOL.md`), honoring a `Range`.
- `PrintInfo.Status` is the Centauri Carbon's print state: 13 printing, 5 pausing, 6 paused,
  12 resuming, 7 stopping, 8 stopped, 9 complete, 14 error, 16 preheating, 18 print start...
  `CurrentTicks` and `TotalTicks` are seconds on the Centauri Carbon (milliseconds in the
  resin spec). The last end state stays until the next print.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from typing import Any

from aiohttp import WSMsgType, web

MAINBOARD = "608715130105041800009c0000000000"
FIRMWARE = "V1.1.29"
MODEL = "Centauri Carbon"
NAME = "Workshop CC"

PRINTING, PAUSING, PAUSED, RESUMING = 13, 5, 6, 12
STOPPING, STOPPED, COMPLETE, ERROR, IDLE = 7, 8, 9, 14, 0
PREHEATING, PRINT_START, HOMING = 16, 18, 1


def idle_status() -> dict[str, Any]:
    return {
        "CurrentStatus": [0],
        "TimeLapseStatus": 0,
        "PlatFormType": 0,
        "TempOfHotbed": 24.5,
        "TempOfNozzle": 25.1,
        "TempOfBox": 23.0,
        "TempTargetHotbed": 0,
        "TempTargetNozzle": 0,
        "TempTargetBox": 0,
        "CurrenCoord": "0.00,0.00,0.00",
        "CurrentFanSpeed": {"ModelFan": 0, "AuxiliaryFan": 0, "BoxFan": 0},
        "ZOffset": 0.0,
        "LightStatus": {"SecondLight": 1, "RgbLight": [0, 0, 0]},
        "PrintInfo": {
            "Status": IDLE,
            "CurrentLayer": 0,
            "TotalLayer": 0,
            "CurrentTicks": 0,
            "TotalTicks": 0,
            "Filename": "",
            "TaskId": "",
            "PrintSpeedPct": 100,
            "Progress": 0,
        },
    }


def attributes() -> dict[str, Any]:
    return {
        "Name": NAME,
        "MachineName": MODEL,
        "BrandName": "ELEGOO",
        "ProtocolVersion": "V3.0.0",
        "FirmwareVersion": FIRMWARE,
        "XYZsize": "256x256x256",
        "MainboardIP": "192.168.1.77",
        "MainboardID": MAINBOARD,
        "MainboardMAC": "aa:bb:cc:dd:ee:ff",
        "NetworkStatus": "wlan",
        "WifiName": "SECRET-HOME-WIFI",
        "Capabilities": ["FILE_TRANSFER", "PRINT_CONTROL", "VIDEO_STREAM"],
        "SupportFileType": ["GCODE"],
        "CameraStatus": 1,
        "RemainingMemory": 5_000_000_000,
    }


def discovery_reply(address: str = "192.168.1.77", board: str = MAINBOARD) -> bytes:
    """What the printer answers to `M99999` (spec, "Device Discovery")."""
    return json.dumps(
        {
            "Id": uuid.uuid4().hex,
            "Data": {
                "Name": NAME,
                "MachineName": MODEL,
                "BrandName": "ELEGOO",
                "MainboardIP": address,
                "MainboardID": board,
                "ProtocolVersion": "V3.0.0",
                "FirmwareVersion": FIRMWARE,
            },
        }
    ).encode()


class FakeElegoo:
    def __init__(self) -> None:
        self.board = MAINBOARD
        self.status: dict[str, Any] = idle_status()
        self.attributes: dict[str, Any] = attributes()
        self.clock_offset = 0.0
        """The printer's clock less this computer's."""
        self.idle_timeout = 60.0
        self.answer = True
        """False: accept sockets but answer nothing (not an SDCP printer)."""
        self.sockets: list[web.WebSocketResponse] = []
        self.connections = 0
        self.requests: list[Any] = []
        """Every message a client sent: `"ping"`, or the parsed request."""
        self.refused: list[Any] = []
        self.pushes_on_change = True
        self.files: dict[str, bytes] = {}
        """What `GET /downloadFile<path>` serves, by path (`/local/Benchy.gcode`)."""
        self.downloads: list[tuple[str, str | None]] = []
        """Every download asked for: the path and its `Range` header."""
        self.honor_range = True
        self._runner: web.AppRunner | None = None
        self.host = "127.0.0.1"
        self.port = 0

    # -- server --------------------------------------------------------------------------------

    async def start(self) -> FakeElegoo:
        app = web.Application()
        app.router.add_get("/websocket", self._websocket)
        app.router.add_get("/downloadFile/{path:.*}", self._download)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return self

    async def close(self) -> None:
        await self.drop()
        if self._runner is not None:
            await self._runner.cleanup()

    async def drop(self) -> None:
        for ws in list(self.sockets):
            with contextlib.suppress(Exception):
                await ws.close()
        self.sockets.clear()

    def now(self) -> int:
        return int(time.time() + self.clock_offset)

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        self.connections += 1
        try:
            while True:
                try:
                    msg = await ws.receive(timeout=self.idle_timeout)
                except asyncio.TimeoutError:
                    break  # the printer drops a silent client
                if msg.type != WSMsgType.TEXT:
                    break
                await self._on_message(ws, msg.data)
        finally:
            if ws in self.sockets:
                self.sockets.remove(ws)
            with contextlib.suppress(Exception):
                await ws.close()
        return ws

    async def _download(self, request: web.Request) -> web.StreamResponse:
        """The file download on the printer's web port (80 on a real one; one community note,
        pycentauri's PROTOCOL.md, reports `GET /downloadFile<path>`). Honors a simple `Range`
        unless `honor_range` is off."""
        path = "/" + request.match_info["path"]
        asked = request.headers.get("Range")
        self.downloads.append((path, asked))
        data = self.files.get(path)
        if data is None:
            return web.Response(status=404)
        if asked and self.honor_range and asked.startswith("bytes="):
            start_text, _, end_text = asked[len("bytes=") :].partition("-")
            start = int(start_text)
            end = min(int(end_text) + 1 if end_text else len(data), len(data))
            return web.Response(
                status=206,
                body=data[start:end],
                headers={"Content-Range": f"bytes {start}-{end - 1}/{len(data)}"},
            )
        return web.Response(body=data)

    async def _on_message(self, ws: web.WebSocketResponse, text: str) -> None:
        if text == "ping":
            self.requests.append("ping")
            if self.answer:
                await ws.send_str("pong")
            return
        try:
            message = json.loads(text)
            data = message["Data"]
            cmd = data["Cmd"]
        except (ValueError, KeyError, TypeError):
            self.refused.append(text)
            return
        self.requests.append(message)
        if cmd not in (0, 1) or data.get("Data") not in ({}, None):
            self.refused.append(message)
            return
        if not self.answer:
            return
        await ws.send_str(
            json.dumps(
                {
                    "Id": uuid.uuid4().hex,
                    "Data": {
                        "Cmd": cmd,
                        "Data": {"Ack": 0},
                        "RequestID": data.get("RequestID"),
                        "MainboardID": self.board,
                        "TimeStamp": self.now(),
                    },
                    "Topic": f"sdcp/response/{self.board}",
                }
            )
        )
        if cmd == 0:
            await ws.send_str(self._status_message(self.status))
        else:
            await ws.send_str(self._attributes_message())

    def _status_message(self, status: dict[str, Any]) -> str:
        return json.dumps(
            {
                "Status": status,
                "MainboardID": self.board,
                "TimeStamp": self.now(),
                "Topic": f"sdcp/status/{self.board}",
            }
        )

    def _attributes_message(self) -> str:
        return json.dumps(
            {
                "Attributes": self.attributes,
                "MainboardID": self.board,
                "TimeStamp": self.now(),
                "Topic": f"sdcp/attributes/{self.board}",
            }
        )

    async def send(self, text: str) -> None:
        for ws in list(self.sockets):
            with contextlib.suppress(Exception):
                await ws.send_str(text)

    async def push(self, partial: dict[str, Any] | None = None) -> None:
        """The status to every client: all of it, or only `partial` (a printer that sends what
        changed)."""
        if self.pushes_on_change:
            await self.send(self._status_message(partial if partial is not None else self.status))

    # -- the printer ---------------------------------------------------------------------------

    @property
    def info(self) -> dict[str, Any]:
        return self.status["PrintInfo"]

    async def set(self, sub: int | None = None, machine: int | None = None, **info: Any) -> None:
        if sub is not None:
            self.info["Status"] = sub
        if machine is not None:
            self.status["CurrentStatus"] = [machine]
        self.info.update(info)
        await self.push()

    async def prepare(self, sub: int = PREHEATING) -> None:
        """A new print's start routine, before it prints: the last job's file, task and
        progress may still be shown."""
        await self.set(sub, machine=1)

    async def print_file(
        self,
        file: str = "/local/Benchy.gcode",
        *,
        task: str | None = None,
        layers: int = 200,
        total_s: int = 3600,
        ticks: int = 0,
        progress: int = 0,
        layer: int = 0,
    ) -> str:
        task = task or uuid.uuid4().hex
        await self.set(
            PRINTING,
            machine=1,
            Filename=file,
            TaskId=task,
            TotalLayer=layers,
            TotalTicks=total_s,
            CurrentTicks=ticks,
            Progress=progress,
            CurrentLayer=layer,
        )
        return task

    async def progress(self, percent: int, layer: int | None = None, ticks: int | None = None):
        total = self.info["TotalLayer"] or 100
        await self.set(
            Progress=percent,
            CurrentLayer=layer if layer is not None else int(total * percent / 100),
            CurrentTicks=ticks
            if ticks is not None
            else int(self.info["TotalTicks"] * percent / 100),
        )

    async def pause(self) -> None:
        await self.set(PAUSING)
        await self.set(PAUSED)

    async def resume(self) -> None:
        await self.set(RESUMING)
        await self.set(PRINTING)

    async def complete(self) -> None:
        await self.set(COMPLETE, Progress=100, CurrentLayer=self.info["TotalLayer"])
        await self.set(machine=0)

    async def cancel(self, clear: bool = False) -> None:
        await self.set(STOPPING)
        if clear:  # as one Centauri Carbon was seen after a cancel: no file, no task
            await self.set(STOPPED, machine=0, Filename="", TaskId="")
        else:
            await self.set(STOPPED, machine=0)

    async def fail(self) -> None:
        await self.set(ERROR, machine=0)

    async def idle(self) -> None:
        """As after a power cycle: nothing of the last job left."""
        self.status = idle_status()
        await self.push()


class FakeDiscovery(asyncio.DatagramProtocol):
    """A printer answering `M99999` on a UDP port (3000 on a real one), from 127.0.0.1."""

    def __init__(self, reply: bytes | None = None) -> None:
        self.reply = reply if reply is not None else discovery_reply("127.0.0.1")
        self.asked: list[bytes] = []
        self.repeat = 1
        self.transport: asyncio.DatagramTransport | None = None
        self.port = 0

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.asked.append(data)
        if data == b"M99999" and self.transport is not None:
            for _ in range(self.repeat):
                self.transport.sendto(self.reply, addr)

    async def start(self) -> FakeDiscovery:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=("127.0.0.1", 0)
        )
        self.port = transport.get_extra_info("sockname")[1]
        return self

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
