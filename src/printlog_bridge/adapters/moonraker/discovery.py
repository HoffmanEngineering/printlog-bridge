"""Finding Moonraker on the local network: a printer that changed its IP address, or every
printer for `printlog-bridge discover`.

Two ways to look, both bounded to private networks:

- Zeroconf: Moonraker's `[zeroconf]` component answers mDNS queries for `_moonraker._tcp`. One
  query goes to the mDNS multicast group, and the answers name each instance's address. It needs
  multicast, which doesn't reach a Docker container on bridge networking.
- A subnet scan: an HTTP `GET /server/info` to each address of a /24 on Moonraker's port. This
  works anywhere the printer can be reached, Docker's bridge networking included.

Either way a candidate is only a candidate: `probe` reads its `/server/info` and
`/machine/system_info`, and the caller compares its `Fingerprint` before using it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import socket
import struct
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from printlog_bridge import __version__

log = logging.getLogger(__name__)

DEFAULT_PORT = 7125
PROBE_TIMEOUT_S = 2
SCAN_CONCURRENCY = 64
MDNS_GROUP = ("224.0.0.251", 5353)
MDNS_SERVICE = "_moonraker._tcp.local"
MDNS_WAIT_S = 2
MAX_PREFIX = 24
"""Scans never cover more than a /24: 254 addresses."""

_NO_MAC = {"", "00:00:00:00:00:00"}


@dataclass(frozen=True)
class Fingerprint:
    """What identifies a Moonraker host whatever its address: its network interfaces' MAC
    addresses and Moonraker's own instance id (a UUID it keeps in its data folder)."""

    instance_id: str | None = None
    macs: frozenset[str] = frozenset()
    addresses: tuple[str, ...] = ()
    """The IPv4 addresses the host reports. Not identity: where to look for it next time."""

    @classmethod
    def from_system_info(cls, info: Mapping[str, Any]) -> Fingerprint:
        """From `machine.system_info`'s `system_info`."""
        instance_ids = info.get("instance_ids") or {}
        instance_id = instance_ids.get("moonraker") if isinstance(instance_ids, Mapping) else None
        macs = set()
        addresses = []
        network = info.get("network") or {}
        for name, interface in network.items() if isinstance(network, Mapping) else ():
            if name == "lo" or not isinstance(interface, Mapping):
                continue
            mac = str(interface.get("mac_address") or "").lower()
            if mac not in _NO_MAC:
                macs.add(mac)
            for address in interface.get("ip_addresses") or []:
                if (
                    isinstance(address, Mapping)
                    and address.get("family") == "ipv4"
                    and not address.get("is_link_local")
                ):
                    addresses.append(str(address.get("address")))
        return cls(str(instance_id) if instance_id else None, frozenset(macs), tuple(addresses))

    def __bool__(self) -> bool:
        return bool(self.instance_id or self.macs)

    def matches(self, other: Fingerprint) -> bool:
        """The same machine: the same Moonraker instance, or a network interface in common (so
        a reinstalled Moonraker on the same board, or the same Moonraker data on a new board,
        is still recognised)."""
        same_instance = self.instance_id is not None and self.instance_id == other.instance_id
        return same_instance or bool(self.macs & other.macs)

    def to_json(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "macs": sorted(self.macs),
            "addresses": list(self.addresses),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Fingerprint:
        return cls(
            data.get("instance_id") or None,
            frozenset(data.get("macs") or ()),
            tuple(data.get("addresses") or ()),
        )


@dataclass
class Found:
    """A Moonraker that answered at `address`:`port`."""

    address: str
    port: int
    server_info: Mapping[str, Any] = field(default_factory=dict)
    fingerprint: Fingerprint = field(default_factory=Fingerprint)
    printer_info: Mapping[str, Any] = field(default_factory=dict)
    """`/printer/info`, read only by `discover`."""
    tools: int | None = None
    """Extruders in `/printer/objects/list`, read only by `discover`."""
    unauthorized: bool = False
    """It answered 401 or 403: Moonraker with `[authorization]`, and no key for it."""


def http_base(url: str) -> str:
    """The http(s) base URL of a configured Moonraker URL, without the WebSocket path."""
    parts = urlsplit(url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    path = parts.path.rstrip("/")
    if path.endswith("/websocket"):
        path = path[: -len("/websocket")]
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def port_of(url: str) -> int:
    parts = urlsplit(url)
    if parts.port is not None:
        return parts.port
    return 443 if parts.scheme in ("https", "wss") else 80


def with_host(url: str, address: str) -> str:
    """`url` with its host replaced by `address`, keeping everything else (port, credentials,
    path)."""
    parts = urlsplit(url)
    userinfo, _, _ = parts.netloc.rpartition("@")
    netloc = f"{userinfo}@{address}" if userinfo else address
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


def private_ipv4(text: str) -> ipaddress.IPv4Address | None:
    """`text` as a private (RFC 1918, or loopback) IPv4 address, or None. Discovery never goes
    near a public address."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if address.version != 4 or not address.is_private or address.is_link_local:
        return None
    return address  # type: ignore[return-value]


def subnet_of(address: str, prefix: int = MAX_PREFIX) -> ipaddress.IPv4Network | None:
    ip = private_ipv4(address)
    if ip is None:
        return None
    return ipaddress.ip_network(f"{ip}/{max(prefix, MAX_PREFIX)}", strict=False)  # type: ignore[return-value]


def parse_subnet(text: str) -> ipaddress.IPv4Network:
    """A subnet given on the command line. Raises ValueError for a public or too large one."""
    network = ipaddress.ip_network(text.strip(), strict=False)
    if network.version != 4 or not network.is_private:
        raise ValueError(f"{text} isn't a private IPv4 network")
    if network.prefixlen < MAX_PREFIX:
        raise ValueError(f"{text} is larger than a /{MAX_PREFIX}; give each /24 to scan")
    return network  # type: ignore[return-value]


def _session(timeout: float) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout),
        headers={"User-Agent": f"printlog-bridge/{__version__}"},
    )


async def probe(
    base_url: str,
    *,
    api_key: str | None = None,
    timeout: float = PROBE_TIMEOUT_S,
    details: bool = False,
) -> Found | None:
    """Whether Moonraker answers at `base_url`, and who it is. Reads `/server/info` and
    `/machine/system_info`, and with `details` (for `discover`) `/printer/info` and
    `/printer/objects/list`. None when nothing that looks like Moonraker answers.

    `api_key` is sent only once the address has answered as Moonraker, or asked for
    authorization, on its port: never to whatever else is on the network."""
    parts = urlsplit(base_url)
    found = Found(parts.hostname or "", port_of(base_url))
    async with _session(timeout) as session:

        async def get(path: str, key: str | None) -> Any:
            headers = {"X-Api-Key": key} if key else {}
            async with session.get(f"{base_url}{path}", headers=headers) as response:
                found.unauthorized = response.status in (401, 403)
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)
                return data.get("result") if isinstance(data, Mapping) else None

        try:
            info = await get("/server/info", None)
            if found.unauthorized and api_key:
                info = await get("/server/info", api_key)
            if found.unauthorized:
                return found
            if not isinstance(info, Mapping) or "klippy_state" not in info:
                return None
            found.server_info = info
            system = await get("/machine/system_info", api_key)
            if isinstance(system, Mapping) and isinstance(system.get("system_info"), Mapping):
                found.fingerprint = Fingerprint.from_system_info(system["system_info"])
            if details:
                printer = await get("/printer/info", api_key)
                if isinstance(printer, Mapping):
                    found.printer_info = printer
                objects = await get("/printer/objects/list", api_key)
                if isinstance(objects, Mapping) and isinstance(objects.get("objects"), list):
                    found.tools = sum(
                        1 for o in objects["objects"] if re.fullmatch(r"extruder\d*", str(o))
                    )
            found.unauthorized = False
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            return None
    return found


async def scan(
    networks: Iterable[ipaddress.IPv4Network],
    base_url: Callable[[str], str],
    *,
    api_key: str | None = None,
    timeout: float = PROBE_TIMEOUT_S,
    details: bool = False,
    wanted: Callable[[Found], bool] | None = None,
    skip: Iterable[str] = (),
) -> list[Found]:
    """Probes every address of `networks` (each at most a /24), `SCAN_CONCURRENCY` at a time.
    With `wanted`, stops at the first Moonraker it accepts and returns just that one."""
    skipped = set(skip)
    addresses = []
    for network in networks:
        if network.prefixlen < MAX_PREFIX:
            raise ValueError(f"won't scan {network}: larger than a /{MAX_PREFIX}")
        addresses.extend(str(a) for a in network.hosts() if str(a) not in skipped)
    limit = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def one(address: str) -> Found | None:
        async with limit:
            return await probe(base_url(address), api_key=api_key, timeout=timeout,
                               details=details)  # fmt: skip

    return await _gather(
        [one(a) for a in dict.fromkeys(addresses)], wanted, keep=lambda f: f is not None
    )


async def _gather(
    work: Sequence[Awaitable[Found | None]],
    wanted: Callable[[Found], bool] | None,
    keep: Callable[[Found | None], bool],
) -> list[Found]:
    tasks = [asyncio.ensure_future(w) for w in work]
    results: list[Found] = []
    try:
        for next_done in asyncio.as_completed(tasks):
            found = await next_done
            if not keep(found):
                continue
            assert found is not None
            if wanted is None:
                results.append(found)
            elif wanted(found):
                return [found]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if wanted is None:
        results.sort(key=lambda f: (ipaddress.ip_address(f.address), f.port))
    return results


# -- zeroconf ----------------------------------------------------------------------------------


async def browse_mdns(
    service: str = MDNS_SERVICE, wait: float = MDNS_WAIT_S
) -> list[tuple[str, int]]:
    """Asks the network for `service` over mDNS, and returns each private IPv4 address and port
    that answered within `wait` seconds. Empty when multicast doesn't get through."""
    loop = asyncio.get_running_loop()
    answers: list[bytes] = []

    class Listener(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: Any) -> None:
            answers.append(data)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setblocking(False)
        transport, _ = await loop.create_datagram_endpoint(Listener, sock=sock)
    except OSError as e:
        sock.close()
        log.debug("mDNS isn't available here: %s", e)
        return []
    try:
        # From an ephemeral port, a "legacy" query (RFC 6762 6.7): responders answer it unicast.
        transport.sendto(mdns_query(service), MDNS_GROUP)
        await asyncio.sleep(wait)
    except OSError as e:
        log.debug("couldn't send the mDNS query: %s", e)
    finally:
        transport.close()

    found: dict[tuple[str, int], None] = {}
    for data in answers:
        with contextlib.suppress(ValueError, IndexError, struct.error):
            for address, port in parse_mdns_answer(data, service):
                if private_ipv4(address) is not None:
                    found[(address, port)] = None
    return list(found)


def mdns_query(service: str) -> bytes:
    """A DNS query for `service`'s PTR records."""
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    return header + _encode_name(service) + struct.pack("!HH", 12, 1)  # PTR, IN


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.strip(".").split("."):
        raw = label.encode()
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def parse_mdns_answer(data: bytes, service: str) -> list[tuple[str, int]]:
    """Each (IPv4 address, port) an mDNS response gives for instances of `service`: its PTR
    records name the instances, their SRV records the host and port, and the host's A records
    the address."""
    _, _, questions, answers, authority, additional = struct.unpack_from("!HHHHHH", data)
    offset = 12
    for _ in range(questions):
        _, offset = _read_name(data, offset)
        offset += 4
    instances: set[str] = set()
    srv: dict[str, tuple[str, int]] = {}
    a_records: dict[str, list[str]] = {}
    want = service.strip(".").lower()
    for _ in range(answers + authority + additional):
        name, offset = _read_name(data, offset)
        kind, _, _, length = struct.unpack_from("!HHIH", data, offset)
        offset += 10
        rdata = offset
        offset += length
        if kind == 12 and name.lower() == want:  # PTR
            instances.add(_read_name(data, rdata)[0].lower())
        elif kind == 33:  # SRV: priority, weight, port, target
            port = struct.unpack_from("!H", data, rdata + 4)[0]
            srv[name.lower()] = (_read_name(data, rdata + 6)[0].lower(), port)
        elif kind == 1 and length == 4:  # A
            a_records.setdefault(name.lower(), []).append(str(ipaddress.IPv4Address(
                data[rdata : rdata + 4]
            )))  # fmt: skip
    found = []
    for instance in sorted(instances):
        if instance not in srv:
            continue
        host, port = srv[instance]
        found.extend((address, port) for address in a_records.get(host, []))
    return found


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    """A (possibly compressed) DNS name at `offset`, and the offset just after it."""
    labels = []
    end = None
    jumps = 0
    while True:
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if end is None:
                end = offset + 2
            jumps += 1
            if jumps > 32:
                raise ValueError("DNS name compression loop")
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            continue
        offset += 1
        if length == 0:
            break
        labels.append(data[offset : offset + length].decode("utf-8", errors="replace"))
        offset += length
    return ".".join(labels), end if end is not None else offset


# -- this host ---------------------------------------------------------------------------------


def local_subnets() -> list[ipaddress.IPv4Network]:
    """The /24 of this host's address on its default route, when that is private. Connecting
    a UDP socket sends nothing: it only asks the kernel which address it would send from."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1: any address with a route will do
        address = sock.getsockname()[0]
    except OSError:
        return []
    finally:
        sock.close()
    network = subnet_of(address)
    return [network] if network is not None else []


def in_container() -> bool:
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
