"""One long-lived connection to Moonraker's JSON-RPC API, kept up across restarts.

Local mode uses Moonraker's Unix socket, where each message ends with an ETX byte. Remote mode
uses its WebSocket. Either way the bridge identifies as an agent, so it appears in Moonraker's
extensions list, then subscribes to the printer objects a job needs and passes status and
history notifications to a listener.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse, urlunparse

import aiohttp

from printlog_bridge import __version__

log = logging.getLogger(__name__)

CLIENT_NAME = "printlog-bridge"
REPO_URL = "https://github.com/HoffmanEngineering/printlog-bridge"
REQUEST_TIMEOUT_S = 10
CONNECT_TIMEOUT_S = 10
UNIX_READ_LIMIT = 4 * 1024 * 1024
ETX = b"\x03"

SUBSCRIPTION: Mapping[str, list[str] | None] = {
    "print_stats": None,
    "toolhead": ["extruder"],  # the active tool
    "virtual_sdcard": None,
    "webhooks": None,
}
OPTIONAL_SUBSCRIPTION: Mapping[str, list[str] | None] = {
    "filament_detect": None,  # Snapmaker U1's RFID reader
}

RemoteMethod = Callable[[Mapping[str, Any]], Awaitable[None]]


class MoonrakerListener(Protocol):
    async def on_klippy_state(self, state: str) -> None:
        """Klipper became `ready`, or stopped being (`startup`, `shutdown`, `disconnected`...)."""

    async def on_status(self, status: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        """`status` is every subscribed object's latest fields; `changed` is just this update."""

    async def on_history(self, action: str, job: Mapping[str, Any]) -> None:
        """A job was `added` to or `finished` in Moonraker's history: the authoritative record."""


class MoonrakerAuthError(Exception):
    """Moonraker refused the bridge. Retrying won't help until its key or trust changes."""


class MoonrakerRpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{message} ({code})")
        self.code = code
        self.message = message

    @property
    def is_duplicate_agent(self) -> bool:
        return "already registered" in self.message

    @property
    def is_auth_failure(self) -> bool:
        # Moonraker reports both as -32602 (invalid params), so the message is what tells.
        return self.code in (401, 403) or self.message in ("Unauthorized", "Invalid API Key")


def rejection(printer: str, where: str, detail: str) -> MoonrakerAuthError:
    return MoonrakerAuthError(
        f"Moonraker at {where} rejected the bridge ({detail}). "
        f"Set moonraker_api_key in [printer {printer}] to Moonraker's API key, "
        "or add this host to [authorization] trusted_clients in moonraker.conf."
    )


async def fetch_server_info(
    printer: str, *, url: str | None, socket: Path | None, api_key: str | None
) -> Mapping[str, Any]:
    """One `server.info` call over the same connection `run` uses, without identifying."""
    transport = await _open_transport(printer, url, socket, api_key)
    rpc = _JsonRpc(transport)
    try:
        return await rpc.call("server.info")
    except MoonrakerRpcError as e:
        if e.is_auth_failure:
            raise rejection(printer, url or str(socket), e.message) from e
        raise
    finally:
        await rpc.close()
        await transport.close()


async def _open_transport(
    printer: str, url: str | None, socket: Path | None, api_key: str | None
) -> _Transport:
    if socket is not None:
        return await _UnixTransport.open(socket)
    target = url or ""
    return await _WebSocketTransport.open(
        websocket_url(target), api_key, lambda detail: rejection(printer, target, detail)
    )


class ConnectionLost(Exception):
    pass


class AlreadyConnected(Exception):
    """Moonraker allows one agent per name, and another bridge holds it."""


class Backoff:
    """Delays between reconnect attempts: doubling from `initial`, capped at `maximum`."""

    def __init__(self, initial: float = 1, maximum: float = 60) -> None:
        self.initial = initial
        self.maximum = maximum
        self._next = initial

    def next(self) -> float:
        delay = self._next
        self._next = min(self._next * 2, self.maximum)
        return delay

    def reset(self) -> None:
        self._next = self.initial


def websocket_url(url: str) -> str:
    """Moonraker's WebSocket endpoint for a configured http(s) or ws(s) URL."""
    parsed = urlparse(url)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    path = parsed.path.rstrip("/")
    if not path.endswith("/websocket"):
        path += "/websocket"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


class MoonrakerSession:
    def __init__(
        self,
        printer: str,
        *,
        listener: MoonrakerListener,
        url: str | None = None,
        socket: Path | None = None,
        api_key: str | None = None,
        remote_methods: Mapping[str, RemoteMethod] | None = None,
        backoff: Backoff | None = None,
    ) -> None:
        if (url is None) == (socket is None):
            raise ValueError("pass exactly one of url or socket")
        self.printer = printer
        self.listener = listener
        self.url = url
        self.socket = socket
        self.api_key = api_key
        self.remote_methods = dict(remote_methods or {})
        self.backoff = backoff or Backoff()
        self.status: dict[str, dict[str, Any]] = {}

    @property
    def where(self) -> str:
        return self.url if self.url is not None else str(self.socket)

    async def run(self) -> None:
        """Stays connected until cancelled, reconnecting with backoff whenever the link drops."""
        while True:
            try:
                transport = await self._open()
                try:
                    await self._serve(_JsonRpc(transport))
                finally:
                    await transport.close()
            except MoonrakerAuthError as e:
                log.error("[printer %s] %s", self.printer, e)
            except AlreadyConnected as e:
                log.warning("[printer %s] %s", self.printer, e)
            except (ConnectionLost, MoonrakerRpcError, aiohttp.ClientError, OSError) as e:
                log.warning("[printer %s] Moonraker at %s: %s", self.printer, self.where, _why(e))
            except asyncio.TimeoutError:
                log.warning("[printer %s] Moonraker at %s didn't answer", self.printer, self.where)

            delay = self.backoff.next()
            log.info("[printer %s] reconnecting in %gs", self.printer, delay)
            await asyncio.sleep(delay)

    async def _open(self) -> _Transport:
        return await _open_transport(self.printer, self.url, self.socket, self.api_key)

    async def _serve(self, rpc: _JsonRpc) -> None:
        try:
            identity = {
                "client_name": CLIENT_NAME,
                "version": __version__,
                "type": "agent",
                "url": REPO_URL,
            }
            if self.api_key:
                identity["api_key"] = self.api_key
            try:
                await rpc.call("server.connection.identify", identity)
            except MoonrakerRpcError as e:
                if e.is_auth_failure:
                    raise rejection(self.printer, self.where, e.message) from e
                if e.is_duplicate_agent:
                    raise AlreadyConnected(
                        f"another printlog-bridge is already connected to Moonraker at "
                        f"{self.where}, and Moonraker allows only one. Stop the other one "
                        "(check other hosts and Docker) and this one takes over."
                    ) from e
                raise

            log.info("[printer %s] connected to Moonraker at %s", self.printer, self.where)
            self.backoff.reset()

            for name in self.remote_methods:
                await rpc.call("connection.register_remote_method", {"method_name": name})

            info = await rpc.call("server.info")
            klippy = info.get("klippy_state", "unknown")
            if klippy == "ready":
                await self._subscribe(rpc)
            else:
                await self.listener.on_klippy_state(klippy)

            async for method, params in rpc.notifications():
                await self._notified(rpc, method, params)
        finally:
            await rpc.close()

    async def _notified(self, rpc: _JsonRpc, method: str, params: Any) -> None:
        if method == "notify_status_update":
            changed = params[0]
            for name, fields in changed.items():
                self.status.setdefault(name, {}).update(fields)
            await self.listener.on_status(self.status, changed)
        elif method == "notify_history_changed":
            await self.listener.on_history(params[0]["action"], params[0]["job"])
        elif method == "notify_klippy_ready":
            await self._subscribe(rpc)
        elif method in ("notify_klippy_shutdown", "notify_klippy_disconnected"):
            await self.listener.on_klippy_state(method[len("notify_klippy_") :])
        elif method in self.remote_methods:
            await self.remote_methods[method](params or {})

    async def _subscribe(self, rpc: _JsonRpc) -> None:
        try:
            available = set((await rpc.call("printer.objects.list"))["objects"])
            objects = dict(SUBSCRIPTION)
            objects.update({k: v for k, v in OPTIONAL_SUBSCRIPTION.items() if k in available})
            result = await rpc.call("printer.objects.subscribe", {"objects": objects})
        except MoonrakerRpcError as e:
            # Klipper went away between ready and subscribe; its next ready notification retries.
            log.warning("[printer %s] couldn't subscribe: %s", self.printer, e)
            return

        self.status = {name: dict(fields) for name, fields in result["status"].items()}
        await self.listener.on_klippy_state("ready")
        await self.listener.on_status(self.status, self.status)


class _JsonRpc:
    """Matches responses to requests, and queues everything else as notifications in order."""

    def __init__(self, transport: _Transport) -> None:
        self._transport = transport
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._notifications: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._reader = asyncio.ensure_future(self._read())

    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        if self._closed:
            raise ConnectionLost("connection closed")
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        try:
            await self._transport.send(message)
            return await asyncio.wait_for(future, REQUEST_TIMEOUT_S)
        finally:
            self._pending.pop(request_id, None)

    async def notifications(self) -> AsyncIterator[tuple[str, Any]]:
        while True:
            item = await self._notifications.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader

    async def _read(self) -> None:
        reason: BaseException = ConnectionLost("Moonraker closed the connection")
        try:
            while True:
                message = await self._transport.receive()
                if message is None:
                    break
                self._dispatch(message)
        except (aiohttp.ClientError, OSError, ValueError) as e:
            reason = ConnectionLost(_why(e))
        finally:
            self._closed = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(reason)
            self._notifications.put_nowait(None)

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            future = self._pending.get(message["id"])
            if future is None or future.done():
                return
            if "error" in message:
                error = message["error"]
                future.set_exception(
                    MoonrakerRpcError(error.get("code", 0), error.get("message", ""))
                )
            else:
                future.set_result(message["result"])
        elif "method" in message:
            self._notifications.put_nowait((message["method"], message.get("params")))


class _Transport(Protocol):
    async def send(self, message: Mapping[str, Any]) -> None: ...

    async def receive(self) -> Mapping[str, Any] | None:
        """The next message, or None once the connection is closed."""

    async def close(self) -> None: ...


class _WebSocketTransport:
    def __init__(self, session: aiohttp.ClientSession, ws: aiohttp.ClientWebSocketResponse):
        self._session = session
        self._ws = ws

    @classmethod
    async def open(
        cls, url: str, api_key: str | None, rejected: Callable[[str], Exception]
    ) -> _WebSocketTransport:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        headers = {"X-Api-Key": api_key} if api_key else {}
        try:
            ws = await asyncio.wait_for(
                session.ws_connect(url, headers=headers, heartbeat=30), CONNECT_TIMEOUT_S
            )
        except aiohttp.WSServerHandshakeError as e:
            await session.close()
            if e.status in (401, 403):
                raise rejected(f"HTTP {e.status}") from e
            raise
        except BaseException:
            await session.close()
            raise
        return cls(session, ws)

    async def send(self, message: Mapping[str, Any]) -> None:
        await self._ws.send_str(json.dumps(message))

    async def receive(self) -> Mapping[str, Any] | None:
        while True:
            msg = await self._ws.receive()
            if msg.type is aiohttp.WSMsgType.TEXT:
                return json.loads(msg.data)
            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                return None

    async def close(self) -> None:
        await self._ws.close()
        await self._session.close()


class _UnixTransport:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def open(cls, path: Path) -> _UnixTransport:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path), limit=UNIX_READ_LIMIT), CONNECT_TIMEOUT_S
        )
        return cls(reader, writer)

    async def send(self, message: Mapping[str, Any]) -> None:
        self._writer.write(json.dumps(message).encode() + ETX)
        await self._writer.drain()

    async def receive(self) -> Mapping[str, Any] | None:
        try:
            data = await self._reader.readuntil(ETX)
        except asyncio.IncompleteReadError:
            return None
        return json.loads(data[:-1])

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await self._writer.wait_closed()


def _why(error: BaseException) -> str:
    return str(error) or type(error).__name__
