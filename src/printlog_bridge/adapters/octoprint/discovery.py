"""Finding OctoPrint on the LAN while the bridge runs (#100).

OctoPrint's bundled discovery plugin advertises `_octoprint._tcp` over mDNS with a TXT record of
`path` (where its web page is, `/` unless behind a proxy prefix), `version`, `api`, and the
`model` keys the user filled in (`vendor`, `model`), and a `uuid`. PrusaLink on a Raspberry Pi
also answers to `_octoprint._tcp`, with only `path`: an answer is OctoPrint's when it has `api`
and `version` (`adapters/common/mdns.py`), so OctoPi on a Prusa MK3S is OctoPrint. Nothing is
asked of OctoPrint: it wants its API key for everything, and the bridge doesn't have it yet.
The TXT record can also hold `u` and `p`, a user and password for HTTP basic auth in front of
OctoPrint: neither is kept.
"""

from __future__ import annotations

from printlog_bridge.adapters.common.mdns import OCTOPRINT_SERVICE, is_octoprint
from printlog_bridge.core.discovery import Discovery, Found
from printlog_bridge.core.mdns import MdnsAnswer


class OctoPrintDiscovery(Discovery):
    kind = "octoprint"
    mdns_services = (OCTOPRINT_SERVICE,)

    async def from_mdns(self, answer: MdnsAnswer) -> Found | None:
        if not is_octoprint(answer):
            return None
        txt = answer.txt
        vendor = (txt.get("vendor") or "").strip()
        model = (txt.get("model") or "").strip()
        name = " ".join(part for part in (vendor, model) if part) or None
        return Found(
            kind="octoprint",
            address=answer.address,
            port=answer.port,
            model=name,
            name=answer.instance or None,
            serial=txt.get("serial") or txt.get("uuid") or None,
        )
