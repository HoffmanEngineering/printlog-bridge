"""Finds a remote printer again after its IP address changes (`discovery = true`, the default).

On every connect the locator reads `machine.system_info` and compares the printer's
`Fingerprint` with the one remembered in its state file: the first connect records it, and a
different printer at the address is refused (`WrongPrinter`). When the printer can't be reached
for `DISCOVERY_AFTER_S`, or a different printer answers at its address, the locator looks for
the same fingerprint on the printer's last known /24, through zeroconf first and then a scan,
at most once every `SCAN_INTERVAL_S`. A match becomes the printer's address in the state file;
the config is never changed. Changing `url` in the config forgets all of it, so the config
always wins.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import time
from collections.abc import Awaitable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Callable

from printlog_bridge.core.images import mask_url
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.status import NullReport, PrinterReport

from . import discovery
from .discovery import Fingerprint, Found
from .session import DISCOVERY_TOPIC, Call, MoonrakerRpcError, WrongPrinter

log = logging.getLogger(__name__)

DISCOVERY_AFTER_S = 3 * 60
"""How long the printer must be unreachable before it is looked for."""
SCAN_INTERVAL_S = 10 * 60
"""The least time between two searches for the same printer."""
MAX_NETWORKS = 4
"""Automatic search covers at most this many /24s; `networks` in the config can name more."""

Scan = Callable[..., Awaitable[list[Found]]]
Browse = Callable[[], Awaitable[list[tuple[str, int]]]]


class Locator:
    def __init__(
        self,
        printer: str,
        *,
        configured_url: str,
        state: PrinterState,
        api_key: str | None = None,
        report: PrinterReport | None = None,
        after: float = DISCOVERY_AFTER_S,
        interval: float = SCAN_INTERVAL_S,
        zeroconf: bool = True,
        networks: Sequence[ipaddress.IPv4Network] = (),
        timeout: float = discovery.PROBE_TIMEOUT_S,
        on_moved: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        scan: Scan = discovery.scan,
        browse: Browse | None = None,
    ) -> None:
        self.printer = printer
        self.configured_url = configured_url
        self.state = state
        self.api_key = api_key
        self.report = report or NullReport()
        self.after = after
        self.interval = interval
        self.zeroconf = zeroconf
        self.configured_networks = list(networks)
        """Where to search instead of the printer's last known networks, when given."""
        self.timeout = timeout
        """How long to wait for each address to answer."""
        self.on_moved = on_moved
        self.clock = clock
        self._scan = scan
        self._browse = browse or discovery.browse_mdns
        self._searched_at: float | None = None
        self._unidentifiable = False

        config = config_key(configured_url)
        if self.state.discovery.get("config") != config:
            # A new url in the config: trust it, and learn who is there afresh.
            self.state.discovery = {"config": config}
            self.state.save()

    @property
    def url(self) -> str:
        """Where to connect: the configured url, at the address the printer was last found."""
        address = self.state.discovery.get("address")
        return discovery.with_host(self.configured_url, address) if address else self.configured_url

    @property
    def fingerprint(self) -> Fingerprint | None:
        data = self.state.discovery.get("fingerprint")
        return Fingerprint.from_json(data) if isinstance(data, Mapping) else None

    def started(self) -> None:
        """Tells the report about a move found by an earlier run."""
        moved = self.state.discovery.get("moved")
        if isinstance(moved, Mapping) and self.url != self.configured_url:
            self.report.moved(moved["from"], moved["to"], moved.get("at"))

    async def verify(self, call: Call) -> None:
        """Checks the printer just connected to is this one. Raises WrongPrinter if not."""
        try:
            result = await call("machine.system_info", None)
            info = result["system_info"]
        except (MoonrakerRpcError, KeyError, TypeError) as e:
            log.warning(
                "[printer %s] couldn't read machine.system_info (%s), so the bridge can't "
                "recognise this printer at another address",
                self.printer, e,
            )  # fmt: skip
            return
        seen = Fingerprint.from_system_info(info)
        if not seen:
            if not self._unidentifiable:
                log.warning(
                    "[printer %s] Moonraker reports neither a MAC address nor an instance id, "
                    "so the bridge can't find this printer if its address changes. Give it a "
                    "DHCP reservation on the router.",
                    self.printer,
                )
            self._unidentifiable = True
            return

        known = self.fingerprint
        if known is not None and not known.matches(seen):
            raise WrongPrinter(
                f"a different printer answers at {discovery.host_of(self.url)}, not the one "
                f"[printer {self.printer}] was first seen as there (its address must have "
                "changed), so the bridge won't log its jobs and looks for the right one. If "
                f'this really is the same printer with a new controller, delete "discovery" '
                f"from {self.state.path} and restart the bridge."
            )
        if known is None:
            log.info(
                "[printer %s] remembered this printer's identity, to find it again if its "
                "address changes",
                self.printer,
            )
        if known != seen:
            self.state.discovery["fingerprint"] = seen.to_json()
            self.state.save()
        self.report.warn(DISCOVERY_TOPIC, None)

    def due(self, down_for: float, wrong: bool) -> bool:
        """Whether to search now: the printer has been unreachable long enough, or another
        printer is at its address, and the last search was long enough ago."""
        if self.fingerprint is None or not (wrong or down_for >= self.after):
            return False
        return self._searched_at is None or self.clock() - self._searched_at >= self.interval

    async def relocate(self) -> str | None:
        """Looks for this printer on its last known networks. Returns its new URL and remembers
        it, or None when it isn't found (or is still where it was)."""
        known = self.fingerprint
        if known is None:
            return None
        self._searched_at = self.clock()
        current = self.url
        networks = self.networks()
        if not networks:
            log.warning(
                "[printer %s] can't look for the printer: %s isn't a private IPv4 address and "
                "the printer didn't report one",
                self.printer, mask_url(current),
            )  # fmt: skip
            return None

        wanted = _matching(known)
        found = await self._from_zeroconf(current, wanted, networks)
        if found is None:
            log.info(
                "[printer %s] looking for the printer on %s",
                self.printer, ", ".join(str(n) for n in networks),
            )  # fmt: skip
            matches = await self._scan(
                networks,
                lambda address: discovery.http_base(discovery.with_host(current, address)),
                api_key=self.api_key,
                timeout=self.timeout,
                wanted=wanted,
            )
            found = matches[0] if matches else None
        if found is None:
            log.info("[printer %s] not found; looking again in %gs", self.printer, self.interval)
            return None

        new_url = discovery.with_host(current, found.address)
        if new_url == current:
            return None
        self._moved(current, new_url, found.fingerprint)
        return new_url

    def networks(self) -> list[ipaddress.IPv4Network]:
        """The networks to search: those in the config, or else the /24 of the current address
        and of each address the printer reported."""
        if self.configured_networks:
            return list(self.configured_networks)
        hosts = [discovery.host_of(self.url)]
        known = self.fingerprint
        if known is not None:
            hosts.extend(known.addresses)
        networks = [n for n in (discovery.subnet_of(h) for h in hosts) if n is not None]
        return list(dict.fromkeys(networks))[:MAX_NETWORKS]

    async def _from_zeroconf(
        self, current: str, wanted: Callable[[Found], bool], networks: list
    ) -> Found | None:
        if not self.zeroconf:
            return None
        try:
            answers = await self._browse()
        except Exception as e:  # zeroconf is only a shortcut; the scan still runs
            log.debug("[printer %s] zeroconf failed: %r", self.printer, e)
            return None
        for address, _port in answers:
            if not any(ipaddress.ip_address(address) in n for n in networks):
                continue
            base = discovery.http_base(discovery.with_host(current, address))
            found = await discovery.probe(base, api_key=self.api_key, timeout=self.timeout)
            if found is not None and wanted(found):
                return found
        return None

    def _moved(self, old_url: str, new_url: str, seen: Fingerprint) -> None:
        old, new = discovery.host_of(old_url), discovery.host_of(new_url)
        at = _now()
        self.state.discovery.update(
            address=new, fingerprint=seen.to_json(), moved={"from": old, "to": new, "at": at}
        )
        self.state.save()
        log.warning(
            "[printer %s] the printer moved from %s to %s; using the new address (the config "
            "is unchanged). A DHCP reservation on the router keeps it in one place.",
            self.printer, old, new,
        )  # fmt: skip
        self.report.moved(old, new, at)
        if self.on_moved is not None:
            self.on_moved(new_url)


def config_key(configured_url: str) -> str:
    """What the state file remembers of the configured url, to notice when it changes: a
    hash, so a password in the url never reaches the state file."""
    return hashlib.sha256(configured_url.encode()).hexdigest()[:16]


def last_found(state: PrinterState, configured_url: str) -> str | None:
    """The address the printer was last found at, when that isn't the configured one. Reads
    `state` without changing it, so `doctor` can use it too."""
    if state.discovery.get("config") != config_key(configured_url):
        return None  # the url changed since: the Locator starts afresh from it
    address = state.discovery.get("address")
    if not isinstance(address, str) or not address:
        return None
    return None if address == discovery.host_of(configured_url) else address


def _matching(known: Fingerprint) -> Callable[[Found], bool]:
    return lambda found: bool(found.fingerprint) and known.matches(found.fingerprint)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
