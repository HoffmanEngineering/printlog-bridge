"""Prusa printers with PrusaLink built in (MK4/MK4S, MK3.5/3.9, Core One, MINI, XL), over its
local HTTP API.

PrusaLink has no push channel, so the bridge polls `/api/v1/status`: every `poll_interval`
(5 s) while a job is under way or about to be, every `idle_poll_interval` (30 s) otherwise.
A finished or stopped job stays on the printer's screen, and in its status, until someone
dismisses it, so the slower poll between jobs loses nothing. When the printer can't be reached
the wait doubles from `retry_s` to a minute; after refused credentials it is five minutes.
`/api/version` is read once per connect for the firmware, `/api/v1/job` once per job for its
file, and the file itself as `files.py` says. Every request is a GET: nothing is ever sent to
the printer or written to it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

import aiohttp

from printlog_bridge.core.adapter import Adapter, PrinterDetails
from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.config import BOOLEANS, ConfigError
from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.events import EventSink
from printlog_bridge.core.state import MemoryState, PrinterState
from printlog_bridge.core.status import PrinterReport
from printlog_bridge.core.trace import Trace

from .client import DEFAULT_USER, PrusaLinkClient, Refused
from .files import PrusaLinkFiles
from .jobs import JobTracker

log = logging.getLogger(__name__)

OPTIONS = frozenset(
    {
        "host",
        "port",
        "username",
        "password",
        "api_key",
        "poll_interval",
        "idle_poll_interval",
        "files",
    }
)
DEFAULT_POLL_S = 5.0
DEFAULT_IDLE_POLL_S = 30.0
MIN_POLL_S = 1.0
RETRY_S, MAX_RETRY_S = 5.0, 60.0
REFUSED_RETRY_S = 300.0
ACCESS_WARNING = "access"
FIRMWARE_WARNING = "firmware"
STATUS_PATH = "/api/v1/status"

BUDDY_FIRMWARE = "Prusa-Firmware-Buddy"
MK3_FIRMWARE = "Prusa-Firmware"
HOST_SOFTWARE = "PrusaLink"
WHERE_IS_THE_PASSWORD = (
    "PrusaLink's user and password are on the printer's screen, under Settings > Network > "
    "PrusaLink, where PrusaLink must be turned on too. The user is maker unless it was changed "
    "(username). Older firmware asks for an API key instead (api_key)."
)
TOO_OLD = (
    "the printer has no PrusaLink v1 API (/api/v1/status), which the bridge needs. Update the "
    "printer's firmware (help.prusa3d.com lists the latest for each model)."
)

_HOST = re.compile(r"[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\]")


class FirmwareTooOld(Exception):
    pass


class UnexpectedAnswer(Exception):
    pass


MMU_SLOTS = 5
"""Filaments an MMU3 feeds."""


class PrusaLinkAdapter(Adapter):
    name = "prusalink"

    def __init__(
        self,
        printer: str,
        host: str,
        *,
        port: int = 80,
        username: str = DEFAULT_USER,
        password: str | None = None,
        api_key: str | None = None,
        poll_interval: float = DEFAULT_POLL_S,
        idle_poll_interval: float = DEFAULT_IDLE_POLL_S,
        files: bool = True,
    ) -> None:
        self.printer = printer
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.idle_poll_interval = idle_poll_interval
        self.files = files
        """Whether to read each job's file for the slicer's estimates, thumbnail and settings."""
        self.retry_s = RETRY_S
        self.status_path = STATUS_PATH
        self.trace: Trace | None = None
        self.state: PrinterState | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}" + ("" if self.port == 80 else f":{self.port}")

    @classmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> PrusaLinkAdapter:
        unknown = sorted(set(options) - OPTIONS)
        if unknown:
            raise ConfigError(
                f"[printer {printer}]: unknown option {unknown[0]!r} "
                f"(the prusalink adapter takes {', '.join(sorted(OPTIONS))})"
            )
        values = {k: v.strip() for k, v in options.items()}
        host = values.get("host", "")
        if not host:
            raise ConfigError(f"[printer {printer}] needs host for adapter = prusalink")
        if not _HOST.fullmatch(host):
            raise ConfigError(
                f"[printer {printer}] host must be the printer's address, such as 192.168.1.60 "
                f"or mk4.local, not {host!r} (a port goes in port)"
            )
        password = values.get("password") or None
        api_key = values.get("api_key") or None
        if password is None and api_key is None:
            raise ConfigError(
                f"[printer {printer}] needs password (or api_key) for adapter = prusalink. "
                f"{WHERE_IS_THE_PASSWORD}"
            )
        port_text = values.get("port") or "80"
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise ConfigError(f"[printer {printer}] port must be a port number, not {port_text!r}")
        poll = _seconds(printer, values, "poll_interval", DEFAULT_POLL_S, MIN_POLL_S)
        idle = _seconds(printer, values, "idle_poll_interval", DEFAULT_IDLE_POLL_S, poll)
        files = BOOLEANS.get((values.get("files") or "true").lower())
        if files is None:
            raise ConfigError(f"[printer {printer}] files must be true or false")
        return cls(
            printer,
            host,
            port=int(port_text),
            username=values.get("username") or DEFAULT_USER,
            password=password,
            api_key=api_key,
            poll_interval=poll,
            idle_poll_interval=idle,
            files=files,
        )

    def describe(self) -> str:
        return f"prusalink, {self.base_url}"

    def use_trace(self, trace: Trace) -> None:
        self.trace = trace

    def use_state(self, state: PrinterState) -> None:
        self.state = state

    def _client(self, trace: Trace | None = None) -> PrusaLinkClient:
        return PrusaLinkClient(
            self.base_url,
            username=self.username,
            password=self.password,
            api_key=self.api_key,
            trace=trace,
        )

    async def run(self, sink: EventSink, report: PrinterReport) -> None:
        client = self._client(self.trace)
        files = PrusaLinkFiles(self.printer, client, report, enabled=self.files)

        async def fetch_job() -> Mapping[str, Any] | None:
            status, body = await client.get_json("/api/v1/job")
            return body if status == 200 and isinstance(body, Mapping) else None

        tracker = JobTracker(
            self.printer,
            sink,
            CurrentJob(self.state or MemoryState()),
            fetch_job=fetch_job,
            files=files,
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
                    report.disconnected("the printer refused the bridge's credentials")
                    report.warn(ACCESS_WARNING, message)
                    retry = REFUSED_RETRY_S
                except FirmwareTooOld:
                    log.warning("[printer %s] %s", self.printer, TOO_OLD)
                    report.disconnected("the printer's firmware has no PrusaLink v1 API")
                    report.warn(FIRMWARE_WARNING, TOO_OLD)
                    retry = REFUSED_RETRY_S
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError, UnexpectedAnswer) as e:
                    if self._was_connected:
                        retry = self.retry_s
                    reason = _unreachable(e, self.base_url)
                    log.info("[printer %s] %s; trying again in %gs", self.printer, reason, retry)
                    report.disconnected(reason)
                except Exception as e:  # a bug: keep watching rather than stop the bridge
                    log.exception("[printer %s] polling failed unexpectedly", self.printer)
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
        self, client: PrusaLinkClient, tracker: JobTracker, report: PrinterReport
    ) -> None:
        """Polls until the printer can't be reached. Raises what ended it."""
        self._was_connected = False
        status_code, version = await client.get_json("/api/version")
        if status_code != 200:
            raise UnexpectedAnswer(f"HTTP {status_code} from /api/version")
        body = await self._status(client)
        self._was_connected = True
        if self.trace is not None:
            self.trace.mark("open")
        report.connected()
        report.warn(ACCESS_WARNING, None)
        report.warn(FIRMWARE_WARNING, None)
        log.info("[printer %s] connected to %s", self.printer, self.describe())
        self._versions(version, report)
        tracker.connected()
        while True:
            await tracker.on_status(body)
            interval = self.poll_interval if tracker.active else self.idle_poll_interval
            await asyncio.sleep(interval)
            body = await self._status(client)

    async def _status(self, client: PrusaLinkClient) -> Mapping[str, Any]:
        status_code, body = await client.get_json(self.status_path)
        if status_code == 404:
            raise FirmwareTooOld()
        if status_code != 200 or not isinstance(body, Mapping):
            raise UnexpectedAnswer(f"HTTP {status_code} from {self.status_path}")
        return body

    def _versions(self, version: Any, report: PrinterReport) -> None:
        firmware, host = versions(version)
        if firmware is not None:
            report.firmware(*firmware)
        if host is not None:
            report.host_software(*host)

    async def printer_details(self) -> PrinterDetails:
        """The host name, and five slots with an MMU (`/api/v1/info`). PrusaLink doesn't name the
        model in words; the slicer's name for it comes from the last job, when there was one."""
        client = self._client()
        try:
            status_code, info = await client.get_json("/api/v1/info")
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, Refused, UnexpectedAnswer):
            status_code, info = 0, None
        finally:
            await client.close()
        if status_code != 200 or not isinstance(info, Mapping):
            return PrinterDetails(name=self.printer, make="Prusa")
        hostname = info.get("hostname")
        return PrinterDetails(
            name=hostname if isinstance(hostname, str) and hostname else self.printer,
            make="Prusa",
            slot_count=MMU_SLOTS if info.get("mmu") is True else None,
        )

    async def check(self) -> Sequence[CheckResult]:
        name = f"Printer {self.printer}"
        client = self._client()
        try:
            status_code, version = await client.get_json("/api/version")
            if status_code != 200:
                return [CheckResult.fail(name, f"{self.base_url} answered HTTP {status_code}")]
            body = await self._status(client)
        except Refused as e:
            return [CheckResult.fail(name, f"{e}. {WHERE_IS_THE_PASSWORD}")]
        except FirmwareTooOld:
            return [CheckResult.fail(name, TOO_OLD)]
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, UnexpectedAnswer) as e:
            return [CheckResult.fail(name, _unreachable(e, self.base_url))]
        finally:
            await client.close()
        parts = [self.describe()]
        firmware, _ = versions(version)
        if firmware is not None:
            parts.append(" ".join(firmware))
        state = body.get("printer", {}).get("state") if isinstance(body, Mapping) else None
        if isinstance(state, str):
            parts.append(state.lower())
        return [CheckResult.ok(name, ", ".join(parts))]


def versions(answer: Any) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    """The firmware and PrusaLink, each as (name, version), from `/api/version`. Buddy firmware
    has PrusaLink built in and answers `api` (PrusaLink's API version) and `firmware`; PrusaLink
    on a Raspberry Pi for an MK3 answers its own `version`, and the MK3's `firmware`."""
    if not isinstance(answer, Mapping):
        return None, None
    firmware_version = str(answer.get("firmware") or "").strip()
    pi_version = str(answer.get("version") or "").strip()
    if pi_version:
        firmware = (MK3_FIRMWARE, firmware_version) if firmware_version else None
        return firmware, (HOST_SOFTWARE, pi_version)
    api = str(answer.get("api") or "").strip()
    firmware = (BUDDY_FIRMWARE, firmware_version) if firmware_version else None
    return firmware, ((HOST_SOFTWARE, api) if api else None)


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


def _unreachable(error: BaseException, url: str) -> str:
    if isinstance(error, asyncio.TimeoutError):
        return f"no answer from {url}"
    if isinstance(error, UnexpectedAnswer):
        return f"{url} isn't answering like PrusaLink ({error})"
    if isinstance(error, (aiohttp.ClientConnectorError, ConnectionRefusedError)):
        return f"can't reach {url}. Is it the printer's address, and is the printer on?"
    return f"can't reach {url} ({error})"
