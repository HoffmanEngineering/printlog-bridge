"""Printers connected in the 3D Print Log app (#100): the desired list, applied while the bridge
runs, without a restart.

Each entry becomes a watcher named `app-<id>` (its state file is `printers/app-<id>.json`, so its
instance id and jobs are kept by the entry's id). The bridge watches only what the list names, and
never a printer it merely found on the network.

- **The config file wins.** An entry for a printer a `[printer]` section already watches (the
  same 3D Print Log printer, serial, MAC, or address and port) is skipped.
- **Only private addresses.** A host name is resolved, and the entry is refused when any address
  it resolves to isn't private. The adapter is given the address, not the name.
- **Credentials, until story 14** brings them from the app encrypted to the bridge, come from an
  `[app_printer <id>]` section of the config file (`access_code` for a Bambu Lab printer,
  `password` for PrusaLink, `moonraker_api_key` for a Moonraker with `[authorization]`). An entry
  that needs one and has none isn't started: the log, `status` and `doctor` say what to add. The
  bridge reads the config again when it changes, so adding it starts the printer.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from printlog_bridge.core.adapter import Adapter, NeedsCredentials
from printlog_bridge.core.config import BridgeConfig, ConfigError, PrinterConfig, app_section
from printlog_bridge.core.discovery import DEFAULT_PORTS, Found, is_private
from printlog_bridge.core.status import StatusBoard
from printlog_bridge.core.watchers import Watchers

log = logging.getLogger(__name__)

NAME_PREFIX = "app-"
RESOLVE_TIMEOUT_S = 5

WATCHING = "watching"
NEEDS_CREDENTIALS = "needs credentials"
CONFIG_WINS = "in the config file"
REFUSED = "refused"


def watcher_name(entry_id: int) -> str:
    return f"{NAME_PREFIX}{entry_id}"


@dataclass(frozen=True)
class DesiredPrinter:
    """One entry of the desired list, as 3D Print Log sends it."""

    id: int
    printer_id: int
    kind: str
    host: str
    port: int | None = None
    serial: str | None = None
    printer_name: str | None = None
    macs: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, data: Any) -> DesiredPrinter | None:
        if not isinstance(data, Mapping):
            return None
        try:
            entry_id, printer_id = int(data["id"]), int(data["printerId"])
            kind, host = str(data["kind"]), str(data["host"]).strip()
        except (KeyError, TypeError, ValueError):
            return None
        if entry_id <= 0 or printer_id <= 0 or not host:
            return None
        port = data.get("port")
        macs = data.get("macs")
        return cls(
            id=entry_id,
            printer_id=printer_id,
            kind=kind,
            host=host,
            port=int(port) if isinstance(port, int) and 0 < port < 65536 else None,
            serial=str(data["serial"]) if data.get("serial") else None,
            printer_name=str(data["printerName"]) if data.get("printerName") else None,
            macs=tuple(str(m) for m in macs) if isinstance(macs, list) else (),
        )


Resolve = Callable[[str], Awaitable[str]]


class Refused(Exception):
    pass


async def resolve_private(host: str) -> str:
    """`host`'s IPv4 address, when every address it resolves to is private. Raises Refused."""
    if is_private(host):
        return str(ipaddress.ip_address(host))
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise Refused(f"{host} isn't a private address")
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), RESOLVE_TIMEOUT_S
        )
    except (OSError, asyncio.TimeoutError) as e:
        raise Refused(f"{host} doesn't resolve here ({e or 'timed out'})") from None
    addresses = list(dict.fromkeys(str(info[4][0]).split("%")[0] for info in infos))
    public = [a for a in addresses if not is_private(a)]
    if not addresses or public:
        raise Refused(f"{host} resolves to {', '.join(public) or 'nothing'}, not a private address")
    v4 = [a for a in addresses if ipaddress.ip_address(a).version == 4]
    return (v4 or addresses)[0]


class AppPrinters:
    def __init__(
        self,
        watchers: Watchers,
        config: BridgeConfig,
        adapters: Mapping[str, type[Adapter]],
        board: StatusBoard,
        resolve: Resolve = resolve_private,
    ) -> None:
        self.watchers = watchers
        self.config = config
        self.adapters = adapters
        self.board = board
        self.resolve = resolve
        self.desired: list[DesiredPrinter] = []
        self.states: dict[int, dict[str, Any]] = {}
        """Each entry's state for `status`: `printer`, `kind`, `host`, `state`, `detail`."""
        self._running: dict[int, tuple[DesiredPrinter, tuple]] = {}
        """What each started watcher was started from: the entry and its secrets."""
        self._said: set[tuple[int, str]] = set()
        self._lock: asyncio.Lock | None = None

    async def apply(self, entries: Sequence[DesiredPrinter]) -> None:
        """Makes the watched app printers the ones `entries` names."""
        self.desired = list(entries)
        await self._apply()

    async def use_config(self, config: BridgeConfig) -> None:
        """The config file changed (an `[app_printer]` section, say): applies the list again."""
        self.config = config
        await self._apply()

    async def _apply(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            wanted = {e.id: e for e in self.desired}
            for entry_id in list(self._running):
                entry, secrets = self._running[entry_id]
                if wanted.get(entry_id) != entry or self._secrets(entry_id) != secrets:
                    await self._stop(entry_id)
            states = {}
            for entry in self.desired:
                if entry.id in self._running:
                    states[entry.id] = self.states[entry.id]
                    continue
                states[entry.id] = await self._start(entry)
            self.states = states
            self.board.bridge(app_printers={str(k): v for k, v in states.items()})

    def _secrets(self, entry_id: int) -> tuple:
        return tuple(sorted(self.config.app_secrets.get(entry_id, {}).items()))

    async def _stop(self, entry_id: int) -> None:
        self._running.pop(entry_id, None)
        await self.watchers.stop(watcher_name(entry_id))

    async def _start(self, entry: DesiredPrinter) -> dict[str, Any]:
        name = watcher_name(entry.id)
        state = {
            "printer": entry.printer_id,
            "printer_name": entry.printer_name,
            "kind": entry.kind,
            "host": entry.host,
            "state": REFUSED,
            "detail": None,
        }
        adapter_type = self.adapters.get(entry.kind)
        if adapter_type is None:
            return self._say(entry, state, REFUSED, f"this bridge has no {entry.kind} adapter")
        try:
            address = await self.resolve(entry.host)
        except Refused as e:
            return self._say(entry, state, REFUSED, f"won't connect: {e}")
        port = entry.port or DEFAULT_PORTS.get(entry.kind)
        found = Found(entry.kind, address, port or 0, serial=entry.serial, macs=entry.macs)
        for printer in self.config.printers:
            if (
                printer.name == name
                or printer.printer_id == entry.printer_id
                or printer.adapter.watches(found)
            ):
                return self._say(
                    entry,
                    state,
                    CONFIG_WINS,
                    f"[printer {printer.name}] in {self.config.path} watches it, and the config "
                    "file wins",
                    level=logging.INFO,
                )
        secrets = self.config.app_secrets.get(entry.id, {})
        try:
            options = adapter_type.app_options(address, entry.port, entry.serial, secrets)
            if options is None:
                return self._say(
                    entry, state, REFUSED, f"the {entry.kind} adapter can't be set up from the app"
                )
            adapter = adapter_type.from_config(name, options)
        except NeedsCredentials as e:
            return self._say(
                entry,
                state,
                NEEDS_CREDENTIALS,
                f"needs {e}: add it under {app_section(entry.id)} in {self.config.path} (the "
                "bridge starts watching as soon as the file is saved)",
            )
        except ConfigError as e:
            return self._say(entry, state, REFUSED, f"can't be set up: {e}")
        printer = PrinterConfig(
            name,
            adapter,
            entry.printer_id,
            adoption=self.config.adoption,
            slicer_settings=self.config.slicer_settings,
        )
        self.watchers.start(printer, fatal=False)
        self._running[entry.id] = (entry, self._secrets(entry.id))
        state.update(state=WATCHING, detail=adapter.describe())
        return state

    def _say(
        self,
        entry: DesiredPrinter,
        state: dict[str, Any],
        what: str,
        detail: str,
        level: int = logging.WARNING,
    ) -> dict[str, Any]:
        """Logs why an entry isn't watched, once per reason."""
        state.update(state=what, detail=detail)
        if (entry.id, detail) not in self._said:
            self._said.add((entry.id, detail))
            log.log(
                level,
                "app printer %s (printer %s in 3D Print Log, %s at %s): %s",
                entry.id, entry.printer_id, entry.kind, entry.host, detail,
            )  # fmt: skip
        return state

    def watching(self) -> list[str]:
        return [watcher_name(i) for i in self._running]
