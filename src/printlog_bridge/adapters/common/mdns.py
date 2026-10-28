"""Telling apart the hosts that answer to `_octoprint._tcp`: OctoPrint, and PrusaLink on a
Raspberry Pi, which announces itself there too, for older PrusaSlicer.

- OctoPrint's discovery plugin (`plugins/discovery/__init__.py`,
  `_create_octoprint_txt_record_dict`) always sends `path`, `version`, `api` and `uuid` in its
  TXT record, and `model` and `vendor` when the user set them: "Prusa" in those (OctoPi on an
  MK3S) is still OctoPrint.
- PrusaLink (Prusa-Link's `prusa/link/service_discovery.py`) registers `_prusalink._tcp`,
  `_http._tcp` and `_octoprint._tcp` as "PrusaLink at <host>", with only `path` in its TXT
  record. Buddy firmware (MK4, MINI, XL, Core One; `lib/WUI/mdns/mdns.c`) registers only
  `_prusalink._tcp`.
"""

from __future__ import annotations

from printlog_bridge.core.mdns import MdnsAnswer

OCTOPRINT_SERVICE = "_octoprint._tcp.local"
PRUSALINK_SERVICES = ("_prusalink._tcp.local", "_prusa-link._tcp.local")


def is_octoprint(answer: MdnsAnswer) -> bool:
    """OctoPrint itself: its TXT record has the keys only OctoPrint sends."""
    return answer.service == OCTOPRINT_SERVICE and "api" in answer.txt and "version" in answer.txt


def is_prusalink(answer: MdnsAnswer) -> bool:
    if answer.service in PRUSALINK_SERVICES:
        return True
    if answer.service != OCTOPRINT_SERVICE or is_octoprint(answer):
        return False
    words = [answer.instance, *answer.txt.keys(), *answer.txt.values()]
    return any("prusa" in w.lower() for w in words)
