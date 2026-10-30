"""Finding FlashForge printers on the LAN. Any UDP datagram to the multicast group 225.0.0.9
(ports 19000 and 8899) or to the broadcast address on port 48899 is answered by each printer
with one datagram naming it (ff-5mp-api-py's `discovery/discovery.py`, MIT). The bridge sends an
empty datagram, as that library does; it carries nothing, and the printer is asked nothing else.

A 5M-series answer (276 bytes or more) is, big-endian: the name, NUL-terminated, in bytes 0 to
0x83; the command port (8899) at 0x84, the vendor id at 0x86, the product id (`MODELS`) at 0x88,
a status at 0x8A, the product type at 0x8C, the HTTP port (8898) at 0x8E, and the serial
number, NUL-terminated, from 0x92. An older printer (Adventurer 3 and 4) answers 140 bytes with
no serial and no HTTP API: left out. ASSUMPTION: from that library's code, not a recorded
printer; its own spec puts the status at 0x90, which the bridge doesn't read.

A printer never announces itself unasked, so the bridge's discovery service finds one only in a
scan ("Scan now" in the app, `sweep`), and `printlog-bridge discover` asks once. A broadcast
doesn't cross routers, and Docker's default (bridge) network doesn't carry it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from printlog_bridge.core.discovery import Discovery, Found

from .protocol import HTTP_PORT, MODELS

ASK_S = 3.0
MULTICAST = "225.0.0.9"
MULTICAST_PORTS = (19000, 8899)
BROADCAST_PORT = 48899
ASK = b""
MIN_REPLY = 0x92 + 1
MAX_REPLY = 1024
_SERIAL = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


@dataclass(frozen=True)
class FoundFlashForge:
    address: str
    serial: str
    name: str | None = None
    model: str | None = None
    http_port: int = HTTP_PORT


def _string(data: bytes) -> str | None:
    text = data.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
    text = re.sub(r"[\x00-\x1f\x7f]", "", text)
    return text or None


def parse_reply(data: bytes, sender: str) -> FoundFlashForge | None:
    """A printer's answer, or None for anything else (an older printer without a serial and the
    HTTP API included). The address is where it answered from."""
    if not MIN_REPLY <= len(data) <= MAX_REPLY:
        return None
    serial = _string(data[0x92 : 0x92 + 128])
    if serial is None or not _SERIAL.fullmatch(serial):
        return None
    product, http_port = (
        struct.unpack_from(">H", data, 0x88)[0],
        struct.unpack_from(">H", data, 0x8E)[0],
    )
    return FoundFlashForge(
        address=sender,
        serial=serial,
        name=_string(data[:0x84]),
        model=MODELS.get(product),
        http_port=http_port if 0 < http_port < 65536 else HTTP_PORT,
    )


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, FoundFlashForge] = {}

    def datagram_received(self, data: bytes, addr) -> None:
        printer = parse_reply(data, addr[0])
        if printer is not None:
            self.found.setdefault(printer.serial.upper(), printer)


async def ask(targets: Sequence[tuple[str, int]], seconds: float = ASK_S) -> list[FoundFlashForge]:
    """Sends an empty datagram to each `(address, port)` from one ephemeral port, and returns
    every printer that answered within `seconds`, once each (by serial), by address. Raises
    OSError when it can't send at all. As `sdcp.discovery.ask`: the socket isn't bound, and the
    asks go out before asyncio takes it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sent = 0
        error: OSError | None = None
        for target in dict.fromkeys(targets):
            try:
                sock.sendto(ASK, target)
                sent += 1
            except OSError as e:
                error = e
        if not sent:
            raise error or OSError("nowhere to ask")
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    loop = asyncio.get_running_loop()
    transport, collector = await loop.create_datagram_endpoint(_Collector, sock=sock)
    try:
        await asyncio.sleep(seconds)
    finally:
        with contextlib.suppress(Exception):
            transport.close()
    return sorted(collector.found.values(), key=lambda f: ipaddress.ip_address(f.address))


def targets_for(networks: Iterable[ipaddress.IPv4Network]) -> list[tuple[str, int]]:
    """The multicast group on both ports, each network's broadcast address and the limited
    broadcast on 48899."""
    out = [(MULTICAST, port) for port in MULTICAST_PORTS]
    out.extend((str(n.broadcast_address), BROADCAST_PORT) for n in networks)
    out.append(("255.255.255.255", BROADCAST_PORT))
    return list(dict.fromkeys(out))


class FlashForgeDiscovery(Discovery):
    """Asks in a scan; reports each printer at its HTTP port, by its serial number (which the
    app passes back when the user connects it, with the check code they enter)."""

    kind = "flashforge"

    async def sweep(self, networks: Sequence[ipaddress.IPv4Network]) -> list[Found]:
        found = await ask(targets_for(networks))
        return [
            Found(
                kind=self.kind,
                address=f.address,
                port=f.http_port,
                model=f.model,
                name=f.name,
                serial=f.serial,
            )
            for f in found
        ]
