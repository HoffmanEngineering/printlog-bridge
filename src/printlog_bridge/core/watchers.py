"""Starting and stopping one printer's watch while the bridge runs (#100): its adapter, its
connection's heartbeat, its spools and its line in the status file. Config-file printers start
when the bridge does; a printer connected in the app starts and stops when the desired list says
(core/app_printers.py).

A stopped printer's queued events still go out: the job logger keeps it, so a print it started
is still logged as far as the bridge saw it.

An adapter returns only when cancelled, so one that raises has a bug (a firmware message it
can't read, say). A config-file printer's takes the bridge down, and the service manager starts
it again. A printer connected in the app is watched again on its own after a wait that doubles
from `RESTART_MIN_S` to `RESTART_MAX_S` (and starts over once it has run for
`RESTART_RESET_S`), so one printer's bug neither stops the others nor leaves it unwatched until
the bridge restarts. The error is logged and shown in `status` until it connects again.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from printlog_bridge.core.config import PrinterConfig
from printlog_bridge.core.connections import Connection, Heartbeat
from printlog_bridge.core.health import ProblemLog
from printlog_bridge.core.lifecycle import JobLogger, LoggedPrinter
from printlog_bridge.core.spools import Spools
from printlog_bridge.core.status import StatusBoard
from printlog_bridge.core.trace import Trace

log = logging.getLogger(__name__)

RESTART_MIN_S = 10.0
RESTART_MAX_S = 15 * 60.0
RESTART_RESET_S = 60 * 60.0
"""An adapter that ran this long before it failed is tried again after `RESTART_MIN_S`."""


def restart_delay(failures: int) -> float:
    """How long to wait before watching a printer again after its adapter's `failures`th error
    in a row."""
    return min(RESTART_MIN_S * 2 ** min(max(failures - 1, 0), 30), RESTART_MAX_S)


def _words(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:.0f}s" if seconds >= 1 else f"{seconds:.2f}s"
    return f"{seconds / 60:.0f} min"


class Watchers:
    def __init__(
        self,
        sink: JobLogger,
        heartbeat: Heartbeat,
        spools: Spools,
        board: StatusBoard,
        problems: ProblemLog | None = None,
        api_url: str | None = None,
        trace: Callable[[str], Trace] | None = None,
    ) -> None:
        self.sink = sink
        self.heartbeat = heartbeat
        self.spools = spools
        self.board = board
        self.problems = problems
        self.api_url = api_url
        self.trace = trace
        """Makes each printer's trace, with `trace = true`."""
        self.traces: dict[str, Trace] = {}
        self.printers: dict[str, PrinterConfig] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._failed: asyncio.Future | None = None
        self._beats: set[asyncio.Task] = set()

    def _failure(self) -> asyncio.Future:
        if self._failed is None:
            self._failed = asyncio.get_running_loop().create_future()
        return self._failed

    def start(self, printer: PrinterConfig, *, fatal: bool = True) -> None:
        """Starts watching `printer`. With `fatal`, an adapter that fails (a bug: adapters
        return only when cancelled) takes the bridge down, as before; otherwise it is logged and
        that printer alone stops, and is watched again after a growing wait."""
        if printer.printer_id is None:
            raise ValueError(f"[printer {printer.name}] has no printer_id")
        name = printer.name
        self.printers[name] = printer
        self.sink.printers[name] = LoggedPrinter(
            printer_id=printer.printer_id,
            source=printer.adapter.name,
            adoption=printer.adoption,
            slicer_settings=printer.slicer_settings,
        )
        self.spools.printers[name] = printer.printer_id
        self.board.add_printer(name)
        if self.problems is not None:
            self.problems.add(name)
        self.heartbeat.add(
            Connection(
                printer=name,
                instance_id=self.sink.state(name).instance_id,
                kind=printer.adapter.name,
                printer_id=printer.printer_id,
                mode=printer.adapter.mode(),
                snapshots=printer.adapter.snapshots,
                slicer_settings=printer.slicer_settings,
            )
        )
        printer.adapter.use_spools(self.spools, self.api_url)
        printer.adapter.use_state(self.sink.state(name))
        if self.trace is not None:
            if name not in self.traces:
                self.traces[name] = self.trace(name)
            printer.adapter.use_trace(self.traces[name])
        log.info(
            "[printer %s] watching: %s, logging to printer %s",
            name, printer.adapter.describe(), printer.printer_id,
        )  # fmt: skip
        report = self.board.printer(name)
        task = asyncio.ensure_future(
            printer.adapter.run(self.sink, report) if fatal else self._keep_watching(printer)
        )
        self._tasks[name] = task
        task.add_done_callback(lambda t, name=name, fatal=fatal: self._ended(name, t, fatal))
        if not fatal:  # started while running: show it in 3D Print Log now, not in 5 minutes
            beat = asyncio.ensure_future(self.heartbeat.beat(name))
            self._beats.add(beat)
            beat.add_done_callback(self._beats.discard)

    async def _keep_watching(self, printer: PrinterConfig) -> None:
        """Runs a non-fatal printer's adapter, and runs it again after a growing wait each time
        it fails, until cancelled (by `stop`, so a printer let go isn't started again)."""
        loop = asyncio.get_running_loop()
        name = printer.name
        failures = 0
        while True:
            began = loop.time()
            try:
                await printer.adapter.run(self.sink, self.board.printer(name))
                return
            except Exception as error:
                if loop.time() - began >= RESTART_RESET_S:
                    failures = 0
                failures += 1
                delay = restart_delay(failures)
                log.error(
                    "[printer %s] stopped watching after an error: %r. It is watched again in "
                    "%s.",
                    name, error, _words(delay), exc_info=error,
                )  # fmt: skip
                self.board.printer(name).disconnected(
                    f"the bridge stopped watching it after an error ({error!r}); it tries again "
                    f"in {_words(delay)}"
                )
            await asyncio.sleep(delay)
            log.info("[printer %s] watching again after an error", name)

    def _ended(self, name: str, task: asyncio.Task, fatal: bool) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        if fatal:
            failure = self._failure()
            if not failure.done():
                failure.set_exception(error)
            return
        log.error("[printer %s] stopped watching after an error: %r", name, error)
        self.board.printer(name).disconnected(f"the bridge stopped watching it: {error!r}")

    async def stop(self, name: str) -> None:
        """Stops watching `name`: its adapter and heartbeat end, and it leaves the status file.
        Its queued events are still sent."""
        task = self._tasks.pop(name, None)
        self.printers.pop(name, None)
        self.heartbeat.remove(name)
        self.spools.printers.pop(name, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        trace = self.traces.pop(name, None)
        if trace is not None:
            trace.close()
        self.board.remove_printer(name)
        log.info("[printer %s] stopped watching", name)

    def watching(self, name: str) -> bool:
        return name in self._tasks

    async def run(self) -> None:
        """Waits until cancelled, or until a fatal watcher fails (then raises its error)."""
        try:
            await self._failure()
        finally:
            tasks = list(self._tasks.values())
            self._tasks.clear()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for trace in self.traces.values():
                trace.close()
