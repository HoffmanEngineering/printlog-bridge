"""Printers that speak SDCP v3 over a local websocket: the Elegoo Centauri Carbon.

**Built from the published protocol, not a recorded printer** (#94): `protocol.py` names the
sources and marks each assumption. One websocket, `ws://<host>:3030/websocket`, with no
authentication. On each connect the bridge asks for the attributes (Cmd 1: the firmware version,
the model) and the status (Cmd 0), then reads the status pushes (`jobs.py`) and says `ping` every
`ping_s`, since the printer drops a client that stays silent. It sends nothing else: no command
that starts, pauses or stops a print, or touches files. When a job starts, the start of its file
is read from the printer's web server (`files.py`; `files = false` turns it off). A
connection that answers no status within `answer_s` isn't an SDCP printer. Dropped, it is tried
again after `retry_s`, doubling to a minute.

The printer's mainboard id, learned from its messages, is kept in the printer's state
(`state.discovery["sdcp_mainboard_id"]`): requests name it, and discovery knows the printer by it
when its address changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import aiohttp

from printlog_bridge.adapters.common.timeouts import bounded
from printlog_bridge.core.adapter import Adapter, PrinterDetails
from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.config import BOOLEANS, ConfigError
from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.discovery import Discovery, Found
from printlog_bridge.core.events import EventSink
from printlog_bridge.core.state import MemoryState, PrinterState
from printlog_bridge.core.status import PrinterReport
from printlog_bridge.core.trace import RECEIVE, SEND, Trace

from .discovery import SdcpDiscovery
from .files import HTTP_PORT, SdcpFiles
from .jobs import JobTracker
from .protocol import (
    ATTRIBUTES,
    PING,
    PONG,
    READ_ONLY_COMMANDS,
    STATUS_REFRESH,
    WS_PATH,
    WS_PORT,
    board_of,
    kind_of,
    request,
)

log = logging.getLogger(__name__)

OPTIONS = frozenset({"host", "port", "http_port", "files"})
RETRY_S, MAX_RETRY_S = 5.0, 60.0
PING_S = 20.0
"""Under the printer's reported 60 s for a silent client."""
ANSWER_S = 15.0
CONNECT_TIMEOUT_S = 10.0
CHECK_S = 5.0
MAX_MESSAGE = 1024 * 1024
FIRMWARE_NAME = "Elegoo"
BOARD_KEY = "sdcp_mainboard_id"
TRACED_ATTRIBUTES = ("Name", "MachineName", "BrandName", "FirmwareVersion", "ProtocolVersion",
                     "MainboardID")  # fmt: skip
"""What the bridge reads of the attributes, and so all a trace keeps of them (they also name
the Wi-Fi network and the printer's MAC address)."""
_HOST_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


class NotSdcp(Exception):
    """A connection that doesn't answer as an SDCP printer does."""


class Dropped(Exception):
    """A working connection that ended."""


class SdcpAdapter(Adapter):
    name = "sdcp"

    def __init__(
        self,
        printer: str,
        host: str,
        port: int = WS_PORT,
        *,
        http_port: int = HTTP_PORT,
        files: bool = True,
    ) -> None:
        self.printer = printer
        self.host = host
        self.port = port
        self.http_port = http_port
        """The printer's web server, where the job's file is read (`files.py`)."""
        self.files = files
        self.retry_s = RETRY_S
        self.ping_s = PING_S
        self.answer_s = ANSWER_S
        self.connect_s = CONNECT_TIMEOUT_S
        self.trace: Trace | None = None
        self.state: PrinterState | None = None
        self._was_connected = False

    @classmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> SdcpAdapter:
        unknown = sorted(set(options) - OPTIONS)
        if unknown:
            raise ConfigError(
                f"[printer {printer}]: unknown option {unknown[0]!r} "
                f"(the sdcp adapter takes {', '.join(sorted(OPTIONS))})"
            )
        host = (options.get("host") or "").strip()
        if not host:
            raise ConfigError(
                f"[printer {printer}] needs host for adapter = sdcp: the printer's IP address, "
                "such as 192.168.1.77 (the printer's screen shows it under network settings)"
            )
        if not _valid_host(host):
            raise ConfigError(
                f"[printer {printer}] host must be the printer's IP address or host name, "
                f"such as 192.168.1.77, not {host!r}"
            )
        port = _port(printer, options, "port", WS_PORT)
        http_port = _port(printer, options, "http_port", HTTP_PORT)
        files = BOOLEANS.get((options.get("files") or "true").strip().lower())
        if files is None:
            raise ConfigError(
                f"[printer {printer}] files must be true or false, not {options['files']!r}"
            )
        return cls(printer, host, port, http_port=http_port, files=files)

    def describe(self) -> str:
        shown = f"[{self.host}]" if ":" in self.host else self.host
        return f"sdcp, {shown}" + ("" if self.port == WS_PORT else f":{self.port}")

    @property
    def url(self) -> str:
        shown = f"[{self.host}]" if ":" in self.host else self.host
        return f"ws://{shown}:{self.port}{WS_PATH}"

    # -- discovery and the app ------------------------------------------------------------

    @classmethod
    def discovery(cls) -> Discovery:
        return SdcpDiscovery()

    @classmethod
    def app_options(
        cls, host: str, port: int | None, serial: str | None, secrets: Mapping[str, str]
    ) -> dict[str, str]:
        """SDCP has no credentials: the address is all."""
        options = {"host": host}
        if port and port != WS_PORT:
            options["port"] = str(port)
        return options

    def _board(self) -> str | None:
        board = self.state.discovery.get(BOARD_KEY) if self.state is not None else None
        return board if isinstance(board, str) and board else None

    def watches(self, found: Found) -> bool:
        if found.kind != self.name:
            return False
        board = self._board()
        if board and found.mainboard_id:
            return board.lower() == found.mainboard_id.lower()
        return (found.address, found.port) == (self.host, self.port)

    def found_entry(self) -> Found | None:
        return Found(self.name, self.host, self.port, mainboard_id=self._board())

    def use_trace(self, trace: Trace) -> None:
        self.trace = trace

    def use_state(self, state: PrinterState) -> None:
        self.state = state

    # -- run ------------------------------------------------------------------------------

    async def run(self, sink: EventSink, report: PrinterReport) -> None:
        files = SdcpFiles(self.printer, self.host, self.http_port, enabled=self.files)
        tracker = JobTracker(
            self.printer, sink, CurrentJob(self.state or MemoryState()), files=files, report=report
        )
        retry = self.retry_s
        while True:
            try:
                await self._session(tracker, report)
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError, NotSdcp, Dropped) as e:
                if self._was_connected:
                    retry = self.retry_s
                reason = _unreachable(e, self.describe())
                log.info("[printer %s] %s; trying again in %gs", self.printer, reason, retry)
                report.disconnected(reason)
            except Exception as e:  # a bug: keep watching rather than stop the bridge
                log.exception("[printer %s] watching the printer failed unexpectedly", self.printer)
                report.disconnected(f"unexpected error: {e!r}")
                retry = MAX_RETRY_S
            finally:
                if self._was_connected and self.trace is not None:
                    self.trace.mark("close")
            await asyncio.sleep(retry)
            retry = min(retry * 2, MAX_RETRY_S)

    async def _session(self, tracker: JobTracker, report: PrinterReport) -> None:
        """Watches until the connection drops. Raises what ended it."""
        self._was_connected = False
        loop = asyncio.get_running_loop()
        async with self._socket() as ws:
            tracker.connected()
            board = self._board() or ""
            await self._send(ws, request(ATTRIBUTES, board))
            await self._send(ws, request(STATUS_REFRESH, board))
            sent = heard = loop.time()
            deadline = heard + self.answer_s
            # One receive at a time, kept across waits: `asyncio.wait` never turns a cancel
            # into a timeout, as aiohttp's `receive(timeout=...)` can on Python 3.9 (then
            # `run` would never stop).
            receiving: asyncio.Future | None = None
            try:
                while True:
                    now = loop.time()
                    if now - sent >= self.ping_s:
                        await self._send(ws, PING)
                        sent = now
                    if not self._was_connected and now >= deadline:
                        raise NotSdcp("no status in answer to the bridge's request")
                    if self._was_connected and now - heard > 3 * self.ping_s:
                        raise Dropped("the printer stopped answering")
                    wait = sent + self.ping_s - now
                    if not self._was_connected:
                        wait = min(wait, deadline - now)
                    if receiving is None:
                        receiving = asyncio.ensure_future(ws.receive())
                    done, _ = await asyncio.wait({receiving}, timeout=max(wait, 0.01))
                    if not done:
                        continue
                    msg, receiving = receiving.result(), None
                    heard = await self._on_message(msg, tracker, report, heard)
            finally:
                await _end_receive(ws, receiving)

    async def _on_message(
        self, msg: aiohttp.WSMessage, tracker: JobTracker, report: PrinterReport, heard: float
    ) -> float:
        """Handles one message; returns when the printer was last heard. Raises Dropped when
        the connection closed."""
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):  # fmt: skip
            raise Dropped("the printer closed the connection")
        if msg.type != aiohttp.WSMsgType.TEXT:
            return heard
        heard = asyncio.get_running_loop().time()
        message = self._read(msg.data)
        if message is None:
            return heard
        kind = kind_of(message)
        self._learn_board(message)
        if kind == "attributes":
            self._on_attributes(message["Attributes"], report)
        elif kind == "status":
            if not self._was_connected:
                self._was_connected = True
                if self.trace is not None:
                    self.trace.mark("open")
                report.connected()
                log.info("[printer %s] connected to %s", self.printer, self.describe())
            await tracker.on_status(message)
        return heard

    @contextlib.asynccontextmanager
    async def _socket(self) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
        """The printer's websocket (the client answers websocket pings itself). The connect and
        the upgrade together are bounded by `connect_s`: a host that accepts the connection but
        never answers the upgrade raises asyncio.TimeoutError."""
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=self.connect_s)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            ws = await bounded(http.ws_connect(self.url, max_msg_size=MAX_MESSAGE), self.connect_s)
            async with ws:
                yield ws

    async def _send(self, ws: aiohttp.ClientWebSocketResponse, text: str) -> None:
        if self.trace is not None:
            self.trace.record(SEND, text)
        await ws.send_str(text)

    def _read(self, raw: str) -> Mapping[str, Any] | None:
        """The message as JSON, traced as the bridge may keep it. None for `pong` and anything
        that isn't a JSON object."""
        body: Any = None
        if raw != PONG:
            with contextlib.suppress(ValueError):
                body = json.loads(raw)
        if self.trace is not None:
            self.trace.record(RECEIVE, _traced(body, raw))
        return body if isinstance(body, Mapping) else None

    def _learn_board(self, message: Mapping[str, Any]) -> None:
        board = board_of(message)
        if board is None or self.state is None or self._board() == board:
            return
        self.state.discovery[BOARD_KEY] = board
        self.state.save()

    def _on_attributes(self, attributes: Mapping[str, Any], report: PrinterReport) -> None:
        version = attributes.get("FirmwareVersion")
        if isinstance(version, str) and version.strip():
            report.firmware(FIRMWARE_NAME, _version(version))

    # -- details and doctor ---------------------------------------------------------------

    async def _attributes(self, wait: float) -> Mapping[str, Any]:
        """The printer's attributes, asked once on a connection of its own. Raises NotSdcp
        when none come within `wait`, and what a failed connection raises."""
        async with self._socket() as ws:
            await ws.send_str(request(ATTRIBUTES, self._board() or ""))

            async def answer() -> Mapping[str, Any]:
                while True:
                    msg = await ws.receive()
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                        aiohttp.WSMsgType.ERROR):  # fmt: skip
                            raise NotSdcp("the connection closed")
                        continue
                    with contextlib.suppress(ValueError):
                        body = json.loads(msg.data)
                        if isinstance(body, Mapping) and isinstance(
                            body.get("Attributes"), Mapping
                        ):
                            return body["Attributes"]

            try:
                return await asyncio.wait_for(answer(), wait)
            except asyncio.TimeoutError:
                raise NotSdcp("no attributes in answer to the bridge's request") from None

    async def printer_details(self) -> PrinterDetails:
        """The printer's own name and model, from its attributes."""
        try:
            attributes = await self._attributes(CHECK_S)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, NotSdcp):
            return PrinterDetails(name=self.printer, make="Elegoo")

        def text(key: str) -> str | None:
            value = attributes.get(key)
            return value.strip() or None if isinstance(value, str) else None

        return PrinterDetails(
            name=text("Name") or self.printer, make="Elegoo", model=text("MachineName")
        )

    async def check(self) -> Sequence[CheckResult]:
        name = f"Printer {self.printer}"
        wait = min(CHECK_S, self.answer_s)
        try:
            attributes = await self._attributes(wait)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, NotSdcp) as e:
            return [CheckResult.fail(name, _unreachable(e, self.describe()))]
        parts = [self.describe()]
        model = attributes.get("MachineName")
        if isinstance(model, str) and model.strip():
            parts.append(model.strip())
        version = attributes.get("FirmwareVersion")
        if isinstance(version, str) and version.strip():
            parts.append(f"firmware {_version(version)}")
        return [CheckResult.ok(name, ", ".join(parts))]


END_RECEIVE_S = 2.0


async def _end_receive(
    ws: aiohttp.ClientWebSocketResponse, receiving: asyncio.Future | None
) -> None:
    """Ends a receive still pending when the session ends, by closing the socket: aiohttp hands
    a waiting receive the close. Cancelling it instead can leave it waiting for good (seen with
    aiohttp 3.13's compiled reader on Python 3.12), and the socket's close would then wait on
    it too. Bounded either way."""
    if receiving is None:
        return
    if not receiving.done():
        with contextlib.suppress(Exception):
            await asyncio.wait({asyncio.ensure_future(ws.close())}, timeout=END_RECEIVE_S)
        await asyncio.wait({receiving}, timeout=END_RECEIVE_S)
    if not receiving.done():
        receiving.cancel()
    elif not receiving.cancelled():
        receiving.exception()  # retrieved: the session already ended for its own reason


def _port(printer: str, options: Mapping[str, str], option: str, default: int) -> int:
    text = (options.get(option) or str(default)).strip()
    try:
        port = int(text)
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        raise ConfigError(f"[printer {printer}] {option} must be a number from 1 to 65535")
    return port


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOST_NAME.fullmatch(host)) and len(host) <= 253


def _version(text: str) -> str:
    """`V1.1.29` as `1.1.29`."""
    text = text.strip()
    return text[1:] if text[:1] in ("V", "v") and text[1:2].isdigit() else text


def _unreachable(error: BaseException, where: str) -> str:
    if isinstance(error, Dropped):
        return f"{where}: {error}"
    if isinstance(error, NotSdcp):
        return f"{where} isn't answering as an SDCP printer does ({error})"
    if isinstance(error, asyncio.TimeoutError):
        return f"no answer from {where}"
    if isinstance(error, (aiohttp.ClientConnectorError, ConnectionRefusedError)):
        return f"can't reach {where}. Is it the printer's address, and is the printer on?"
    return f"can't reach {where} ({error})"


def _traced(body: Any, raw: str) -> str:
    """What a trace keeps of a message: status pushes, answers to Cmd 0 and 1 (the bridge's
    own kind of request) and `pong` in full; of the attributes only what the bridge reads; of
    anything else its kind."""
    if raw == PONG:
        return raw
    if not isinstance(body, Mapping):
        return json.dumps({"kind": "not a JSON object"})
    kind = kind_of(body)
    if kind == "status":
        return raw
    if kind == "response":
        data = body.get("Data")
        cmd = data.get("Cmd") if isinstance(data, Mapping) else None
        # Another client's answers (a file list for the slicer) may come here too.
        return raw if cmd in READ_ONLY_COMMANDS else json.dumps({"kind": kind, "Cmd": cmd})
    if kind == "attributes":
        attributes = body.get("Attributes")
        kept = {k: attributes[k] for k in TRACED_ATTRIBUTES if k in attributes}  # type: ignore[union-attr,operator]
        return json.dumps({**{k: v for k, v in body.items() if k != "Attributes"},
                           "Attributes": kept})  # fmt: skip
    return json.dumps({"kind": kind})
