"""A fake Duet for the tests: RepRapFirmware's HTTP API as a Duet 2 or 3 serves it without an
SBC ("standalone"), and the DuetSoftwareFramework's (a Duet 3 with a Raspberry Pi, "SBC").

Built from Duet3D's documentation and sources, not a recorded printer (#89):

- docs.duet3d.com, "RepRapFirmware HTTP requests": `rr_connect?password=` answers `err` (0 ok,
  1 wrong password, 2 no free session), `sessionTimeout` (ms), `boardType`, `apiLevel` and,
  from RRF 3.5, `sessionKey`, which every later request sends as `X-Session-Key`; a request
  without a live session is 401; `rr_disconnect`; `rr_model?key=&flags=` answers
  `{"key", "flags", "result"}` (and `next` for an array that didn't fit, fetched again with
  `a<next>`); `rr_fileinfo?name=` the file's slicer facts; `rr_thumbnail?name=&offset=` one
  piece of a thumbnail's base64 text with the offset of the `next` (0 at the end).
- The object model ("Object Model" page, DuetWebControl's `src/store/machine/model`): `seqs`
  counts each key's changes; `flags=f` gives only the fields that change often (`state.status`,
  `job.filePosition`, `job.duration`, `job.rawExtrusion`, each extruder's `position` and
  `rawPosition`...). `job.file` is the file printing (`fileName` null between jobs),
  `job.lastFileName`, `lastFileCancelled` and `lastFileAborted` say how the last one ended.
  `tools[].extruders` maps tools to extruders.
- DuetSoftwareFramework's DuetWebServer (`MachineController`): `/machine/connect?password=`
  answers `{"sessionKey"}` (403 for a wrong password), `/machine/model` (`/machine/status`
  before 3.4) the whole object model, `/machine/fileinfo/<file>` the file's facts, and
  `/machine/disconnect`. It answers no `rr_` request here.

ASSUMPTION throughout: none of it has been checked against a real board yet.
"""

from __future__ import annotations

import base64
import copy
import secrets
import time
from collections.abc import Sequence
from typing import Any

from aiohttp import web

PASSWORD = "Rr5-pw!x"
FIRMWARE = "3.5.2"
BOARD = "Duet 3 MB6HC"
PNG = b"\x89PNG\r\n\x1a\n" + b"duet-thumbnail" * 200

LIVE = {
    "state": ("status", "upTime", "currentTool"),
    "job": ("filePosition", "duration", "layer", "pauseDuration", "rawExtrusion", "timesLeft"),
}
LIVE_EXTRUDER = ("position", "rawPosition")
FULL_KEYS = ("boards", "job", "move", "network", "sbc", "state", "tools", "global")


def extruder(diameter: float = 1.75) -> dict[str, Any]:
    return {
        "driver": "0.3",
        "factor": 1.0,
        "filament": "PLA",
        "filamentDiameter": diameter,
        "position": 0.0,
        "rawPosition": 0.0,
        "pressureAdvance": 0.05,
        "stepsPerMm": 420.0,
    }


def tool(number: int, extruders: Sequence[int]) -> dict[str, Any]:
    return {
        "number": number,
        "name": f"T{number}",
        "extruders": list(extruders),
        "heaters": [number + 1],
        "state": "off",
        "filamentExtruder": extruders[0] if extruders else -1,
    }


def empty_file() -> dict[str, Any]:
    return {
        "fileName": None,
        "filament": [],
        "generatedBy": None,
        "height": 0,
        "lastModified": None,
        "layerHeight": 0,
        "numLayers": 0,
        "printTime": None,
        "simulatedTime": None,
        "size": 0,
        "thumbnails": [],
    }


class FakeDuet:
    def __init__(
        self,
        password: str | None = PASSWORD,
        *,
        sbc: bool = False,
        extruders: int = 1,
        tools: Sequence[Sequence[int]] | None = None,
        session_keys: bool = True,
    ) -> None:
        self.password = password
        """None: no password set (M551), so any is taken."""
        self.sbc = sbc
        self.session_keys = session_keys
        """RRF 3.5 and later: `sessionKey`. Older RRF keeps a session per client address."""
        self.session_timeout_ms = 8000
        self.max_sessions = 8
        self.page_size = 64
        """How many items of an array `rr_model` sends at once."""
        self.model: dict[str, Any] = {
            "boards": [
                {
                    "firmwareName": "RepRapFirmware",
                    "firmwareVersion": FIRMWARE,
                    "name": BOARD,
                    "shortName": "MB6HC",
                    "uniqueId": "08DLM-956BA-NA3TJ-6J1F0-3SD6S-1BMUF",
                }
            ],
            "global": {"secretVar": "do-not-trace"},
            "job": {
                "build": None,
                "duration": None,
                "file": empty_file(),
                "filePosition": 0,
                "lastDuration": None,
                "lastFileName": None,
                "lastFileAborted": False,
                "lastFileCancelled": False,
                "lastFileSimulated": False,
                "layer": None,
                "pauseDuration": None,
                "rawExtrusion": None,
                "timesLeft": {"filament": None, "file": None, "slicer": None},
                "warmUpDuration": None,
            },
            "move": {"extruders": [extruder() for _ in range(extruders)]},
            "network": {
                "name": "Railcore",
                "hostname": "railcore",
                "interfaces": [
                    {"mac": "02:00:00:12:34:56", "actualIP": "192.168.1.88", "ssid": "HomeWiFi"}
                ],
            },
            "sbc": {"dsf": {"version": "3.5.2"}} if sbc else None,
            "state": {"status": "idle", "upTime": 100, "currentTool": 0},
            "tools": [tool(n, e) for n, e in enumerate(tools or [[i] for i in range(extruders)])],
            "seqs": {k: 1 for k in FULL_KEYS},
        }
        self.files: dict[str, dict[str, Any]] = {}
        """Each file's `rr_fileinfo` facts, by its full name (`0:/gcodes/...`)."""
        self.thumbnail_text: dict[tuple[str, int], str] = {}
        """Each thumbnail's base64 text, by file and offset."""
        self.requests: list[str] = []
        self.queries: list[str] = []
        self.methods: set[str] = set()
        self.headers: list[dict[str, str]] = []
        self.sessions: dict[str, float] = {}
        self.ip_session: float | None = None
        self.disconnects = 0
        self.down = False
        self.busy = 0
        """Answer the next this many requests 503, as a busy board does."""
        self._runner: web.AppRunner | None = None
        self.port = 0

    # --- control -------------------------------------------------------------------------

    async def start(self) -> str:
        app = web.Application(middlewares=[self._log])
        if self.sbc:
            app.router.add_get("/machine/connect", self._machine_connect)
            app.router.add_get("/machine/disconnect", self._machine_disconnect)
            app.router.add_get("/machine/model", self._machine_model)
            app.router.add_get("/machine/status", self._machine_model)
            app.router.add_get("/machine/fileinfo/{path:.*}", self._machine_fileinfo)
        else:
            app.router.add_get("/rr_connect", self._connect)
            app.router.add_get("/rr_disconnect", self._disconnect)
            app.router.add_get("/rr_model", self._rr_model)
            app.router.add_get("/rr_fileinfo", self._fileinfo)
            app.router.add_get("/rr_thumbnail", self._thumbnail)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return f"127.0.0.1:{self.port}"

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def expire_sessions(self) -> None:
        self.sessions.clear()
        self.ip_session = None

    def bump(self, *keys: str) -> None:
        for key in keys:
            self.model["seqs"][key] += 1

    @property
    def job(self) -> dict[str, Any]:
        return self.model["job"]

    def add_file(
        self,
        name: str,
        *,
        filament: Sequence[float] = (4130.2,),
        print_time: int = 3723,
        generated_by: str = "PrusaSlicer 2.9.2+win64 on 2026-10-27 at 10:00:00 UTC",
        thumbnail: bytes | None = PNG,
        size: int = 1_000_000,
    ) -> str:
        path = f"0:/gcodes/{name}"
        thumbnails = []
        if thumbnail is not None:
            offset = 120
            text = base64.b64encode(thumbnail).decode()
            self.thumbnail_text[(path, offset)] = text
            thumbnails.append(
                {"width": 300, "height": 300, "fmt": "png", "offset": offset, "size": len(text)}
            )
            self.thumbnail_text[(path, 9000)] = base64.b64encode(b"qoif-small").decode()
            thumbnails.append({"width": 32, "height": 32, "fmt": "qoi", "offset": 9000,
                               "size": 16})  # fmt: skip
        self.files[path] = {
            "fileName": path,
            "filament": list(filament),
            "firstLayerHeight": 0.3,
            "generatedBy": generated_by,
            "height": 12.4,
            "lastModified": "2026-10-27T10:00:00",
            "layerHeight": 0.2,
            "numLayers": 62,
            "printTime": print_time,
            "simulatedTime": None,
            "size": size,
            "thumbnails": thumbnails,
        }
        return path

    def print_file(self, name: str, *, duration: float = 0.0, **file) -> None:
        """Starts printing `name` from the SD card: rawPosition starts again at 0."""
        path = f"0:/gcodes/{name}"
        if path not in self.files:
            self.add_file(name, **file)
        info = self.files[path]
        job = self.job
        job["file"] = copy.deepcopy({k: v for k, v in info.items() if k != "firstLayerHeight"})
        job.update(duration=duration, pauseDuration=0, filePosition=0, rawExtrusion=0.0,
                   layer=1, lastFileName=path, lastFileCancelled=False, lastFileAborted=False,
                   warmUpDuration=30)  # fmt: skip
        for e in self.model["move"]["extruders"]:
            e["rawPosition"] = 0.0
            e["position"] = 0.0
        self.model["state"]["status"] = "processing"
        self.bump("job", "state")

    def simulate_file(self, name: str) -> None:
        self.print_file(name)
        self.model["state"]["status"] = "simulating"

    def extrude(self, mm: Sequence[float], *, seconds: float = 0.0, position: int = 0) -> None:
        """The job feeds `mm[e]` more through each extruder `e`, takes `seconds` more, and is
        `position` bytes further through its file."""
        job = self.job
        for e, amount in zip(self.model["move"]["extruders"], mm):
            e["rawPosition"] = round(e["rawPosition"] + amount, 3)
            e["position"] = round(e["position"] + amount, 3)
        job["rawExtrusion"] = round((job["rawExtrusion"] or 0.0) + sum(mm), 3)
        job["duration"] = (job["duration"] or 0) + seconds
        job["filePosition"] += position

    def status(self, status: str) -> None:
        self.model["state"]["status"] = status

    def end(self, *, cancelled: bool = False, aborted: bool = False) -> None:
        job = self.job
        job["lastDuration"] = job["duration"]
        job.update(file=empty_file(), duration=None, filePosition=0, layer=None,
                   lastFileCancelled=cancelled, lastFileAborted=aborted)  # fmt: skip
        self.model["state"]["status"] = "idle"
        self.bump("job", "state")

    def reset(self) -> None:
        """The board restarted: the job, the sessions and the totals are gone."""
        self.end()
        self.job["lastFileName"] = None
        self.job["lastDuration"] = None
        for e in self.model["move"]["extruders"]:
            e["rawPosition"] = 0.0
        self.expire_sessions()

    def requests_of(self, path: str) -> list[str]:
        return [r for r in self.requests if r == path]

    # --- the HTTP side -------------------------------------------------------------------

    @web.middleware
    async def _log(self, request: web.Request, handler):
        self.requests.append(request.path)
        self.queries.append(request.path_qs)
        self.methods.add(request.method)
        self.headers.append(dict(request.headers))
        if self.down:
            raise web.HTTPServiceUnavailable()
        if self.busy > 0 and not request.path.endswith("connect"):
            self.busy -= 1
            raise web.HTTPServiceUnavailable()
        return await handler(request)

    def _password_ok(self, given: str | None) -> bool:
        if self.password is None:
            return True
        return given == self.password

    def _authorized(self, request: web.Request) -> bool:
        now = time.monotonic()
        timeout = self.session_timeout_ms / 1000
        if self.session_keys or self.sbc:
            key = request.headers.get("X-Session-Key", "")
            seen = self.sessions.get(key)
            if seen is None or now - seen > timeout:
                self.sessions.pop(key, None)
                return False
            self.sessions[key] = now
            return True
        if self.ip_session is None or now - self.ip_session > timeout:
            self.ip_session = None
            return False
        self.ip_session = now
        return True

    def _new_session(self) -> str | None:
        now = time.monotonic()
        timeout = self.session_timeout_ms / 1000
        self.sessions = {k: t for k, t in self.sessions.items() if now - t <= timeout}
        if len(self.sessions) >= self.max_sessions:
            return None
        key = str(secrets.randbelow(2**31 - 1) + 1)
        self.sessions[key] = now
        return key

    async def _connect(self, request: web.Request) -> web.Response:
        if not self._password_ok(request.query.get("password")):
            return web.json_response({"err": 1})
        body: dict[str, Any] = {
            "err": 0,
            "sessionTimeout": self.session_timeout_ms,
            "boardType": "mb6hc",
            "apiLevel": 1 if self.session_keys else 0,
        }
        if self.session_keys:
            key = self._new_session()
            if key is None:
                return web.json_response({"err": 2})
            body["sessionKey"] = int(key)
        else:
            self.ip_session = time.monotonic()
        return web.json_response(body)

    async def _disconnect(self, request: web.Request) -> web.Response:
        self.disconnects += 1
        self.sessions.pop(request.headers.get("X-Session-Key", ""), None)
        if not self.session_keys:
            self.ip_session = None
        return web.json_response({"err": 0})

    def _unauthorized(self) -> web.Response:
        return web.Response(status=401, text="Not authorized")

    def frequent(self) -> dict[str, Any]:
        model = self.model
        result: dict[str, Any] = {
            key: {f: model[key][f] for f in fields} for key, fields in LIVE.items()
        }
        result["move"] = {
            "extruders": [{f: e[f] for f in LIVE_EXTRUDER} for e in model["move"]["extruders"]]
        }
        result["seqs"] = dict(model["seqs"])
        return result

    async def _rr_model(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        key = request.query.get("key", "")
        flags = request.query.get("flags", "")
        if key == "":
            if "f" not in flags:
                return web.json_response({"key": "", "flags": flags, "result": self.model})
            return web.json_response({"key": "", "flags": flags, "result": self.frequent()})
        if key not in self.model:
            return web.json_response({"key": key, "flags": flags, "result": None})
        value = copy.deepcopy(self.model[key])
        body: dict[str, Any] = {"key": key, "flags": flags}
        if isinstance(value, list):
            start = 0
            for part in flags.split("a")[1:]:
                digits = "".join(c for c in part if c.isdigit())
                start = int(digits or 0)
            page = value[start : start + self.page_size]
            body["result"] = page
            if start + self.page_size < len(value):
                body["next"] = start + self.page_size
        else:
            body["result"] = value
        return web.json_response(body)

    def info(self, name: str) -> dict[str, Any] | None:
        info = self.files.get(name)
        return None if info is None else copy.deepcopy(info)

    async def _fileinfo(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        info = self.info(request.query.get("name", ""))
        if info is None:
            return web.json_response({"err": 1})
        return web.json_response({"err": 0, **info})

    async def _thumbnail(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        name = request.query.get("name", "")
        offset = int(request.query.get("offset", "0"))
        for (path, start), text in self.thumbnail_text.items():
            if path == name and start <= offset < start + len(text):
                piece = text[offset - start : offset - start + 1024]
                after = offset + len(piece)
                return web.json_response(
                    {
                        "fileName": name,
                        "offset": offset,
                        "data": piece,
                        "next": 0 if after >= start + len(text) else after,
                        "err": 0,
                    }
                )
        return web.json_response({"fileName": name, "offset": offset, "err": 1})

    # --- DuetSoftwareFramework -----------------------------------------------------------

    async def _machine_connect(self, request: web.Request) -> web.Response:
        if not self._password_ok(request.query.get("password")):
            return web.Response(status=403)
        key = self._new_session()
        if key is None:
            return web.Response(status=503)
        return web.json_response({"sessionKey": key})

    async def _machine_disconnect(self, request: web.Request) -> web.Response:
        self.disconnects += 1
        self.sessions.pop(request.headers.get("X-Session-Key", ""), None)
        return web.Response(status=204)

    async def _machine_model(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        return web.json_response(self.model)

    async def _machine_fileinfo(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        name = request.match_info["path"]
        info = self.info(name)
        if info is None:
            return web.Response(status=404)
        if request.query.get("readThumbnailContent") == "true":
            for thumb in info["thumbnails"]:
                thumb["data"] = self.thumbnail_text.get((name, thumb["offset"]))
        return web.json_response(info)
