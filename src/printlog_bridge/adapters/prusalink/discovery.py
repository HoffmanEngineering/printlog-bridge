"""Finding Prusa printers (PrusaLink) on the LAN while the bridge runs (#100).

PrusaLink advertises itself over mDNS for PrusaSlicer to find: `_prusa-link._tcp`, and also as
`_octoprint._tcp`, which OctoPrint itself uses. An `_octoprint._tcp` answer counts only when its
TXT record names Prusa; any other is OctoPrint's (`adapters/octoprint/discovery.py`). Nothing
is asked of the printer: PrusaLink wants its password for everything, and the bridge doesn't
have it yet.

Not seen on a real network yet (October 2026): which TXT keys each firmware sends is a guess from
PrusaSlicer's browser, so the Prusa test is deliberately loose (any key or value naming Prusa).
"""

from __future__ import annotations

from printlog_bridge.core.discovery import Discovery, Found
from printlog_bridge.core.mdns import MdnsAnswer

PRUSALINK_SERVICE = "_prusa-link._tcp.local"
OCTOPRINT_SERVICE = "_octoprint._tcp.local"


def is_prusa(answer: MdnsAnswer) -> bool:
    if answer.service == PRUSALINK_SERVICE:
        return True
    words = [answer.instance, *answer.txt.keys(), *answer.txt.values()]
    return any("prusa" in w.lower() for w in words)


class PrusaLinkDiscovery(Discovery):
    kind = "prusalink"
    mdns_services = (PRUSALINK_SERVICE, OCTOPRINT_SERVICE)

    async def from_mdns(self, answer: MdnsAnswer) -> Found | None:
        if not is_prusa(answer):
            return None
        txt = answer.txt
        model = txt.get("printer_type") or txt.get("model") or txt.get("printer")
        return Found(
            kind="prusalink",
            address=answer.address,
            port=answer.port,
            model=model or None,
            name=answer.instance or None,
            serial=txt.get("serial") or txt.get("sn") or None,
        )
