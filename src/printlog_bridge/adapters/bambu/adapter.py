"""Bambu Lab printers (X1, P1, A1, H2), over their local MQTT broker and FTPS server.

The bridge connects to the printer itself on the LAN, as Bambu Studio does in LAN mode: MQTT over
TLS on port 8883 (`mqtt.py`), user `bblp` and the printer's LAN access code, for its status
(`jobs.py`), and FTPS on port 990 (`ftps.py`) for the job's 3MF, which has the slicer's grams per
filament, the plate's thumbnail and the project's settings. It never sends the printer a command:
only `pushall` (send the whole status) and `get_version`, and it never writes a file.

Bambu's "Authorization Control" firmware (from January 2025, X1 first, then P1, A1 and the H2)
limits what third-party software may do on the LAN. Commands that change anything need Bambu's
own authorization; reading the status is not one of them by Bambu's account, but the MQTT
channel and FTP are only fully open with LAN Only Mode and Developer Mode on, which Bambu's wiki
says is what third-party integrations should use. So a refused connection names both, with
where to find the access code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
from collections.abc import Mapping, Sequence
from typing import Any

from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.config import BOOLEANS, ConfigError
from printlog_bridge.core.current_job import CurrentJob
from printlog_bridge.core.events import EventSink, SpoolSink
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.status import PrinterReport
from printlog_bridge.core.trace import RECEIVE, SEND, Trace

from .files import BambuFiles
from .ftps import FTPS_PORT, BambuFtps, FilesRefused
from .jobs import JobTracker
from .mqtt import MqttClient, MqttClosed, MqttRefused
from .tls import CertificateChanged, CertificatePin, client_context, parse_fingerprint

log = logging.getLogger(__name__)

MQTT_PORT = 8883
USER = "bblp"
OPTIONS = frozenset(
    {"host", "serial", "access_code", "tls_fingerprint", "ftps", "mqtt_port", "ftps_port"}
)
PUSHALL_INTERVAL_S = 30 * 60
"""A P1 sends only what changed; a whole status now and then corrects anything missed. Not
often: OpenBambuAPI warns that `pushall` more than every 5 minutes makes a P1P lag."""
CHECK_TIMEOUT_S = 10.0
MIN_BACKOFF_S, MAX_BACKOFF_S = 1.0, 60.0
REFUSED_BACKOFF_S = 300.0
"""After a refused access code or a changed certificate: something the user must fix."""
ACCESS_WARNING = "access"
AUTH_WARNING = "authorization"

_SERIAL = re.compile(r"[A-Za-z0-9]{8,24}")
_HOST = re.compile(r"[A-Za-z0-9.\-:\[\]]+")

FIRMWARE_NAME = "Bambu"
WHERE_IS_THE_CODE = (
    "The access code is on the printer's screen: on an X1 or H2, Settings > LAN Only; on a P1, "
    "Settings > WLAN; on an A1, Settings > LAN Only Mode."
)
DEVELOPER_MODE = (
    "On firmware with Bambu's Authorization Control, third-party software also needs LAN Only "
    "Mode and Developer Mode turned on (on the same settings page)."
)


def refused_message(code: int) -> str:
    return (
        f"the printer refused the access code (MQTT {code}). Check access_code. "
        f"{WHERE_IS_THE_CODE} {DEVELOPER_MODE}"
    )


class BambuAdapter(Adapter):
    name = "bambu"

    def __init__(
        self,
        printer: str,
        host: str,
        serial: str,
        access_code: str,
        *,
        tls_fingerprint: str | None = None,
        ftps: bool = True,
        mqtt_port: int = MQTT_PORT,
        ftps_port: int = FTPS_PORT,
    ) -> None:
        self.printer = printer
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self.tls_fingerprint = tls_fingerprint
        self.ftps = ftps
        """Whether to read each job's 3MF for the slicer's grams, its thumbnail and settings."""
        self.mqtt_port = mqtt_port
        self.ftps_port = ftps_port
        self.spools: SpoolSink | None = None
        self.trace: Trace | None = None
        self.state: PrinterState | None = None
        self._connected = False

    @classmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> BambuAdapter:
        unknown = sorted(set(options) - OPTIONS)
        if unknown:
            raise ConfigError(
                f"[printer {printer}]: unknown option {unknown[0]!r} "
                f"(the bambu adapter takes {', '.join(sorted(OPTIONS))})"
            )
        values = {k: v.strip() for k, v in options.items()}
        for option in ("host", "serial", "access_code"):
            if not values.get(option):
                raise ConfigError(f"[printer {printer}] needs {option} for adapter = bambu")
        host = values["host"]
        if not _HOST.fullmatch(host):
            raise ConfigError(
                f"[printer {printer}] host must be the printer's address, such as "
                f"192.168.1.50, not {host!r}"
            )
        serial = values["serial"]
        if not _SERIAL.fullmatch(serial):
            raise ConfigError(
                f"[printer {printer}] serial must be the printer's serial number, letters and "
                "digits such as 01S00C123456789 (on the printer's device information page or "
                "its sticker, or from printlog-bridge discover)"
            )
        if any(c.isspace() for c in values["access_code"]):
            raise ConfigError(f"[printer {printer}] access_code can't hold spaces")
        fingerprint = values.get("tls_fingerprint") or None
        if fingerprint is not None and parse_fingerprint(fingerprint) is None:
            raise ConfigError(
                f"[printer {printer}] tls_fingerprint must be a SHA-256 fingerprint "
                "(64 hex digits, colons allowed), as the bridge's log names it"
            )
        ftps = BOOLEANS.get((values.get("ftps") or "true").lower())
        if ftps is None:
            raise ConfigError(f"[printer {printer}] ftps must be true or false")
        return cls(
            printer,
            host,
            serial,
            values["access_code"],
            tls_fingerprint=fingerprint,
            ftps=ftps,
            mqtt_port=_port(printer, values, "mqtt_port", MQTT_PORT),
            ftps_port=_port(printer, values, "ftps_port", FTPS_PORT),
        )

    def describe(self) -> str:
        return f"bambu, {self.host} (serial {self.serial})"

    def use_spools(self, spools: SpoolSink, api_url: str | None = None) -> None:
        self.spools = spools

    def use_trace(self, trace: Trace) -> None:
        self.trace = trace

    def use_state(self, state: PrinterState) -> None:
        self.state = state

    @property
    def report_topic(self) -> str:
        return f"device/{self.serial}/report"

    @property
    def request_topic(self) -> str:
        return f"device/{self.serial}/request"

    async def run(self, sink: EventSink, report: PrinterReport) -> None:
        state = self.state or _MemoryState()
        pin = CertificatePin(self.printer, self.tls_fingerprint, state)
        files = BambuFiles(self.printer, self._ftps(pin), report) if self.ftps else None
        tracker = JobTracker(
            self.printer, sink, CurrentJob(state), files=files, report=report, spools=self.spools
        )
        backoff = MIN_BACKOFF_S
        try:
            while True:
                self._connected = False
                try:
                    await self._session(tracker, pin, report)
                except MqttRefused as e:
                    message = refused_message(e.code)
                    log.warning("[printer %s] %s", self.printer, message)
                    report.disconnected("the printer refused the access code")
                    report.warn(ACCESS_WARNING, message)
                    backoff = REFUSED_BACKOFF_S
                except CertificateChanged as e:
                    log.warning("[printer %s] %s", self.printer, e)
                    report.disconnected("the printer's certificate changed")
                    report.warn(ACCESS_WARNING, str(e))
                    backoff = REFUSED_BACKOFF_S
                except (MqttClosed, OSError, ssl.SSLError, asyncio.TimeoutError) as e:
                    if self._connected:
                        backoff = MIN_BACKOFF_S
                    reason = _reason(e, self.host, self.mqtt_port)
                    log.info("[printer %s] %s; trying again in %gs", self.printer, reason, backoff)
                    report.disconnected(reason)
                except Exception as e:  # a bug: keep watching rather than stop the bridge
                    log.exception("[printer %s] the connection failed unexpectedly", self.printer)
                    report.disconnected(f"unexpected error: {e!r}")
                    backoff = MAX_BACKOFF_S
                await asyncio.sleep(backoff)
                backoff = (
                    MIN_BACKOFF_S
                    if backoff >= REFUSED_BACKOFF_S
                    else min(backoff * 2, MAX_BACKOFF_S)
                )
        finally:
            await tracker.close()

    async def _session(self, tracker: JobTracker, pin: CertificatePin, report: PrinterReport):
        client = await MqttClient.connect(
            self.host,
            self.mqtt_port,
            client_context(),
            client_id=f"printlog-bridge-{self.serial}",
            username=USER,
            password=self.access_code,
            verify=lambda der: pin.check(der, "mqtt"),
        )
        if self.trace is not None:
            self.trace.mark("open")
        self._connected = True
        try:
            report.connected()
            report.warn(ACCESS_WARNING, None)
            report.warn(AUTH_WARNING, None)
            log.info("[printer %s] connected to %s", self.printer, self.describe())
            tracker.connected()
            await client.subscribe(self.report_topic)
            await self._request(client, {"info": {"sequence_id": "0", "command": "get_version"}})
            await self._pushall(client)
            loop = asyncio.get_running_loop()
            last_pushall = loop.time()
            while True:
                topic, payload = await client.receive()
                if topic != self.report_topic:
                    continue
                text = payload.decode("utf-8", errors="replace")
                if self.trace is not None:
                    self.trace.record(RECEIVE, text)
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                self._on_info(message, report)
                self._on_refusal(message, report)
                await tracker.on_message(message)
                if loop.time() - last_pushall > PUSHALL_INTERVAL_S:
                    await self._pushall(client)
                    last_pushall = loop.time()
        finally:
            await client.close()
            if self.trace is not None:
                self.trace.mark("close")

    async def _pushall(self, client: MqttClient) -> None:
        await self._request(
            client,
            {"pushing": {"sequence_id": "0", "command": "pushall", "version": 1, "push_target": 1}},
        )

    async def _request(self, client: MqttClient, request: Mapping[str, Any]) -> None:
        text = json.dumps(request)
        if self.trace is not None:
            self.trace.record(SEND, text)
        await client.publish(self.request_topic, text.encode())

    def _on_info(self, message: Mapping[str, Any], report: PrinterReport) -> None:
        version = firmware_version(message)
        if version is not None:
            report.firmware(FIRMWARE_NAME, version)

    def _on_refusal(self, message: Mapping[str, Any], report: PrinterReport) -> None:
        """A request the printer turned down: on firmware with Authorization Control, without
        Developer Mode, even `pushall` may be."""
        for part in message.values():
            if not isinstance(part, Mapping):
                continue
            result = str(part.get("result") or "").lower()
            if result.startswith("fail") and part.get("command") in ("pushall", "get_version"):
                text = (
                    f"the printer turned down the bridge's {part.get('command')} request "
                    f"({part.get('reason') or 'no reason given'}). {DEVELOPER_MODE}"
                )
                log.warning("[printer %s] %s", self.printer, text)
                report.warn(AUTH_WARNING, text)

    def _ftps(self, pin: CertificatePin) -> BambuFtps:
        return BambuFtps(
            self.host,
            self.access_code,
            port=self.ftps_port,
            verify=lambda der: pin.check(der, "ftps"),
        )

    async def check(self) -> Sequence[CheckResult]:
        name = f"Printer {self.printer}"
        pin = CertificatePin(self.printer, self.tls_fingerprint, self.state)
        try:
            client = await MqttClient.connect(
                self.host,
                self.mqtt_port,
                client_context(),
                client_id=f"printlog-bridge-check-{self.serial}",
                username=USER,
                password=self.access_code,
                verify=lambda der: pin.check(der, "mqtt"),
                timeout=CHECK_TIMEOUT_S,
            )
        except MqttRefused as e:
            return [CheckResult.fail(name, refused_message(e.code))]
        except CertificateChanged as e:
            return [CheckResult.fail(name, str(e))]
        except (MqttClosed, OSError, ssl.SSLError, asyncio.TimeoutError) as e:
            return [CheckResult.fail(name, _unreachable(e, self.host, self.mqtt_port))]
        status: dict[str, Any] = {}
        version = None
        try:
            await client.subscribe(self.report_topic)
            await self._request(client, {"info": {"sequence_id": "0", "command": "get_version"}})
            await self._pushall(client)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + CHECK_TIMEOUT_S
            while (not status or version is None) and loop.time() < deadline:
                try:
                    _, payload = await asyncio.wait_for(client.receive(), deadline - loop.time())
                except asyncio.TimeoutError:
                    break
                try:
                    message = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    version = firmware_version(message) or version
                    if isinstance(message.get("print"), dict) and "gcode_state" in message["print"]:
                        status = message["print"]
        except MqttClosed as e:
            return [CheckResult.fail(name, _unreachable(e, self.host, self.mqtt_port))]
        finally:
            await client.close()
        parts = [self.describe()]
        if version:
            parts.append(f"firmware {version}")
        if not status:
            results = [
                CheckResult.warn(
                    name,
                    f"{', '.join(parts)}: connected, but the printer sent no status within "
                    f"{CHECK_TIMEOUT_S:g}s. {DEVELOPER_MODE}",
                )
            ]
        else:
            parts.append(str(status.get("gcode_state", "")).lower())
            results = [CheckResult.ok(name, ", ".join(parts))]
        if self.ftps:
            results.append(await self._check_files(pin))
        return results

    async def _check_files(self, pin: CertificatePin) -> CheckResult:
        name = f"Printer {self.printer} files"
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, self._ftps(pin).probe), CHECK_TIMEOUT_S
            )
        except FilesRefused:
            return CheckResult.warn(
                name,
                "the printer refused the access code for its files (FTPS), so prints are logged "
                f"without the slicer's grams, thumbnail and settings. {DEVELOPER_MODE}",
            )
        except CertificateChanged as e:
            return CheckResult.warn(name, str(e))
        except Exception as e:
            return CheckResult.warn(
                name,
                f"can't read the printer's files: {_unreachable(e, self.host, self.ftps_port)}. "
                "Prints are logged without the slicer's grams, thumbnail and settings (set "
                "ftps = false to stop trying).",
            )
        return CheckResult.ok(name, f"FTPS on port {self.ftps_port}")


def firmware_version(message: Mapping[str, Any]) -> str | None:
    """The printer's firmware version from a `get_version` answer: its `ota` module's."""
    info = message.get("info")
    if not isinstance(info, Mapping) or info.get("command") != "get_version":
        return None
    for module in info.get("module") or []:
        if isinstance(module, Mapping) and module.get("name") == "ota":
            version = str(module.get("sw_ver") or "").strip()
            return version or None
    return None


class _MemoryState(PrinterState):
    """For a printer with no printer_id, which the bridge gives no state: nothing is kept."""

    def __init__(self) -> None:
        super().__init__(None, "", {})  # type: ignore[arg-type]

    def save(self) -> None:
        pass


def _port(printer: str, values: Mapping[str, str], option: str, default: int) -> int:
    text = values.get(option)
    if not text:
        return default
    if not text.isdigit() or not 0 < int(text) < 65536:
        raise ConfigError(f"[printer {printer}] {option} must be a port number, not {text!r}")
    return int(text)


def _reason(error: BaseException, host: str, port: int) -> str:
    if isinstance(error, MqttClosed):
        return f"the connection to {host} ended: {error}"
    return _unreachable(error, host, port)


def _unreachable(error: BaseException, host: str, port: int) -> str:
    if isinstance(error, asyncio.TimeoutError):
        return f"no answer from {host}:{port}"
    if isinstance(error, ConnectionRefusedError):
        return (
            f"{host} refused a connection on port {port}. Is it the printer's address? "
            f"{DEVELOPER_MODE}"
        )
    if isinstance(error, ssl.SSLError):
        return f"TLS to {host}:{port} failed ({error.reason or error})"
    return f"can't reach {host}:{port} ({error})"
