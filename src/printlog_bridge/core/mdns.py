"""Asking the local network over mDNS (RFC 6762) which printers offer a service, with no library.

One query, for every service type at once, goes from an ephemeral port to the mDNS multicast
group: a "legacy" query (RFC 6762 6.7), which responders answer unicast to that port. So nothing
binds port 5353, which avahi (or Home Assistant) usually holds. It needs multicast, which doesn't
reach a Docker container on bridge networking.

Each answer is read for its PTR records (the instances of a service), their SRV records (host
and port), TXT records (key=value pairs) and the hosts' A records (IPv4 addresses).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

MDNS_GROUP = ("224.0.0.251", 5353)
WAIT_S = 2.0
PTR, SRV, TXT, A = 12, 33, 16, 1


@dataclass(frozen=True)
class MdnsAnswer:
    """One instance of `service` at `address`:`port`."""

    service: str
    """The service type asked for, such as `_moonraker._tcp.local`."""
    instance: str
    """The instance's own name, such as `Moonraker Instance on voron`."""
    host: str
    address: str
    port: int
    txt: Mapping[str, str] = field(default_factory=dict)


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.strip(".").split("."):
        raw = label.encode()
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def query_packet(services: Sequence[str]) -> bytes:
    """A DNS query for each of `services`' PTR records."""
    header = struct.pack("!HHHHHH", 0, 0, len(services), 0, 0, 0)
    return header + b"".join(encode_name(s) + struct.pack("!HH", PTR, 1) for s in services)


def read_name(data: bytes, offset: int) -> tuple[str, int]:
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


def _txt(data: bytes, start: int, length: int) -> dict[str, str]:
    pairs: dict[str, str] = {}
    offset, end = start, start + length
    while offset < end:
        size = data[offset]
        raw = data[offset + 1 : offset + 1 + size].decode("utf-8", errors="replace")
        offset += 1 + size
        key, _, value = raw.partition("=")
        if key:
            pairs.setdefault(key.lower(), value)
    return pairs


def parse(data: bytes, services: Iterable[str]) -> list[MdnsAnswer]:
    """Each instance of `services` an mDNS response names, at each IPv4 address of its host.
    Raises ValueError, IndexError or struct.error for a malformed packet."""
    wanted = {s.strip(".").lower() for s in services}
    _, _, questions, answers, authority, additional = struct.unpack_from("!HHHHHH", data)
    offset = 12
    for _ in range(questions):
        _, offset = read_name(data, offset)
        offset += 4
    instances: dict[str, str] = {}
    srv: dict[str, tuple[str, int]] = {}
    txt: dict[str, dict[str, str]] = {}
    a_records: dict[str, list[str]] = {}
    for _ in range(answers + authority + additional):
        name, offset = read_name(data, offset)
        kind, _, _, length = struct.unpack_from("!HHIH", data, offset)
        offset += 10
        rdata = offset
        offset += length
        lowered = name.lower()
        if kind == PTR and lowered in wanted:
            instances[read_name(data, rdata)[0].lower()] = lowered
        elif kind == SRV:  # priority, weight, port, target
            port = struct.unpack_from("!H", data, rdata + 4)[0]
            srv[lowered] = (read_name(data, rdata + 6)[0].lower(), port)
        elif kind == TXT:
            txt[lowered] = _txt(data, rdata, length)
        elif kind == A and length == 4:
            address = str(ipaddress.IPv4Address(data[rdata : rdata + 4]))
            a_records.setdefault(lowered, []).append(address)
    found = []
    for instance in sorted(instances):
        if instance not in srv:
            continue
        service = instances[instance]
        host, port = srv[instance]
        label = instance[: -len(service) - 1] if instance.endswith("." + service) else instance
        for address in a_records.get(host, []):
            found.append(MdnsAnswer(service, label, host, address, port, txt.get(instance, {})))
    return found


async def ask(services: Sequence[str], wait: float = WAIT_S) -> list[MdnsAnswer]:
    """Asks the network for `services` and returns every answer heard within `wait` seconds.
    Empty when multicast doesn't get through."""
    if not services:
        return []
    loop = asyncio.get_running_loop()
    packets: list[bytes] = []

    class Listener(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: Any) -> None:
            if len(packets) < 256:  # bounded, whatever answers
                packets.append(data)

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
        transport.sendto(query_packet(services), MDNS_GROUP)
        await asyncio.sleep(wait)
    except OSError as e:
        log.debug("couldn't send the mDNS query: %s", e)
    finally:
        transport.close()

    found: dict[tuple[str, str, int], MdnsAnswer] = {}
    for data in packets:
        with contextlib.suppress(ValueError, IndexError, struct.error):
            for answer in parse(data, services):
                found.setdefault((answer.service, answer.address, answer.port), answer)
    return list(found.values())
