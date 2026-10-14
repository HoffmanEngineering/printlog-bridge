"""Moonraker's Spoolman integration, when it points at 3D Print Log.

3D Print Log answers Spoolman's API at `/spoolman`, so a user can set moonraker.conf's

    [spoolman]
    server: https://printer-<id>:<secret>@api.3dprintlog.com/spoolman

and Mainsail, Fluidd and KlipperScreen list their 3D Print Log spools, by spool number, in their
spool pickers. A pick there sets Moonraker's active spool, which Moonraker announces to every
client as `notify_active_spool_set`. The bridge loads that spool into the active tool's slot in
3D Print Log, so the spool the printer says is loaded and the one 3D Print Log logs the print on
are the same. A pick while a job runs is a swap, split where it happened, the same as
`PRINTLOG_SET_SPOOL` mid-print: it goes through the same `SpoolSink.spool_command`, stamped with
the job's point when it was heard.

Only when the `[spoolman]` server is 3D Print Log's own host (the bridge's `api_url`; the
userinfo and port aside): with a Spoolman server of the user's own, spool ids are that server's,
and mean nothing in 3D Print Log. Moonraker's config is read on every connect, since a change to
it takes a Moonraker restart, which is a reconnect.

A cleared pick changes nothing in 3D Print Log: clearing the active spool in Moonraker says
nothing about what's in the slot. A pick is never answered on the console (it didn't come from a
macro); what came of it is in the bridge's log and `printlog-bridge status`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from printlog_bridge.core.events import JobPoint, SpoolCommand, SpoolSink

from .session import Call, MoonrakerListener
from .spools import tool_count
from .tools import slot_of

log = logging.getLogger(__name__)


def host_of(url: object) -> str | None:
    """The URL's host in lowercase, without userinfo or port; None when it has none."""
    if not isinstance(url, str):
        return None
    try:
        return urlsplit(url.strip()).hostname or None
    except ValueError:
        return None


class SpoolmanSync(MoonrakerListener):
    """A MoonrakerListener that loads the spool picked on the printer into the active tool's
    slot, when Moonraker's Spoolman server is 3D Print Log."""

    def __init__(
        self,
        printer: str,
        api_url: str,
        spools: SpoolSink,
        point: Callable[[], JobPoint | None] | None = None,
    ) -> None:
        self.printer = printer
        self.api_host = host_of(api_url)
        self.spools = spools
        self.point = point or (lambda: None)
        """Where the running job is (the JobTracker's), so a pick mid-print is a swap."""
        self.enabled = False
        """Whether Moonraker's `[spoolman]` server is 3D Print Log, as read on connect."""
        self.moonraker_spool: int | None = None
        """Moonraker's active spool (a 3D Print Log spool number), as last read or set."""
        self.active_slot: int | None = None
        """The active tool's slot (`toolhead.extruder`), or None until Klipper says."""
        self._call: Call | None = None
        self._tools: int | None = None
        self._tasks: set[asyncio.Future] = set()
        self._in_order: asyncio.Lock | None = None

    async def on_connected(self, call: Call) -> None:
        self._call = call
        self._tools = None
        self.active_slot = None
        # Set once known, not reset first: what the last connection read stands until then.
        enabled = await self._points_at_printlog(call)
        self.moonraker_spool = await self._active_spool(call) if enabled else None
        self.enabled = enabled

    async def on_klippy_state(self, state: str) -> None:
        if state == "ready":
            self._tools = None  # a config change may have added a tool

    async def on_status(self, status: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        slot = slot_of((status.get("toolhead") or {}).get("extruder"))
        if slot is not None:
            self.active_slot = slot

    async def on_history(self, action: str, job: Mapping[str, Any]) -> None:
        pass

    async def on_gcode_response(self, line: str) -> None:
        pass

    async def on_active_spool(self, spool_id: int | None) -> None:
        self.moonraker_spool = spool_id
        if not self.enabled:
            log.debug("[printer %s] Moonraker's active spool is now %s", self.printer, spool_id)
            return
        if spool_id is None:
            log.info(
                "[printer %s] the spool picked on the printer was cleared; 3D Print Log's "
                "slots are left as they are",
                self.printer,
            )
            return
        slot = self.active_slot
        if slot is None:
            log.info(
                "[printer %s] spool number %d was picked on the printer before Klipper said "
                "which tool is active; nothing changed",
                self.printer, spool_id,
            )  # fmt: skip
            return
        # Where the job is now, not once 3D Print Log answers: the split falls at the pick.
        command = SpoolCommand(self.printer, slot, spool_number=spool_id, during=self.point())
        log.info(
            "[printer %s] spool number %d was picked on the printer, for T%d",
            self.printer, spool_id, slot,
        )  # fmt: skip
        self._spawn(self._carry_out(command, self._call))

    async def _points_at_printlog(self, call: Call) -> bool:
        """Whether moonraker.conf's `[spoolman]` server is 3D Print Log. Says so in the log,
        once a connection, either way."""
        try:
            config = (await call("server.config", None)).get("config") or {}
        except Exception as e:
            log.info(
                "[printer %s] couldn't read Moonraker's config (%s), so spools picked on the "
                "printer aren't synced with 3D Print Log",
                self.printer, e,
            )  # fmt: skip
            return False
        section = config.get("spoolman")
        if not isinstance(section, Mapping):
            log.info(
                "[printer %s] Moonraker has no [spoolman] section, so spools picked on the "
                "printer aren't synced with 3D Print Log",
                self.printer,
            )
            return False
        # Only the host is ever logged: the server URL holds the printer's Spoolman secret.
        host = host_of(section.get("server"))
        if host is None or self.api_host is None or host != self.api_host:
            log.info(
                "[printer %s] Moonraker's Spoolman server is %s, not 3D Print Log (%s), so "
                "spools picked on the printer aren't synced with 3D Print Log",
                self.printer, host or "unset", self.api_host,
            )  # fmt: skip
            return False
        log.info(
            "[printer %s] Moonraker's Spoolman server is 3D Print Log: a spool picked on the "
            "printer is loaded into the active tool's slot",
            self.printer,
        )
        return True

    async def _active_spool(self, call: Call) -> int | None:
        try:
            spool_id = (await call("server.spoolman.get_spool_id", None)).get("spool_id")
        except Exception as e:
            log.debug("[printer %s] couldn't read Moonraker's active spool: %s", self.printer, e)
            return None
        if isinstance(spool_id, int) and not isinstance(spool_id, bool):
            return spool_id
        return None

    async def _carry_out(self, command: SpoolCommand, call: Call | None) -> None:
        if self._in_order is None:
            self._in_order = asyncio.Lock()  # made here, in the running loop (Python 3.9)
        async with self._in_order:  # picks are carried out in the order they were made
            if self._tools is None and call is not None:
                self._tools = await tool_count(call)
            # Never raises; it logs what came of it, and puts it on the status board.
            await self.spools.spool_command(replace(command, slot_count=self._tools))

    def _spawn(self, work) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Future) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("[printer %s] a spool pick failed", self.printer, exc_info=task.exception())
