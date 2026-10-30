"""Printer adapters, by the name `adapter = ...` uses in the config file.

To add one, subclass `printlog_bridge.core.adapter.Adapter` in its own subpackage and list it
here. See docs/adapters.md.
"""

from __future__ import annotations

from collections.abc import Mapping

from printlog_bridge.adapters.anycubic import AnycubicAdapter
from printlog_bridge.adapters.bambu import BambuAdapter
from printlog_bridge.adapters.duet import DuetAdapter
from printlog_bridge.adapters.flashforge import FlashForgeAdapter
from printlog_bridge.adapters.moonraker import MoonrakerAdapter
from printlog_bridge.adapters.octoprint import OctoPrintAdapter
from printlog_bridge.adapters.prusalink import PrusaLinkAdapter
from printlog_bridge.adapters.sdcp import SdcpAdapter
from printlog_bridge.core.adapter import Adapter

ADAPTERS: Mapping[str, type[Adapter]] = {
    MoonrakerAdapter.name: MoonrakerAdapter,
    BambuAdapter.name: BambuAdapter,
    PrusaLinkAdapter.name: PrusaLinkAdapter,
    OctoPrintAdapter.name: OctoPrintAdapter,
    SdcpAdapter.name: SdcpAdapter,
    DuetAdapter.name: DuetAdapter,
    FlashForgeAdapter.name: FlashForgeAdapter,
    AnycubicAdapter.name: AnycubicAdapter,
}
