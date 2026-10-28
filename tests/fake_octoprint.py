"""A fake OctoPrint for the tests: its REST API and its push socket, as OctoPrint 1.11.8 with its
virtual printer answered (`tests/fixtures/octoprint/`, recorded with
`tests/integration/record_octoprint.py`; `tests/integration/test_real_octoprint.py` compares the
two).

- Every `/api/` and `/downloads/` request needs `X-Api-Key`; a wrong or missing one is 403 with
  `{"error": ...}`. `POST /api/login` with `passive` answers the key's user with a `session`.
- `/sockjs/websocket` is SockJS's raw WebSocket: one JSON message per frame. It sends
  `connected` at once and nothing more until `{"auth": "<user>:<session>"}`, then `history`,
  then `current` every `tick` seconds (OctoPrint: 0.5 s times the throttle), and each `event`.
- `history` and `current` carry `state` (`text`, `flags`), `job`, `progress`, `serverTime` and
  `markings` (`print` at a start, `done` and `cancel` at ends).
- A start sends `PrinterStateChanged`, `PrintStarted`; a finish `PrintDone` (with `time`); a
  cancel `PrintCancelling`, `PrintCancelled` and `PrintFailed` with `reason` `cancelled`; an
  error `Error` and `PrintFailed` with `reason` `error`.
- `/downloads/files/local/<path>` honors `Range` (206 and `Content-Range`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections.abc import Mapping
from typing import Any

from aiohttp import WSMsgType, web

API_KEY = "B7E0C4D1A2F34E5B8C9D0E1F2A3B4C5D"
USER = "admin"
VERSION = "1.11.8"
FORBIDDEN = {
    "error": "You don't have the permission to access the requested resource. It is either "
    "read-protected or not readable by the server."
}


def analysis(length: float = 1332.37, volume: float = 3.2, tools: int = 1, seconds=642.0):
    return {
        "estimatedPrintTime": seconds,
        "filament": {f"tool{n}": {"length": length, "volume": volume} for n in range(tools)},
    }


class FakeOctoPrint:
    def __init__(self, api_key: str = API_KEY) -> None:
        self.api_key = api_key
        self.tick = 0.05
        self.marks_enabled = True
        """OctoPrint before marks: `markings` left out."""
        self.files: dict[str, bytes] = {}
        self.analyses: dict[str, dict[str, Any]] = {}
        self.thumbnails: dict[str, bytes] = {}
        self.frame: bytes | None = None
        """What `/webcam/?action=snapshot` serves (mjpg-streamer's, without a key)."""
        self.settings: dict[str, Any] = {
            "appearance": {"name": "Ender 3"},
            "webcam": {"webcamEnabled": True, "snapshotUrl": "", "webcams": []},
            "plugins": {"virtual_printer": {"enabled": True}},
        }
        self.profile = {"default": True, "model": "Ender-3", "extruder": {"count": 1}}
        self.connected_to_printer = True
        self.state = "Operational"
        self.flags = _flags("Operational")
        self.job: dict[str, Any] | None = None
        self.completion: float | None = None
        self.started: float | None = None
        self.marks: list[dict[str, Any]] = [
            {"label": "Connected", "time": time.time() - 3600, "type": "connected"}
        ]
        self.sessions: set[str] = set()
        self.sockets: list[web.WebSocketResponse] = []
        self.requests: list[tuple[str, str, Mapping[str, str]]] = []
        self.throttles: list[Any] = []
        self.forbidden = 0
        self.down = False
        self._runner: web.AppRunner | None = None
        self._ticker: asyncio.Task | None = None
        self.port = 0

    # -- the server ---------------------------------------------------------------------

    async def start(self) -> str:
        app = web.Application(middlewares=[self._middleware])
        app.router.add_get("/api/version", self._version)
        app.router.add_get("/api/printer", self._printer)
        app.router.add_get("/api/job", self._job)
        app.router.add_get("/api/settings", self._settings)
        app.router.add_get("/api/printerprofiles", self._profiles)
        app.router.add_post("/api/login", self._login)
        app.router.add_get("/api/files/local/{path:.+}", self._file)
        app.router.add_get("/downloads/files/local/{path:.+}", self._download)
        app.router.add_get("/plugin/prusaslicerthumbnails/thumbnail/{path:.+}", self._thumbnail)
        app.router.add_get("/sockjs/websocket", self._socket)
        app.router.add_get("/webcam/", self._webcam)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self._ticker = asyncio.ensure_future(self._tick())
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def close(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
        await self.drop()
        if self._runner is not None:
            await self._runner.cleanup()

    async def drop(self) -> None:
        """Closes every socket, as a restart of OctoPrint's server or the network would."""
        for ws in list(self.sockets):
            await ws.close()
        self.sockets.clear()

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        self.requests.append((request.method, request.path, dict(request.headers)))
        if self.down:
            raise web.HTTPServiceUnavailable()
        protected = request.path.startswith(("/api/", "/downloads/", "/plugin/"))
        if protected and request.headers.get("X-Api-Key") != self.api_key:
            self.forbidden += 1
            return web.json_response(FORBIDDEN, status=403)
        return await handler(request)

    def requests_of(self, path: str) -> list[tuple[str, str, Mapping[str, str]]]:
        return [r for r in self.requests if r[1] == path]

    @property
    def methods(self) -> set[str]:
        return {method for method, path, _ in self.requests if path != "/api/login"}

    async def _version(self, request: web.Request) -> web.Response:
        return web.json_response({"api": "0.1", "server": VERSION, "text": f"OctoPrint {VERSION}"})

    async def _printer(self, request: web.Request) -> web.Response:
        if not self.connected_to_printer:
            return web.Response(status=409, text="Printer is not operational")
        return web.json_response(
            {
                "sd": {"ready": True},
                "state": {"error": "", "flags": self.flags, "text": self.state},
                "temperature": {"bed": {"actual": 21.3, "offset": 0, "target": 0.0}},
            }
        )

    async def _job(self, request: web.Request) -> web.Response:
        status = self.status()
        return web.json_response(
            {"job": status["job"], "progress": status["progress"], "state": self.state}
        )

    async def _settings(self, request: web.Request) -> web.Response:
        return web.json_response(self.settings)

    async def _profiles(self, request: web.Request) -> web.Response:
        return web.json_response({"profiles": {"_default": self.profile}})

    async def _login(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("passive") is not True:
            return web.Response(status=400)
        session = secrets.token_hex(16)
        self.sessions.add(session)
        return web.json_response(
            {
                "name": USER,
                "session": session,
                "active": True,
                "admin": True,
                "user": True,
                "_login_mechanism": "apikey",
                "groups": ["admins"],
            }  # fmt: skip
        )

    async def _file(self, request: web.Request) -> web.Response:
        path = request.match_info["path"]
        if path not in self.files:
            return web.Response(status=404)
        entry: dict[str, Any] = {
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "origin": "local",
            "size": len(self.files[path]),
            "gcodeAnalysis": self.analyses.get(path, {}),
            "refs": {
                "download": f"{self.url}/downloads/files/local/{path}",
                "resource": f"{self.url}/api/files/local/{path}",
            },
        }
        if path in self.thumbnails:
            entry["thumbnail"] = f"plugin/prusaslicerthumbnails/thumbnail/{path}.png?2"
        return web.json_response(entry)

    async def _download(self, request: web.Request) -> web.Response:
        path = request.match_info["path"]
        data = self.files.get(path)
        if data is None:
            return web.Response(status=404)
        wanted = request.headers.get("Range", "")
        if wanted.startswith("bytes="):
            first, _, last = wanted[6:].partition("-")
            start, end = int(first), min(int(last), len(data) - 1)
            return web.Response(
                status=206,
                body=data[start : end + 1],
                headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"},
            )
        return web.Response(body=data)

    async def _thumbnail(self, request: web.Request) -> web.Response:
        path = request.match_info["path"].rsplit(".png", 1)[0]
        if path not in self.thumbnails:
            return web.Response(status=404)
        return web.Response(body=self.thumbnails[path], content_type="image/png")

    async def _webcam(self, request: web.Request) -> web.Response:
        if self.frame is None or request.query.get("action") != "snapshot":
            return web.Response(status=404)
        return web.Response(body=self.frame, content_type="image/jpeg")

    async def _socket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json(
            {
                "connected": {
                    "version": VERSION,
                    "display_version": VERSION,
                    "online": True,
                    "safe_mode": None,
                    "permissions": [],
                }
            }  # fmt: skip
        )
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            body = json.loads(msg.data)
            if "auth" in body:
                user, _, session = str(body["auth"]).partition(":")
                if user == USER and session in self.sessions:
                    self.sockets.append(ws)
                    await ws.send_json({"history": self.status()})
                    await ws.send_json({"timelapse": None})
            elif "throttle" in body:
                self.throttles.append(body["throttle"])
        if ws in self.sockets:
            self.sockets.remove(ws)
        return ws

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self.tick)
            await self.send({"current": self.status()})

    async def send(self, message: Mapping[str, Any]) -> None:
        for ws in list(self.sockets):
            with contextlib.suppress(ConnectionError, RuntimeError):
                await ws.send_json(message)

    async def event(self, kind: str, payload: Any) -> None:
        await self.send({"event": {"type": kind, "payload": payload}})

    # -- the printer --------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        job = self.job or {
            "estimatedPrintTime": None,
            "filament": {"length": None, "volume": None},
            "file": {"date": None, "name": None, "origin": None, "path": None, "size": None},
            "lastPrintTime": None,
            "user": None,
        }
        printing = self.started is not None
        status: dict[str, Any] = {
            "busyFiles": [],
            "currentZ": None,
            "job": job,
            "logs": [],
            "messages": [],
            "offsets": {},
            "progress": {
                "completion": self.completion,
                "filepos": None,
                "printTime": int(time.time() - self.started) if printing else None,  # type: ignore[operator]
                "printTimeLeft": None,
                "printTimeLeftOrigin": None,
            },
            "resends": {"count": 0, "ratio": 0, "transmitted": 0},
            "serverTime": time.time(),
            "state": {"error": "", "flags": self.flags, "text": self.state},
            "temps": [],
        }
        if self.marks_enabled:
            status["markings"] = list(self.marks)
        return status

    def add(self, path: str, data: bytes, **analysis_of) -> None:
        self.files[path] = data
        self.analyses[path] = analysis(**analysis_of)

    def _payload(self, **more) -> dict[str, Any]:
        file = self.job["file"]  # type: ignore[index]
        return {"name": file["name"], "origin": "local", "owner": USER, "path": file["path"],
                "size": file["size"], **more}  # fmt: skip

    def _set(self, state: str) -> None:
        self.state = state
        self.flags = _flags(state)

    async def print_file(self, path: str, *, completion: float = 0.0, ago: float = 0.0) -> None:
        """Starts `path` (added with `add`), as if `ago` seconds ago."""
        data = self.files[path]
        self.job = {
            "averagePrintTime": None,
            **self.analyses.get(path, {}),
            "file": {
                "date": 1793151015,
                "display": path.rsplit("/", 1)[-1],
                "name": path.rsplit("/", 1)[-1],
                "origin": "local",
                "path": path,
                "size": len(data),
            },  # fmt: skip
            "lastPrintTime": None,
            "user": USER,
        }
        self.started = time.time() - ago
        self.completion = completion
        self.marks.append({"label": "Start", "time": self.started, "type": "print"})
        self._set("Printing")
        await self.event(
            "PrinterStateChanged", {"state_id": "PRINTING", "state_string": "Printing"}
        )
        await self.event("PrintStarted", self._payload(user=USER))

    async def done(self, *, quietly: bool = False) -> None:
        """The job completes. `quietly`: without its events (a bridge that wasn't there)."""
        elapsed = time.time() - self.started  # type: ignore[operator]
        self.completion = 100.0
        self.marks.append({"label": "Done", "time": time.time(), "type": "done"})
        self._set("Operational")
        self.started = None
        if not quietly:
            await self.event("PrintDone", self._payload(time=elapsed))
        self.completion = None

    async def cancel(self, *, quietly: bool = False) -> None:
        elapsed = time.time() - self.started  # type: ignore[operator]
        progress = int(self.completion or 0)
        self.marks.append({"label": "Cancel", "time": time.time(), "type": "cancel"})
        self._set("Operational")
        self.started = None
        if not quietly:
            await self.event("PrintCancelling", self._payload(user=USER))
            payload = self._payload(progress=progress, time=elapsed, user=USER, fileposition=0)
            await self.event("PrintCancelled", payload)
            await self.event("PrintFailed", {**payload, "reason": "cancelled"})
        self.completion = None

    async def fail(self, error: str) -> None:
        elapsed = time.time() - self.started  # type: ignore[operator]
        progress = int(self.completion or 0)
        self._set("Error")
        self.started = None
        await self.event("Error", {"error": error, "reason": "firmware"})
        payload = self._payload(progress=progress, time=elapsed, user=USER, fileposition=0)
        await self.event("PrintFailed", {**payload, "reason": "error"})
        self.completion = None


def _flags(state: str) -> dict[str, bool]:
    flags = dict.fromkeys(
        ("cancelling", "closedOrError", "error", "finishing", "operational", "paused",
         "pausing", "printing", "ready", "resuming"),
        False,
    )  # fmt: skip
    flags["sdReady"] = True
    if state == "Operational":
        flags.update(operational=True, ready=True)
    elif state == "Printing":
        flags.update(operational=True, printing=True)
    elif state == "Paused":
        flags.update(operational=True, paused=True)
    elif state == "Error":
        flags.update(error=True, closedOrError=True)
    return flags
