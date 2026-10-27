"""Registers each printer's connection with 3D Print Log, and keeps it alive with a heartbeat.

A bridge watching three printers registers three connections, each under its printer's instance
id (kept in the state directory). The API links a print to its connection only if the connection
was registered before the print was created, so the bridge registers on start and again right
before creating a print if that first attempt failed. When that second attempt can't reach 3D
Print Log either, the print waits and is tried again like any other request: created anyway, it
would get through as soon as the API was back (the end of an outage) and never link. The app
shows a connection as stale after 15 minutes without a heartbeat.

Each heartbeat also carries the printer's health (#37): its firmware and host software, each with
its version, once the adapter has said, the outbox's backlog, and the problems logged since the
last heartbeat. docs/privacy.md lists every field.

3D Print Log answers each heartbeat with the newest bridge release (`latestAgentVersion`), which
goes in the status file for `status` and `doctor`, and in the log once per version (#39).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from printlog_bridge import __version__
from printlog_bridge.core.api_client import (
    ApiClient,
    ApiRejected,
    ApiUnavailable,
    AuthRejected,
    RepairNeeded,
)
from printlog_bridge.core.health import ProblemLog, Problems, utc_now
from printlog_bridge.core.outbox import read_waiting
from printlog_bridge.core.status import FIRMWARE, HOST_SOFTWARE, StatusBoard
from printlog_bridge.core.updates import is_newer, update_notice

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 5 * 60
MAX_DISPLAY_NAME = 100
"""The API's limit."""
MAX_VERSION = 50
"""For a software name and its version alike."""
MAX_PRINTER_MODEL = 100
"""The API's limits."""

TOPIC = "heartbeat"
NO_PROBLEMS = Problems(0, None, None)


@dataclass(frozen=True)
class Connection:
    printer: str
    """The `[printer <name>]`."""
    instance_id: str
    kind: str
    """The adapter's name, such as `moonraker`."""
    printer_id: int
    mode: str | None = None
    """How the adapter reaches the printer (`local` or `remote`), if it has modes."""
    snapshots: bool | None = None
    """Whether a webcam frame is added to each print; None for an adapter without webcams."""
    slicer_settings: bool = False
    """Whether each print's slicer settings are sent (`slicer_settings`)."""


class Heartbeat:
    def __init__(
        self,
        api: ApiClient,
        connections: Sequence[Connection],
        board: StatusBoard,
        interval: float = HEARTBEAT_INTERVAL_S,
        problems: ProblemLog | None = None,
        clock: Callable[[], datetime] = utc_now,
        update_check: bool = True,
    ) -> None:
        self.api = api
        self.connections = {c.printer: c for c in connections}
        self.board = board
        self.interval = interval
        self.update_check = update_check
        self.problems = problems
        """Where the problems each heartbeat reports are counted; None reports none."""
        self.clock = clock
        self._registered: set[str] = set()
        self._announced: set[str] = set()

    async def run(self) -> None:
        """Beats for every printer now and then every `interval`, until cancelled."""
        while True:
            await self.beat_all()
            await asyncio.sleep(self.interval)

    async def beat_all(self) -> None:
        for printer in list(self.connections):
            if printer in self.connections:  # not stopped meanwhile
                await self.beat(printer)

    def add(self, connection: Connection) -> None:
        """Starts beating for a printer the bridge starts watching while it runs (one connected
        in the app). Its first heartbeat goes with the next round, or before its first print."""
        self.connections[connection.printer] = connection
        self._registered.discard(connection.printer)

    def remove(self, printer: str) -> None:
        """Stops beating for a printer no longer watched: 3D Print Log shows it as offline."""
        self.connections.pop(printer, None)
        self._registered.discard(printer)

    async def ensure_registered(self, printer: str) -> None:
        """Registers the printer's connection if no heartbeat has got through yet. Raises
        ApiUnavailable when 3D Print Log couldn't be reached, so the print this runs before
        waits for it; a refused connection doesn't raise, since trying again won't change it."""
        if printer in self.connections and printer not in self._registered:
            await self.beat(printer, raise_unavailable=True)

    async def beat(self, printer: str, raise_unavailable: bool = False) -> bool:
        connection = self.connections.get(printer)
        if connection is None:
            return False
        report = self.board.printer(printer)
        problems = self.problems.problems(printer) if self.problems else NO_PROBLEMS
        body = self._body(connection, problems)
        try:
            answer = await self.api.put_connection(connection.instance_id, body)
        except RepairNeeded as e:
            problem = f"{e}. Until then the printer shows as offline and its jobs wait."
        except AuthRejected:
            problem = (
                "3D Print Log rejected the API key, so the printer shows as offline and its jobs "
                "aren't logged. Check the key with `printlog-bridge doctor` "
                "(`printlog-bridge store-key` replaces it), then restart the bridge."
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
            if raise_unavailable:
                report.warn(TOPIC, problem)
                raise
        except Exception as e:  # a heartbeat must never take the bridge down
            problem = f"couldn't send the heartbeat: {e!r}"
            log.exception("[printer %s] couldn't send the heartbeat", printer)
        else:
            if printer not in self._registered:
                log.info("[printer %s] registered with 3D Print Log", printer)
            self._registered.add(printer)
            if self.problems:
                self.problems.reported(printer, problems.count)
            report.heartbeat()
            report.warn(TOPIC, None)
            self._heard_latest(answer)
            return True
        report.warn(TOPIC, problem)
        return False

    def _heard_latest(self, answer: object) -> None:
        """Keeps the newest release the API reported, and logs it the first time it is newer
        than this bridge. Every printer's check-in answers the same, so it is logged once."""
        if not self.update_check:
            return
        latest = answer.get("latestAgentVersion") if isinstance(answer, dict) else None
        if not isinstance(latest, str) or not latest.strip():
            latest = None
        self.board.latest_version(latest)
        if latest is not None and is_newer(latest) and latest not in self._announced:
            self._announced.add(latest)
            log.info("%s", update_notice(latest))

    def _body(self, connection: Connection, problems: Problems) -> dict[str, Any]:
        """Everything a heartbeat sends. Every field is listed in docs/privacy.md, and none may
        hold a host name, an IP address, a file name or a key (tests/test_privacy.py)."""
        known = self.board.get(connection.printer)
        software = known.get("software") or {}
        waiting = read_waiting(self.board.state_dir).get(connection.printer)
        now = self.clock()
        return {
            "kind": connection.kind,
            "displayName": f"printlog-bridge ({connection.printer})"[:MAX_DISPLAY_NAME],
            "agentVersion": __version__,
            "printerId": connection.printer_id,
            "mode": connection.mode,
            **_software(software.get(FIRMWARE), "firmware"),
            **_software(software.get(HOST_SOFTWARE), "hostSoftware"),
            "printerModel": _cut(known.get("printer_model"), MAX_PRINTER_MODEL),
            "outboxDepth": waiting.entries if waiting else 0,
            "outboxOldestAgeSeconds": (
                max(0, int((now - waiting.since).total_seconds())) if waiting else None
            ),
            "lastError": problems.last,
            "lastErrorAt": problems.last_at.isoformat() if problems.last_at else None,
            "errorCount": problems.count,
            "snapshots": connection.snapshots,
            "slicerSettings": connection.slicer_settings,
        }


def _software(known: Any, prefix: str) -> dict[str, str]:
    """`<prefix>Name` and `<prefix>Version`, together, or neither until both are known: a
    null would read as cleared to an older API, so the first check-in after a start, sent
    before the adapter has asked the printer, leaves them out (#80)."""
    if not isinstance(known, dict):
        return {}
    name, version = _cut(known.get("name"), MAX_VERSION), _cut(known.get("version"), MAX_VERSION)
    if name is None or version is None:
        return {}
    return {f"{prefix}Name": name, f"{prefix}Version": version}


def _cut(value: Any, limit: int) -> str | None:
    return str(value)[:limit] if value else None
