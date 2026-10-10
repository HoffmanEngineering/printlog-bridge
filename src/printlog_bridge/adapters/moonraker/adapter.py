"""Klipper printers, through Moonraker.

Local mode talks to Moonraker's Unix socket on the printer's own host. Remote mode talks to it
over the network, for locked-down printers (Snapmaker U1, Creality K2) where nothing can be
installed on the printer itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import aiohttp

from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.config import ConfigError
from printlog_bridge.core.events import EventSink

DEFAULT_SOCKET = "~/printer_data/comms/moonraker.sock"
OPTIONS = frozenset({"url", "socket"})
CHECK_TIMEOUT_S = 5

_HTTP_SCHEME_FOR = {"http": "http", "https": "https", "ws": "http", "wss": "https"}


class MoonrakerAdapter(Adapter):
    name = "moonraker"

    def __init__(self, printer: str, url: str | None, socket: Path | None) -> None:
        self.printer = printer
        self.url = url
        self.socket = socket

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

        if "url" in options:
            url = options["url"].strip().rstrip("/")
            parsed = urlparse(url)
            if parsed.scheme not in _HTTP_SCHEME_FOR or not parsed.netloc:
                raise ConfigError(
                    f"[printer {printer}] url must look like http://printer.local:7125, "
                    f"not {options['url']!r}"
                )
            return cls(printer, url=url, socket=None)

        socket = Path(options.get("socket", DEFAULT_SOCKET).strip()).expanduser()
        return cls(printer, url=None, socket=socket)

    def describe(self) -> str:
        if self.url is not None:
            return f"moonraker, remote {self.url}"
        return f"moonraker, local socket {self.socket}"

    async def run(self, sink: EventSink) -> None:
        raise NotImplementedError("Watching Moonraker lands with printlog-bridge#3.")

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
            info = await self._server_info()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
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

    async def _server_info(self) -> Mapping[str, object]:
        timeout = aiohttp.ClientTimeout(total=CHECK_TIMEOUT_S)
        if self.socket is not None:
            connector: aiohttp.BaseConnector = aiohttp.UnixConnector(path=str(self.socket))
            info_url = "http://localhost/server/info"
        else:
            connector = aiohttp.TCPConnector()
            info_url = _http_base(self.url or "") + "/server/info"

        session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        async with session, session.get(info_url) as response:
            response.raise_for_status()
            body = await response.json()
        return body.get("result", {})


def _http_base(url: str) -> str:
    """The HTTP root for a configured URL, which may be Moonraker's websocket endpoint."""
    parsed = urlparse(url)
    path = parsed.path
    if path.endswith("/websocket"):
        path = path[: -len("/websocket")]
    return urlunparse((_HTTP_SCHEME_FOR[parsed.scheme], parsed.netloc, path, "", "", ""))


def _reason(error: BaseException) -> str:
    if isinstance(error, asyncio.TimeoutError):
        return f"no answer within {CHECK_TIMEOUT_S}s"
    return str(error) or type(error).__name__
