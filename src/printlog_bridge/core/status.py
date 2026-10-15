"""What `printlog-bridge run` is doing, in `state_dir/status.json`, for `printlog-bridge status`.

The running bridge rewrites the file whenever something changes, and at least every
`TOUCH_INTERVAL_S`, so a file that stops changing means the bridge has stopped.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from printlog_bridge import __version__
from printlog_bridge.core.outbox import read_waiting
from printlog_bridge.core.trace import trace_dir
from printlog_bridge.core.updates import is_newer, update_notice

if TYPE_CHECKING:  # config imports the adapter interface, which imports this module
    from printlog_bridge.core.config import BridgeConfig

STATUS_FILE = "status.json"
TOUCH_INTERVAL_S = 60
RUNNING_WITHIN = timedelta(seconds=3 * TOUCH_INTERVAL_S)
"""A status file older than this was left by a bridge that stopped without saying so."""
STALE_HEARTBEAT = timedelta(minutes=15)
"""When 3D Print Log starts showing a connection as offline."""

Clock = Callable[[], datetime]

log = logging.getLogger(__name__)


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

    def moved(self, old: str, new: str, at: str | None = None) -> None:
        """The printer was found at a new address (`old` and `new` are hosts, `at` when, ISO
        8601, defaulting to now)."""

    def software(self, name: str, version: str) -> None:
        """The version of the printer's `name` software (`klipper`, `moonraker`), sent with the
        connection's heartbeat."""

    def printer_model(self, model: str) -> None:
        """The printer's model, as the slicer named it in a job's file, sent with the heartbeat.
        Never a host name or anything the user typed for the printer."""


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

    def moved(self, old: str, new: str, at: str | None = None) -> None:
        pass

    def software(self, name: str, version: str) -> None:
        pass

    def printer_model(self, model: str) -> None:
        pass


class StatusBoard:
    def __init__(self, state_dir: Path, clock: Clock = utc_now) -> None:
        self.state_dir = state_dir
        self.path = state_dir / STATUS_FILE
        self.clock = clock
        self._data: dict[str, Any] = {}
        self._failing = False

    def start(self, printers: Iterable[str], trace: bool = False) -> None:
        now = self._now()
        self._data = {
            "version": __version__,
            "pid": os.getpid(),
            "started_at": now,
            "updated_at": now,
            "stopped_at": None,
            "trace": trace,
            "latest_version": None,
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
                    "moved": None,
                    "software": {},
                    "printer_model": None,
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

    def latest_version(self, version: str | None) -> None:
        """The newest bridge release, as 3D Print Log last reported it, for `status` and
        `doctor` to say when this one is behind."""
        if self._data.get("latest_version") != version:
            self._data["latest_version"] = version
            self._write()

    def _now(self) -> str:
        return self.clock().isoformat()

    def _write(self) -> None:
        """Writes the file. A full or read-only disk is logged once and never raised: the
        status file is only a report, and the bridge must keep logging prints without it."""
        self._data["updated_at"] = self._now()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as e:
            if not self._failing:
                log.warning(
                    "couldn't write %s (%s); `printlog-bridge status` is out of date until it "
                    "can be written again",
                    self.path, e,
                )  # fmt: skip
            self._failing = True
        else:
            if self._failing:
                log.info("%s is written again", self.path)
            self._failing = False


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

    def moved(self, old: str, new: str, at: str | None = None) -> None:
        self.board.update(
            self.printer, moved={"from": old, "to": new, "at": at or self.board._now()}
        )

    def software(self, name: str, version: str) -> None:
        software = dict(self.board.get(self.printer).get("software") or {})
        if software.get(name) == version:
            return
        software[name] = version
        self.board.update(self.printer, software=software)

    def printer_model(self, model: str) -> None:
        if self.board.get(self.printer).get("printer_model") != model:
            self.board.update(self.printer, printer_model=model)

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
    update = _update_line(config, status, running)
    if update:
        lines.append(update)
    trace = _trace_line(config, status, running)
    if trace:
        lines.append(trace)

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


def _update_line(config: BridgeConfig, status: dict[str, Any], running: bool) -> str | None:
    """When the last heartbeat heard of a newer bridge than the one running (or, when none
    runs, the one installed)."""
    latest = status.get("latest_version")
    current = status.get("version") if running else __version__
    if not config.update_check or not isinstance(current, str) or not is_newer(latest, current):
        return None
    return f"Update: {update_notice(latest, current)}"


def _trace_line(config: BridgeConfig, status: dict[str, Any], running: bool) -> str | None:
    folder = trace_dir(config.state_dir)
    if running and status.get("trace"):
        return f"Trace: on, recording every Moonraker message in {folder}"
    if not config.trace:
        return None
    if running:
        return (
            "Trace: on in the config, but the running bridge started without it; restart it "
            "to start recording"
        )
    return f"Trace: on; the bridge records every Moonraker message while it runs, in {folder}"


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
    moved = known.get("moved")
    if moved:
        lines.append(
            f"Address: moved from {moved['from']} to {moved['to']} at {_clock(moved['at'])}; "
            "the bridge found it again, and the config is unchanged"
        )
    if running and known.get("printer_state"):
        lines.append(f"Printer state: {known['printer_state']}")
    key = f"API key from {config.describe_api_key()}" if config.api_key else "no API key"
    lines.append(f"Pairing: not paired; {key}")

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

    waiting = read_waiting(config.state_dir).get(printer.name)
    if waiting is None:
        lines.append("Outbox: empty")
    else:
        jobs = f"{waiting.jobs} job{'' if waiting.jobs == 1 else 's'} waiting to be logged"
        since = f"the oldest for {_ago(now, waiting.since.isoformat())}"
        if waiting.jobs == 1:
            since = f"for {_ago(now, waiting.since.isoformat())}"
        line = f"Outbox: {jobs}, {since}"
        if waiting.last_error:
            line += f"; last try: {waiting.last_error}"
        lines.append(line)

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
