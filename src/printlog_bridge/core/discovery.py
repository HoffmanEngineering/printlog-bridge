"""Finding printers on the local network while the bridge runs, for 3D Print Log's "Found on your
network" (#100). The bridge reports what it finds; it never watches a printer because it found
it: the user connects one in the app, and the bridge hears that from its desired list
(core/bridge_sync.py, core/app_printers.py).

Each adapter says how its printers are found with a `Discovery` hook (`Adapter.discovery()`):

- `mdns_services`: the mDNS service types its printers advertise, and `from_mdns` to tell
  whether an answer is one of them (asking it, if it must).
- `udp_ports` and `from_datagram`: announcements to listen for (Bambu Lab's NOTIFY on 2021).
- `sweep`: an active search of the given networks, run only when the user asks for one ("Scan
  now" in the app): a subnet scan looks like a port scan to network security tools.

`DiscoveryService` runs them: one mDNS query for every service type every `MDNS_INTERVAL_S`
(a legacy query from an ephemeral port, core/mdns.py: nothing binds 5353), a listener on each UDP
port, and a sweep on demand. Only what a hook recognizes is kept; nothing else on the network is.
The table is bounded (`MAX_FOUND`), keyed by identity as 3D Print Log computes it, and forgets a
printer not seen for `TTL_S` (`SWEEP_TTL_S` for one only a sweep found).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from printlog_bridge.core import mdns
from printlog_bridge.core.mdns import MdnsAnswer

log = logging.getLogger(__name__)

MDNS_INTERVAL_S = 5 * 60
TTL_S = 30 * 60
SWEEP_TTL_S = 24 * 60 * 60
MAX_FOUND = 50
"""The API's cap on a check-in's `discovered`."""
MIN_SWEEP_INTERVAL_S = 60
MAX_TEXT = 100
MAX_MACS = 8
_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

KINDS = ("moonraker", "octoprint", "prusalink", "bambu", "sdcp", "duet", "flashforge", "anycubic")
"""What 3D Print Log accepts as a discovered printer's `kind`."""
DEFAULT_PORTS = {"moonraker": 7125, "bambu": 8883, "prusalink": 80, "octoprint": 80, "sdcp": 3030,
                 "duet": 80, "flashforge": 8898, "anycubic": 18910}  # fmt: skip

_PRIVATE = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7")
)
"""What 3D Print Log takes as a private address (BridgeRules.IsPrivate)."""


def is_private(address: str) -> bool:
    """A private IP literal, as 3D Print Log counts one: RFC 1918, IPv4 link-local, IPv6 unique
    local. Never a name, loopback or a zone id."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in _PRIVATE)


def clean_mac(text: str) -> str | None:
    """A MAC address as `aa:bb:cc:dd:ee:ff`, or None for anything else (zero, broadcast or
    multicast included, which 3D Print Log refuses)."""
    digits = re.sub(r"[:\-.]", "", text.strip().lower())
    if not re.fullmatch(r"[0-9a-f]{12}", digits):
        return None
    if digits in ("000000000000", "ffffffffffff") or int(digits[1], 16) & 1:
        return None
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", str(value)).strip()[:MAX_TEXT].strip()
    return cleaned or None


@dataclass(frozen=True)
class Found:
    """A printer an adapter recognized on the network: exactly what a check-in reports of it."""

    kind: str
    address: str
    port: int
    model: str | None = None
    name: str | None = None
    serial: str | None = None
    mainboard_id: str | None = None
    macs: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        """The key 3D Print Log tells printers apart by: the serial, else the mainboard id,
        else the lowest MAC and the port (two Moonrakers on one Pi share MACs), else the
        address and port."""
        if self.serial:
            return f"{self.kind}:sn:{self.serial.upper()}"
        if self.mainboard_id:
            return f"{self.kind}:mb:{self.mainboard_id.lower()}"
        if self.macs:
            return f"{self.kind}:mac:{min(self.macs)}:{self.port}"
        return f"{self.kind}:ip:{self.address}:{self.port}"

    def cleaned(self) -> Found | None:
        """This printer as 3D Print Log takes it, or None when it can't be reported (an
        address that isn't private, a kind it doesn't know)."""
        if self.kind not in KINDS or not is_private(self.address):
            return None
        if not 0 < int(self.port) < 65536:
            return None
        serial = self.serial if self.serial and _SERIAL.fullmatch(self.serial) else None
        board = self.mainboard_id
        board = board if board and _SERIAL.fullmatch(board) else None
        macs = sorted({m for m in (clean_mac(x) for x in self.macs) if m})[:MAX_MACS]
        return replace(
            self,
            address=str(ipaddress.ip_address(self.address)),
            port=int(self.port),
            model=_text(self.model),
            name=_text(self.name),
            serial=serial,
            mainboard_id=board,
            macs=tuple(macs),
        )

    def to_json(self, configured: bool = False) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "address": self.address,
            "port": self.port,
            "model": self.model,
            "name": self.name,
            "serial": self.serial,
            "mainboardId": self.mainboard_id,
            "macs": list(self.macs),
            "configured": configured,
        }

    def same_printer(self, other: Found) -> bool:
        """Whether `other` is this printer: the same serial or mainboard id, a MAC in common
        (on the same port), or the same address and port."""
        if self.kind != other.kind:
            return False
        if self.serial and other.serial:
            return self.serial.upper() == other.serial.upper()
        if self.mainboard_id and other.mainboard_id:
            return self.mainboard_id.lower() == other.mainboard_id.lower()
        if self.macs and other.macs and set(self.macs) & set(other.macs):
            return self.port == other.port
        return self.address == other.address and self.port == other.port


class Discovery:
    """An adapter's discovery hook. Every method is optional; the default finds nothing."""

    kind: str = ""
    mdns_services: tuple[str, ...] = ()
    udp_ports: tuple[int, ...] = ()

    async def from_mdns(self, answer: MdnsAnswer) -> Found | None:
        """The printer behind an mDNS answer for one of `mdns_services`, or None when it isn't
        one of this adapter's. May ask the printer (with a short timeout); never sends a key."""
        return None

    def from_datagram(self, port: int, data: bytes, sender: str) -> Found | None:
        """The printer that sent `data` to `port`, or None for anything else. No I/O."""
        return None

    async def sweep(self, networks: Sequence[ipaddress.IPv4Network]) -> list[Found]:
        """Every printer of this kind on `networks` (each a private /24 or smaller)."""
        return []


@dataclass
class _Entry:
    found: Found
    seen: float
    expires: float
    sources: set[str] = field(default_factory=set)


Ask = Callable[[Sequence[str], float], Awaitable[Sequence[MdnsAnswer]]]
Bind = Callable[[int, Callable[[bytes, str], None]], Awaitable[Any]]


async def bind_udp(port: int, on_datagram: Callable[[bytes, str], None]) -> Any:
    """Listens on UDP `port` (beside anything else listening there, such as Bambu Studio) and
    hands each datagram to `on_datagram`. Returns the transport. Raises OSError."""

    class Listener(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: Any) -> None:
            on_datagram(data, addr[0])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(Listener, sock=sock)
    return transport


class DiscoveryService:
    def __init__(
        self,
        hooks: Iterable[Discovery],
        *,
        mdns_interval: float = MDNS_INTERVAL_S,
        ttl: float = TTL_S,
        sweep_ttl: float = SWEEP_TTL_S,
        max_found: int = MAX_FOUND,
        min_sweep_interval: float = MIN_SWEEP_INTERVAL_S,
        ask: Ask = mdns.ask,
        bind: Bind = bind_udp,
        clock: Callable[[], float] = time.monotonic,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.hooks = [h for h in hooks if h is not None]
        self.mdns_interval = mdns_interval
        self.ttl = ttl
        self.sweep_ttl = sweep_ttl
        self.max_found = max_found
        self.min_sweep_interval = min_sweep_interval
        self.ask = ask
        self.listen_on = bind
        self.clock = clock
        self.on_change = on_change
        self._entries: dict[str, _Entry] = {}
        self._last_sweep: float | None = None
        self._sweeping: asyncio.Lock | None = None
        self.sources: dict[str, str] = {}
        """Each passive source and how it is doing (`listening`, or why not), for status."""
        self._transports: list[Any] = []

    @property
    def services(self) -> list[str]:
        return list(dict.fromkeys(s for h in self.hooks for s in h.mdns_services))

    # -- what was found ------------------------------------------------------------------------

    def found(self) -> list[Found]:
        """What is known now, oldest first, after forgetting what expired."""
        now = self.clock()
        for key in [k for k, e in self._entries.items() if e.expires <= now]:
            del self._entries[key]
        return [e.found for e in sorted(self._entries.values(), key=lambda e: e.seen)]

    def heard(self, found: Found | None, source: str, *, swept: bool = False) -> Found | None:
        """Records a printer a hook recognized. Returns it as kept, or None when it was
        dropped (not reportable)."""
        if found is None:
            return None
        cleaned = found.cleaned()
        if cleaned is None:
            log.debug("discovery: dropped %s from %s (not reportable)", found, source)
            return None
        now = self.clock()
        life = self.sweep_ttl if swept else self.ttl
        key = cleaned.identity
        entry = self._entries.get(key)
        if entry is None:
            # The same printer under a weaker identity (an address, before its MAC was known).
            entry = next((e for e in self._entries.values() if e.found.same_printer(cleaned)), None)
            if entry is not None:
                del self._entries[entry.found.identity]
        if entry is None:
            entry = _Entry(cleaned, now, now + life)
            log.info(
                "discovery: found a %s printer at %s:%s%s",
                cleaned.kind, cleaned.address, cleaned.port,
                f" ({cleaned.name})" if cleaned.name else "",
            )  # fmt: skip
            changed = True
        else:
            changed = entry.found != _merge(entry.found, cleaned)
            entry.found = _merge(entry.found, cleaned)
            entry.seen = now
            entry.expires = max(entry.expires, now + life)
        entry.sources.add(source)
        self._entries[entry.found.identity] = entry
        self._bound()
        if changed and self.on_change is not None:
            self.on_change()
        return entry.found

    def _bound(self) -> None:
        while len(self._entries) > self.max_found:
            oldest = min(self._entries.values(), key=lambda e: e.seen)
            del self._entries[oldest.found.identity]

    def _known(self, kind: str, address: str, port: int) -> Found | None:
        """A printer already found at this address, still fresh: refreshed, not asked again."""
        now = self.clock()
        for entry in self._entries.values():
            f = entry.found
            if f.kind == kind and f.address == address and f.port == port and entry.expires > now:
                entry.seen = now
                entry.expires = max(entry.expires, now + self.ttl)
                return f
        return None

    # -- passive -------------------------------------------------------------------------------

    async def run(self) -> None:
        """Listens and asks until cancelled."""
        try:
            await self._listen()
            while True:
                await self.mdns_round()
                await asyncio.sleep(self.mdns_interval)
        finally:
            for transport in self._transports:
                with contextlib.suppress(Exception):
                    transport.close()
            self._transports = []

    async def _listen(self) -> None:
        for port in dict.fromkeys(p for h in self.hooks for p in h.udp_ports):
            hooks = [h for h in self.hooks if port in h.udp_ports]

            def on_datagram(data: bytes, sender: str, port: int = port, hooks=hooks) -> None:
                if len(data) > 4096:
                    return
                for hook in hooks:
                    try:
                        found = hook.from_datagram(port, data, sender)
                    except Exception:  # a hook's bug must not stop listening
                        log.debug("discovery: %s: unreadable datagram", hook.kind, exc_info=True)
                        continue
                    if found is not None:
                        self.heard(found, f"udp {port}")
                        return

            name = f"UDP {port} ({', '.join(h.kind for h in hooks)})"
            try:
                self._transports.append(await self.listen_on(port, on_datagram))
            except OSError as e:
                self.sources[name] = f"can't listen: {e.strerror or e}"
                log.warning(
                    "discovery: can't listen on UDP port %s (%s), so %s printers are found only "
                    "by a scan. In Docker the container needs the host's network.",
                    port, e.strerror or e, ", ".join(h.kind for h in hooks),
                )  # fmt: skip
            else:
                self.sources[name] = "listening"
        if self.services:
            self.sources["mDNS"] = f"asking every {self.mdns_interval / 60:g} min"

    async def mdns_round(self, wait: float = mdns.WAIT_S) -> None:
        services = self.services
        if not services:
            return
        try:
            answers = await self.ask(services, wait)
        except Exception:
            log.debug("discovery: the mDNS query failed", exc_info=True)
            return
        for answer in answers:
            if not is_private(answer.address):
                continue
            for hook in self.hooks:
                if answer.service not in [s.strip(".").lower() for s in hook.mdns_services]:
                    continue
                if self._known(hook.kind, answer.address, answer.port) is not None:
                    break
                try:
                    found = await hook.from_mdns(answer)
                except Exception:
                    log.debug("discovery: %s couldn't read %s", hook.kind, answer, exc_info=True)
                    continue
                if found is not None:
                    self.heard(found, "mdns")
                    break

    # -- on demand -----------------------------------------------------------------------------

    async def sweep(self, networks: Sequence[ipaddress.IPv4Network]) -> bool:
        """One active search of `networks` by every hook, and an mDNS round. False (and nothing
        done) within `min_sweep_interval` of the last one."""
        if self._sweeping is None:
            self._sweeping = asyncio.Lock()
        async with self._sweeping:
            now = self.clock()
            if self._last_sweep is not None and now - self._last_sweep < self.min_sweep_interval:
                return False
            self._last_sweep = now
            log.info(
                "discovery: scanning %s, as asked in 3D Print Log",
                ", ".join(str(n) for n in networks) or "no network",
            )
            results = await asyncio.gather(
                self.mdns_round(),
                *(h.sweep(networks) for h in self.hooks),
                return_exceptions=True,
            )
            for hook, result in zip(self.hooks, results[1:]):
                if isinstance(result, BaseException):
                    log.warning("discovery: the %s scan failed: %r", hook.kind, result)
                    continue
                for found in result:
                    self.heard(found, "scan", swept=True)
            return True

    def status(self) -> dict[str, Any]:
        found = self.found()
        return {
            "sources": dict(self.sources),
            "found": len(found),
            "kinds": sorted({f.kind for f in found}),
        }


def _merge(old: Found, new: Found) -> Found:
    """What is known of a printer: the newest answer, keeping what it left out."""
    return replace(
        new,
        model=new.model or old.model,
        name=new.name or old.name,
        serial=new.serial or old.serial,
        mainboard_id=new.mainboard_id or old.mainboard_id,
        macs=new.macs or old.macs,
    )


def networks_of(addresses: Iterable[str]) -> list[ipaddress.IPv4Network]:
    """The /24 of each private IPv4 address, once each."""
    out: list[ipaddress.IPv4Network] = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.version != 4 or not is_private(address) or ip.is_link_local:
            continue
        network = ipaddress.ip_network(f"{ip}/24", strict=False)
        if network not in out:
            out.append(network)  # type: ignore[arg-type]
    return out


def default_route_address() -> str | None:
    """This host's address on its default route. Connecting a UDP socket sends nothing: it only
    asks the kernel which address it would send from."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1: any address with a route will do
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
