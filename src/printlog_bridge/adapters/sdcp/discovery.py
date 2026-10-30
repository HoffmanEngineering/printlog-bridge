"""Finding SDCP printers (the Elegoo Centauri Carbon) on the LAN: the text `M99999` to UDP port
3000, broadcast, is answered by each printer with its name, model, mainboard id and firmware
(`protocol.py`). Nothing else is asked, and asking needs no credentials.

A printer never announces itself unasked, so the bridge's discovery service finds one only in a
scan ("Scan now" in the app, `sweep`), and `printlog-bridge discover` asks once. A broadcast
doesn't cross routers, and Docker's default (bridge) network doesn't carry it: the container has
to use the host's network.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from printlog_bridge.core.discovery import Discovery, Found

from .protocol import DISCOVERY_ASK, DISCOVERY_PORT, WS_PORT

ASK_S = 3.0
"""How long to wait for answers."""
MAX_REPLY = 4096


@dataclass(frozen=True)
class FoundSdcp:
    address: str
    mainboard_id: str
    name: str | None = None
    model: str | None = None
    firmware: str | None = None

    @property
    def resin(self) -> bool:
        """One of Elegoo's resin printers (Saturn, Mars, Jupiter), which answer `M99999` too
        but print through the resin spec's states, which the adapter doesn't follow (#97)."""
        return bool(self.model and _RESIN.search(self.model))


_RESIN = re.compile(r"\b(saturn|mars|jupiter)\b", re.IGNORECASE)


def parse_reply(data: bytes, sender: str) -> FoundSdcp | None:
    """A printer's answer to `M99999`, or None for anything else. The address is where it
    answered from, not the `MainboardIP` it names."""
    if len(data) > MAX_REPLY:
        return None
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("Data"), dict):
        return None
    info = body["Data"]
    board = info.get("MainboardID")
    if not isinstance(board, str) or not board.strip():
        return None

    def text(key: str) -> str | None:
        value = info.get(key)
        return value.strip() or None if isinstance(value, str) else None

    return FoundSdcp(
        address=sender,
        mainboard_id=board.strip(),
        name=text("Name"),
        model=text("MachineName"),
        firmware=text("FirmwareVersion"),
    )


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, FoundSdcp] = {}

    def datagram_received(self, data: bytes, addr) -> None:
        printer = parse_reply(data, addr[0])
        if printer is not None:
            self.found.setdefault(printer.mainboard_id.lower(), printer)


async def ask(targets: Sequence[tuple[str, int]], seconds: float = ASK_S) -> list[FoundSdcp]:
    """Sends `M99999` to each `(address, port)` from one ephemeral port and returns every
    printer that answered within `seconds`, once each (by mainboard id), by address. Raises
    OSError when it can't send at all.

    The socket is never bound: the first send gives it an ephemeral port (as the mDNS query's),
    and the answers come back to that port. The asks go out before the socket is handed to
    asyncio, since Windows' event loop can't read from a socket without a port yet."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sent = 0
        error: OSError | None = None
        for target in dict.fromkeys(targets):
            try:
                sock.sendto(DISCOVERY_ASK, target)
                sent += 1
            except OSError as e:  # an unreachable network: the others may still answer
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
    """Each network's broadcast address, and the limited broadcast (255.255.255.255), on the
    discovery port."""
    out = [(str(n.broadcast_address), DISCOVERY_PORT) for n in networks]
    out.append(("255.255.255.255", DISCOVERY_PORT))
    return list(dict.fromkeys(out))


class SdcpDiscovery(Discovery):
    """Asks with `M99999` in a scan; reports each printer at its websocket port, by its
    mainboard id. Resin printers are left out until an adapter follows them."""

    kind = "sdcp"

    async def sweep(self, networks: Sequence[ipaddress.IPv4Network]) -> list[Found]:
        found = await ask(targets_for(networks))
        return [
            Found(
                kind=self.kind,
                address=f.address,
                port=WS_PORT,
                model=f.model,
                name=f.name,
                mainboard_id=f.mainboard_id,
            )
            for f in found
            if not f.resin
        ]
