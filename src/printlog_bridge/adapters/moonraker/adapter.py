"""Klipper printers, through Moonraker.

Local mode talks to Moonraker's Unix socket on the printer's own host. Remote mode talks to it
over the network, for locked-down printers (Snapmaker U1, Creality K2) where nothing can be
installed on the printer itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.config import ConfigError
from printlog_bridge.core.events import EventSink
from printlog_bridge.core.status import PrinterReport

from .images import MoonrakerImages
from .jobs import JobTracker
from .session import (
    ConnectionLost,
    MoonrakerAuthError,
    MoonrakerRpcError,
    MoonrakerSession,
    fetch_server_info,
)

log = logging.getLogger(__name__)

DEFAULT_SOCKET = "~/printer_data/comms/moonraker.sock"
OPTIONS = frozenset({"url", "socket", "moonraker_api_key", "snapshots"})
BOOLEANS = {
    "true": True, "yes": True, "on": True, "1": True,
    "false": False, "no": False, "off": False, "0": False,
}  # fmt: skip
CHECK_TIMEOUT_S = 5

URL_SCHEMES = frozenset({"http", "https", "ws", "wss"})


class MoonrakerAdapter(Adapter):
    name = "moonraker"

    def __init__(
        self,
        printer: str,
        url: str | None,
        socket: Path | None,
        api_key: str | None = None,
        snapshots: bool = True,
    ) -> None:
        self.printer = printer
        self.url = url
        self.socket = socket
        self.api_key = api_key
        self.snapshots = snapshots
        """Whether a frame from the printer's webcam is added to each print when it ends."""

    @classmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> MoonrakerAdapter:
        unknown = sorted(set(options) - OPTIONS)
        if unknown:
            raise ConfigError(
                f"[printer {printer}]: unknown option {unknown[0]!r} "
                f"(the moonraker adapter takes {', '.join(sorted(OPTIONS))})"
            )

        if "url" in options and "socket" in options:
            raise ConfigError(
                f"[printer {printer}]: set either url or socket, not both. "
                "url is for remote mode, socket for local mode."
            )

        api_key = options.get("moonraker_api_key", "").strip() or None
        snapshots = BOOLEANS.get(options.get("snapshots", "true").strip().lower())
        if snapshots is None:
            raise ConfigError(
                f"[printer {printer}] snapshots must be true or false, not {options['snapshots']!r}"
            )

        if "url" in options:
            url = options["url"].strip().rstrip("/")
            parsed = urlparse(url)
            if parsed.scheme not in URL_SCHEMES or not parsed.netloc:
                raise ConfigError(
                    f"[printer {printer}] url must look like http://printer.local:7125, "
                    f"not {options['url']!r}"
                )
            return cls(printer, url=url, socket=None, api_key=api_key, snapshots=snapshots)

        if api_key is not None:
            raise ConfigError(
                f"[printer {printer}] moonraker_api_key is only for remote mode (url). "
                "Moonraker trusts its local socket without one."
            )

        socket = Path(options.get("socket", DEFAULT_SOCKET).strip()).expanduser()
        return cls(printer, url=None, socket=socket, snapshots=snapshots)

    def describe(self) -> str:
        if self.url is not None:
            return f"moonraker, remote {self.url}"
        return f"moonraker, local socket {self.socket}"

    async def run(self, sink: EventSink, report: PrinterReport) -> None:
        images = MoonrakerImages(
            self.printer,
            url=self.url,
            socket=self.socket,
            api_key=self.api_key,
            snapshots=self.snapshots,
        )
        session = MoonrakerSession(
            self.printer,
            listener=JobTracker(self.printer, sink, report, images),
            url=self.url,
            socket=self.socket,
            api_key=self.api_key,
            report=report,
        )
        await session.run()

    async def check(self) -> Sequence[CheckResult]:
        name = f"Printer {self.printer}"

        if self.socket is not None and not self.socket.exists():
            return [
                CheckResult.fail(
                    name,
                    f"no Moonraker socket at {self.socket}. Is Moonraker running on this host? "
                    "For a printer on another host, set url instead.",
                )
            ]

        where = self.url or str(self.socket)
        try:
            info = await asyncio.wait_for(
                fetch_server_info(
                    self.printer, url=self.url, socket=self.socket, api_key=self.api_key
                ),
                CHECK_TIMEOUT_S,
            )
        except MoonrakerAuthError as e:
            return [CheckResult.fail(name, str(e))]
        except (
            ConnectionLost,
            MoonrakerRpcError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError,
        ) as e:
            return [CheckResult.fail(name, f"can't reach Moonraker at {where}: {_reason(e)}")]

        klippy = info.get("klippy_state", "unknown")
        version = info.get("moonraker_version")
        detail = ", ".join(
            part
            for part in (
                self.describe(),
                f"Moonraker {version}" if version else "",
                f"Klipper {klippy}",
            )
            if part
        )
        if klippy != "ready":
            return [CheckResult.warn(name, detail)]
        return [CheckResult.ok(name, detail)]


def _reason(error: BaseException) -> str:
    if isinstance(error, asyncio.TimeoutError):
        return f"no answer within {CHECK_TIMEOUT_S}s"
    return str(error) or type(error).__name__
