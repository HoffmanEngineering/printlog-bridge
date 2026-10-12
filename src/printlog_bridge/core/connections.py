"""Registers each printer's connection with 3D Print Log, and keeps it alive with a heartbeat.

A bridge watching three printers registers three connections, each under its printer's instance
id (kept in the state directory). The API links a print to its connection only if the connection
was registered before the print was created, so the bridge registers on start and again right
before creating a print if that first attempt failed. The app shows a connection as stale after
15 minutes without a heartbeat.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from printlog_bridge import __version__
from printlog_bridge.core.api_client import ApiClient, ApiRejected, ApiUnavailable, AuthRejected
from printlog_bridge.core.status import StatusBoard

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 5 * 60
MAX_DISPLAY_NAME = 100
"""The API's limit."""

TOPIC = "heartbeat"


@dataclass(frozen=True)
class Connection:
    printer: str
    """The `[printer <name>]`."""
    instance_id: str
    kind: str
    """The adapter's name, such as `moonraker`."""
    printer_id: int


class Heartbeat:
    def __init__(
        self,
        api: ApiClient,
        connections: Sequence[Connection],
        board: StatusBoard,
        interval: float = HEARTBEAT_INTERVAL_S,
    ) -> None:
        self.api = api
        self.connections = {c.printer: c for c in connections}
        self.board = board
        self.interval = interval
        self._registered: set[str] = set()

    async def run(self) -> None:
        """Beats for every printer now and then every `interval`, until cancelled."""
        while True:
            await self.beat_all()
            await asyncio.sleep(self.interval)

    async def beat_all(self) -> None:
        for printer in self.connections:
            await self.beat(printer)

    async def ensure_registered(self, printer: str) -> None:
        """Registers the printer's connection if no heartbeat has got through yet. Never raises."""
        if printer in self.connections and printer not in self._registered:
            await self.beat(printer)

    async def beat(self, printer: str) -> bool:
        connection = self.connections[printer]
        report = self.board.printer(printer)
        body = {
            "kind": connection.kind,
            "displayName": f"printlog-bridge ({printer})"[:MAX_DISPLAY_NAME],
            "agentVersion": __version__,
            "printerId": connection.printer_id,
        }
        try:
            await self.api.put_connection(connection.instance_id, body)
        except AuthRejected:
            problem = (
                "3D Print Log rejected the API key, so the printer shows as offline and its jobs "
                "aren't logged. Check api_key in [bridge], then restart the bridge."
            )
            log.error("[printer %s] %s", printer, problem)
        except ApiRejected as e:
            problem = (
                f"3D Print Log refused the connection ({e}). Check printer_id in "
                f"[printer {printer}]."
            )
            log.error("[printer %s] %s", printer, problem)
        except ApiUnavailable as e:
            problem = f"couldn't send the heartbeat: {e}. It is sent again in a few minutes."
            log.warning("[printer %s] %s", printer, problem)
        except Exception as e:  # a heartbeat must never take the bridge down
            problem = f"couldn't send the heartbeat: {e!r}"
            log.exception("[printer %s] couldn't send the heartbeat", printer)
        else:
            if printer not in self._registered:
                log.info("[printer %s] registered with 3D Print Log", printer)
            self._registered.add(printer)
            report.heartbeat()
            report.warn(TOPIC, None)
            return True
        report.warn(TOPIC, problem)
        return False
