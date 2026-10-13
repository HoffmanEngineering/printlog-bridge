"""`printlog-bridge run`: watches every configured printer at once and logs its jobs."""

from __future__ import annotations

import asyncio
import logging
import os

from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.config import API_KEY_ENV, BridgeConfig, ConfigError
from printlog_bridge.core.connections import HEARTBEAT_INTERVAL_S, Connection, Heartbeat
from printlog_bridge.core.doctor import API_KEYS_PAGE, PRINTERS_PAGE
from printlog_bridge.core.lifecycle import BACKOFF_S, JobLogger, LoggedPrinter
from printlog_bridge.core.spools import REFRESH_INTERVAL_S, Spools
from printlog_bridge.core.status import TOUCH_INTERVAL_S, StatusBoard
from printlog_bridge.core.support import known_secrets
from printlog_bridge.core.trace import Trace, trace_dir

log = logging.getLogger(__name__)


def check_runnable(config: BridgeConfig) -> None:
    """What `run` needs beyond a valid config. Raises ConfigError saying what to set."""
    if config.api_key is None:
        raise ConfigError(
            f"no API key. Create one at {API_KEYS_PAGE}, then run `printlog-bridge store-key` "
            f"to store it (or set the {API_KEY_ENV} environment variable)."
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
    spool_interval: float = REFRESH_INTERVAL_S,
) -> None:
    """Runs every printer's adapter until cancelled, logging their jobs to the API.

    Each job event goes into the outbox in the state directory first, and the job logger sends
    it from there. Registers each printer's connection and keeps it alive, and keeps
    `status.json` in the state directory up to date for `printlog-bridge status`.
    """
    check_runnable(config)
    assert config.api_key is not None  # check_runnable made sure

    board = StatusBoard(config.state_dir)
    board.start([p.name for p in config.printers], trace=config.trace)
    traces = _start_tracing(config)
    try:
        async with ApiClient(config.api_url, config.api_key) as api:
            spools = Spools(
                api,
                {p.name: p.printer_id for p in config.printers if p.printer_id is not None},
                board,
                interval=spool_interval,
            )
            sink = JobLogger(
                api,
                {
                    p.name: LoggedPrinter(
                        printer_id=p.printer_id, source=p.adapter.name, adoption=p.adoption
                    )
                    for p in config.printers
                    if p.printer_id is not None
                },
                config.state_dir,
                backoff=backoff,
                board=board,
                spools=spools,
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
                printer.adapter.use_spools(spools)
                if printer.printer_id is not None:
                    printer.adapter.use_state(sink.state(printer.name))
                log.info(
                    "[printer %s] watching: %s, logging to printer %s",
                    printer.name, printer.adapter.describe(), printer.printer_id,
                )  # fmt: skip
            try:
                await asyncio.gather(
                    sink.run(),
                    heartbeat.run(),
                    spools.run(),
                    _keep_touching(board, touch_interval),
                    *(p.adapter.run(sink, board.printer(p.name)) for p in config.printers),
                )
            finally:
                sink.close()
    finally:
        for trace in traces:
            trace.close()
        board.stopped()


def _start_tracing(config: BridgeConfig) -> list[Trace]:
    """With `trace = true`, a trace for each printer, handed to its adapter."""
    if not config.trace:
        return []
    secrets = known_secrets(config.path, config.state_dir, os.environ)
    if config.api_key:
        secrets.append(config.api_key)
    traces = []
    for printer in config.printers:
        trace = Trace(config.state_dir, printer.name, secrets=secrets)
        printer.adapter.use_trace(trace)
        traces.append(trace)
    log.info(
        "tracing is on: every message to and from each printer is recorded in %s",
        trace_dir(config.state_dir),
    )
    return traces


async def _keep_touching(board: StatusBoard, interval: float) -> None:
    """Rewrites the status file now and then, so `status` can tell the bridge is still up."""
    while True:
        await asyncio.sleep(interval)
        board.touch()
