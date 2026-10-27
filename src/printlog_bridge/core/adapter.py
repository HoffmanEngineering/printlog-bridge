"""The interface every printer adapter implements. See docs/adapters.md."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.discovery import Discovery, Found
from printlog_bridge.core.events import EventSink, SpoolSink
from printlog_bridge.core.status import PrinterReport

if TYPE_CHECKING:
    from printlog_bridge.core.state import PrinterState
    from printlog_bridge.core.trace import Trace


@dataclass(frozen=True)
class PrinterDetails:
    """What an adapter can tell about its printer, to fill in a new printer in 3D Print Log when
    the bridge pairs (`printlog-bridge pair`). Every field is a suggestion the user can change."""

    name: str | None = None
    """Such as the host name the printer goes by on the network."""
    make: str | None = None
    model: str | None = None
    slot_count: int | None = None
    """How many filament positions (tools, MMU or AMS slots) it has, when the adapter knows."""
    description: str | None = None


class Adapter(ABC):
    """Watches one printer and reports its jobs as events.

    One instance is created per `[printer <name>]` section. The bridge never calls into a printer
    any other way, so a new kind of printer is a new subclass, registered by name in
    `printlog_bridge.adapters`.
    """

    name: ClassVar[str]
    """What `adapter = ...` names in the config file."""

    snapshots: bool | None = None
    """Whether a frame from the printer's webcam is added to each print when it ends, for the
    connection's heartbeat. None for an adapter that never takes one."""

    def mode(self) -> str | None:
        """How the adapter reaches the printer, `local` or `remote`, for the connection's
        heartbeat. Optional: None for an adapter without modes. Never the address itself."""
        return None

    @classmethod
    @abstractmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> Adapter:
        """Build the adapter from its section's options (everything except `adapter`).

        Raise `ConfigError` with a message naming the section and the option for anything
        missing or invalid. Do no I/O here: `doctor` loads the config before it checks anything.
        """

    @abstractmethod
    def describe(self) -> str:
        """One line for `doctor` and logs, such as "moonraker, remote http://u1.local:7125"."""

    @abstractmethod
    async def run(self, sink: EventSink, report: PrinterReport) -> None:
        """Watch the printer until cancelled, emitting each job's events to `sink` in order.

        Tell `report` when the connection comes up or drops, the printer's own state, and
        anything the user has to fix, for `printlog-bridge status`. Reconnect on your own after a
        dropped connection; return only when cancelled.
        """

    def use_spools(  # noqa: B027 - optional, does nothing
        self, spools: SpoolSink, api_url: str | None = None
    ) -> None:
        """Called before `run` with where to send spool commands and slot readings, for an
        adapter whose printer has them (PRINTLOG_SET_SPOOL, an RFID reader), and where 3D Print
        Log's API is, so it can tell the printer's own integrations that point at 3D Print Log
        (Moonraker's Spoolman) from those that don't. Optional."""

    def use_trace(self, trace: Trace) -> None:  # noqa: B027 - optional, does nothing
        """Called before `run` when `trace = true`: record every message to and from the
        printer with `trace.record(SEND | RECEIVE, raw)`, and connections with `trace.mark`.
        Optional; an adapter without it simply isn't traced."""

    def use_state(self, state: PrinterState) -> None:  # noqa: B027 - optional, does nothing
        """Called before `run` with the printer's state file, for an adapter that remembers
        something of its own between runs in `state.discovery` (and calls `state.save()`), such
        as where it last found the printer. `doctor` also calls it before `check`, with the
        state read-only (`PrinterState.peek`): its `save()` raises. Optional."""

    def serves(self, path: Path) -> str | None:
        """Whether the software on the printer's host serves `path` to its users, so anyone who
        can open its web page can download it, for `doctor`'s check of where the API key is kept.
        Optional: None (the default) when it doesn't. Otherwise, words that follow "which" and
        "a folder", naming who can download it, such as "Moonraker serves to anyone who can open
        Mainsail or Fluidd". No I/O beyond looking at the local file system."""
        return None

    async def printer_details(self) -> PrinterDetails:
        """What the adapter knows of the printer's name, make, model and slots, asking the
        printer if it must, for `pair` to suggest when it creates the printer in 3D Print Log.
        Optional. Never raises for a printer that can't be reached: return what is known."""
        return PrinterDetails()

    @abstractmethod
    async def check(self) -> Sequence[CheckResult]:
        """Diagnostics for `doctor`, such as whether the printer is reachable."""

    # -- discovery and app-managed printers (#100) ---------------------------------------------

    app_secrets: ClassVar[tuple[str, ...]] = ()
    """The credentials a printer connected in the app takes: sealed to the bridge in the app
    (core/credentials.py), or from an `[app_printer <id>]` section of the config file. The same
    names as 3D Print Log's `secretFields`. `from_config` must use them as given (no trimming):
    a value opened from the app is used exactly as it opened."""

    @classmethod
    def discovery(cls) -> Discovery | None:
        """How this kind of printer is found on the network (core/discovery.py). Optional: None
        when it isn't. Never connects to a printer to log it."""
        return None

    @classmethod
    def app_options(
        cls, host: str, port: int | None, serial: str | None, secrets: Mapping[str, str]
    ) -> dict[str, str] | None:
        """The `from_config` options for a printer the user connected in the app, at `host` (an
        IP address the bridge resolved and checked is private), or None when this adapter can't
        watch one that way. `secrets` holds its credentials (from the app, or its
        `[app_printer <id>]` section). Raise `NeedsCredentials` when one is missing, naming it in
        `fields`. Optional."""
        return None

    def watches(self, found: Found) -> bool:
        """Whether `found` is the printer this adapter watches (by serial, MAC, or address and
        port), so a config-file printer is reported as such and wins over the app. No I/O."""
        return False

    def found_entry(self) -> Found | None:
        """This config-file printer as discovery would report it, when its address is known.
        Optional. No I/O."""
        return None


class NeedsCredentials(Exception):
    """A printer connected in the app needs a secret the bridge doesn't have yet. The message
    names the options to add; `fields` names the `app_secrets` that are missing, which the bridge
    reports to 3D Print Log as `missing` (none for something that isn't a credential)."""

    def __init__(self, message: str, fields: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.fields = fields
