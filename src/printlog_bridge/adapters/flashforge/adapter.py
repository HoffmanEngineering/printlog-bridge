"""FlashForge Adventurer 5M, 5M Pro and AD5X printers, over their LAN HTTP API on port 8898
(`protocol.py`), with the serial number and the check code the printer shows in LAN mode.

The API has no push channel, so the bridge polls `/detail`: every `poll_interval` (5 s) while a
job is under way or about to be, every `idle_poll_interval` (30 s) otherwise. Unreachable, the
wait doubles from `retry_s` to a minute; after a refused check code it is five minutes. Every
request is a read (`protocol.READ_ONLY`): the bridge never sends `/control`, `/printGcode` or
`/uploadGcode`, and never touches the legacy TCP port 8899 beyond `doctor` seeing whether it
listens.

Printers on the Klipper Mod, Forge-X or ZMOD firmware run Moonraker: `doctor` says to use
`adapter = moonraker` when the HTTP API doesn't answer but Moonraker does.

**Built from community sources, not a recorded printer** (#88): `protocol.py` and `jobs.py` mark
what to check against one.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp

from printlog_bridge.adapters.common.timeouts import bounded
from printlog_bridge.core.adapter import Adapter, NeedsCredentials, PrinterDetails
from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.config import BOOLEANS, ConfigError
from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.discovery import Discovery, Found
from printlog_bridge.core.events import EventSink, SpoolReading, SpoolSink
from printlog_bridge.core.secrets import scrub
from printlog_bridge.core.state import MemoryState, PrinterState
from printlog_bridge.core.status import PrinterReport
from printlog_bridge.core.trace import Trace

from .client import REQUEST_TIMEOUT_S, FlashForgeClient, NotFlashForge, Refused
from .discovery import FlashForgeDiscovery
from .jobs import JobTracker
from .protocol import HTTP_PORT, MOONRAKER_PORT, TCP_PORT, filament_by_slot, view_of

log = logging.getLogger(__name__)

OPTIONS = frozenset(
    {"host", "port", "serial", "check_code", "poll_interval", "idle_poll_interval", "files"}
)
DEFAULT_POLL_S = 5.0
DEFAULT_IDLE_POLL_S = 30.0
MIN_POLL_S = 1.0
RETRY_S, MAX_RETRY_S = 5.0, 60.0
REFUSED_RETRY_S = 300.0
PROBE_S = 3.0
"""How long `doctor` waits on Moonraker and the legacy port when the HTTP API doesn't answer."""
ACCESS_WARNING = "access"
FIRMWARE_WARNING = "firmware"
MAKE = "FlashForge"
WHERE_IS_THE_CHECK_CODE = (
    "The serial number and check code are on the printer's screen: Settings > Network, turn on "
    "LAN-only mode, and the screen shows the check code (the serial number is under Settings > "
    "About, and starts with SN)."
)
TOO_OLD = (
    "the printer answers only on FlashForge's old TCP port (8899), not the HTTP API on 8898: "
    "update its firmware (2.x or newer on an Adventurer 5M or 5M Pro). The bridge doesn't use "
    "the old port, which takes control of the printer away from FlashPrint and Orca"
)
MOONRAKER = (
    "the printer runs Moonraker on port 7125 (the Klipper Mod, Forge-X or ZMOD firmware): "
    "watch it with adapter = moonraker and url = http://{host}:7125 instead"
)
_HOST_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
_SERIAL = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class FlashForgeAdapter(Adapter):
    name = "flashforge"
    app_secrets = ("check_code",)

    def __init__(
        self,
        printer: str,
        host: str,
        *,
        serial: str,
        check_code: str,
        port: int = HTTP_PORT,
        poll_interval: float = DEFAULT_POLL_S,
        idle_poll_interval: float = DEFAULT_IDLE_POLL_S,
        files: bool = True,
    ) -> None:
        self.printer = printer
        self.host = host
        self.serial = serial
        self.check_code = check_code
        self.port = port
        self.poll_interval = poll_interval
        self.idle_poll_interval = idle_poll_interval
        self.files = files
        """Whether to ask the printer for each job's file list entry and thumbnail."""
        self.retry_s = RETRY_S
        self.request_timeout = REQUEST_TIMEOUT_S
        self.probe_s = PROBE_S
        self.moonraker_port = MOONRAKER_PORT
        self.tcp_port = TCP_PORT
        self.trace: Trace | None = None
        self.state: PrinterState | None = None
        self.spools: SpoolSink | None = None
        self._was_connected = False

    @property
    def base_url(self) -> str:
        shown = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{shown}:{self.port}"

    # -- config ---------------------------------------------------------------------------

    @classmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> FlashForgeAdapter:
        unknown = sorted(set(options) - OPTIONS)
        if unknown:
            raise ConfigError(
                f"[printer {printer}]: unknown option {unknown[0]!r} "
                f"(the flashforge adapter takes {', '.join(sorted(OPTIONS))})"
            )
        values = {k: v.strip() for k, v in options.items() if k != "check_code"}
        host = values.get("host", "")
        if not host:
            raise ConfigError(
                f"[printer {printer}] needs host for adapter = flashforge: the printer's IP "
                "address, shown in its network settings"
            )
        if not _valid_host(host):
            raise ConfigError(
                f"[printer {printer}] host must be the printer's IP address or host name, such "
                f"as 192.168.1.60, not {host!r} (a port goes in port)"
            )
        port_text = values.get("port") or str(HTTP_PORT)
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise ConfigError(f"[printer {printer}] port must be a port number, not {port_text!r}")
        serial = values.get("serial", "")
        if not serial or not _SERIAL.fullmatch(serial):
            raise ConfigError(
                f"[printer {printer}] needs serial for adapter = flashforge: the printer's "
                "serial number, such as SNMOMC9900728 (Settings > About on its screen; "
                "printlog-bridge discover lists it)"
            )
        # The check code is used as given (one opened from the app exactly as it opened).
        check_code = options.get("check_code")
        if check_code is None or not check_code.strip():
            raise ConfigError(
                f"[printer {printer}] needs check_code for adapter = flashforge. "
                f"{WHERE_IS_THE_CHECK_CODE}"
            )
        poll = _seconds(printer, values, "poll_interval", DEFAULT_POLL_S, MIN_POLL_S)
        idle = _seconds(printer, values, "idle_poll_interval", DEFAULT_IDLE_POLL_S, poll)
        files = BOOLEANS.get((values.get("files") or "true").lower())
        if files is None:
            raise ConfigError(f"[printer {printer}] files must be true or false")
        return cls(
            printer,
            host,
            serial=serial,
            check_code=check_code,
            port=int(port_text),
            poll_interval=poll,
            idle_poll_interval=idle,
            files=files,
        )

    def describe(self) -> str:
        return f"flashforge, {self.base_url}"

    # -- discovery and the app ------------------------------------------------------------

    @classmethod
    def discovery(cls) -> Discovery:
        return FlashForgeDiscovery()

    @classmethod
    def app_options(
        cls, host: str, port: int | None, serial: str | None, secrets: Mapping[str, str]
    ) -> dict[str, str]:
        if not serial:
            raise NeedsCredentials(
                "a serial number, which 3D Print Log sends for a printer found on the network"
            )
        check_code = secrets.get("check_code")
        if check_code is None or not check_code.strip():
            raise NeedsCredentials(
                "check_code (on the printer's screen: Settings > Network, LAN-only mode)",
                fields=("check_code",),
            )
        options = {"host": host, "serial": serial, "check_code": check_code}
        if port and port != HTTP_PORT:
            options["port"] = str(port)
        return options

    def watches(self, found: Found) -> bool:
        if found.kind != self.name:
            return False
        if found.serial:
            return found.serial.upper() == self.serial.upper()
        return (found.address, found.port) == (self.host, self.port)

    def found_entry(self) -> Found | None:
        return Found(self.name, self.host, self.port, serial=self.serial)

    def use_trace(self, trace: Trace) -> None:
        self.trace = trace

    def use_state(self, state: PrinterState) -> None:
        self.state = state

    def use_spools(self, spools: SpoolSink, api_url: str | None = None) -> None:
        self.spools = spools

    def _client(self, trace: Trace | None = None) -> FlashForgeClient:
        return FlashForgeClient(
            self.base_url,
            serial=self.serial,
            check_code=self.check_code,
            trace=trace,
            timeout=self.request_timeout,
        )

    # -- run ------------------------------------------------------------------------------

    async def run(self, sink: EventSink, report: PrinterReport) -> None:
        client = self._client(self.trace)
        state = self.state or MemoryState()
        spools = self.spools

        async def reading(event: SpoolReading) -> Any:
            assert spools is not None
            return await spools.spool_reading(event)

        tracker = JobTracker(
            self.printer,
            sink,
            CurrentJob(state),
            files=_Files(self.printer, client) if self.files else None,
            report=report,
            spools=reading if spools is not None else None,
        )
        retry = self.retry_s
        try:
            while True:
                try:
                    await self._session(client, tracker, report)
                except Refused as e:
                    message = f"{e}. {WHERE_IS_THE_CHECK_CODE}"
                    log.warning("[printer %s] %s", self.printer, message)
                    report.disconnected("the printer refused the serial number or check code")
                    report.warn(ACCESS_WARNING, message)
                    report.credentials_refused(("check_code",))
                    retry = REFUSED_RETRY_S
                except NotFlashForge as e:
                    message = f"{self.base_url} isn't answering as FlashForge's HTTP API ({e})"
                    log.warning("[printer %s] %s", self.printer, message)
                    report.disconnected(message)
                    report.warn(FIRMWARE_WARNING, f"{message}. Run printlog-bridge doctor.")
                    if self._was_connected:  # it answered a moment ago: a hiccup, likely
                        retry = self.retry_s
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                    if self._was_connected:
                        retry = self.retry_s
                    reason = _unreachable(e, self.base_url, self.check_code)
                    log.info("[printer %s] %s; trying again in %gs", self.printer, reason, retry)
                    report.disconnected(reason)
                except Exception as e:  # a bug: keep watching rather than stop the bridge
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
        self, client: FlashForgeClient, tracker: JobTracker, report: PrinterReport
    ) -> None:
        """Polls until the printer can't be reached. Raises what ended it."""
        self._was_connected = False
        detail = await client.detail()
        self._was_connected = True
        if self.trace is not None:
            self.trace.mark("open")
        report.connected()
        report.warn(ACCESS_WARNING, None)
        report.warn(FIRMWARE_WARNING, None)
        log.info("[printer %s] connected to %s", self.printer, self.describe())
        firmware = view_of(detail).firmware
        if firmware:
            report.firmware(MAKE, firmware)
        tracker.connected()
        while True:
            await tracker.on_detail(detail)
            interval = self.poll_interval if tracker.active else self.idle_poll_interval
            await asyncio.sleep(interval)
            detail = await client.detail()

    # -- details and doctor ---------------------------------------------------------------

    async def _detail(self) -> Mapping[str, Any]:
        client = self._client()
        try:
            return await client.detail()
        finally:
            await client.close()

    async def printer_details(self) -> PrinterDetails:
        try:
            detail = await self._detail()
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, NotFlashForge, Refused):
            return PrinterDetails(name=self.printer, make=MAKE)
        view = view_of(detail)
        return PrinterDetails(
            name=view.name_on_screen or self.printer,
            make=MAKE,
            model=view.model,
            slot_count=view.slot_count or 1,
        )

    async def check(self) -> Sequence[CheckResult]:
        name = f"Printer {self.printer}"
        try:
            detail = await self._detail()
        except Refused as e:
            return [CheckResult.fail(name, f"{e}. {WHERE_IS_THE_CHECK_CODE}")]
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, NotFlashForge) as e:
            other = await self._other_firmware()
            if other is not None:
                return [CheckResult.fail(name, f"{self.base_url}: {other}")]
            if isinstance(e, NotFlashForge):
                return [CheckResult.fail(name, f"{self.base_url} isn't FlashForge's API ({e})")]
            return [CheckResult.fail(name, _unreachable(e, self.base_url, self.check_code))]
        view = view_of(detail)
        parts = [self.describe()]
        firmware = f"firmware {view.firmware}" if view.firmware else None
        if view.model or firmware:
            parts.append(" ".join(p for p in (view.model, firmware) if p))
        if view.slot_count:
            parts.append(f"IFS with {view.slot_count} slots")
        if view.state:
            parts.append(view.state)
        return [CheckResult.ok(name, ", ".join(parts))]

    async def _other_firmware(self) -> str | None:
        """Why the HTTP API doesn't answer, when something else on the printer says: Moonraker
        (a Klipper firmware), or only the legacy TCP port (firmware too old). Asks Moonraker
        `GET /server/info`; only connects to 8899, sending nothing."""
        shown = f"[{self.host}]" if ":" in self.host else self.host
        try:
            async with (
                aiohttp.ClientSession() as http,
                http.get(
                    f"http://{shown}:{self.moonraker_port}/server/info",
                    timeout=aiohttp.ClientTimeout(total=self.probe_s),
                    allow_redirects=False,
                ) as response,
            ):
                body = await response.json(content_type=None)
            if isinstance(body, Mapping) and isinstance(body.get("result"), Mapping):
                return MOONRAKER.format(host=self.host)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError):
            pass
        try:
            _, writer = await bounded(
                asyncio.open_connection(self.host, self.tcp_port), self.probe_s
            )
        except (OSError, asyncio.TimeoutError):
            return None
        writer.close()
        with contextlib.suppress(Exception):
            await bounded(writer.wait_closed(), 1.0)
        return TOO_OLD


class _Files:
    """The job's IFS slots from `/gcodeList` and its thumbnail from `/gcodeThumb`. A failure
    logs the job without them, never an error."""

    def __init__(self, printer: str, client: FlashForgeClient) -> None:
        self.printer = printer
        self.client = client

    async def grams_by_slot(self, path: str) -> dict[int, float]:
        try:
            return filament_by_slot(await self.client.gcode_list(), path)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, NotFlashForge, Refused) as e:
            log.info(
                "[printer %s] can't read the file list (%s): the job's grams aren't split "
                "by slot", self.printer, self.client.safe_error(e),
            )  # fmt: skip
            return {}

    async def thumbnail(self, path: str) -> bytes | None:
        try:
            return await self.client.thumbnail(path)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, NotFlashForge, Refused):
            return None


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


def _unreachable(error: BaseException, url: str, check_code: str | None = None) -> str:
    """Why the printer can't be had, in words safe to log."""
    if isinstance(error, asyncio.TimeoutError):
        return f"no answer from {url}"
    if isinstance(error, (aiohttp.ClientConnectorError, ConnectionRefusedError)):
        return f"can't reach {url}. Is it the printer's address, and is the printer on?"
    text = scrub(str(error), [check_code] if check_code else [])
    return f"can't reach {url} ({type(error).__name__}{': ' + text if text else ''})"
