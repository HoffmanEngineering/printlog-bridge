"""Klipper printers, through Moonraker.

Local mode talks to Moonraker's Unix socket on the printer's own host. Remote mode talks to it
over the network, for locked-down printers (such as the Snapmaker U1) where nothing can be
installed on the printer itself.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from printlog_bridge.core.adapter import Adapter, PrinterDetails
from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.config import BOOLEANS, ConfigError, parse_duration
from printlog_bridge.core.discovery import Discovery, Found
from printlog_bridge.core.events import EventSink, SpoolSink
from printlog_bridge.core.images import scrub_url
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.status import PrinterReport
from printlog_bridge.core.trace import Trace

from . import discovery as lan
from .images import MoonrakerImages
from .jobs import JobTracker
from .locator import Locator, last_found
from .notifier import NotifierCheck, message, old_notifiers
from .session import (
    ConnectionLost,
    Listeners,
    MoonrakerAuthError,
    MoonrakerListener,
    MoonrakerRpcError,
    MoonrakerSession,
    fetch_once,
    fetch_server_info,
    where,
)
from .spoolman import SpoolmanSync
from .spools import SpoolWatcher
from .timelapse import TimelapseWatcher
from .tools import EXTRUDER
from .u1_rfid import U1RfidReader

log = logging.getLogger(__name__)

DEFAULT_SOCKET = "~/printer_data/comms/moonraker.sock"
DISCOVERY_OPTIONS = frozenset(
    {
        "discovery",
        "discovery_after",
        "discovery_interval",
        "discovery_timeout",
        "discovery_zeroconf",
        "discovery_networks",
    }
)
OPTIONS = (
    frozenset({"url", "socket", "moonraker_api_key", "snapshots", "timelapse"}) | DISCOVERY_OPTIONS
)
MIN_DISCOVERY_INTERVAL_S = 30
"""Searches no more often than this, so a network isn't swept over and over."""
MAX_DISCOVERY_NETWORKS = 16
CHECK_TIMEOUT_S = 5
CHECK_ERRORS = (
    ConnectionLost,
    MoonrakerRpcError,
    aiohttp.ClientError,
    asyncio.TimeoutError,
    OSError,
)
"""What `check` reports as Moonraker being out of reach."""

URL_SCHEMES = frozenset({"http", "https", "ws", "wss"})


MOONRAKER_SERVED = ("config", "gcodes", "logs")
"""The folders of Moonraker's data folder (printer_data) that it serves to its clients."""
SERVED_BY_MOONRAKER = "Moonraker serves to anyone who can open Mainsail or Fluidd"


def moonraker_serves(path: Path) -> bool:
    """Whether `path` is in a folder Moonraker serves to its clients: Klipper's config folder,
    or its gcodes or logs, next to Moonraker's `comms` folder. Anyone who can open Mainsail or
    Fluidd can download what is in them."""
    folder = path.parent
    if any((folder / name).is_file() for name in ("moonraker.conf", "printer.cfg")):
        return True
    for ancestor in (folder, *folder.parents):
        if ancestor.name not in MOONRAKER_SERVED:
            continue
        data = ancestor.parent
        if data.name == "printer_data" or (data / "comms").is_dir():
            return True
    return False


class MoonrakerAdapter(Adapter):
    name = "moonraker"

    def __init__(
        self,
        printer: str,
        url: str | None,
        socket: Path | None,
        api_key: str | None = None,
        snapshots: bool = True,
        discovery: bool = True,
        discovery_options: Mapping[str, object] | None = None,
        timelapse: bool = True,
    ) -> None:
        self.printer = printer
        self.url = url
        self.socket = socket
        self.api_key = api_key
        self.snapshots = snapshots
        """Whether a frame from the printer's webcam is added to each print when it ends."""
        self.timelapse = timelapse
        """Whether moonraker-timelapse's video of each print is attached to it (Pro)."""
        self.discovery = discovery and url is not None
        """In remote mode, whether to find the printer again when its address changes."""
        self.spools: SpoolSink | None = None
        self.api_url: str | None = None
        """3D Print Log's API, for telling whether Moonraker's Spoolman server is it."""
        self.trace: Trace | None = None
        self.state: PrinterState | None = None
        self.locator_options: dict = dict(discovery_options or {})
        """Passed on to the Locator: `after`, `interval`, `timeout`, `zeroconf`, `networks`."""

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

        # Used as given: configparser trimmed a config file's; one from the app is used as it
        # opened.
        api_key = options.get("moonraker_api_key") or None
        if api_key is not None and not api_key.strip():
            api_key = None
        snapshots = _boolean(printer, options, "snapshots")
        timelapse = _boolean(printer, options, "timelapse")
        discovery = _boolean(printer, options, "discovery")
        discovery_options = _discovery_options(printer, options)

        if "url" in options:
            url = options["url"].strip().rstrip("/")
            parsed = urlparse(url)
            if parsed.scheme not in URL_SCHEMES or not parsed.netloc:
                raise ConfigError(
                    f"[printer {printer}] url must look like http://printer.local:7125, "
                    f"not {options['url']!r}"
                )
            return cls(
                printer,
                url=url,
                socket=None,
                api_key=api_key,
                snapshots=snapshots,
                discovery=discovery,
                discovery_options=discovery_options,
                timelapse=timelapse,
            )

        if api_key is not None:
            raise ConfigError(
                f"[printer {printer}] moonraker_api_key is only for remote mode (url). "
                "Moonraker trusts its local socket without one."
            )
        local_only = sorted(DISCOVERY_OPTIONS & set(options))
        if local_only:
            raise ConfigError(
                f"[printer {printer}] {local_only[0]} is only for remote mode (url): the local "
                "socket doesn't change address."
            )

        socket = Path(options.get("socket", DEFAULT_SOCKET).strip()).expanduser()
        return cls(printer, url=None, socket=socket, snapshots=snapshots, timelapse=timelapse)

    app_secrets = ("moonraker_api_key",)

    @classmethod
    def discovery(cls) -> Discovery:
        return lan.MoonrakerDiscovery()

    @classmethod
    def app_options(
        cls, host: str, port: int | None, serial: str | None, secrets: Mapping[str, str]
    ) -> dict[str, str]:
        options = {"url": f"http://{host}:{port or lan.DEFAULT_PORT}"}
        options.update({k: v for k, v in secrets.items() if v.strip()})
        return options

    def _address(self) -> tuple[str, int] | None:
        if self.url is None:
            return None
        known = self.state.discovery.get("address") if self.state is not None else None
        return (known or lan.host_of(self.url)), lan.port_of(self.url)

    def _macs(self) -> frozenset[str]:
        data = self.state.discovery.get("fingerprint") if self.state is not None else None
        return lan.Fingerprint.from_json(data).macs if isinstance(data, Mapping) else frozenset()

    def watches(self, found: Found) -> bool:
        where = self._address()
        if found.kind != self.name or where is None:
            return False
        macs = self._macs()
        if macs and found.macs and macs & set(found.macs):
            return found.port == where[1]
        configured = (lan.host_of(self.url or ""), where[1])
        return (found.address, found.port) in (where, configured)

    def found_entry(self) -> Found | None:
        where = self._address()
        if where is None:
            return None
        return Found(self.name, where[0], where[1], macs=tuple(sorted(self._macs())))

    def serves(self, path: Path) -> str | None:
        return SERVED_BY_MOONRAKER if moonraker_serves(path) else None

    def mode(self) -> str:
        return "local" if self.url is None else "remote"

    def describe(self) -> str:
        if self.url is not None:
            return f"moonraker, remote {where(self.url, None)}"
        return f"moonraker, local socket {self.socket}"

    async def run(self, sink: EventSink, report: PrinterReport) -> None:
        locator = None
        if self.discovery and self.url is not None and self.state is not None:

            def moved(url: str) -> None:
                self.url = images.url = url
                if timelapse is not None:
                    timelapse.url = url

            locator = Locator(
                self.printer,
                configured_url=self.url,
                state=self.state,
                api_key=self.api_key,
                report=report,
                on_moved=moved,
                **self.locator_options,
            )
            locator.started()
            self.url = locator.url
        images = MoonrakerImages(
            self.printer,
            url=self.url,
            socket=self.socket,
            api_key=self.api_key,
            snapshots=self.snapshots,
        )
        timelapse = (
            TimelapseWatcher(
                self.printer, sink, url=self.url, socket=self.socket, api_key=self.api_key
            )
            if self.timelapse
            else None
        )
        tracker = JobTracker(self.printer, sink, report, images, timelapse)
        listeners: list[MoonrakerListener] = [tracker]
        unwatch = None
        if self.spools is not None:
            # The U1's reader only reports on a printer with `filament_detect`; elsewhere it
            # never hears from it. After the tracker, which knows where the job is by then.
            watcher = SpoolWatcher(
                self.printer, self.spools, U1RfidReader(self.printer), point=tracker.point
            )
            listeners.append(watcher)
            if self.api_url is not None:
                # A spool picked in Mainsail or Fluidd, when its Spoolman server is 3D Print Log;
                # and Moonraker's active spool kept on the active tool's spool in 3D Print Log.
                sync = SpoolmanSync(self.printer, self.api_url, self.spools, point=tracker.point)
                listeners.append(sync)
                watch = getattr(self.spools, "watch_loaded", None)
                if watch is not None:
                    unwatch = watch(self.printer, sync.on_loaded)
        # Last, so the jobs missed while disconnected are caught up first.
        listeners.append(NotifierCheck(self.printer, report))
        listener = Listeners(*listeners)
        session = MoonrakerSession(
            self.printer,
            listener=listener,
            url=self.url,
            socket=self.socket,
            api_key=self.api_key,
            report=report,
            trace=self.trace,
            locator=locator,
            notifications=(
                {"notify_timelapse_event": timelapse.on_event} if timelapse is not None else None
            ),
        )
        try:
            await session.run()
        finally:
            if unwatch is not None:
                unwatch()
            await tracker.close()

    def use_spools(self, spools: SpoolSink, api_url: str | None = None) -> None:
        self.spools = spools
        self.api_url = api_url

    def use_trace(self, trace: Trace) -> None:
        self.trace = trace

    def use_state(self, state: PrinterState) -> None:
        self.state = state

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

        found_at = self._last_found()
        if found_at is not None:
            # Where `run` connects: the address the printer was last found at.
            assert self.url is not None
            configured = lan.host_of(self.url)
            found_url = lan.with_host(self.url, found_at)
            try:
                info = await self._server_info(found_url)
            except MoonrakerAuthError as e:
                return [CheckResult.fail(name, str(e))]
            except CHECK_ERRORS:
                pass  # it may be back at the configured address
            else:
                moved = (
                    f"found at {found_at} (the config says {configured}); a DHCP reservation "
                    "keeps it in one place"
                )
                result = CheckResult.warn(name, f"{self._detail(info)}, {moved}")
                return [result, *await self._notifier_check(found_url)]

        try:
            info = await self._server_info(self.url)
        except MoonrakerAuthError as e:
            return [CheckResult.fail(name, str(e))]
        except CHECK_ERRORS as e:
            also = f", nor at {found_at} where it was last found" if found_at else ""
            return [
                CheckResult.fail(
                    name,
                    f"can't reach Moonraker at {where(self.url, self.socket)}: "
                    f"{_reason(e, self.url)}{also}",
                )
            ]

        detail = self._detail(info)
        if info.get("klippy_state", "unknown") != "ready":
            result = CheckResult.warn(name, detail)
        else:
            result = CheckResult.ok(name, detail)
        return [result, *await self._notifier_check(self.url)]

    async def printer_details(self) -> PrinterDetails:
        """The host name, the kinematics and how many extruders Klipper has (one slot each)."""
        url = lan.with_host(self.url, self._last_found()) if self._last_found() else self.url

        async def call(method: str, params: Mapping[str, Any] | None = None) -> Any:
            return await asyncio.wait_for(
                fetch_once(
                    self.printer,
                    method,
                    url=url,
                    socket=self.socket,
                    api_key=self.api_key,
                    params=params,
                ),
                CHECK_TIMEOUT_S,
            )

        try:
            info = await call("printer.info")
            objects = (await call("printer.objects.list")).get("objects") or []
            query = {"objects": {"configfile": ["settings"]}}
            status = (await call("printer.objects.query", query)).get("status") or {}
        except (MoonrakerAuthError, *CHECK_ERRORS, AttributeError):
            return PrinterDetails(name=self.printer, make="Klipper")
        hostname = info.get("hostname") if isinstance(info, Mapping) else None
        extruders = sum(1 for o in objects if isinstance(o, str) and EXTRUDER.fullmatch(o))
        settings = (status.get("configfile") or {}).get("settings") or {}
        kinematics = (settings.get("printer") or {}).get("kinematics")
        kinematics = kinematics if isinstance(kinematics, str) and kinematics else None
        return PrinterDetails(
            name=hostname if isinstance(hostname, str) and hostname else self.printer,
            make="Klipper",
            model=kinematics,
            slot_count=extruders or None,
            description=", ".join(
                part
                for part in (
                    f"{kinematics} kinematics" if kinematics else "",
                    f"{extruders} extruder{'' if extruders == 1 else 's'}" if extruders else "",
                )
                if part
            )
            or None,
        )

    async def _notifier_check(self, url: str | None) -> list[CheckResult]:
        """A warning when Moonraker still has a notifier logging prints to 3D Print Log, or
        nothing: also when its config can't be read, which `run` doesn't need."""
        try:
            answer = await asyncio.wait_for(
                fetch_once(
                    self.printer,
                    "server.config",
                    url=url,
                    socket=self.socket,
                    api_key=self.api_key,
                ),
                CHECK_TIMEOUT_S,
            )
        except (MoonrakerAuthError, *CHECK_ERRORS):
            return []
        found = old_notifiers(answer) if isinstance(answer, Mapping) else []
        if not found:
            return []
        return [CheckResult.warn(f"Printer {self.printer} notifier", message(found))]

    def _last_found(self) -> str | None:
        """For `doctor`: where discovery last found the printer, from the state file it was
        given read-only, when that isn't the configured address."""
        if not self.discovery or self.url is None or self.state is None:
            return None
        return last_found(self.state, self.url)

    async def _server_info(self, url: str | None) -> Mapping[str, Any]:
        return await asyncio.wait_for(
            fetch_server_info(self.printer, url=url, socket=self.socket, api_key=self.api_key),
            CHECK_TIMEOUT_S,
        )

    def _detail(self, info: Mapping[str, Any]) -> str:
        version = info.get("moonraker_version")
        return ", ".join(
            part
            for part in (
                self.describe(),
                f"Moonraker {version}" if version else "",
                f"Klipper {info.get('klippy_state', 'unknown')}",
            )
            if part
        )


def _discovery_options(printer: str, options: Mapping[str, str]) -> dict[str, object]:
    """The Locator's settings from the `discovery_*` options; defaults for those not set."""
    out: dict[str, object] = {}
    for option, key, least in (
        ("discovery_after", "after", 0),
        ("discovery_interval", "interval", MIN_DISCOVERY_INTERVAL_S),
        ("discovery_timeout", "timeout", 0.1),
    ):
        if not options.get(option, "").strip():
            continue
        seconds = parse_duration(options[option])
        if seconds is None or seconds < least:
            raise ConfigError(
                f"[printer {printer}] {option} must be a time such as 90s, 3m or 1h"
                f"{f' (at least {least:g}s)' if least >= 1 else ''}, not {options[option]!r}"
            )
        out[key] = seconds
    if options.get("discovery_zeroconf", "").strip():
        out["zeroconf"] = _boolean(printer, options, "discovery_zeroconf")
    text = options.get("discovery_networks", "").strip()
    if text:
        networks = []
        for item in re.split(r"[\s,]+", text):
            try:
                networks.append(lan.parse_subnet(item))
            except ValueError as e:
                raise ConfigError(
                    f"[printer {printer}] discovery_networks: {e}. List private networks of a "
                    "/24 or smaller, such as 192.168.1.0/24, 192.168.2.0/24."
                ) from None
        networks = list(dict.fromkeys(networks))
        if len(networks) > MAX_DISCOVERY_NETWORKS:
            raise ConfigError(
                f"[printer {printer}] discovery_networks lists {len(networks)} networks; "
                f"at most {MAX_DISCOVERY_NETWORKS}"
            )
        out["networks"] = networks
    return out


def _boolean(printer: str, options: Mapping[str, str], option: str) -> bool:
    value = BOOLEANS.get(options.get(option, "true").strip().lower())
    if value is None:
        raise ConfigError(
            f"[printer {printer}] {option} must be true or false, not {options[option]!r}"
        )
    return value


def _reason(error: BaseException, url: str | None) -> str:
    if isinstance(error, asyncio.TimeoutError):
        return f"no answer within {CHECK_TIMEOUT_S}s"
    return scrub_url(error, url)
