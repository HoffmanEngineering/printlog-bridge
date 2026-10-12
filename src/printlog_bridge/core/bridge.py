"""`printlog-bridge run`: watches every configured printer at once and logs its jobs."""

from __future__ import annotations

import asyncio
import logging

from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.config import API_KEY_ENV, BridgeConfig, ConfigError
from printlog_bridge.core.connections import HEARTBEAT_INTERVAL_S, Connection, Heartbeat
from printlog_bridge.core.doctor import API_KEYS_PAGE, PRINTERS_PAGE
from printlog_bridge.core.lifecycle import BACKOFF_S, JobLogger, LoggedPrinter
from printlog_bridge.core.status import TOUCH_INTERVAL_S, StatusBoard

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


async def run_bridge(
    config: BridgeConfig,
    *,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
    touch_interval: float = TOUCH_INTERVAL_S,
    backoff: tuple[float, float] = BACKOFF_S,
) -> None:
    """Runs every printer's adapter until cancelled, logging their jobs to the API.

    Each job event goes into the outbox in the state directory first, and the job logger sends
    it from there. Registers each printer's connection and keeps it alive, and keeps
    `status.json` in the state directory up to date for `printlog-bridge status`.
    """
    check_runnable(config)
    assert config.api_key is not None  # check_runnable made sure

    board = StatusBoard(config.state_dir)
    board.start([p.name for p in config.printers])
    try:
        async with ApiClient(config.api_url, config.api_key) as api:
            sink = JobLogger(
                api,
                {
                    p.name: LoggedPrinter(printer_id=p.printer_id, source=p.adapter.name)
                    for p in config.printers
                    if p.printer_id is not None
                },
                config.state_dir,
                backoff=backoff,
                board=board,
            )
            heartbeat = Heartbeat(
                api,
                [
                    Connection(
                        printer=p.name,
                        instance_id=sink.state(p.name).instance_id,
                        kind=p.adapter.name,
                        printer_id=p.printer_id,
                    )
                    for p in config.printers
                    if p.printer_id is not None
                ],
                board,
                interval=heartbeat_interval,
            )
            sink.before_create = heartbeat.ensure_registered
            for printer in config.printers:
                log.info(
                    "[printer %s] watching: %s, logging to printer %s",
                    printer.name, printer.adapter.describe(), printer.printer_id,
                )  # fmt: skip
            try:
                await asyncio.gather(
                    sink.run(),
                    heartbeat.run(),
                    _keep_touching(board, touch_interval),
                    *(p.adapter.run(sink, board.printer(p.name)) for p in config.printers),
                )
            finally:
                sink.close()
    finally:
        board.stopped()


async def _keep_touching(board: StatusBoard, interval: float) -> None:
    """Rewrites the status file now and then, so `status` can tell the bridge is still up."""
    while True:
        await asyncio.sleep(interval)
        board.touch()
