"""The interface every printer adapter implements. See docs/adapters.md."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from typing import ClassVar

from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.events import EventSink
from printlog_bridge.core.status import PrinterReport


class Adapter(ABC):
    """Watches one printer and reports its jobs as events.

    One instance is created per `[printer <name>]` section. The bridge never calls into a printer
    any other way, so a new kind of printer is a new subclass, registered by name in
    `printlog_bridge.adapters`.
    """

    name: ClassVar[str]
    """What `adapter = ...` names in the config file."""

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

    @abstractmethod
    async def check(self) -> Sequence[CheckResult]:
        """Diagnostics for `doctor`, such as whether the printer is reachable."""
