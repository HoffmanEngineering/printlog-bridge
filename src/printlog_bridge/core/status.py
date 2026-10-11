"""What `printlog-bridge run` is doing, in `state_dir/status.json`, for `printlog-bridge status`.

The running bridge rewrites the file whenever something changes, and at least every
`TOUCH_INTERVAL_S`, so a file that stops changing means the bridge has stopped.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from printlog_bridge import __version__
from printlog_bridge.core.state import PrinterState

if TYPE_CHECKING:  # config imports the adapter interface, which imports this module
    from printlog_bridge.core.config import BridgeConfig

STATUS_FILE = "status.json"
TOUCH_INTERVAL_S = 60
RUNNING_WITHIN = timedelta(seconds=3 * TOUCH_INTERVAL_S)
"""A status file older than this was left by a bridge that stopped without saying so."""
STALE_HEARTBEAT = timedelta(minutes=15)
"""When 3D Print Log starts showing a connection as offline."""

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PrinterReport(Protocol):
    """What an adapter tells `printlog-bridge status` about its printer."""

    def connected(self) -> None:
        """The adapter is connected to the printer."""

    def disconnected(self, reason: str) -> None:
        """The connection dropped or couldn't be made, and why, in words for the user."""

    def printer_state(self, state: str) -> None:
        """The printer's own state, such as Klipper's `ready` or `shutdown`."""

    def warn(self, topic: str, message: str | None) -> None:
        """Something the user should fix, until the same `topic` is cleared with None."""


class NullReport:
    """A PrinterReport that keeps nothing, for code run outside `printlog-bridge run`."""

    def connected(self) -> None:
        pass

    def disconnected(self, reason: str) -> None:
        pass

    def printer_state(self, state: str) -> None:
        pass

    def warn(self, topic: str, message: str | None) -> None:
        pass


class StatusBoard:
    def __init__(self, state_dir: Path, clock: Clock = utc_now) -> None:
        self.path = state_dir / STATUS_FILE
        self.clock = clock
        self._data: dict[str, Any] = {}

    def start(self, printers: Iterable[str]) -> None:
        now = self._now()
        self._data = {
            "version": __version__,
            "pid": os.getpid(),
            "started_at": now,
            "updated_at": now,
            "stopped_at": None,
            "printers": {
                name: {
                    "connection": "starting",
                    "connection_detail": None,
                    "connection_since": now,
                    "printer_state": None,
                    "last_event": None,
                    "last_event_at": None,
                    "last_heartbeat_at": None,
                    "warnings": {},
                }
                for name in printers
            },
        }
        self._write()

    def printer(self, name: str) -> BoardReport:
        return BoardReport(self, name)

    def touch(self) -> None:
        self._write()

    def stopped(self) -> None:
        self._data["stopped_at"] = self._now()
        self._write()

    def update(self, printer: str, **fields: Any) -> None:
        self._data["printers"][printer].update(fields)
        self._write()

    def get(self, printer: str) -> dict[str, Any]:
        return self._data["printers"][printer]

    def _now(self) -> str:
        return self.clock().isoformat()

    def _write(self) -> None:
        self._data["updated_at"] = self._now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)


class BoardReport:
    """One printer's part of the board: the PrinterReport adapters get, plus the bridge's own."""

    def __init__(self, board: StatusBoard, printer: str) -> None:
        self.board = board
        self.printer = printer

    def connected(self) -> None:
        if self.board.get(self.printer)["connection"] == "connected":
            return
        self.board.update(
            self.printer,
            connection="connected",
            connection_detail=None,
            connection_since=self.board._now(),
        )

    def disconnected(self, reason: str) -> None:
        current = self.board.get(self.printer)
        since = current["connection_since"]
        if current["connection"] != "disconnected":
            since = self.board._now()
        self.board.update(
            self.printer,
            connection="disconnected",
            connection_detail=reason,
            connection_since=since,
        )

    def printer_state(self, state: str) -> None:
        self.board.update(self.printer, printer_state=state)

    def warn(self, topic: str, message: str | None) -> None:
        warnings = dict(self.board.get(self.printer)["warnings"])
        if message is None:
            if topic not in warnings:
                return
            warnings.pop(topic)
        else:
            if warnings.get(topic) == message:
                return
            warnings[topic] = message
        self.board.update(self.printer, warnings=warnings)

    def event(self, text: str) -> None:
        self.board.update(self.printer, last_event=text, last_event_at=self.board._now())

    def heartbeat(self) -> None:
        self.board.update(self.printer, last_heartbeat_at=self.board._now())


def read_status(state_dir: Path) -> dict[str, Any] | None:
    """What the last `run` wrote, or None before the first one (or if the file is unreadable)."""
    try:
        data = json.loads((state_dir / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def format_status(config: BridgeConfig, now: datetime | None = None) -> tuple[str, int]:
    """`printlog-bridge status`: the report, and the exit code (0 only while the bridge runs)."""
    now = now or utc_now()
    status = read_status(config.state_dir)
    if status is None:
        return (
            f"The bridge hasn't run yet with the state directory {config.state_dir}. "
            "Start it with `printlog-bridge run`, or check the setup with "
            "`printlog-bridge doctor`.",
            1,
        )

    running = (
        status.get("stopped_at") is None and now - _time(status["updated_at"]) <= RUNNING_WITHIN
    )
    if running:
        lines = [
            f"Bridge: running since {_clock(status['started_at'])} "
            f"(version {status.get('version')}, pid {status.get('pid')})"
        ]
    elif status.get("stopped_at"):
        lines = [f"Bridge: not running: stopped at {_clock(status['stopped_at'])}"]
    else:
        lines = [
            f"Bridge: not running: it last checked in {_ago(now, status['updated_at'])} ago and "
            "may have crashed. Check its log."
        ]

    for printer in config.printers:
        known = status.get("printers", {}).get(printer.name)
        linked = (
            f"printer {printer.printer_id} in 3D Print Log"
            if printer.printer_id is not None
            else "no printer_id yet"
        )
        lines.append(f"Printer {printer.name} ({linked})")
        lines.extend(f"  {line}" for line in _printer_lines(printer, known, running, config, now))

    return "\n".join(lines), 0 if running else 1


def _printer_lines(printer, known, running: bool, config: BridgeConfig, now: datetime) -> list[str]:
    if known is None:
        connection = "not watched by the running bridge; restart it" if running else "not running"
        known = {}
    elif not running:
        connection = "not running"
    else:
        connection = f"{known['connection']} for {_ago(now, known['connection_since'])}"
        if known.get("connection_detail"):
            connection += f": {known['connection_detail']}"
        elif known["connection"] == "connected":
            connection += f" ({printer.adapter.describe()})"

    lines = [f"Connection: {connection}"]
    if running and known.get("printer_state"):
        lines.append(f"Printer state: {known['printer_state']}")
    lines.append("Pairing: not paired; logging with the API key in [bridge]")

    if known.get("last_event"):
        lines.append(f"Last event: {known['last_event']} ({_ago(now, known['last_event_at'])} ago)")
    else:
        lines.append("Last event: none yet")

    heartbeat = known.get("last_heartbeat_at")
    if heartbeat is None:
        lines.append("Heartbeat: none yet")
    elif now - _time(heartbeat) >= STALE_HEARTBEAT:
        lines.append(
            f"Heartbeat: {_ago(now, heartbeat)} ago; 3D Print Log shows the printer as offline"
        )
    else:
        lines.append(f"Heartbeat: {_ago(now, heartbeat)} ago")

    state = PrinterState.peek(config.state_dir, printer.name)
    waiting = state.waiting() if state is not None else 0
    if waiting:
        lines.append(f"Outbox: {waiting} job{'' if waiting == 1 else 's'} waiting to be logged")
    else:
        lines.append("Outbox: empty")

    warnings = list((known.get("warnings") or {}).values())
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in warnings)
    else:
        lines.append("Warnings: none")
    return lines


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _clock(value: str) -> str:
    return _time(value).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _ago(now: datetime, value: str) -> str:
    seconds = max(0, int((now - _time(value)).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"
