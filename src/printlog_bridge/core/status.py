"""What `printlog-bridge run` is doing, in `state_dir/status.json`, for `printlog-bridge status`.

The running bridge rewrites the file whenever something changes, and at least every
`TOUCH_INTERVAL_S`, so a file that stops changing means the bridge has stopped.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from printlog_bridge import __version__
from printlog_bridge.core import credentials
from printlog_bridge.core.device import read_pending_pairing
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
FIRMWARE, HOST_SOFTWARE = "firmware", "host"
"""The keys of a printer's `software` in the status file, each `{"name", "version"}`."""

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

    def firmware(self, name: str, version: str) -> None:
        """The printer's firmware and its version (`Klipper`, `Prusa-Firmware-Buddy`), sent with
        the connection's heartbeat once known."""

    def host_software(self, name: str, version: str) -> None:
        """The software the adapter talks to on the printer's host and its version (`Moonraker`,
        `PrusaLink`), sent with the connection's heartbeat once known. Never for a printer the
        adapter talks to directly."""

    def printer_model(self, model: str) -> None:
        """The printer's model, as the slicer named it in a job's file, sent with the heartbeat.
        Never a host name or anything the user typed for the printer."""

    def credentials_refused(self, fields: Sequence[str]) -> None:
        """The printer refused the bridge's credentials: `fields` names the options it sent
        (`access_code`, `password`), so a printer connected in the app can report the ones that
        came from the app as `rejected_by_printer`. `connected()` clears it."""


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

    def firmware(self, name: str, version: str) -> None:
        pass

    def host_software(self, name: str, version: str) -> None:
        pass

    def printer_model(self, model: str) -> None:
        pass

    def credentials_refused(self, fields: Sequence[str]) -> None:
        pass


class StatusBoard:
    def __init__(self, state_dir: Path, clock: Clock = utc_now) -> None:
        self.state_dir = state_dir
        self.path = state_dir / STATUS_FILE
        self.clock = clock
        self._data: dict[str, Any] = {}
        self._failing = False

    def start(
        self, printers: Iterable[str], trace: bool = False, credentials: str | None = None
    ) -> None:
        """`credentials` is what the bridge signs in with: `device token` or `API key`."""
        now = self._now()
        self._data = {
            "version": __version__,
            "pid": os.getpid(),
            "started_at": now,
            "updated_at": now,
            "stopped_at": None,
            "trace": trace,
            "latest_version": None,
            "credentials": credentials,
            "credentials_problem": None,
            "setup_problem": None,
            "printers": {name: _new_printer(now) for name in printers},
            "bridge": None,
        }
        self._write()

    def add_printer(self, name: str) -> None:
        """A printer the bridge started watching while it runs (one connected in the app)."""
        if name not in self._data["printers"]:
            self._data["printers"][name] = _new_printer(self._now())
            self._write()

    def remove_printer(self, name: str) -> None:
        if self._data["printers"].pop(name, None) is not None:
            self._write()

    def bridge(self, **fields: Any) -> None:
        """The bridge's own part (#100): `discovery`, `check_in`, `desired_version`,
        `app_printers`. Only fields that changed are written."""
        current = self._data.get("bridge") or {}
        merged = {**current, **fields}
        if merged != current:
            self._data["bridge"] = merged
            self._write()

    def printer(self, name: str) -> BoardReport:
        return BoardReport(self, name)

    def touch(self) -> None:
        self._write()

    def stopped(self) -> None:
        self._data["stopped_at"] = self._now()
        self._write()

    def update(self, printer: str, **fields: Any) -> None:
        known = self._data["printers"].get(printer)
        if known is None:
            return  # no longer watched: an adapter stopping as it was told
        known.update(fields)
        self._write()

    def get(self, printer: str) -> dict[str, Any]:
        return self._data["printers"].get(printer) or _new_printer(self._now())

    def credentials_problem(self, problem: str | None) -> None:
        """What's wrong with the bridge's credentials (a device token that needs a re-pair), or
        None once it's fixed."""
        if self._data.get("credentials_problem") != problem:
            self._data["credentials_problem"] = problem
            self._write()

    def setup_problem(self, problem: str | None) -> None:
        """Why the bridge waits instead of watching printers (none configured yet:
        core/first_run.py), or None."""
        if self._data.get("setup_problem") != problem:
            self._data["setup_problem"] = problem
            self._write()

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


def _new_printer(now: str) -> dict[str, Any]:
    return {
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
        "credentials_refused": [],
    }


class BoardReport:
    """One printer's part of the board: the PrinterReport adapters get, plus the bridge's own."""

    def __init__(self, board: StatusBoard, printer: str) -> None:
        self.board = board
        self.printer = printer

    def connected(self) -> None:
        if self.board.get(self.printer).get("credentials_refused"):
            self.board.update(self.printer, credentials_refused=[])
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

    def firmware(self, name: str, version: str) -> None:
        self._software(FIRMWARE, name, version)

    def host_software(self, name: str, version: str) -> None:
        self._software(HOST_SOFTWARE, name, version)

    def _software(self, role: str, name: str, version: str) -> None:
        software = dict(self.board.get(self.printer).get("software") or {})
        known = {"name": name, "version": version}
        if software.get(role) == known:
            return
        software[role] = known
        self.board.update(self.printer, software=software)

    def printer_model(self, model: str) -> None:
        if self.board.get(self.printer).get("printer_model") != model:
            self.board.update(self.printer, printer_model=model)

    def credentials_refused(self, fields: Sequence[str]) -> None:
        refused = sorted(set(fields))
        if self.board.get(self.printer).get("credentials_refused") != refused:
            self.board.update(self.printer, credentials_refused=refused)

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


def is_running(status: Mapping[str, Any], now: datetime) -> bool:
    """Whether the bridge that wrote `status` still runs: it hasn't stopped, and it touched the
    file lately. `status` and the container's health check both go by this."""
    return status.get("stopped_at") is None and now - _time(status["updated_at"]) <= RUNNING_WITHIN


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

    running = is_running(status, now)
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
    lines.append(_credentials_line(config, status, running))
    pending = read_pending_pairing(config.state_dir) if running else None
    if pending is not None:
        link = (
            f" (or open {pending.verification_uri_complete})"
            if pending.verification_uri_complete
            else ""
        )
        lines.append(
            f"Pairing code: {pending.user_code}, to enter at {pending.verification_uri}{link}"
        )
    if running and status.get("setup_problem"):
        lines.append(f"Setup: waiting for printers: {status['setup_problem']}")
    update = _update_line(config, status, running)
    if update:
        lines.append(update)
    trace = _trace_line(config, status, running)
    if trace:
        lines.append(trace)

    lines.extend(bridge_lines(config, status, running, now))

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


def pairing_state(config: BridgeConfig, now: Callable[[], float] = time.time) -> str:
    """`printlog-bridge status --pairing`, one line for a script (the one-line Docker install):
    `pending <code> <address> [<link>]` while a pairing waits for approval (a re-pair of a paired
    bridge too), else `paired`, `api-key`, or `waiting` (no credentials and no code yet)."""
    pending = read_pending_pairing(config.state_dir, now=now)
    if pending is not None:
        return " ".join(
            part
            for part in (
                "pending",
                pending.user_code,
                pending.verification_uri,
                pending.verification_uri_complete,
            )
            if part
        )
    if config.device is not None:
        return "paired"
    if config.api_key is not None:
        return "api-key"
    return "waiting"


def bridge_lines(
    config: BridgeConfig, status: Mapping[str, Any], running: bool, now: datetime
) -> list[str]:
    """Discovery, the check-in, the desired list and the printers connected in the app (#100)."""
    bridge = status.get("bridge") or {}
    if config.device is None:
        return ["Discovery: off; it needs the bridge paired (`printlog-bridge pair`)"]
    lines = [key_line(config, bridge.get("key") if running else None)]
    discovery = bridge.get("discovery")
    if not config.discovery:
        lines.append("Discovery: off ([bridge] discovery = false)")
    elif running and isinstance(discovery, Mapping):
        found = discovery.get("found") or 0
        kinds = ", ".join(discovery.get("kinds") or [])
        sources = "; ".join(f"{k}: {v}" for k, v in (discovery.get("sources") or {}).items())
        lines.append(
            f"Discovery: on, {found} printer{'' if found == 1 else 's'} found"
            + (f" ({kinds})" if kinds else "")
            + (f"; {sources}" if sources else "")
        )
    elif running:
        lines.append("Discovery: on, starting after the first check-in")
    else:
        lines.append("Discovery: on while the bridge runs")
    if bridge.get("last_sweep_at"):
        lines.append(f"Last scan: {_ago(now, bridge['last_sweep_at'])} ago (asked in the app)")
    check_in = bridge.get("check_in") or {}
    if running and (check_in.get("at") or check_in.get("problem")):
        line = "Check-in: " + (
            f"last {_ago(now, check_in['at'])} ago" if check_in.get("at") else "none yet"
        )
        if check_in.get("problem"):
            line += f"; {check_in['problem']}"
        lines.append(line)
    if bridge.get("desired_version"):
        lines.append(f"Desired printers: version {bridge['desired_version']}")
    for entry, app in sorted((bridge.get("app_printers") or {}).items(), key=lambda kv: int(kv[0])):
        name = f" ({app['printer_name']})" if app.get("printer_name") else ""
        lines.append(
            f"App printer {entry}: printer {app.get('printer')}{name} in 3D Print Log, "
            f"{app.get('kind')} at {app.get('host')}: {app.get('state')}"
            + (f": {app['detail']}" if app.get("detail") else "")
        )
        known = status.get("printers", {}).get(f"app-{entry}")
        if known is not None and running:
            connection = f"{known['connection']} for {_ago(now, known['connection_since'])}"
            if known.get("connection_detail"):
                connection += f": {known['connection_detail']}"
            lines.append(f"  Connection: {connection}")
            if known.get("last_event"):
                lines.append(
                    f"  Last event: {known['last_event']} ({_ago(now, known['last_event_at'])} ago)"
                )
            for warning in (known.get("warnings") or {}).values():
                lines.append(f"  Warning: {warning}")
        for name, words in sorted((app.get("credentials") or {}).items()):
            lines.append(f"  {name}: {words}")
    return lines


def key_line(config: BridgeConfig, reported: Mapping[str, Any] | None) -> str:
    """The bridge's encryption key and where it stands with 3D Print Log (as the running bridge
    last heard), for `status`. Reads the key file; never makes one."""
    try:
        key = credentials.read_key(config.state_dir)
    except credentials.KeyUnavailable as e:
        return f"Encryption key: none: {e}"
    if key is None:
        return "Encryption key: none yet; the bridge makes one when it runs"
    line = f"Encryption key: {credentials.short_fingerprint(key.fingerprint)}"
    if reported and reported.get("fingerprint") == key.fingerprint and reported.get("detail"):
        line += f", {reported['detail']}"
    return line


def _credentials_line(config: BridgeConfig, status: dict[str, Any], running: bool) -> str:
    """How the bridge signs in: paired (a device token), or not, with an API key or none."""
    problem = status.get("credentials_problem") if running else None
    if problem:
        return f"Pairing: {problem}"
    if config.device is not None:
        name = f" as {config.device.name!r}" if config.device.name else ""
        return f"Pairing: paired{name} (device token in {config.device_token_file})"
    if config.api_key is not None:
        return f"Pairing: not paired; API key from {config.describe_api_key()}"
    return "Pairing: not paired, and no API key. Run `printlog-bridge pair`."


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
    # Python 3.9's fromisoformat doesn't take "Z", which the check-in times use.
    return datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)


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
