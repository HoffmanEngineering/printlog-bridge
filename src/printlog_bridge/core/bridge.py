"""`printlog-bridge run`: watches every configured printer at once and logs its jobs."""

from __future__ import annotations

import asyncio
import logging

from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.config import API_KEY_ENV, BridgeConfig, ConfigError
from printlog_bridge.core.doctor import API_KEYS_PAGE, PRINTERS_PAGE
from printlog_bridge.core.lifecycle import JobLogger, LoggedPrinter

log = logging.getLogger(__name__)


def check_runnable(config: BridgeConfig) -> None:
    """What `run` needs beyond a valid config. Raises ConfigError saying what to set."""
    if config.api_key is None:
        raise ConfigError(
            f"no API key. Create one at {API_KEYS_PAGE}, then set api_key in [bridge] "
            f"or the {API_KEY_ENV} environment variable."
        )
    for printer in config.printers:
        if printer.printer_id is None:
            raise ConfigError(
                f"[printer {printer.name}] has no printer_id, so its prints have nowhere to go. "
                "Set it to the number at the end of the printer's page in 3D Print Log "
                f"({PRINTERS_PAGE}<number>)."
            )


async def run_bridge(config: BridgeConfig) -> None:
    """Runs every printer's adapter until cancelled, logging their jobs to the API."""
    check_runnable(config)
    assert config.api_key is not None  # check_runnable made sure

    async with ApiClient(config.api_url, config.api_key) as api:
        sink = JobLogger(
            api,
            {
                p.name: LoggedPrinter(printer_id=p.printer_id, source=p.adapter.name)
                for p in config.printers
                if p.printer_id is not None
            },
            config.state_dir,
        )
        for printer in config.printers:
            log.info(
                "[printer %s] watching: %s, logging to printer %s",
                printer.name, printer.adapter.describe(), printer.printer_id,
            )  # fmt: skip
        await asyncio.gather(*(printer.adapter.run(sink) for printer in config.printers))
