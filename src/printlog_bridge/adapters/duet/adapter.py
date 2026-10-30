"""Duet boards running RepRapFirmware (Duet 2 WiFi/Ethernet, Duet 3 Mini 5+, 6HC, 6XD; Railcore,
E3D ToolChanger, Jubilee and many custom builds), over their HTTP API, standalone or with an SBC
(`client.py`).

RepRapFirmware has no push channel the bridge uses, so it polls the object model: every
`poll_interval` (5 s) while a job is under way, every `idle_poll_interval` (30 s) otherwise. A
board's session lapses after 8 s without a request, so between polls a cheap read of the live
fields (`keep_alive`) runs every half session timeout: rr_connect, which carries the password
over plain HTTP, is sent once per connection, and a job that starts between idle polls is
seen within seconds. Unreachable, the wait doubles from `retry_s` to a
minute; after a refused password it is five minutes. On stop the session is closed
(`rr_disconnect`), so it doesn't hold one of the board's few sessions until it lapses.

**Built from Duet3D's documentation, not a recorded board** (#89): `client.py`, `model.py`,
`jobs.py` and `files.py` mark what to check against one.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

import aiohttp

from printlog_bridge.core.adapter import Adapter, PrinterDetails
from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.config import BOOLEANS, ConfigError
from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.discovery import Discovery, Found
from printlog_bridge.core.events import EventSink
from printlog_bridge.core.secrets import scrub
from printlog_bridge.core.state import MemoryState, PrinterState
from printlog_bridge.core.status import PrinterReport
from printlog_bridge.core.trace import Trace

from . import model as om
from .client import REQUEST_TIMEOUT_S, SBC, Busy, DuetClient, NotDuet, Refused
from .discovery import DuetDiscovery
from .files import DuetFiles
from .jobs import JobTracker

log = logging.getLogger(__name__)

OPTIONS = frozenset(
    {"url", "host", "port", "password", "poll_interval", "idle_poll_interval", "files"}
)
DEFAULT_PORT = 80
DEFAULT_POLL_S = 5.0
DEFAULT_IDLE_POLL_S = 30.0
MIN_POLL_S = 1.0
RETRY_S, MAX_RETRY_S = 5.0, 60.0
REFUSED_RETRY_S = 300.0
KEEP_ALIVE_SHARE = 0.5
"""Of the board's session timeout, between keep-alive reads."""
MAX_BUSY = 5
"""Polls in a row a board may answer 503 before it counts as gone."""
ACCESS_WARNING = "access"
HOST_SOFTWARE = "DuetSoftwareFramework"
WHERE_IS_THE_PASSWORD = (
    "The password is the one set with M551 in the board's config.g (sys/config.g, which Duet "
    "Web Control edits under System). Leave password out when there is no M551."
)
_HOST_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


class DuetAdapter(Adapter):
    name = "duet"
    app_secrets = ("password",)

    def __init__(
        self,
        printer: str,
        host: str,
        *,
        port: int = DEFAULT_PORT,
        password: str | None = None,
        poll_interval: float = DEFAULT_POLL_S,
        idle_poll_interval: float = DEFAULT_IDLE_POLL_S,
        files: bool = True,
    ) -> None:
        self.printer = printer
        self.host = host
        self.port = port
        self.password = password
        self.poll_interval = poll_interval
        self.idle_poll_interval = idle_poll_interval
        self.files = files
        """Whether to ask the board for each job's file info and thumbnail."""
        self.retry_s = RETRY_S
        self.request_timeout = REQUEST_TIMEOUT_S
        self.trace: Trace | None = None
        self.state: PrinterState | None = None
        self._was_connected = False

    @property
    def base_url(self) -> str:
        shown = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{shown}" + ("" if self.port == DEFAULT_PORT else f":{self.port}")

    # -- config ---------------------------------------------------------------------------

    @classmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> DuetAdapter:
        unknown = sorted(set(options) - OPTIONS)
        if unknown:
            raise ConfigError(
                f"[printer {printer}]: unknown option {unknown[0]!r} "
                f"(the duet adapter takes {', '.join(sorted(OPTIONS))})"
            )
        values = {k: v.strip() for k, v in options.items() if k != "password"}
        url, host = values.get("url", ""), values.get("host", "")
        if url and host:
            raise ConfigError(f"[printer {printer}] takes url or host for adapter = duet, not both")
        port_text = values.get("port", "")
        if url:
            parsed = urlparse(url)
            try:
                url_port = parsed.port
            except ValueError:
                url_port = -1
            if (
                parsed.scheme != "http"
                or not parsed.hostname
                or url_port == -1
                or (parsed.path not in ("", "/") or parsed.query or parsed.username)
            ):
                raise ConfigError(
                    f"[printer {printer}] url must be the board's address, such as "
                    f"http://192.168.1.88 or http://duet3.local, not {url!r}"
                )
            if port_text:
                raise ConfigError(f"[printer {printer}] give the port in url or in port, not both")
            host = parsed.hostname
            port_text = str(url_port or DEFAULT_PORT)
        if not host:
            raise ConfigError(
                f"[printer {printer}] needs url or host for adapter = duet: the board's address, "
                "such as 192.168.1.88 or duet3.local (Duet Web Control's address)"
            )
        if not _valid_host(host):
            raise ConfigError(
                f"[printer {printer}] host must be the board's IP address or host name, such as "
                f"192.168.1.88, not {host!r} (a port goes in port)"
            )
        port_text = port_text or str(DEFAULT_PORT)
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise ConfigError(f"[printer {printer}] port must be a port number, not {port_text!r}")
        poll = _seconds(printer, values, "poll_interval", DEFAULT_POLL_S, MIN_POLL_S)
        idle = _seconds(printer, values, "idle_poll_interval", DEFAULT_IDLE_POLL_S, poll)
        files = BOOLEANS.get((values.get("files") or "true").lower())
        if files is None:
            raise ConfigError(f"[printer {printer}] files must be true or false")
        # The password is used as given (one opened from the app exactly as it opened); blank
        # is the same as none.
        password = options.get("password")
        return cls(
            printer,
            host,
            port=int(port_text),
            password=password if password is not None and password.strip() else None,
            poll_interval=poll,
            idle_poll_interval=idle,
            files=files,
        )

    def describe(self) -> str:
        return f"duet, {self.base_url}"

    # -- discovery and the app ------------------------------------------------------------

    @classmethod
    def discovery(cls) -> Discovery:
        return DuetDiscovery()

    @classmethod
    def app_options(
        cls, host: str, port: int | None, serial: str | None, secrets: Mapping[str, str]
    ) -> dict[str, str]:
        """The password is optional: a board without M551 takes any."""
        options = {"host": host, "port": str(port or DEFAULT_PORT)}
        password = secrets.get("password")
        if password is not None and password.strip():
            options["password"] = password
        return options

    def watches(self, found: Found) -> bool:
        return found.kind == self.name and (found.address, found.port) == (self.host, self.port)

    def found_entry(self) -> Found | None:
        return Found(self.name, self.host, self.port)

    def use_trace(self, trace: Trace) -> None:
        self.trace = trace

    def use_state(self, state: PrinterState) -> None:
        self.state = state

    def _client(self, trace: Trace | None = None) -> DuetClient:
        return DuetClient(
            self.base_url, password=self.password, trace=trace, timeout=self.request_timeout
        )

    # -- run ------------------------------------------------------------------------------

    async def run(self, sink: EventSink, report: PrinterReport) -> None:
        client = self._client(self.trace)
        state = self.state or MemoryState()
        tracker = JobTracker(
            self.printer,
            sink,
            CurrentJob(state),
            state,
            files=DuetFiles(self.printer, client, enabled=self.files),
            report=report,
        )
        retry = self.retry_s
        try:
            while True:
                try:
                    await self._session(client, tracker, report)
                except Refused as e:
                    message = f"{e}. {WHERE_IS_THE_PASSWORD}"
                    log.warning("[printer %s] %s", self.printer, message)
                    report.disconnected("the board refused the bridge's password")
                    report.warn(ACCESS_WARNING, message)
                    report.credentials_refused(("password",))
                    retry = REFUSED_RETRY_S
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError, NotDuet, Busy) as e:
                    if self._was_connected:
                        retry = self.retry_s
                    reason = _unreachable(e, self.base_url, self.password)
                    log.info("[printer %s] %s; trying again in %gs", self.printer, reason, retry)
                    report.disconnected(reason)
                except Exception as e:  # a bug: keep watching rather than stop the bridge
                    # Not log.exception: the traceback's message can hold a request's URL.
                    text = client.safe_error(e)
                    log.error("[printer %s] polling failed unexpectedly: %s", self.printer, text)
                    report.disconnected(f"unexpected error: {text}")
                    retry = MAX_RETRY_S
                finally:
                    if self._was_connected and self.trace is not None:
                        self.trace.mark("close")
                await asyncio.sleep(retry)
                retry = self.retry_s if retry >= REFUSED_RETRY_S else min(retry * 2, MAX_RETRY_S)
        finally:
            await client.close()

    async def _session(
        self, client: DuetClient, tracker: JobTracker, report: PrinterReport
    ) -> None:
        """Polls until the board can't be reached. Raises what ended it."""
        self._was_connected = False
        await client.close()  # a fresh session and a full read of the model
        await client.connect()
        model = await client.model()
        self._was_connected = True
        if self.trace is not None:
            self.trace.mark("open")
        report.connected()
        report.warn(ACCESS_WARNING, None)
        log.info(
            "[printer %s] connected to %s%s",
            self.printer, self.describe(), " (SBC)" if client.mode == SBC else "",
        )  # fmt: skip
        self._versions(model, report)
        tracker.connected()
        busy = 0
        while True:
            await tracker.on_model(model)
            interval = self.poll_interval if tracker.active else self.idle_poll_interval
            await self._wait(client, interval)
            try:
                model = await client.model()
                busy = 0
            except Busy:
                busy += 1
                if busy >= MAX_BUSY:
                    raise

    async def _wait(self, client: DuetClient, interval: float) -> None:
        """Waits `interval`, keeping the session alive meanwhile with a cheap read every half
        session timeout (4 s on RepRapFirmware), so it never lapses between idle polls and
        rr_connect, which carries the password, is sent once per connection. Ends early when
        that read shows the job or the state changed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + interval
        keep = max(0.05, KEEP_ALIVE_SHARE * client.session_timeout_s)
        while True:
            left = deadline - loop.time()
            if left <= keep:
                await asyncio.sleep(max(left, 0.0))
                return
            await asyncio.sleep(keep)
            try:
                if await client.keep_alive():
                    return
            except Busy:
                continue

    def _versions(self, model: Mapping[str, Any], report: PrinterReport) -> None:
        firmware = om.firmware(model)
        if firmware is not None:
            report.firmware(*firmware)
        dsf = om.dsf_version(model)
        if dsf is not None:
            report.host_software(HOST_SOFTWARE, dsf)

    # -- details and doctor ---------------------------------------------------------------

    async def _read_model(self) -> tuple[DuetClient, Mapping[str, Any]]:
        client = self._client()
        try:
            await client.connect()
            return client, await client.model()
        except BaseException:
            await client.close()
            raise

    async def printer_details(self) -> PrinterDetails:
        """The machine's name (M550) and its tools, from the object model."""
        try:
            client, model = await self._read_model()
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, NotDuet, Busy, Refused):
            return PrinterDetails(name=self.printer)
        await client.close()
        return PrinterDetails(
            name=om.machine_name(model) or self.printer,
            slot_count=om.tool_count(model),
            description=om.board_name(model),
        )

    async def check(self) -> Sequence[CheckResult]:
        name = f"Printer {self.printer}"
        try:
            client, model = await self._read_model()
        except Refused as e:
            return [CheckResult.fail(name, f"{e}. {WHERE_IS_THE_PASSWORD}")]
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, NotDuet, Busy) as e:
            return [CheckResult.fail(name, _unreachable(e, self.base_url, self.password))]
        mode = client.mode
        await client.close()
        parts = [self.describe()]
        firmware = om.firmware(model)
        board = om.board_name(model)
        if firmware is not None:
            parts.append(" ".join(firmware) + (f" on {board}" if board else ""))
        dsf = om.dsf_version(model)
        if mode == SBC or dsf is not None:
            parts.append(f"SBC mode ({HOST_SOFTWARE} {dsf or 'version unknown'})")
        status = om.status(model)
        if status:
            parts.append(status)
        return [CheckResult.ok(name, ", ".join(parts))]


def _seconds(
    printer: str, values: Mapping[str, str], option: str, default: float, minimum: float
) -> float:
    text = values.get(option)
    if not text:
        return max(default, minimum)
    try:
        seconds = float(text)
    except ValueError:
        seconds = -1.0
    if not seconds >= minimum:
        raise ConfigError(
            f"[printer {printer}] {option} must be a number of seconds, at least {minimum:g}, "
            f"not {text!r}"
        )
    return seconds


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOST_NAME.fullmatch(host)) and len(host) <= 253


def _unreachable(error: BaseException, url: str, password: str | None = None) -> str:
    """Why the board can't be had, in words safe to log: never an error's own text unscrubbed,
    which can name a request's URL, and rr_connect's holds the password."""
    if isinstance(error, Busy):
        return f"{url}: {error}"
    if isinstance(error, NotDuet):
        return f"{url} isn't answering as a Duet does ({error})"
    if isinstance(error, asyncio.TimeoutError):
        return f"no answer from {url}"
    if isinstance(error, (aiohttp.ClientConnectorError, ConnectionRefusedError)):
        return f"can't reach {url}. Is it the board's address, and is the printer on?"
    text = scrub(str(error), [password] if password else [])
    return f"can't reach {url} ({type(error).__name__}{': ' + text if text else ''})"
