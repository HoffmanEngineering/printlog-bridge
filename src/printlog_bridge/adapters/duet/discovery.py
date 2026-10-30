"""Finding Duets on the LAN over mDNS (#100).

A Duet 3 on Ethernet registers `_http._tcp` (and `_ftp._tcp`, `_telnet._tcp`) under its
host name (`duet3` unless M550 changed it), with `product=RepRapFirmware for Duet 3 ...` and
`version=<firmware>` in the TXT record (RepRapFirmware's `LwipEthernetInterface.cpp`). Plenty of
other things register `_http._tcp`, so only an answer whose TXT names RepRapFirmware or a Duet in
`product` is taken, and nothing is asked of any of them.

ASSUMPTION: from RepRapFirmware's sources, not seen on a real network. A Duet on Wi-Fi (its
ESP module answers mDNS itself) or one behind an SBC (the Pi's avahi) may send no such TXT, and
then isn't found: connect it by address.
"""

from __future__ import annotations

from printlog_bridge.core.discovery import Discovery, Found
from printlog_bridge.core.mdns import MdnsAnswer

HTTP_SERVICE = "_http._tcp.local"
PRODUCT_PREFIX = "RepRapFirmware for "


def is_duet(answer: MdnsAnswer) -> bool:
    if answer.service != HTTP_SERVICE:
        return False
    product = answer.txt.get("product", "").lower()
    return "reprapfirmware" in product or product.startswith("duet")


def model_of(answer: MdnsAnswer) -> str | None:
    product = answer.txt.get("product", "").strip()
    if product.startswith(PRODUCT_PREFIX):
        product = product[len(PRODUCT_PREFIX) :]
    return product or None


class DuetDiscovery(Discovery):
    kind = "duet"
    mdns_services = (HTTP_SERVICE,)

    async def from_mdns(self, answer: MdnsAnswer) -> Found | None:
        if not is_duet(answer):
            return None
        return Found(
            kind="duet",
            address=answer.address,
            port=answer.port,
            model=model_of(answer),
            name=answer.instance or None,
        )
