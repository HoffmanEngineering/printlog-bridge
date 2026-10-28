"""Printers run by OctoPrint, over its REST API and its push socket.

On each connect: `GET /api/version` (OctoPrint's version, for the heartbeat), `GET /api/settings`
(the webcam's snapshot URL, and whether the Webhooks plugin also posts to 3D Print Log,
`webhooks.py`), `POST /api/login` with `passive` (who the API key belongs to, and a session for
the socket), then the socket, `/sockjs/websocket`, authenticated with that session. Its
`history` and `current` statuses and its `event`s drive the jobs (`jobs.py`); a job's file is
read once, as `files.py` says. A dropped connection is retried after `retry_s`, doubling to a
minute; a refused key, after five minutes. Nothing is ever sent to the printer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp

from printlog_bridge.core.adapter import Adapter, NeedsCredentials, PrinterDetails
from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.config import BOOLEANS, ConfigError
from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.discovery import Discovery, Found
from printlog_bridge.core.events import EventSink
from printlog_bridge.core.images import ImageUnavailable, fetch_image
from printlog_bridge.core.state import MemoryState, PrinterState
from printlog_bridge.core.status import PrinterReport
from printlog_bridge.core.trace import RECEIVE, SEND, Trace
from printlog_bridge.core.urls import mask_url

from . import webhooks
from .client import OctoPrintClient, Refused
from .discovery import OctoPrintDiscovery
from .files import OctoPrintFiles
from .jobs import JobTracker

log = logging.getLogger(__name__)

OPTIONS = frozenset({"url", "api_key", "files", "snapshots"})
RETRY_S, MAX_RETRY_S = 5.0, 60.0
REFUSED_RETRY_S = 300.0
THROTTLE = 4
"""`current` every 0.5 s times this."""
FIRST_STATUS_S = 15.0
SNAPSHOT_TIMEOUT_S = 10.0
ACCESS_WARNING = "access"
HOST_SOFTWARE = "OctoPrint"
WHERE_IS_THE_KEY = (
    "Make a key for the bridge in OctoPrint: Settings > Application Keys (or your user's "
    "Settings > API Key), and put it in api_key. The user it belongs to needs the Status, "
    "File List, File Download and Settings Read permissions (an admin has them all)."
)
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class UnexpectedAnswer(Exception):
    pass


class OctoPrintAdapter(Adapter):
    name = "octoprint"

    def __init__(
        self,
        printer: str,
        url: str,
        api_key: str,
        *,
        files: bool = True,
        snapshots: bool = True,
    ) -> None:
        self.printer = printer
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.files = files
        self.snapshots = snapshots
        self.retry_s = RETRY_S
        self.trace: Trace | None = None
        self.state: PrinterState | None = None
        self._snapshot_url: str | None = None

    @classmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> OctoPrintAdapter:
        unknown = sorted(set(options) - OPTIONS)
        if unknown:
            raise ConfigError(
                f"[printer {printer}]: unknown option {unknown[0]!r} "
                f"(the octoprint adapter takes {', '.join(sorted(OPTIONS))})"
            )
        url = (options.get("url") or "").strip()
        if not url:
            raise ConfigError(
                f"[printer {printer}] needs url for adapter = octoprint: OctoPrint's address, "
                "such as http://octopi.local"
            )
        parts = urlsplit(url)
        try:
            parts.port  # noqa: B018 - raises for a bad port
        except ValueError:
            parts = parts._replace(netloc="")
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.query:
            raise ConfigError(
                f"[printer {printer}] url must be OctoPrint's web address, such as "
                f"http://octopi.local or http://192.168.1.50:5000, not {url!r}"
            )
        if parts.username or parts.password:
            raise ConfigError(f"[printer {printer}] url can't hold a user or password")
        # As given: one opened from the app is used exactly as it opened. Blank is missing.
        api_key = options.get("api_key")
        if api_key is None or not api_key.strip():
            raise ConfigError(
                f"[printer {printer}] needs api_key for adapter = octoprint. {WHERE_IS_THE_KEY}"
            )
        flags = {}
        for option in ("files", "snapshots"):
            value = BOOLEANS.get((options.get(option) or "true").strip().lower())
            if value is None:
                raise ConfigError(f"[printer {printer}] {option} must be true or false")
            flags[option] = value
        return cls(printer, url, api_key, **flags)

    def describe(self) -> str:
        return f"octoprint, {mask_url(self.url)}"

    def credential_fields(self) -> tuple[str, ...]:
        return ("api_key",)

    app_secrets = ("api_key",)

    @classmethod
    def discovery(cls) -> Discovery:
        return OctoPrintDiscovery()

    @classmethod
    def app_options(
        cls, host: str, port: int | None, serial: str | None, secrets: Mapping[str, str]
    ) -> dict[str, str]:
        api_key = secrets.get("api_key")
        if api_key is None or not api_key.strip():
            raise NeedsCredentials(
                "api_key (in OctoPrint: Settings > Application Keys)", fields=("api_key",)
            )
        shown = f"[{host}]" if ":" in host else host
        port = port or 80
        return {"url": f"http://{shown}" + ("" if port == 80 else f":{port}"), "api_key": api_key}

    def _address(self) -> tuple[str, int]:
        parts = urlsplit(self.url)
        default = 443 if parts.scheme == "https" else 80
        return (parts.hostname or "", parts.port or default)

    def watches(self, found: Found) -> bool:
        return found.kind == self.name and (found.address, found.port) == self._address()

    def found_entry(self) -> Found | None:
        host, port = self._address()
        return Found(self.name, host, port)

    def use_trace(self, trace: Trace) -> None:
        self.trace = trace

    def use_state(self, state: PrinterState) -> None:
        self.state = state

    def serves(self, path: Path) -> str | None:
        """OctoPrint serves its `uploads` folder (`~/.octoprint/uploads`, beside its
        `config.yaml`) to anyone who can open it."""
        for folder in path.parents:
            if folder.name == "uploads" and (folder.parent / "config.yaml").is_file():
                return "OctoPrint serves to anyone who can open it"
        return None

    def _client(self, trace: Trace | None = None) -> OctoPrintClient:
        return OctoPrintClient(self.url, self.api_key, trace=trace)

    # -- run ------------------------------------------------------------------------------

    async def run(self, sink: EventSink, report: PrinterReport) -> None:
        client = self._client(self.trace)
        files = OctoPrintFiles(self.printer, client, report, enabled=self.files)
        tracker = JobTracker(
            self.printer,
            sink,
            CurrentJob(self.state or MemoryState()),
            files=files,
            report=report,
            snapshot=self._snapshot if self.snapshots else None,
        )
        retry = self.retry_s
        try:
            while True:
                try:
                    await self._session(client, tracker, report)
                except Refused as e:
                    message = f"{e}. {WHERE_IS_THE_KEY}"
                    log.warning("[printer %s] %s", self.printer, message)
                    report.disconnected("OctoPrint refused the bridge's API key")
                    report.warn(ACCESS_WARNING, message)
                    report.credentials_refused(self.credential_fields())
                    retry = REFUSED_RETRY_S
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError, UnexpectedAnswer) as e:
                    if self._was_connected:
                        retry = self.retry_s
                    reason = _unreachable(e, mask_url(self.url))
                    log.info("[printer %s] %s; trying again in %gs", self.printer, reason, retry)
                    report.disconnected(reason)
                except Exception as e:  # a bug: keep watching rather than stop the bridge
                    log.exception(
                        "[printer %s] watching OctoPrint failed unexpectedly", self.printer
                    )
                    report.disconnected(f"unexpected error: {e!r}")
                    retry = MAX_RETRY_S
                finally:
                    if self._was_connected and self.trace is not None:
                        self.trace.mark("close")
                await asyncio.sleep(retry)
                retry = self.retry_s if retry >= REFUSED_RETRY_S else min(retry * 2, MAX_RETRY_S)
        finally:
            await client.close()

    _was_connected = False

    async def _session(
        self, client: OctoPrintClient, tracker: JobTracker, report: PrinterReport
    ) -> None:
        """Watches until the connection drops. Raises what ended it."""
        self._was_connected = False
        status, version = await client.get_json("/api/version")
        if status != 200:
            raise UnexpectedAnswer(f"HTTP {status} from /api/version")
        await self._settings(client, report)
        user, session = await client.login()
        queue: asyncio.Queue[Mapping[str, Any] | None] = asyncio.Queue()
        async with client.socket() as ws:
            await self._receive(ws, "connected", FIRST_STATUS_S)
            auth = json.dumps({"auth": f"{user}:{session}"})
            if self.trace is not None:
                self.trace.record(SEND, json.dumps({"auth": "<user>:<session>"}))
            await ws.send_str(auth)
            await ws.send_str(json.dumps({"throttle": THROTTLE}))
            first = await self._receive(ws, "history", FIRST_STATUS_S)
            self._was_connected = True
            if self.trace is not None:
                self.trace.mark("open")
            report.connected()
            report.warn(ACCESS_WARNING, None)
            server = version.get("server") if isinstance(version, Mapping) else None
            if isinstance(server, str) and server.strip():
                report.host_software(HOST_SOFTWARE, server.strip())
            log.info("[printer %s] connected to %s", self.printer, self.describe())
            tracker.connected()
            await queue.put({"history": first})
            worker = asyncio.ensure_future(_work(queue, tracker))
            try:
                while True:
                    message = await self._next(ws)
                    if "reauthRequired" in message:
                        raise UnexpectedAnswer("OctoPrint asked to log in again")
                    if worker.done():
                        worker.result()  # raises what stopped it
                    await queue.put(message)
            finally:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker

    async def _next(self, ws: aiohttp.ClientWebSocketResponse) -> Mapping[str, Any]:
        while True:
            msg = await ws.receive()
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):  # fmt: skip
                raise UnexpectedAnswer("OctoPrint closed the socket")
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            body = None
            with contextlib.suppress(ValueError):
                body = json.loads(msg.data)
            if self.trace is not None:
                self.trace.record(RECEIVE, _traced_message(body, msg.data))
            if isinstance(body, Mapping):
                return body

    async def _receive(
        self, ws: aiohttp.ClientWebSocketResponse, kind: str, timeout: float
    ) -> Mapping[str, Any]:
        """Waits for the message `kind` (`connected`, `history`) and returns what it holds."""

        async def wait() -> Mapping[str, Any]:
            while True:
                message = await self._next(ws)
                if isinstance(message.get(kind), Mapping):
                    return message[kind]

        try:
            return await asyncio.wait_for(wait(), timeout)
        except asyncio.TimeoutError:
            raise UnexpectedAnswer(f"no {kind} message on OctoPrint's socket") from None

    async def _settings(self, client: OctoPrintClient, report: PrinterReport) -> None:
        """The webcam's snapshot URL, and the Webhooks plugin's warning. A key without the
        Settings Read permission changes nothing."""
        try:
            status, settings = await client.get_json("/api/settings")
        except Refused:
            return
        if status != 200 or not isinstance(settings, Mapping):
            return
        if webhooks.posts_to_printlog(settings):
            log.warning("[printer %s] %s", self.printer, webhooks.MESSAGE)
            report.warn(webhooks.TOPIC, webhooks.MESSAGE)
        else:
            report.warn(webhooks.TOPIC, None)
        self._snapshot_url = snapshot_url(settings, self.url)

    async def _snapshot(self) -> bytes | None:
        url = self._snapshot_url
        if url is None:
            return None
        try:
            return await fetch_image(url, timeout=SNAPSHOT_TIMEOUT_S)
        except ImageUnavailable:
            raise
        except Exception as e:  # never more than "no picture"
            raise ImageUnavailable(f"no snapshot: {type(e).__name__}") from None

    # -- details and doctor ---------------------------------------------------------------

    async def printer_details(self) -> PrinterDetails:
        """OctoPrint's name for itself (Settings > Appearance), and the default printer
        profile's model and extruders."""
        client = self._client()
        name = model = None
        slots = None
        try:
            status, settings = await client.get_json("/api/settings")
            if status == 200 and isinstance(settings, Mapping):
                appearance = settings.get("appearance")
                if isinstance(appearance, Mapping) and isinstance(appearance.get("name"), str):
                    name = appearance["name"].strip() or None
            status, profiles = await client.get_json("/api/printerprofiles")
            listed = profiles.get("profiles") if isinstance(profiles, Mapping) else None
            for profile in (listed or {}).values() if isinstance(listed, Mapping) else ():
                if isinstance(profile, Mapping) and profile.get("default") is True:
                    text = profile.get("model")
                    model = text.strip() or None if isinstance(text, str) else None
                    extruder = profile.get("extruder")
                    count = extruder.get("count") if isinstance(extruder, Mapping) else None
                    if isinstance(count, int) and count > 1 and not extruder.get("sharedNozzle"):  # type: ignore[union-attr]
                        slots = count
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, Refused):
            pass
        finally:
            await client.close()
        return PrinterDetails(name=name or self.printer, model=model, slot_count=slots)

    async def check(self) -> Sequence[CheckResult]:
        name = f"Printer {self.printer}"
        client = self._client()
        shown = mask_url(self.url)
        try:
            status, version = await client.get_json("/api/version")
            if status != 200:
                return [CheckResult.fail(name, f"{shown} answered HTTP {status}")]
            await client.login()
            printer_status, printer = await client.get_json("/api/printer")
            try:
                _, settings = await client.get_json("/api/settings")
            except Refused:
                settings = None
        except Refused as e:
            return [CheckResult.fail(name, f"{e}. {WHERE_IS_THE_KEY}")]
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError) as e:
            return [CheckResult.fail(name, _unreachable(e, shown))]
        finally:
            await client.close()
        parts = [self.describe()]
        server = version.get("server") if isinstance(version, Mapping) else None
        if isinstance(server, str):
            parts.append(f"OctoPrint {server}")
        if webhooks.posts_to_printlog(settings):
            return [CheckResult.warn(name, f"{', '.join(parts)}. {webhooks.MESSAGE}")]
        if printer_status == 409:
            return [
                CheckResult.warn(
                    name,
                    f"{', '.join(parts)}, but OctoPrint isn't connected to the printer: connect "
                    "it in OctoPrint's Connection panel (or turn on auto-connect)",
                )
            ]
        state = printer.get("state") if isinstance(printer, Mapping) else None
        text = state.get("text") if isinstance(state, Mapping) else None
        if isinstance(text, str) and text:
            parts.append(text.lower())
        return [CheckResult.ok(name, ", ".join(parts))]


async def _work(queue: asyncio.Queue, tracker: JobTracker) -> None:
    """Hands the socket's messages to the tracker, in order, apart from the reading, so a
    file read at a job's start doesn't hold up the socket."""
    while True:
        message = await queue.get()
        if message is None:
            return
        if "history" in message or "current" in message:
            status = message.get("history") or message.get("current")
            if isinstance(status, Mapping):
                await tracker.on_status(status)
        elif isinstance(message.get("event"), Mapping):
            event = message["event"]
            if isinstance(event.get("type"), str):
                await tracker.on_event(event["type"], event.get("payload"))


def snapshot_url(settings: Mapping[str, Any], base_url: str) -> str | None:
    """Where to GET a webcam frame: the classic webcam's `snapshotUrl` when the webcam is on, or
    the first webcam that can take one (OctoPrint 1.9's `webcams`). A loopback address (OctoPi's
    `http://127.0.0.1:8080/?action=snapshot`) means OctoPrint's own host; a path is on its web
    server."""
    webcam = settings.get("webcam")
    if not isinstance(webcam, Mapping) or webcam.get("webcamEnabled") is False:
        return None
    candidates: list[Any] = [webcam.get("snapshotUrl")]
    for cam in webcam.get("webcams") or ():
        if isinstance(cam, Mapping) and cam.get("canSnapshot") is not False:
            compat = cam.get("compat")
            if isinstance(compat, Mapping):
                candidates.append(compat.get("snapshot"))
    for url in candidates:
        if isinstance(url, str) and url.strip():
            return _resolve(url.strip(), base_url)
    return None


def _resolve(url: str, base_url: str) -> str:
    parts = urlsplit(url)
    base = urlsplit(base_url)
    if parts.scheme not in ("http", "https"):
        return urljoin(base_url.rstrip("/") + "/", url)
    if (parts.hostname or "").lower() in LOOPBACK and base.hostname:
        host = f"[{base.hostname}]" if ":" in base.hostname else base.hostname
        netloc = host + (f":{parts.port}" if parts.port else "")
        return parts._replace(netloc=netloc).geturl()
    return url


def _unreachable(error: BaseException, url: str) -> str:
    if isinstance(error, asyncio.TimeoutError):
        return f"no answer from {url}"
    if isinstance(error, UnexpectedAnswer):
        return f"{url} isn't answering like OctoPrint ({error})"
    if isinstance(error, (aiohttp.ClientConnectorError, ConnectionRefusedError)):
        return f"can't reach {url}. Is it OctoPrint's address, and is it running?"
    return f"can't reach {url} ({error})"


TRACED_MESSAGES = ("connected", "current", "history", "event", "reauthRequired")


def _traced_message(body: Any, raw: str) -> str:
    """What the trace keeps of a socket message: in full only the kinds the bridge reads.
    Other plugins' messages (`plugin`) and the rest are named, not kept: they can hold
    anything, a token among them."""
    if not isinstance(body, Mapping):
        return "(not a JSON object)"
    if any(key in TRACED_MESSAGES for key in body):
        return raw
    kinds = sorted(str(key) for key in body)
    plugin = body.get("plugin")
    name = plugin.get("plugin") if isinstance(plugin, Mapping) else None
    return json.dumps({"type": kinds, "plugin": name} if name else {"type": kinds})
