"""Printers connected in the 3D Print Log app (#100): the desired list, applied while the bridge
runs, without a restart.

Each entry becomes a watcher named `app-<id>` (its state file is `printers/app-<id>.json`, so its
instance id and jobs are kept by the entry's id). The bridge watches only what the list names, and
never a printer it merely found on the network.

- **The config file wins.** An entry for a printer a `[printer]` section already watches (the
  same 3D Print Log printer, serial, MAC, or address and port) is skipped.
- **Only private addresses.** A host name is resolved, and the entry is refused when any address
  it resolves to isn't private. The adapter is given the address, not the name.
- **Credentials** (`access_code` for a Bambu Lab printer, `password` for PrusaLink,
  `moonraker_api_key` for a Moonraker with `[authorization]`) come from the app, sealed to this
  bridge's key (core/credentials.py), and are opened here, in memory only. An
  `[app_printer <id>]` section of the config file still works, and **wins** for any field it sets:
  the config file is the bridge's own (and the app can't vouch for what it forwards: anyone with
  the public key can seal a value), it's the rule for printers too, and setups made before
  sealing keep exactly what they had. An entry that needs a credential and has none isn't
  started: the log, `status` and `doctor` say so. A changed value (in the app, or in the file,
  which the bridge reads again when it changes) restarts the printer with it.
- **Credential issues** for 3D Print Log's check-in: a sealed value that doesn't open here
  (`undecryptable`) or isn't a credential (`invalid_plaintext`), a needed one that's absent
  (`missing`), and one from the app the printer refused (`rejected_by_printer`, as the adapter
  reports through `PrinterReport.credentials_refused`). Worked out again for every check-in, so
  fixed ones clear.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from printlog_bridge.core.adapter import Adapter, NeedsCredentials
from printlog_bridge.core.config import BridgeConfig, ConfigError, PrinterConfig, app_section
from printlog_bridge.core.credentials import (
    InvalidPlaintext,
    KeyUnavailable,
    Opener,
    Undecryptable,
)
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

MISSING = "missing"
UNDECRYPTABLE = "undecryptable"
INVALID_PLAINTEXT = "invalid_plaintext"
REJECTED_BY_PRINTER = "rejected_by_printer"
MAX_ISSUES = 200
FROM_APP = "app"
FROM_CONFIG = "config"
ISSUE_WORDS = {
    MISSING: "needed, and not entered in the app",
    UNDECRYPTABLE: "entered in the app, but doesn't open with this bridge's key: enter it again",
    INVALID_PLAINTEXT: "entered in the app, but isn't a valid credential: enter it again",
    REJECTED_BY_PRINTER: "entered in the app, and the printer refused it: check it and enter it "
    "again",
}


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
    secret_fields: tuple[str, ...] = ()
    """The credentials 3D Print Log takes for this kind (`secretFields`)."""
    sealed: tuple[tuple[str, str], ...] = dataclass_field(default=(), repr=False)
    """Each field's sealed value (`secrets`), as sent: only this bridge's key opens them."""

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
        fields = data.get("secretFields")
        sealed = {}
        for secret in data.get("secrets") or []:
            if not isinstance(secret, Mapping) or secret.get("stale") is True:
                continue
            name, value = secret.get("field"), secret.get("sealed")
            if isinstance(name, str) and isinstance(value, str):
                sealed[name] = value
        return cls(
            id=entry_id,
            printer_id=printer_id,
            kind=kind,
            host=host,
            port=int(port) if isinstance(port, int) and 0 < port < 65536 else None,
            serial=str(data["serial"]) if data.get("serial") else None,
            printer_name=str(data["printerName"]) if data.get("printerName") else None,
            macs=tuple(str(m) for m in macs) if isinstance(macs, list) else (),
            secret_fields=(
                tuple(f for f in fields if isinstance(f, str)) if isinstance(fields, list) else ()
            ),
            sealed=tuple(sorted(sealed.items())),
        )


Resolve = Callable[[str], Awaitable[str]]


@dataclass
class Credentials:
    """One entry's credentials, worked out from the app's sealed values and the config file."""

    values: dict[str, str] = dataclass_field(default_factory=dict, repr=False)
    sources: dict[str, str] = dataclass_field(default_factory=dict)
    """`app` or `config`, by field."""
    issues: dict[str, str] = dataclass_field(default_factory=dict)
    """Sealed values that didn't open: `undecryptable` or `invalid_plaintext`, by field."""
    overridden: tuple[str, ...] = ()
    """Fields set both ways, where the config file's value is used."""

    @property
    def key(self) -> tuple:
        return tuple(sorted(self.values.items()))


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
        opener: Opener | None = None,
        on_secrets: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.watchers = watchers
        self.config = config
        self.adapters = adapters
        self.board = board
        self.resolve = resolve
        self.opener = opener
        self.on_secrets = on_secrets
        """Told every value opened from the app after each apply, for the log and trace
        scrubbers."""
        self.desired: list[DesiredPrinter] = []
        self.states: dict[int, dict[str, Any]] = {}
        """Each entry's state for `status`: `printer`, `kind`, `host`, `state`, `detail`, and
        `credentials` (each field's source or issue, in words: never a value)."""
        self._running: dict[int, tuple[DesiredPrinter, tuple]] = {}
        """What each started watcher was started from: the entry and its credentials."""
        self._credentials: dict[int, Credentials] = {}
        self._missing: dict[int, tuple[str, ...]] = {}
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

    async def refresh(self) -> None:
        """The key or the bridge's id changed: opens everything again."""
        await self._apply()

    async def _apply(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            wanted = {e.id: e for e in self.desired}
            self._credentials = {e.id: self._work_out(e) for e in self.desired}
            for entry_id in list(self._running):
                entry, secrets = self._running[entry_id]
                now = self._credentials.get(entry_id)
                if wanted.get(entry_id) != entry or now is None or now.key != secrets:
                    await self._stop(entry_id)
            states = {}
            for entry in self.desired:
                if entry.id in self._running:
                    states[entry.id] = self.states[entry.id]
                else:
                    states[entry.id] = await self._start(entry)
                states[entry.id]["credentials"] = self._describe(entry)
            self.states = states
            self._publish()
            if self.on_secrets is not None:
                self.on_secrets(self.opened())

    def _publish(self) -> None:
        self.board.bridge(app_printers={str(k): dict(v) for k, v in self.states.items()})

    def opened(self) -> list[str]:
        """Every value opened from the app, for the scrubbers. Never written down."""
        return [
            value
            for credentials in self._credentials.values()
            for name, value in credentials.values.items()
            if credentials.sources.get(name) == FROM_APP
        ]

    def _work_out(self, entry: DesiredPrinter) -> Credentials:
        """Opens `entry`'s sealed values and adds its `[app_printer]` section, which wins."""
        adapter_type = self.adapters.get(entry.kind)
        allowed = set(adapter_type.app_secrets) if adapter_type is not None else set()
        result = Credentials()
        for name, sealed in entry.sealed:
            if name not in allowed:
                continue
            try:
                if self.opener is None:
                    raise KeyUnavailable("no encryption key")
                result.values[name] = self.opener.open(entry.id, name, sealed)
                result.sources[name] = FROM_APP
            except InvalidPlaintext:
                result.issues[name] = INVALID_PLAINTEXT
            except (Undecryptable, KeyUnavailable):
                result.issues[name] = UNDECRYPTABLE
        local = {k: v for k, v in self.config.app_secrets.get(entry.id, {}).items() if v.strip()}
        result.overridden = tuple(sorted(set(local) & set(result.values)))
        for name, value in local.items():
            result.values[name] = value
            result.sources[name] = FROM_CONFIG
        for name in result.overridden:
            self._once(
                entry,
                f"{name} is set both in the app and under {app_section(entry.id)} in "
                f"{self.config.path}: the config file's is used (remove it there to use the "
                "app's)",
                logging.INFO,
            )
        return result

    def _describe(self, entry: DesiredPrinter) -> dict[str, str]:
        """Each credential's source or issue, in words, for `status`."""
        credentials = self._credentials.get(entry.id) or Credentials()
        issues = self._issues_of(entry)
        out = {}
        for name in sorted(set(credentials.sources) | set(issues)):
            if name in issues:
                out[name] = ISSUE_WORDS[issues[name]]
            elif credentials.sources[name] == FROM_APP:
                out[name] = "from the app"
            else:
                out[name] = f"from {app_section(entry.id)} in the config file" + (
                    ", which overrides the app's" if name in credentials.overridden else ""
                )
        return out

    def _issues_of(self, entry: DesiredPrinter) -> dict[str, str]:
        credentials = self._credentials.get(entry.id) or Credentials()
        issues = dict(credentials.issues)
        for name in self._missing.get(entry.id, ()):
            issues.setdefault(name, MISSING)
        if entry.id in self._running:
            refused = self.board.get(watcher_name(entry.id)).get("credentials_refused") or []
            for name in refused:
                source = credentials.sources.get(name)
                if source == FROM_APP:
                    issues.setdefault(name, REJECTED_BY_PRINTER)
                elif source is None:
                    issues.setdefault(name, MISSING)
        adapter_type = self.adapters.get(entry.kind)
        allowed = set(adapter_type.app_secrets) if adapter_type is not None else set()
        if entry.secret_fields:
            allowed &= set(entry.secret_fields)
        return {k: v for k, v in issues.items() if k in allowed}

    def credential_issues(self) -> list[dict[str, Any]]:
        """What the check-in reports: one code per entry and field, `[]` when all is well.
        Also brings `status`'s words up to date with what the printers said since."""
        issues = []
        changed = False
        for entry in self.desired:
            for name, code in sorted(self._issues_of(entry).items()):
                issues.append({"entryId": entry.id, "field": name, "code": code})
            state = self.states.get(entry.id)
            if state is not None:
                words = self._describe(entry)
                if state.get("credentials") != words:
                    state["credentials"] = words
                    changed = True
        if changed:
            self._publish()
        return issues[:MAX_ISSUES]

    async def _stop(self, entry_id: int) -> None:
        self._running.pop(entry_id, None)
        await self.watchers.stop(watcher_name(entry_id))

    async def _start(self, entry: DesiredPrinter) -> dict[str, Any]:
        name = watcher_name(entry.id)
        state: dict[str, Any] = {
            "printer": entry.printer_id,
            "printer_name": entry.printer_name,
            "kind": entry.kind,
            "host": entry.host,
            "state": REFUSED,
            "detail": None,
        }
        self._missing.pop(entry.id, None)
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
        credentials = self._credentials.get(entry.id) or self._work_out(entry)
        try:
            options = adapter_type.app_options(
                address, entry.port, entry.serial, dict(credentials.values)
            )
            if options is None:
                return self._say(
                    entry, state, REFUSED, f"the {entry.kind} adapter can't be set up from the app"
                )
            adapter = adapter_type.from_config(name, options)
        except NeedsCredentials as e:
            self._missing[entry.id] = e.fields
            unopened = [f for f in e.fields if f in credentials.issues]
            if unopened:
                detail = (
                    f"needs {e}: the {unopened[0]} was "
                    + (ISSUE_WORDS[credentials.issues[unopened[0]]])
                )
            else:
                detail = (
                    f"needs {e}: enter it for this printer in 3D Print Log (Bridges), or add it "
                    f"under {app_section(entry.id)} in {self.config.path} (the bridge starts "
                    "watching as soon as it has it)"
                )
            return self._say(entry, state, NEEDS_CREDENTIALS, detail)
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
        self._running[entry.id] = (entry, credentials.key)
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
        self._once(entry, detail, level)
        return state

    def _once(self, entry: DesiredPrinter, detail: str, level: int) -> None:
        if (entry.id, detail) not in self._said:
            self._said.add((entry.id, detail))
            log.log(
                level,
                "app printer %s (printer %s in 3D Print Log, %s at %s): %s",
                entry.id, entry.printer_id, entry.kind, entry.host, detail,
            )  # fmt: skip

    def watching(self) -> list[str]:
        return [watcher_name(i) for i in self._running]
