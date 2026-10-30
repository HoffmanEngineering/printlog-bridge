"""Finding Anycubic printers in LAN mode: an SSDP M-SEARCH for `ac:3dprinter:fdm` and
`ac:3dprinter:lcd`, which a Kobra S1 and a Photon P1 Max each answered (October 2026,
`tests/fixtures/anycubic/ssdp_answers.json`) with `ST`, `USN` (`uuid:fdm:<MAC>`) and a
`LOCATION` naming their `/info` with its fields as the query: `modelId`, `modelName`, `ctrlType`,
the serial (`cn`) and the handshake `token`. The token is never kept.

Neither printer announced itself unasked in three minutes of listening, so the bridge finds one
only in a scan ("Scan now" in the app, `sweep`), and `printlog-bridge discover` asks once. The
search goes to the SSDP group and each network's broadcast address from one ephemeral port; the
answers come back there. Docker's default (bridge) network carries neither: the container has to
use the host's network.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from printlog_bridge.core.discovery import Discovery, Found

from .handshake import HTTP_PORT

SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900
SEARCH_TARGETS = ("ac:3dprinter:fdm", "ac:3dprinter:lcd")
ASK_S = 3.0
MAX_REPLY = 4096
_MAC = re.compile(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")


def search(target: str) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {target}\r\n\r\n"
    ).encode()


@dataclass(frozen=True)
class FoundAnycubic:
    address: str
    model_id: str | None
    model: str | None = None
    serial: str | None = None
    mac: str | None = None
    resin: bool = False
    lan_mode: bool = True


def parse_reply(data: bytes, sender: str) -> FoundAnycubic | None:
    """An Anycubic printer's answer to the search, or None for anything else. The address is
    where it answered from. The `token` in its `LOCATION` is dropped."""
    if len(data) > MAX_REPLY:
        return None
    text = data.decode("latin-1")
    headers = {}
    for line in text.split("\r\n")[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    target = headers.get("st", "")
    if not target.startswith("ac:3dprinter"):
        return None
    location = headers.get("location", "")
    try:
        fields = dict(parse_qsl(urlsplit(location).query))
    except ValueError:
        fields = {}
    mac_match = _MAC.search(headers.get("usn", ""))
    mac = mac_match.group(0).replace("-", ":").lower() if mac_match else None
    kind = target.rsplit(":", 1)[-1]
    return FoundAnycubic(
        address=sender,
        model_id=fields.get("modelId") or None,
        model=fields.get("modelName") or None,
        serial=fields.get("cn") or None,
        mac=mac,
        resin=kind == "lcd",
        lan_mode=fields.get("ctrlType", "lan") in ("lan", "net"),
    )


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, FoundAnycubic] = {}

    def datagram_received(self, data: bytes, addr) -> None:
        printer = parse_reply(data, addr[0])
        if printer is not None:
            self.found.setdefault(printer.address, printer)


async def ask(targets: Sequence[tuple[str, int]], seconds: float = ASK_S) -> list[FoundAnycubic]:
    """Sends the searches to each `(address, port)` from one ephemeral port and returns every
    printer that answered within `seconds`, once per address. Raises OSError when it can't send
    at all. As SDCP's `ask`: the socket is never bound, and the searches go out before it is
    handed to asyncio."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sent = 0
        error: OSError | None = None
        for target in dict.fromkeys(targets):
            for st in SEARCH_TARGETS:
                try:
                    sock.sendto(search(st), target)
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
    """The SSDP group, each network's broadcast address and the limited broadcast."""
    out = [(SSDP_GROUP, SSDP_PORT)]
    out += [(str(n.broadcast_address), SSDP_PORT) for n in networks]
    out.append(("255.255.255.255", SSDP_PORT))
    return list(dict.fromkeys(out))


class AnycubicDiscovery(Discovery):
    """Searches over SSDP in a scan; reports each printer in LAN mode at its `/info` port, by
    its serial and MAC address."""

    kind = "anycubic"

    async def sweep(self, networks: Sequence[ipaddress.IPv4Network]) -> list[Found]:
        found = await ask(targets_for(networks))
        return [
            Found(
                kind=self.kind,
                address=f.address,
                port=HTTP_PORT,
                model=f.model,
                serial=f.serial,
                macs=(f.mac,) if f.mac else (),
            )
            for f in found
            if f.lan_mode
        ]
