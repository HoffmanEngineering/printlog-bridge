"""Anycubic printers in LAN mode: the Kobra S1 and Kobra 3 (FDM), and the Photon P1 and P1 Max
(resin), over the printer's own MQTT broker.

Each connection starts with the handshake (`handshake.py`): `/info` and a signed `/ctrl` on port
18910 give this connection's MQTT login, which changes every time and is kept in memory only.
Then MQTT over TLS to the printer (9883 on the S1, 8883 on the P1; `/ctrl` says), a subscription
to its reports, and the read-only queries of `protocol.py` now and every 30 s; `jobs.py` turns
the reports into job events. The bridge never sends anything that changes the printer: no start,
pause or stop, no setting, file, light, fan or temperature. The broker is always the configured
host, whatever `/ctrl` names, so the login never goes anywhere else.

A resin printer's broker takes only the client id `/ctrl` gives, so one client at a time:
the bridge and Photon Workshop (or Anycubic's app in LAN mode) push each other off.

The printer's serial and MAC address, learned from `/info`, are kept in the printer's state
(`state.discovery["anycubic_identity"]`) so that discovery knows the printer when its address
changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
import ssl
from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp

from printlog_bridge.adapters.common.mqtt import MqttClient, MqttClosed, MqttRefused
from printlog_bridge.adapters.common.timeouts import bounded
from printlog_bridge.core.adapter import Adapter, PrinterDetails
from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.config import ConfigError
from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.discovery import Discovery, Found
from printlog_bridge.core.events import EventSink
from printlog_bridge.core.state import MemoryState, PrinterState
from printlog_bridge.core.status import PrinterReport
from printlog_bridge.core.trace import RECEIVE, SEND, Trace

from . import protocol
from .discovery import AnycubicDiscovery
from .handshake import (
    HTTP_PORT,
    Credentials,
    HandshakeError,
    Info,
    LanModeOff,
    handshake,
)
from .jobs import JobTracker

log = logging.getLogger(__name__)

OPTIONS = frozenset({"host", "port", "mqtt_port"})
RETRY_S, MAX_RETRY_S = 5.0, 60.0
CONNECT_TIMEOUT_S = 10.0
CHECK_S = 8.0
FIRMWARE_NAME = "Anycubic"
IDENTITY_KEY = "anycubic_identity"
LAN_WARNING = "lan_mode"
TAKEOVER_WARNING = "takeover"
TAKEOVER_S = 30.0
"""A session of a fixed client id (a Photon's) that drops this soon was taken over."""
TAKEOVER_BACKOFF_S = 300.0
TAKEOVER_MESSAGE = (
    "another client (Photon Workshop, or Anycubic's app in LAN mode) took the printer's "
    "connection: a Photon takes one LAN client at a time. The bridge tries again every "
    "5 minutes; close Photon Workshop's printer page to let it watch"
)
ONE_CLIENT = (
    "the printer takes one LAN client at a time: while the bridge watches it, Photon Workshop "
    "can't connect, and connecting it pushes the bridge off for 5 minutes"
)
TRACE_DROPPED = frozenset({"urls", "ip", "fileuploadurl", "rtspurl", "rtsp", "cn", "sn", "usn",
                           "deviceid", "device_id", "clientid", "client_id", "bssid"})  # fmt: skip
"""Keys a trace never keeps of a report (`urls` holds a signed upload address), lowercased."""
TRACE_DROPPED_WORDS = frozenset({"mac", "ssid", "wifi", "wlan", "serial", "sn", "cn", "ip"})
"""Words of a key a trace never keeps: the printer's identifiers and the Wi-Fi network's."""
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_HOST_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


class Dropped(Exception):
    """A working connection that ended."""


class AnycubicAdapter(Adapter):
    name = "anycubic"

    def __init__(
        self, printer: str, host: str, port: int = HTTP_PORT, *, mqtt_port: int | None = None
    ) -> None:
        self.printer = printer
        self.host = host
        self.port = port
        self.mqtt_port = mqtt_port
        """The broker's port when set; else the one `/ctrl` names."""
        self.retry_s = RETRY_S
        self.connect_s = CONNECT_TIMEOUT_S
        self.query_s = protocol.QUERY_INTERVAL_S
        self.takeover_s = TAKEOVER_S
        self.takeover_backoff_s = TAKEOVER_BACKOFF_S
        self._connected_at = 0.0
        self._taken_over = False
        self.trace: Trace | None = None
        self.state: PrinterState | None = None
        self._was_connected = False

    @classmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> AnycubicAdapter:
        unknown = sorted(set(options) - OPTIONS)
        if unknown:
            raise ConfigError(
                f"[printer {printer}]: unknown option {unknown[0]!r} "
                f"(the anycubic adapter takes {', '.join(sorted(OPTIONS))})"
            )
        host = (options.get("host") or "").strip()
        if not host:
            raise ConfigError(
                f"[printer {printer}] needs host for adapter = anycubic: the printer's IP "
                "address, such as 192.168.1.88 (the printer's screen shows it under network "
                "settings)"
            )
        if not _valid_host(host):
            raise ConfigError(
                f"[printer {printer}] host must be the printer's IP address or host name, "
                f"such as 192.168.1.88, not {host!r}"
            )
        port = _port(printer, options, "port", HTTP_PORT)
        mqtt_port = _port(printer, options, "mqtt_port", None) if options.get("mqtt_port") else None
        return cls(printer, host, port, mqtt_port=mqtt_port)

    def describe(self) -> str:
        shown = f"[{self.host}]" if ":" in self.host else self.host
        return f"anycubic, {shown}" + ("" if self.port == HTTP_PORT else f":{self.port}")

    # -- discovery and the app ------------------------------------------------------------

    @classmethod
    def discovery(cls) -> Discovery:
        return AnycubicDiscovery()

    @classmethod
    def app_options(
        cls, host: str, port: int | None, serial: str | None, secrets: Mapping[str, str]
    ) -> dict[str, str]:
        """No credentials: the printer hands them out itself (`/ctrl`)."""
        options = {"host": host}
        if port and port != HTTP_PORT:
            options["port"] = str(port)
        return options

    def _identity(self) -> Mapping[str, Any]:
        kept = self.state.discovery.get(IDENTITY_KEY) if self.state is not None else None
        return kept if isinstance(kept, Mapping) else {}

    def watches(self, found: Found) -> bool:
        if found.kind != self.name:
            return False
        serial = self._identity().get("serial")
        if isinstance(serial, str) and serial and found.serial:
            return serial.upper() == found.serial.upper()
        return (found.address, found.port) == (self.host, self.port)

    def found_entry(self) -> Found | None:
        serial = self._identity().get("serial")
        return Found(self.name, self.host, self.port,
                     serial=serial if isinstance(serial, str) else None)  # fmt: skip

    def use_trace(self, trace: Trace) -> None:
        self.trace = trace

    def use_state(self, state: PrinterState) -> None:
        self.state = state

    def _learn(self, info: Info) -> None:
        if self.state is None or self.state.read_only or not info.serial:
            return
        kept = {"serial": info.serial, "model_id": info.model_id}
        if dict(self._identity()) != kept:
            self.state.discovery[IDENTITY_KEY] = kept
            self.state.save()

    # -- run ------------------------------------------------------------------------------

    async def run(self, sink: EventSink, report: PrinterReport) -> None:
        state = self.state or MemoryState()
        tracker: JobTracker | None = None
        retry = self.retry_s
        while True:
            self._was_connected = False
            try:
                info, credentials = await handshake(self.host, self.port, timeout=self.connect_s)
                self._learn(info)
                report.warn(LAN_WARNING, None)
                if tracker is None or tracker.resin != info.resin:
                    tracker = JobTracker(
                        self.printer, sink, CurrentJob(state), resin=info.resin, report=report
                    )
                await self._session(info, credentials, tracker, report)
            except LanModeOff as e:
                log.warning("[printer %s] %s", self.printer, e)
                report.disconnected("LAN mode is off")
                report.warn(LAN_WARNING, f"{e}.")
                retry = MAX_RETRY_S
            except (HandshakeError, MqttRefused, MqttClosed, Dropped, aiohttp.ClientError,
                    OSError, ssl.SSLError, asyncio.TimeoutError) as e:  # fmt: skip
                if self._was_connected:
                    retry = self.retry_s
                if self._was_taken_over(e, credentials_fixed=tracker is not None and tracker.resin):
                    retry = self.takeover_backoff_s
                    report.disconnected("another client took the printer's connection")
                    if not self._taken_over:
                        self._taken_over = True
                        log.warning("[printer %s] %s", self.printer, TAKEOVER_MESSAGE)
                        report.warn(TAKEOVER_WARNING, f"{TAKEOVER_MESSAGE}.")
                else:
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
            if retry < self.takeover_backoff_s:
                retry = min(retry * 2, MAX_RETRY_S)

    def _was_taken_over(self, error: BaseException, *, credentials_fixed: bool) -> bool:
        """A connection of the one client id a Photon takes that ended within seconds: another
        client connected with it (MQTT gives the first one no reason, it just ends)."""
        if not (self._was_connected and credentials_fixed):
            return False
        if not isinstance(error, (MqttClosed, Dropped)):
            return False
        return asyncio.get_running_loop().time() - self._connected_at < self.takeover_s

    async def _connect(self, credentials: Credentials) -> MqttClient:
        return await MqttClient.connect(
            self.host,  # never /ctrl's broker host: the login goes to the configured printer
            self.mqtt_port or credentials.broker_port,
            protocol.tls_context(),
            client_id=credentials.client_id or protocol.client_id(),
            username=credentials.username,
            password=credentials.password,
            timeout=self.connect_s,
        )

    async def _session(
        self, info: Info, credentials: Credentials, tracker: JobTracker, report: PrinterReport
    ) -> None:
        client = await self._connect(credentials)
        topics = protocol.Topics(info.model_id, credentials.device_id, resin=info.resin)
        self._was_connected = True
        loop = asyncio.get_running_loop()
        self._connected_at = loop.time()
        if self.trace is not None:
            self.trace.mark("open")
        report.connected()
        log.info("[printer %s] connected to %s (%s)", self.printer, self.describe(),
                 info.model_name or f"model {info.model_id}")  # fmt: skip
        tracker.connected()
        querier: asyncio.Task | None = None
        receiving: asyncio.Future | None = None
        try:
            for topic in topics.subscriptions:
                await client.subscribe(topic)
            querier = asyncio.ensure_future(self._query(client, topics, info.resin))
            while True:
                # One receive at a time, kept across waits (a packet is never cut short), and
                # the querier beside it: a query that fails ends the session at once.
                if receiving is None:
                    receiving = asyncio.ensure_future(client.receive())
                done, _ = await asyncio.wait(
                    {receiving, querier}, return_when=asyncio.FIRST_COMPLETED
                )
                if querier in done:
                    error = None if querier.cancelled() else querier.exception()
                    raise Dropped(f"sending a query failed ({error or 'stopped'})")
                topic, payload = receiving.result()
                receiving = None
                if self._taken_over and loop.time() - self._connected_at >= self.takeover_s:
                    self._taken_over = False
                    report.warn(TAKEOVER_WARNING, None)
                if not topics.is_report(topic):
                    continue
                message = self._read(payload)
                if message is None:
                    continue
                kind = message.get("type") or protocol.kind_of(topic)
                if not isinstance(kind, str) or not kind:
                    continue
                self._on_firmware(kind, message, report)
                await tracker.on_message(kind, message)
        finally:
            if querier is not None:
                querier.cancel()
            await client.close()  # a pending receive ends with the connection
            for task in (querier, receiving):
                if task is None:
                    continue
                if not task.done():
                    with contextlib.suppress(Exception):
                        await asyncio.wait({task}, timeout=2)
                    task.cancel()
                elif not task.cancelled():
                    task.exception()  # retrieved: the session already ended for its own reason

    async def _query(self, client: MqttClient, topics: protocol.Topics, resin: bool) -> None:
        """The queries now, then the periodic ones every `query_s`, until cancelled."""
        batch = protocol.queries(resin)
        while True:
            for query in batch:
                topic, text = topics.request(query)
                if self.trace is not None:
                    self.trace.record(SEND, text)
                await client.publish(topic, text.encode())
            await asyncio.sleep(self.query_s)
            batch = protocol.queries(resin, periodic=True)

    def _read(self, payload: bytes) -> Mapping[str, Any] | None:
        text = payload.decode("utf-8", errors="replace")
        try:
            body: Any = json.loads(text)
        except ValueError:
            body = None
        if self.trace is not None:
            self.trace.record(RECEIVE, traced(body))
        return body if isinstance(body, Mapping) else None

    def _on_firmware(self, kind: str, message: Mapping[str, Any], report: PrinterReport) -> None:
        version = firmware_of(kind, message)
        if version:
            report.firmware(FIRMWARE_NAME, version)

    # -- details and doctor ---------------------------------------------------------------

    async def _probe(self, wait: float) -> tuple[Info, Mapping[str, Any] | None]:
        """The handshake, a connection, and the first answer to the first query (None when
        none came within `wait`). Raises what the handshake or the connection raise."""
        info, credentials = await handshake(self.host, self.port, timeout=min(wait, 10.0))
        self._learn(info)
        client = await self._connect(credentials)
        topics = protocol.Topics(info.model_id, credentials.device_id, resin=info.resin)
        try:
            for topic in topics.subscriptions:
                await client.subscribe(topic)
            first = protocol.queries(info.resin)[0]
            topic, text = topics.request(first)
            await client.publish(topic, text.encode())

            async def answer() -> Mapping[str, Any]:
                while True:
                    topic, payload = await client.receive()
                    if not topics.is_report(topic):
                        continue
                    with contextlib.suppress(ValueError):
                        body = json.loads(payload)
                        if isinstance(body, Mapping) and body.get("type") == first.type:
                            return body

            try:
                return info, await bounded(answer(), wait)
            except asyncio.TimeoutError:
                return info, None
        finally:
            await client.close()

    async def printer_details(self) -> PrinterDetails:
        try:
            info, _ = await self._probe(CHECK_S)
        except Exception:
            return PrinterDetails(name=self.printer, make="Anycubic")
        return PrinterDetails(
            name=self.printer, make="Anycubic", model=info.model_name, slot_count=1
        )

    async def check(self) -> Sequence[CheckResult]:
        name = f"Printer {self.printer}"
        try:
            info, answer = await self._probe(CHECK_S)
        except LanModeOff as e:
            return [CheckResult.fail(name, f"{self.describe()}: {e}.")]
        except (HandshakeError, MqttRefused, MqttClosed, aiohttp.ClientError, OSError,
                ssl.SSLError, asyncio.TimeoutError) as e:  # fmt: skip
            return [CheckResult.fail(name, _unreachable(e, self.describe()))]
        parts = [self.describe(), info.model_name or f"model {info.model_id}"]
        if info.resin:
            parts.append("resin")
        version = firmware_of(protocol.queries(info.resin)[0].type, answer) if answer else None
        if version:
            parts.append(f"firmware {version}")
        if answer is None:
            return [
                CheckResult.warn(
                    name,
                    f"{', '.join(parts)}: connected to its broker, but it sent no status "
                    f"within {CHECK_S:g}s",
                )
            ]
        data = answer.get("data")
        state = data.get("state") if isinstance(data, Mapping) else answer.get("state")
        if isinstance(state, str) and state:
            parts.append(state)
        text = ", ".join(parts)
        if info.resin:
            text += f". Note: {ONE_CLIENT}"
        return [CheckResult.ok(name, text)]


def firmware_of(kind: str, message: Mapping[str, Any]) -> str | None:
    """The firmware version in an FDM printer's `info` report (`data.version`) or a resin
    printer's `lanInfo` answer (ASSUMPTION: `firmware_version` or `version`)."""
    data = message.get("data")
    if kind not in ("info", "lanInfo") or not isinstance(data, Mapping):
        return None
    for key in ("version", "firmware_version", "firmwareVersion"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def traced(body: Any) -> str:
    """What a trace keeps of a report: the whole of it but its addresses and identifiers: `urls`
    (a signed upload address), the printer's IP and MAC addresses, serial (`cn`, `sn`), device
    and client ids, and anything naming the Wi-Fi network, at any depth. Secret-looking fields
    are scrubbed by the trace itself."""
    if not isinstance(body, Mapping):
        return json.dumps({"kind": "not a JSON object"})

    def strip(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {k: strip(v) for k, v in value.items() if not _dropped(str(k))}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return json.dumps(strip(body))


def _dropped(key: str) -> bool:
    if key.lower() in TRACE_DROPPED:
        return True
    return any(w.lower() in TRACE_DROPPED_WORDS for w in _WORD.findall(key))


def _port(printer: str, options: Mapping[str, str], option: str, default: int | None) -> int:
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


def _unreachable(error: BaseException, where: str) -> str:
    if isinstance(error, (HandshakeError, Dropped)):
        return f"{where}: {error}"
    if isinstance(error, MqttRefused):
        return f"{where}: the printer's MQTT broker refused the login /ctrl gave ({error})"
    if isinstance(error, MqttClosed):
        return f"{where}: the printer's MQTT connection ended ({error})"
    if isinstance(error, asyncio.TimeoutError):
        return f"no answer from {where}"
    if isinstance(error, ConnectionRefusedError) or (
        isinstance(error, OSError) and getattr(error, "winerror", None) == 1225
    ):
        return (
            f"{where}: the printer refused the connection. Is LAN mode on (on the printer's "
            "screen)? A resin printer opens its MQTT broker only in LAN mode"
        )
    if isinstance(error, aiohttp.ClientConnectorError):
        return f"can't reach {where}. Is it the printer's address, and is the printer on?"
    if isinstance(error, ssl.SSLError):
        return f"{where}: TLS to the printer's broker failed ({error.__class__.__name__})"
    if isinstance(error, aiohttp.ClientError):
        # Never the error's text: aiohttp's names the request's URL, /ctrl's signed query too.
        return f"can't reach {where} ({error.__class__.__name__})"
    return f"can't reach {where} ({error.__class__.__name__}: {error})"
