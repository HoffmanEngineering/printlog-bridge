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

The other way, Moonraker's active spool follows the active tool's spool in 3D Print Log
(`server.spoolman.post_spool_id`): when the active tool changes (`toolhead.extruder`), and when
that tool's slot gets another spool in 3D Print Log (`Spools.watch_loaded`). So the usage
Moonraker reports, and the spool Mainsail shows as active, is the one the tool prints from. An
empty slot, or a spool without a number, leaves Moonraker's active spool as it was: there's
nothing to set it to, and clearing it would only stop its usage reports.

Nothing read is written back: a pick loaded into the slot finds Moonraker already on that spool,
and the `notify_active_spool_set` that answers the bridge's own post isn't taken for a pick. On
connect, Moonraker is set to the active tool's spool only when no print is running or paused:
the running print's usage is on Moonraker's active spool, and changing it then would put the
rest of the print on another spool than the one it started on. From the next tool or spool
change it follows again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from printlog_bridge.core.events import JobPoint, SpoolCommand, SpoolSink

from .session import Call, MoonrakerListener, MoonrakerRpcError
from .spools import tool_count
from .tools import slot_of

log = logging.getLogger(__name__)

METHOD_NOT_FOUND = -32601
"""What Moonraker answers a `server.spoolman.*` call with when its spoolman component isn't
loaded."""
BUSY = frozenset({"printing", "paused"})
"""`print_stats.state`s with a print under way, whose usage is on Moonraker's active spool."""


def host_of(url: object) -> str | None:
    """The URL's host in lowercase, without userinfo or port; None when it has none."""
    if not isinstance(url, str):
        return None
    try:
        return urlsplit(url.strip()).hostname or None
    except ValueError:
        return None


def spool_number(filament: Mapping[str, Any]) -> int | None:
    number = filament.get("spoolNumber")
    return number if isinstance(number, int) and not isinstance(number, bool) else None


class SpoolmanSync(MoonrakerListener):
    """A MoonrakerListener that loads the spool picked on the printer into the active tool's
    slot, and keeps Moonraker's active spool on the active tool's spool, when Moonraker's
    Spoolman server is 3D Print Log."""

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
        self._numbers: dict[int, int | None] | None = None
        """Each slot's spool number in 3D Print Log, as `Spools` last said; None until it has."""
        self._busy: bool | None = None
        """Whether a print is running or paused (`print_stats.state`); None until Klipper says."""
        self._settled = False
        """Whether this connection's first sync is decided: set Moonraker's active spool, or,
        with a print under way, leave it."""
        self._posted: int | None = None
        """The spool number the bridge set Moonraker to, until Moonraker announces it."""
        self._posts: asyncio.Lock | None = None

    async def on_connected(self, call: Call) -> None:
        self._call = call
        self._tools = None
        self.active_slot = None
        self._busy = None
        self._settled = False
        self._posted = None
        # Set once known, not reset first: what the last connection read stands until then.
        enabled = await self._points_at_printlog(call)
        spool, loaded = await self._active_spool(call) if enabled else (None, False)
        self.moonraker_spool = spool
        self.enabled = enabled and loaded

    async def on_klippy_state(self, state: str) -> None:
        if state == "ready":
            self._tools = None  # a config change may have added a tool

    async def on_status(self, status: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        state = (status.get("print_stats") or {}).get("state")
        if isinstance(state, str):
            self._busy = state in BUSY
        slot = slot_of((status.get("toolhead") or {}).get("extruder"))
        if slot is None:
            return
        tool_changed = slot != self.active_slot
        self.active_slot = slot
        self._follow(tool_changed)

    def on_loaded(self, loaded: Mapping[int, Mapping[str, Any]]) -> None:
        """What 3D Print Log has in the printer's slots changed (`Spools.watch_loaded`)."""
        numbers = {slot: spool_number(filament) for slot, filament in loaded.items()}
        before, self._numbers = self._numbers, numbers
        slot = self.active_slot
        self._follow(
            before is not None and slot is not None and before.get(slot) != numbers.get(slot)
        )

    async def on_history(self, action: str, job: Mapping[str, Any]) -> None:
        pass

    async def on_gcode_response(self, line: str) -> None:
        pass

    async def on_active_spool(self, spool_id: int | None) -> None:
        self.moonraker_spool = spool_id
        posted, self._posted = self._posted, None
        if posted is not None and spool_id == posted:
            # Moonraker telling every client what the bridge set it to: not a pick. Moonraker
            # announces only a change, so whatever it announces next is someone's pick.
            log.debug("[printer %s] Moonraker's active spool is now %s", self.printer, spool_id)
            return
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

    async def _active_spool(self, call: Call) -> tuple[int | None, bool]:
        """Moonraker's active spool, and whether its spoolman component is loaded at all."""
        try:
            spool_id = (await call("server.spoolman.get_spool_id", None)).get("spool_id")
        except MoonrakerRpcError as e:
            if e.code == METHOD_NOT_FOUND:
                log.debug(
                    "[printer %s] Moonraker has no spoolman component loaded (%s), so its "
                    "active spool isn't synced",
                    self.printer, e,
                )  # fmt: skip
                return None, False
            log.debug("[printer %s] couldn't read Moonraker's active spool: %s", self.printer, e)
            return None, True
        except Exception as e:
            log.debug("[printer %s] couldn't read Moonraker's active spool: %s", self.printer, e)
            return None, True
        if isinstance(spool_id, int) and not isinstance(spool_id, bool):
            return spool_id, True
        return None, True

    def _follow(self, changed: bool) -> None:
        """Sets Moonraker's active spool to the active tool's when `changed` (the tool, or its
        spool in 3D Print Log), or for the connection's first sync, once all it needs is known.
        """
        slot = self.active_slot
        if not self.enabled or self._call is None or slot is None or self._numbers is None:
            return
        if not self._settled:
            if self._busy is None:
                return
            self._settled = True
            if self._busy:
                log.info(
                    "[printer %s] a print is under way, so Moonraker's active spool is left as "
                    "it is (spool number %s) until the tool or T%d's spool changes",
                    self.printer, self.moonraker_spool, slot,
                )  # fmt: skip
                return
        elif not changed:
            return
        number = self._numbers.get(slot)
        if number is None:
            what = "a spool with no number" if slot in self._numbers else "no spool"
            log.debug(
                "[printer %s] T%d has %s in 3D Print Log; Moonraker's active spool is left as "
                "it is",
                self.printer, slot, what,
            )  # fmt: skip
            return
        if number == self.moonraker_spool:
            return  # Moonraker has it already, as after a pick on the printer
        previous, self.moonraker_spool, self._posted = self.moonraker_spool, number, number
        self._spawn(self._post(number, slot, previous, self._call))

    async def _post(self, number: int, slot: int, previous: int | None, call: Call) -> None:
        if self._posts is None:
            self._posts = asyncio.Lock()  # made here, in the running loop (Python 3.9)
        async with self._posts:  # set in the order the tools and spools changed
            try:
                await call("server.spoolman.post_spool_id", {"spool_id": number})
            except Exception as e:
                if self._posted == number:
                    self._posted = None
                if self.moonraker_spool == number:
                    self.moonraker_spool = previous
                if isinstance(e, MoonrakerRpcError) and e.code == METHOD_NOT_FOUND:
                    self.enabled = False  # the next connect looks again
                    log.debug(
                        "[printer %s] Moonraker has no spoolman component loaded (%s)",
                        self.printer, e,
                    )  # fmt: skip
                    return
                log.warning(
                    "[printer %s] couldn't set Moonraker's active spool to spool number %d, "
                    "T%d's: %s",
                    self.printer, number, slot, e,
                )  # fmt: skip
                return
        log.info(
            "[printer %s] set Moonraker's active spool to spool number %d, T%d's in 3D Print Log",
            self.printer, number, slot,
        )  # fmt: skip

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
            log.error(
                "[printer %s] syncing the active spool failed",
                self.printer,
                exc_info=task.exception(),
            )
