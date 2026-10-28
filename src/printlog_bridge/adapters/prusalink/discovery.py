"""Finding Prusa printers (PrusaLink) on the LAN while the bridge runs (#100).

PrusaLink advertises itself over mDNS as `_prusalink._tcp` (Buddy firmware and PrusaLink on a
Raspberry Pi alike), and PrusaLink on a Pi also as `_octoprint._tcp`, for older PrusaSlicer.
An `_octoprint._tcp` answer is PrusaLink's only when it isn't OctoPrint's and names Prusa
(`adapters/common/mdns.py`); the rest is OctoPrint's (`adapters/octoprint/discovery.py`).
`_prusa-link._tcp`, which the first version asked for, is still asked for too. Nothing is asked
of the printer: PrusaLink wants its password for everything, and the bridge doesn't have it yet.

Not seen on a real network yet (October 2026): which TXT keys each firmware sends is from
Prusa's sources (Buddy's `lib/WUI/mdns/mdns.c` sends none; Prusa-Link's
`service_discovery.py` sends `path`), so the model and serial are read loosely.
"""

from __future__ import annotations

from printlog_bridge.adapters.common.mdns import (
    OCTOPRINT_SERVICE,
    PRUSALINK_SERVICES,
    is_prusalink,
)
from printlog_bridge.core.discovery import Discovery, Found
from printlog_bridge.core.mdns import MdnsAnswer


class PrusaLinkDiscovery(Discovery):
    kind = "prusalink"
    mdns_services = (*PRUSALINK_SERVICES, OCTOPRINT_SERVICE)

    async def from_mdns(self, answer: MdnsAnswer) -> Found | None:
        if not is_prusalink(answer):
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
