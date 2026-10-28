"""What the printer announces on the LAN while the probe runs: mDNS, SSDP, Bambu Lab's NOTIFY
and the Chitu/SDCP discovery reply.

Only datagrams from the probed printer's address are recorded; from anything else only a count
per source is kept (it shows whether multicast reaches this computer at all). Every listener is
best effort: a port another program holds (Windows' SSDP service holds 1900) or a blocked
multicast group becomes a note, and the probe goes on.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import socket
import struct
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable

from printlog_bridge.core.mdns import encode_name, read_name
from printlog_bridge.probe import allowlist
from printlog_bridge.probe.record import Recorder, payload

MDNS_FOLLOW_UP_S = 2.5
MAX_QUESTIONS = 32
"""Service types per mDNS query (the allow-list's limit)."""
MAX_LEARNED = 64
"""Service types learned from the printer that are asked for, at most."""
BOARD_WAIT_S = 3.0
RECORD_TYPES = {1: "A", 12: "PTR", 16: "TXT", 28: "AAAA", 33: "SRV", 13: "HINFO", 47: "NSEC"}


@dataclass(frozen=True)
class Network:
    """Where discovery messages go and what is listened on. Tests point these at fakes."""

    mdns_group: tuple[str, int] = ("224.0.0.251", 5353)
    mdns_port: int = 5353
    ssdp_group: tuple[str, int] = ("239.255.255.250", 1900)
    ssdp_port: int = 1900
    listen: tuple[tuple[str, int, str | None], ...] = (
        ("ssdp-notify", 1900, "239.255.255.250"),
        ("bambu", 2021, None),
        ("bambu", 1990, "239.255.255.250"),
    )
    """(source, UDP port, multicast group to join) for passive announcements."""
    chitu_port: int = 3000
    broadcast: str = "255.255.255.255"


def valid_service(name: str) -> bool:
    """Whether a service type the printer listed may be asked for: a `.local` name whose labels
    are 1 to 63 bytes, as the allow-list takes. Anything else is recorded, never asked."""
    if not isinstance(name, str) or not name.lower().endswith(".local") or len(name) > 200:
        return False
    labels = name.strip(".").split(".")
    return all(0 < len(label.encode("utf-8")) <= 63 for label in labels)


def mdns_query(services: Sequence[str]) -> bytes:
    header = struct.pack("!HHHHHH", 0, 0, len(services), 0, 0, 0)
    return header + b"".join(encode_name(s) + struct.pack("!HH", 12, 1) for s in services)


def mdns_records(data: bytes) -> list[dict[str, Any]]:
    """Every record in an mDNS message, decoded where the type is known. Raises ValueError,
    IndexError or struct.error for a malformed one."""
    _, _, questions, answers, authority, additional = struct.unpack_from("!HHHHHH", data)
    offset = 12
    for _ in range(questions):
        _, offset = read_name(data, offset)
        offset += 4
    records = []
    for _ in range(answers + authority + additional):
        name, offset = read_name(data, offset)
        kind, klass, ttl, length = struct.unpack_from("!HHIH", data, offset)
        offset += 10
        rdata = data[offset : offset + length]
        record: dict[str, Any] = {"name": name, "type": RECORD_TYPES.get(kind, kind), "ttl": ttl}
        if kind == 12:
            record["data"] = read_name(data, offset)[0]
        elif kind == 33:
            priority, weight, port = struct.unpack_from("!HHH", data, offset)
            record["data"] = {"port": port, "target": read_name(data, offset + 6)[0]}
            record["data"].update(priority=priority, weight=weight)
        elif kind == 16:
            texts, at = [], 0
            while at < len(rdata):
                size = rdata[at]
                texts.append(rdata[at + 1 : at + 1 + size].decode("utf-8", errors="replace"))
                at += 1 + size
            record["data"] = texts
        elif kind == 1 and length == 4:
            record["data"] = str(ipaddress.IPv4Address(rdata))
        elif kind == 28 and length == 16:
            record["data"] = str(ipaddress.IPv6Address(rdata))
        else:
            record["data"] = rdata.hex()
        records.append(record)
        offset += length
    return records


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self, on_datagram: Callable[[bytes, str], None]) -> None:
        self.on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self.on_datagram(data, addr[0])

    def error_received(self, exc: Exception) -> None:  # an ICMP unreachable, on Windows
        pass


def _is_multicast(address: str) -> bool:
    with contextlib.suppress(ValueError):
        return ipaddress.ip_address(address).is_multicast
    return False


async def open_udp(
    on_datagram: Callable[[bytes, str], None],
    port: int = 0,
    group: str | None = None,
    broadcast: bool = False,
) -> tuple[Any, str | None]:
    """A UDP socket on `port` (0: any), in `group` when given. Returns the transport and a
    warning when the group couldn't be joined. Raises OSError when the port can't be bound."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    warning = None
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if broadcast:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind(("", port))
        if group and _is_multicast(group):
            try:
                membership = socket.inet_aton(group) + socket.inet_aton("0.0.0.0")
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            except OSError as e:
                warning = f"couldn't join {group}: {e.strerror or e}"
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: _Datagrams(on_datagram), sock=sock)
    return transport, warning


class Discovery:
    def __init__(self, rec: Recorder, address: str, network: Network | None = None) -> None:
        self.rec = rec
        self.address = address
        self.network = network or Network()
        self.others: Counter[str] = Counter()
        """Datagrams from other hosts, per source: counted, never recorded."""
        self.heard: Counter[str] = Counter()
        self._transports: list[Any] = []
        self._services_asked: set[str] = set()
        self._board: asyncio.Future | None = None
        self._mdns: Any | None = None
        self._learned_services: set[str] = set()

    # -- the mainboard id, for SDCP's websocket -----------------------------------------------

    async def board(self, timeout: float = BOARD_WAIT_S) -> str | None:
        """The MainboardID from the printer's SDCP discovery reply, waiting up to `timeout`."""
        if self._board is None:
            return None
        with contextlib.suppress(asyncio.TimeoutError):
            return await asyncio.wait_for(asyncio.shield(self._board), timeout)
        return None

    # -- running -------------------------------------------------------------------------------

    async def run(self, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        self._board = loop.create_future()
        try:
            await self._start()
            follow_up = min(MDNS_FOLLOW_UP_S, max(0.0, deadline - time.monotonic()))
            await asyncio.sleep(follow_up)
            await self._ask_mdns_again()
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
        finally:
            self.close()
            self.rec.receive(
                "discovery",
                fromPrinter=dict(self.heard),
                fromOtherHosts=dict(self.others),
                note="datagrams heard per source during the window; only the printer's are "
                "recorded",
            )

    def close(self) -> None:
        for transport in self._transports:
            with contextlib.suppress(Exception):
                transport.close()
        self._transports.clear()
        if self._board is not None and not self._board.done():
            self._board.set_result(None)

    async def _open(self, source: str, on_datagram, **options) -> Any | None:
        try:
            transport, warning = await open_udp(on_datagram, **options)
        except OSError as e:
            port = options.get("port", 0)
            hint = ""
            if sys.platform == "win32" and port == 1900:
                hint = " (Windows' SSDP Discovery service may hold it)"
            self.rec.note(
                f"{source}: couldn't listen on UDP {port}{hint}: {e.strerror or e}; "
                "announcements to it weren't heard"
            )
            return None
        if warning:
            self.rec.note(f"{source}: {warning}; multicast announcements may be missed")
        self._transports.append(transport)
        return transport

    def _handler(self, source: str, parse: Callable[[bytes], dict[str, Any]] | None = None):
        def on_datagram(data: bytes, sender: str) -> None:
            if sender != self.address:
                self.others[source] += 1
                return
            self.heard[source] += 1
            extra: dict[str, Any] = {}
            if parse is not None:
                try:
                    extra = parse(data)
                except (ValueError, IndexError, struct.error) as e:
                    extra = {"parseError": str(e)}
            self.rec.receive(f"udp:{source}", message=payload(data), **extra)

        return on_datagram

    async def _start(self) -> None:
        net = self.network
        # mDNS: a legacy query from an ephemeral port (answered unicast to it), to the group
        # and straight to the printer's port 5353.
        self._mdns = await self._open("mdns", self._handler("mdns", self._parse_mdns))
        await self._ask_mdns(allowlist.MDNS_SERVICES)
        # SSDP: M-SEARCH from an ephemeral port; the answers come back to it.
        ssdp = await self._open("ssdp", self._handler("ssdp"))
        if ssdp is not None:
            for target in (net.ssdp_group, (self.address, net.ssdp_port)):
                self._sendto(ssdp, "udp-ssdp-msearch", "ssdp", allowlist.SSDP_MSEARCH, target)
        # Passive announcements: SSDP NOTIFY, Bambu Lab's NOTIFY on 2021 and 1990.
        for source, port, group in net.listen:
            await self._open(source, self._handler(source), port=port, group=group)
        # Chitu / SDCP discovery: "M99999" broadcast, and straight to the printer.
        chitu = await self._open("chitu", self._handler("chitu", self._parse_chitu), broadcast=True)
        if chitu is not None:
            for target in ((net.broadcast, net.chitu_port), (self.address, net.chitu_port)):
                self._sendto(
                    chitu, "udp-chitu-discovery", "chitu", allowlist.CHITU_DISCOVERY, target
                )

    def _sendto(self, transport, rule: str, source: str, data: bytes, target) -> None:
        try:
            self.rec.send(
                rule, f"udp:{source}", data, to=f"{target[0]}:{target[1]}", message=payload(data)
            )
        except allowlist.NotAllowed:
            return  # recorded as refused; discovery goes on
        try:
            transport.sendto(data, target)
        except OSError as e:
            self.rec.error(f"udp:{source}", f"couldn't send to {target[0]}:{target[1]}: {e}")

    async def _ask_mdns(self, services: Sequence[str]) -> None:
        services = [s for s in services if s.lower() not in self._services_asked]
        if not services or self._mdns is None:
            return
        self._services_asked.update(s.lower() for s in services)
        for start in range(0, len(services), MAX_QUESTIONS):
            packet = mdns_query(services[start : start + MAX_QUESTIONS])
            for target in (self.network.mdns_group, (self.address, self.network.mdns_port)):
                self._sendto(self._mdns, "udp-mdns-query", "mdns", packet, target)

    async def _ask_mdns_again(self) -> None:
        await self._ask_mdns(sorted(self._learned_services))

    def _parse_mdns(self, data: bytes) -> dict[str, Any]:
        records = mdns_records(data)
        for record in records:
            if record["type"] == "PTR" and record["name"].lower() == "_services._dns-sd._udp.local":
                name = str(record["data"])
                if not valid_service(name):
                    self.rec.note(
                        f"mDNS: the printer listed a service type that isn't a valid "
                        f".local name, not asked for: {name[:80]!r}"
                    )
                elif len(self._learned_services) < MAX_LEARNED:
                    self._learned_services = self._learned_services | {name}
        return {"records": records}

    def _parse_chitu(self, data: bytes) -> dict[str, Any]:
        with contextlib.suppress(ValueError, AttributeError, TypeError, KeyError):
            board = json.loads(data.decode("utf-8"))["Data"]["MainboardID"]
            if isinstance(board, str) and board and self._board and not self._board.done():
                self._board.set_result(board)
            return {"sdcp": True}
        return {}
