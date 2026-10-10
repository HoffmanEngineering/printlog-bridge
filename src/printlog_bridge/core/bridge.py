"""`printlog-bridge run`: watches every configured printer at once."""

from __future__ import annotations

import asyncio
import logging

from printlog_bridge.core.config import BridgeConfig
from printlog_bridge.core.events import JobEvent

log = logging.getLogger(__name__)


class LoggingSink:
    """Logs each job event. The outbox (#9) and API calls (#4) take its place."""

    async def emit(self, event: JobEvent) -> None:
        log.info("[printer %s] %s", event.printer, event)


async def run_bridge(config: BridgeConfig) -> None:
    """Runs every printer's adapter until cancelled."""
    sink = LoggingSink()
    for printer in config.printers:
        log.info("[printer %s] watching: %s", printer.name, printer.adapter.describe())
    await asyncio.gather(*(printer.adapter.run(sink) for printer in config.printers))
