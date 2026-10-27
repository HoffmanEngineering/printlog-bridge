"""Finding Bambu Lab printers on the LAN, for `printlog-bridge discover`.

A Bambu printer announces itself every few seconds with an SSDP-style `NOTIFY` (as Bambu Studio
finds it in LAN mode), sent to the broadcast address on UDP port 2021 and to 239.255.255.250:1990
(OpenBambuAPI, ha-bambulab). The headers carry its address (`Location`), serial (`USN`), model
code (`DevModel.bambu.com`) and name (`DevName.bambu.com`). Listening needs no access code, and
says nothing about the code itself, which only the printer's screen shows.

A broadcast doesn't cross routers, and Docker's default (bridge) network doesn't receive it: the
container has to use the host's network to hear one.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from dataclasses import dataclass

SSDP_PORT = 2021
LISTEN_S = 6.0
"""Long enough to hear each printer at least once."""
NOTIFY_TYPE = "urn:bambulab-com:device:3dprinter:1"
MODELS = {
    "BL-P001": "X1 Carbon",
    "BL-P002": "X1",
    "C13": "X1E",
    "C11": "P1P",
    "C12": "P1S",
    "N1": "A1 mini",
    "N2S": "A1",
    "O1D": "H2D",
}
"""Model codes as ha-bambulab names them; an unknown one is shown as the printer sends it."""


@dataclass(frozen=True)
class FoundBambu:
    address: str
    serial: str
    model: str | None
    name: str | None

    @property
    def model_name(self) -> str | None:
        return MODELS.get(self.model or "", self.model)


def parse_notify(data: bytes, sender: str) -> FoundBambu | None:
    """A printer's announcement, or None for anything else."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or not lines[0].upper().startswith("NOTIFY"):
        return None
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    serial = headers.get("usn", "")
    if not serial.isalnum() or "bambulab" not in headers.get("nt", "").lower():
        return None
    address = headers.get("location") or sender
    return FoundBambu(
        address=address,
        serial=serial,
        model=headers.get("devmodel.bambu.com") or None,
        name=headers.get("devname.bambu.com") or None,
    )


class _Listener(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, FoundBambu] = {}

    def datagram_received(self, data: bytes, addr) -> None:
        printer = parse_notify(data, addr[0])
        if printer is not None:
            self.found[printer.serial] = printer


async def listen(seconds: float = LISTEN_S, port: int = SSDP_PORT) -> list[FoundBambu]:
    """Every printer heard on `port` within `seconds`, by address. Raises OSError when the port
    can't be listened on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # beside Bambu Studio
        sock.bind(("", port))
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    loop = asyncio.get_running_loop()
    transport, listener = await loop.create_datagram_endpoint(_Listener, sock=sock)
    try:
        await asyncio.sleep(seconds)
    finally:
        with contextlib.suppress(Exception):
            transport.close()
    return sorted(listener.found.values(), key=lambda f: f.address)
