"""The interface every printer adapter implements. See docs/adapters.md."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.events import EventSink, PastJob, SpoolSink
from printlog_bridge.core.status import PrinterReport

if TYPE_CHECKING:
    from printlog_bridge.core.state import PrinterState
    from printlog_bridge.core.trace import Trace


class PrinterUnavailable(Exception):
    """The printer's history couldn't be read. The message says why, for the user."""


class Adapter(ABC):
    """Watches one printer and reports its jobs as events.

    One instance is created per `[printer <name>]` section. The bridge never calls into a printer
    any other way, so a new kind of printer is a new subclass, registered by name in
    `printlog_bridge.adapters`.
    """

    name: ClassVar[str]
    """What `adapter = ...` names in the config file."""

    history_name: ClassVar[str] = ""
    """What the user calls where the printer keeps its job history, such as "Moonraker", for
    the note on a backfilled print ("Imported from Moonraker history")."""

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

    async def past_jobs(self, since: datetime) -> Sequence[PastJob]:
        """The printer's finished jobs that started after `since`, oldest first, for
        `printlog-bridge backfill`. Called on its own, without `run`, while the bridge itself
        may be running; it must not disturb that. Raise `PrinterUnavailable` saying why when
        the history can't be read. Optional: an adapter without it can't backfill."""
        raise PrinterUnavailable(f"the {self.name} adapter can't read a printer's job history")

    @abstractmethod
    async def check(self) -> Sequence[CheckResult]:
        """Diagnostics for `doctor`, such as whether the printer is reachable."""
