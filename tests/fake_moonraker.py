"""A small stand-in for Moonraker's JSON-RPC API, over a WebSocket or a Unix socket.

It speaks just enough of the protocol for the bridge: identify, server.info,
printer.objects.list/subscribe, printer.gcode.script, connection.register_remote_method,
server.history.list, server.files.roots, server.files.metadata and server.webcams.list, plus
pushing notifications (console lines among them)
and dropping every client to simulate a restart. `start_job` and `finish_job` drive a print the
way Moonraker's history component reports one. Over HTTP it serves the gcodes root's files
(`GET /server/files/gcodes/<path>`), which is how a remote bridge gets a thumbnail, and
`/server/info`, `/machine/system_info`, `/printer/info` and `/printer/objects/list`, which is how
discovery recognises it. `start(host=..., port=...)` serves it at a chosen address, so a test
can move it from 127.0.0.87 to 127.0.0.91, as a printer given a new address by DHCP.

Authorization copies real Moonraker (checked against v0.9): the WebSocket upgrade always
succeeds, the Unix socket is trusted, and a WebSocket is authorized by an `X-Api-Key` header on
the upgrade or an `api_key` in identify. Until then every method fails with -32602. A file
download needs the `X-Api-Key` header (401 without it) whenever an API key is set.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

DEFAULT_OBJECTS = (
    "webhooks",
    "print_stats",
    "toolhead",
    "extruder",
    "extruder1",
    "virtual_sdcard",
    "gcode_move",
)

Send = Callable[[dict], Awaitable[None]]


class FakeMoonraker:
    def __init__(
        self,
        *,
        klippy_state: str = "ready",
        objects: tuple[str, ...] = DEFAULT_OBJECTS,
        api_key: str | None = None,
        reject_upgrade: bool = False,
        history: bool = True,
        mac: str | None = "2c:cf:67:00:00:01",
        instance_id: str | None = "6f1c1b5e-0000-4000-8000-000000000001",
        hostname: str = "voron",
    ) -> None:
        self.klippy_state = klippy_state
        self.objects = list(objects)
        self.api_key = api_key
        self.reject_upgrade = reject_upgrade
        """Answer the upgrade with 401, as a reverse proxy with its own auth would."""
        self.history_enabled = history
        """False is a moonraker.conf without [history]: its methods don't exist."""
        self.mac = mac
        self.instance_id = instance_id
        """With `mac`, who this printer is (`machine.system_info`); None leaves it out."""
        self.hostname = hostname
        self.http_requests: list[str] = []
        """Every plain HTTP path asked for, other than the WebSocket."""
        self.jobs: list[dict[str, Any]] = []
        """Moonraker's job history, oldest first."""
        self.status: dict[str, dict[str, Any]] = {
            "webhooks": {"state": "ready", "state_message": "Printer is ready"},
            "print_stats": {"state": "standby", "filename": ""},
            "toolhead": {"extruder": "extruder", "homed_axes": ""},
            "virtual_sdcard": {"progress": 0.0, "is_active": False},
            "filament_detect": {"info": []},
        }
        self.files: dict[str, bytes] = {}
        """Files under the gcodes root, by path relative to it (`.thumbs/x-300x300.png`)."""
        self.file_metadata: dict[str, dict[str, Any]] = {}
        """What server.files.metadata answers, by file name."""
        self.gcodes_root = "/home/pi/printer_data/gcodes"
        self.webcams: list[dict[str, Any]] = []
        """moonraker.conf's [webcam] sections, as server.webcams.list lists them."""
        self.calls: list[str] = []
        """Every method called, in order."""
        self.downloads: list[str] = []
        self.identified: list[dict] = []
        self.subscriptions: list[dict] = []
        self.registered: list[str] = []
        self.scripts: list[str] = []
        """Every G-code script run through printer.gcode.script."""
        self.upgrade_headers: list[dict[str, str]] = []
        self._clients: dict[Send, Callable[[], Awaitable[None]]] = {}
        self._ids = 0
        self._agents: set[str] = set()
        self._server: TestServer | None = None
        self._unix: asyncio.AbstractServer | None = None

    # -- serving -------------------------------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int | None = None) -> str:
        """Serves over HTTP; returns the base URL (http://127.0.0.1:port)."""
        app = web.Application()
        app.router.add_get("/websocket", self._websocket)
        app.router.add_get("/server/files/gcodes/{path:.+}", self._download)
        for path in ("/server/info", "/machine/system_info", "/printer/info",
                     "/printer/objects/list"):  # fmt: skip
            app.router.add_get(path, self._http_get)
        self._server = TestServer(app, host=host, port=port)
        await self._server.start_server()
        return str(self._server.make_url("")).rstrip("/")

    async def stop(self) -> None:
        """Stops serving, as a printer that went away; `start` serves again."""
        await self.close()
        self._server = None

    async def start_unix(self, path: str) -> None:
        self._unix = await asyncio.start_unix_server(self._unix_client, path=path)

    async def close(self) -> None:
        await self.drop()
        if self._server is not None:
            await self._server.close()
        if self._unix is not None:
            self._unix.close()
            await self._unix.wait_closed()

    async def _websocket(self, request: web.Request) -> web.StreamResponse:
        self.upgrade_headers.append(dict(request.headers))
        if self.reject_upgrade:
            raise web.HTTPUnauthorized()
        conn = {
            "authorized": self.api_key is None or request.headers.get("X-Api-Key") == self.api_key
        }

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        async def send(message: dict) -> None:
            await ws.send_str(json.dumps(message))

        self._clients[send] = ws.close
        try:
            async for msg in ws:
                if msg.type is WSMsgType.TEXT:
                    await self._handle(json.loads(msg.data), send, conn)
        finally:
            self._clients.pop(send, None)
            self._agents.discard(conn.get("agent"))
        return ws

    async def _download(self, request: web.Request) -> web.Response:
        path = request.match_info["path"]
        self.downloads.append(path)
        if self.api_key is not None and request.headers.get("X-Api-Key") != self.api_key:
            return web.Response(status=401)
        if path not in self.files:
            return web.Response(status=404)
        return web.Response(body=self.files[path], content_type="image/png")

    async def _http_get(self, request: web.Request) -> web.Response:
        self.http_requests.append(request.path)
        if self.api_key is not None and request.headers.get("X-Api-Key") != self.api_key:
            return web.json_response(
                {"error": {"code": 401, "message": "Unauthorized"}}, status=401
            )
        method = request.path.strip("/").replace("/", ".")
        try:
            result = self._dispatch(method, {}, {"authorized": True})
        except _RpcError as e:
            return web.json_response(
                {"error": {"code": e.code, "message": e.message}},
                status=e.code if 400 <= e.code < 600 else 500,
            )
        return web.json_response({"result": result})

    async def _unix_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        async def send(message: dict) -> None:
            writer.write(json.dumps(message).encode() + b"\x03")
            await writer.drain()

        async def close() -> None:
            writer.close()

        self._clients[send] = close
        conn = {"authorized": True}
        try:
            while True:
                try:
                    data = await reader.readuntil(b"\x03")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                await self._handle(json.loads(data[:-1]), send, conn)
        finally:
            self._clients.pop(send, None)
            self._agents.discard(conn.get("agent"))
            writer.close()

    # -- JSON-RPC ------------------------------------------------------------------------------

    async def _handle(self, request: dict, send: Send, conn: dict) -> None:
        method = request.get("method")
        params = request.get("params") or {}
        try:
            result = self._dispatch(method, params, conn)
        except _RpcError as e:
            await send(
                {
                    "jsonrpc": "2.0",
                    "error": {"code": e.code, "message": e.message},
                    "id": request.get("id"),
                }
            )
            return
        await send({"jsonrpc": "2.0", "result": result, "id": request.get("id")})

    def _dispatch(self, method: str, params: dict, conn: dict) -> Any:
        self.calls.append(method)
        if method == "server.connection.identify":
            if "api_key" in params:
                if params["api_key"] != self.api_key:
                    raise _RpcError(-32602, "Invalid API Key")
                conn["authorized"] = True
            if not conn["authorized"]:
                raise _RpcError(-32602, "Unauthorized")
            if params.get("type") == "agent":
                name = params["client_name"]
                if name in self._agents:
                    # Real Moonraker's message, stray quote included.
                    raise _RpcError(400, f"Agent '{name}' already registered and connected'")
                self._agents.add(name)
                conn["agent"] = name
            self.identified.append(params)
            self._ids += 1
            return {"connection_id": self._ids}
        if not conn["authorized"]:
            raise _RpcError(-32602, "Unauthorized")
        if method == "server.info":
            return self._server_info()
        if method == "machine.system_info":
            return {"system_info": self._system_info()}
        if method == "printer.info":
            return {"state": self.klippy_state, "hostname": self.hostname,
                    "software_version": "v0.12.0-fake"}  # fmt: skip
        if method == "printer.objects.list":
            self._require_klippy()
            return {"objects": list(self.objects)}
        if method == "printer.objects.subscribe":
            self._require_klippy()
            objects = params["objects"]
            self.subscriptions.append(objects)
            status = {}
            for name, fields in objects.items():
                current = self.status.get(name, {})
                status[name] = (
                    dict(current)
                    if fields is None
                    else {f: current[f] for f in fields if f in current}
                )
            return {"eventtime": 1.0, "status": status}
        if method == "server.history.list":
            return self._history_list(params)
        if method == "server.files.roots":
            return [
                {"name": "config", "path": "/home/pi/printer_data/config", "permissions": "rw"},
                {"name": "gcodes", "path": self.gcodes_root, "permissions": "rw"},
            ]
        if method == "server.files.metadata":
            filename = params.get("filename")
            if filename not in self.file_metadata:
                raise _RpcError(404, f"Metadata not available for <{filename}>")
            return {"filename": filename, **self.file_metadata[filename]}
        if method == "server.webcams.list":
            return {"webcams": [dict(w) for w in self.webcams]}
        if method == "printer.gcode.script":
            self._require_klippy()
            self.scripts.append(params["script"])
            return "ok"
        if method == "connection.register_remote_method":
            self.registered.append(params["method_name"])
            return "ok"
        raise _RpcError(-32601, "Method not found")

    def _history_list(self, params: dict) -> dict:
        if not self.history_enabled:
            raise _RpcError(-32601, "Method not found")
        # The same filters as Moonraker's SQL: since is start_time > since, before is
        # end_time < before, ordered by job_id, then LIMIT and OFFSET.
        jobs = list(self.jobs)
        if params.get("since") is not None:
            jobs = [j for j in jobs if j["start_time"] > params["since"]]
        if params.get("before") is not None:
            jobs = [j for j in jobs if (j["end_time"] or 0) < params["before"]]
        if str(params.get("order", "desc")).lower() == "desc":
            jobs.reverse()
        start, limit = int(params.get("start", 0)), int(params.get("limit", 50))
        if limit > 0:
            jobs = jobs[start : start + limit]
        return {"count": len(jobs), "jobs": [dict(j) for j in jobs]}

    def _require_klippy(self) -> None:
        if self.klippy_state != "ready":
            raise _RpcError(503, "Klippy Host not connected")

    def _server_info(self) -> dict:
        return {"klippy_state": self.klippy_state, "moonraker_version": "v0.9.3-fake"}

    def _system_info(self) -> dict:
        info: dict[str, Any] = {
            "cpu_info": {"model": "fake"},
            "network": {"lo": {"mac_address": "00:00:00:00:00:00", "ip_addresses": []}},
        }
        if self.mac is not None:
            address = self._server.host if self._server is not None else "127.0.0.1"
            info["network"]["wlan0"] = {
                "mac_address": self.mac,
                "ip_addresses": [
                    {"family": "ipv4", "address": address, "is_link_local": False},
                    {"family": "ipv6", "address": "fe80::1", "is_link_local": True},
                ],
            }
        if self.instance_id is not None:
            info["instance_ids"] = {"moonraker": self.instance_id, "klipper": "k-1"}
        return info

    # -- driving it from a test ----------------------------------------------------------------

    async def notify(self, method: str, *params: Any) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = list(params)
        for send in list(self._clients):
            await send(message)

    async def start_job(
        self,
        filename: str = "benchy.gcode",
        metadata: dict[str, Any] | None = None,
        *,
        notify: bool = True,
        start_time: float | None = None,
    ) -> dict[str, Any]:
        """A print starts: Moonraker adds it to history and announces it."""
        job = {
            "job_id": f"{len(self.jobs) + 1:06X}",
            "user": "No User",
            "filename": filename,
            "exists": True,
            "status": "in_progress",
            "start_time": time.time() if start_time is None else start_time,
            "end_time": None,
            "print_duration": 0.0,
            "total_duration": 0.0,
            "filament_used": 0.0,
            "metadata": dict(metadata or {}),
            "auxiliary_data": [],
        }
        self.jobs.append(job)
        if notify:
            await self.notify("notify_history_changed", {"action": "added", "job": dict(job)})
        return job

    async def finish_job(
        self,
        job: dict[str, Any],
        status: str = "completed",
        *,
        filament_used: float = 1234.5,
        print_duration: float = 600.0,
        total_duration: float = 660.0,
        notify: bool = True,
    ) -> None:
        """The print ends: Moonraker records the outcome and announces it."""
        job.update(
            status=status,
            end_time=job["start_time"] + total_duration,
            filament_used=filament_used,
            print_duration=print_duration,
            total_duration=total_duration,
        )
        if notify:
            await self.notify("notify_history_changed", {"action": "finished", "job": dict(job)})

    async def set_status(self, **objects: dict[str, Any]) -> None:
        """Klipper reports new values, sent as a status update."""
        for name, fields in objects.items():
            self.status.setdefault(name, {}).update(fields)
        await self.notify("notify_status_update", objects, time.monotonic())

    async def respond(self, line: str) -> None:
        """Klipper prints a line to the console, as `action_respond_info` does."""
        await self.notify("notify_gcode_response", line)

    async def call_remote_method(self, method: str, params: dict) -> None:
        for send in list(self._clients):
            await send({"jsonrpc": "2.0", "method": method, "params": params})

    async def drop(self) -> None:
        """Closes every client connection, as a Moonraker restart would."""
        for close in list(self._clients.values()):
            await close()

    @property
    def client_count(self) -> int:
        return len(self._clients)


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message


async def eventually(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Waits until `predicate()` is true, failing the test after `timeout` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met within the timeout")
        await asyncio.sleep(0.01)
