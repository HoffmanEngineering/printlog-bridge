"""Moonraker's Spoolman integration, when it points at 3D Print Log or at the user's own Spoolman.

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

With a Spoolman server of the user's own (#38), the spool active there is assigned to its
material in 3D Print Log, one way only: nothing is ever written to Spoolman (Moonraker reports
usage to it already) or to Moonraker's active spool. The spool is read through Moonraker's
Spoolman proxy (`GET /v1/spool/{id}`), and 3D Print Log finds its material by the Spoolman id
stored on it, else by the tag UID in the spool's `nfc_id` extra field (where nfc2klipper and
FilaMan keep it); with neither, the bridge logs how to link it, and nothing changes.

Which slot: a toolchanger whose tool macros keep each tool's spool (Fluidd's and Mainsail's
`spool_id` variable on `gcode_macro T<n>`, or `t<n>__spool_id` in `[save_variables]`) has each
tool's spool assigned to that tool's slot, and Moonraker's active spool, which its tool change
macros set from those, is left to them. Elsewhere the active spool is the active tool's: slot 0
on a single-tool printer. On connect the spools are assigned when no print is under way, and
then only those that changed since the bridge last saw them; from then on, each change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from printlog_bridge.core.events import JobPoint, SpoolCommand, SpoolmanSpool, SpoolSink

from .session import Call, ConnectionLost, MoonrakerListener, MoonrakerRpcError
from .spools import tool_count
from .tools import macro_slot, slot_of

log = logging.getLogger(__name__)

PRINTLOG = "printlog"
"""Moonraker's Spoolman server is 3D Print Log."""
OWN = "own"
"""Moonraker's Spoolman server is the user's own."""
TAG_FIELD = "nfc_id"
"""The Spoolman extra field holding a spool's tag UID (nfc2klipper, FilaMan)."""
SAVED_TOOL_SPOOL = re.compile(r"t(\d+)__spool_id", re.IGNORECASE)
"""A `[save_variables]` variable holding a tool's spool in the user's own Spoolman."""

METHOD_NOT_FOUND = -32601
"""What Moonraker answers a `server.spoolman.*` call with when its spoolman component isn't
loaded."""
ANSWER_GRACE_S = 1.0
"""How long after Moonraker answers a post the bridge still takes its announcement for the
post's own (see `_echo`)."""
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


def spool_id_of(value: object) -> int | None:
    """A Spoolman spool id from a macro or saved variable, or None for none (or not one)."""
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def tool_spools(status: Mapping[str, Any]) -> dict[int, int | None]:
    """Each tool's spool in the user's own Spoolman, by slot, when the printer's macros keep
    one per tool; empty when they don't. A tool macro's `spool_id` is the live one, and wins
    over a saved variable."""
    spools: dict[int, int | None] = {}
    variables = (status.get("save_variables") or {}).get("variables")
    if isinstance(variables, Mapping):
        for name, value in variables.items():
            match = SAVED_TOOL_SPOOL.fullmatch(str(name))
            if match is not None:
                spools[int(match.group(1))] = spool_id_of(value)
    for name, fields in status.items():
        slot = macro_slot(str(name))
        if slot is not None and isinstance(fields, Mapping) and "spool_id" in fields:
            spools[slot] = spool_id_of(fields["spool_id"])
    return spools


def tag_values(raw: object) -> tuple[str, ...]:
    """The tag UIDs in a Spoolman extra field. Spoolman keeps extra values as JSON text
    (`"\\"04A23F11\\""`); a value that isn't is taken as written."""
    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
    values = value if isinstance(value, list) else [value]
    return tuple(str(v).strip() for v in values if isinstance(v, (str, int)) and str(v).strip())


def describe_spool(spool_id: int, body: Mapping[str, Any]) -> SpoolmanSpool:
    """What 3D Print Log needs of a Spoolman spool (`GET /v1/spool/{id}`): its id and tags, and
    its name for the log."""
    filament = body.get("filament")
    filament = filament if isinstance(filament, Mapping) else {}
    vendor = filament.get("vendor")
    parts = ((vendor if isinstance(vendor, Mapping) else {}).get("name"), filament.get("name"))
    name = " ".join(p.strip() for p in parts if isinstance(p, str) and p.strip()) or None
    extra = body.get("extra")
    tags = tag_values((extra if isinstance(extra, Mapping) else {}).get(TAG_FIELD))
    return SpoolmanSpool(spool_id, name, tags)


@dataclass(eq=False)
class _Post:
    """A spool number the bridge set Moonraker's active spool to, until Moonraker announces it."""

    number: int
    answered_at: float | None = None
    """When Moonraker answered the post (the bridge's clock), or None until it has. An answer
    says nothing of its announcement, which Moonraker sends first."""


class SpoolmanSync(MoonrakerListener):
    """A MoonrakerListener that loads the spool picked on the printer into the active tool's
    slot, and keeps Moonraker's active spool on the active tool's spool, when Moonraker's
    Spoolman server is 3D Print Log; and assigns the spools active in the user's own Spoolman
    to their materials, when it's theirs."""

    def __init__(
        self,
        printer: str,
        api_url: str,
        spools: SpoolSink,
        point: Callable[[], JobPoint | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.printer = printer
        self.api_host = host_of(api_url)
        self.spools = spools
        self.clock = clock
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
        self._posted: deque[_Post] = deque()
        """The bridge's posts Moonraker hasn't announced yet, oldest first (see `_echo`)."""
        self._posts: asyncio.Lock | None = None
        self.external = False
        """Whether Moonraker's `[spoolman]` server is the user's own, as read on connect."""
        self.per_tool = False
        """Whether the printer's macros keep each tool's spool in the user's own Spoolman."""
        self._seen_active: int | None = None
        """The active spool in the user's own Spoolman last assigned, or left as it was for a
        print under way. Kept across connections, so a reconnect assigns only a change."""
        self._seen_tools: dict[int, int | None] = {}
        """The same for each tool's spool, by slot."""
        self._spoolman_host: str | None = None

    async def on_connected(self, call: Call) -> None:
        self._call = call
        self._tools = None
        self.active_slot = None
        self._busy = None
        self._settled = False
        self._posted.clear()
        # Set once known, not reset first: what the last connection read stands until then.
        kind = await self._server_kind(call)
        spool, loaded = await self._active_spool(call) if kind else (None, False)
        self.moonraker_spool = spool
        self.enabled = kind == PRINTLOG and loaded
        self.external = kind == OWN and loaded
        if self.enabled:
            log.info(
                "[printer %s] Moonraker's Spoolman server is 3D Print Log: a spool picked on the "
                "printer is loaded into the active tool's slot",
                self.printer,
            )
        elif self.external:
            log.info(
                "[printer %s] Moonraker's Spoolman server is your own (%s): the spool active "
                "there is assigned to its material in 3D Print Log, and nothing is written to "
                "Spoolman",
                self.printer, self._spoolman_host,
            )  # fmt: skip

    async def on_klippy_state(self, state: str) -> None:
        if state == "ready":
            self._tools = None  # a config change may have added a tool

    async def on_status(self, status: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        state = (status.get("print_stats") or {}).get("state")
        if isinstance(state, str):
            self._busy = state in BUSY
        slot = slot_of((status.get("toolhead") or {}).get("extruder"))
        tool_changed = slot is not None and slot != self.active_slot
        if slot is not None:
            self.active_slot = slot
        if self.external:
            self._follow_own(status)
        elif slot is not None:
            self._follow(tool_changed, by_tool=True)

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
        if self._echo(spool_id):
            log.debug("[printer %s] Moonraker's active spool is now %s", self.printer, spool_id)
            if not self._posted:
                # A pick may have crossed the post (Moonraker set it, then the post): if the
                # slot has the pick by now, Moonraker is set to it again. Else it's the post's
                # number, and nothing is posted.
                self._follow(True)
            return
        self.moonraker_spool = spool_id
        if self.external:
            self._own_pick(spool_id)
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

    async def _server_kind(self, call: Call) -> str | None:
        """Whose moonraker.conf's `[spoolman]` server is: 3D Print Log's (`PRINTLOG`), the
        user's own (`OWN`), or None without one. Says so in the log when there's none;
        `on_connected` says which it is, once the component is found."""
        self._spoolman_host = None
        try:
            config = (await call("server.config", None)).get("config") or {}
        except Exception as e:
            log.info(
                "[printer %s] couldn't read Moonraker's config (%s), so spools picked on the "
                "printer aren't synced with 3D Print Log",
                self.printer, e,
            )  # fmt: skip
            return None
        section = config.get("spoolman")
        if not isinstance(section, Mapping):
            log.info(
                "[printer %s] Moonraker has no [spoolman] section, so spools picked on the "
                "printer aren't synced with 3D Print Log",
                self.printer,
            )
            return None
        # Only the host is ever logged: the server URL holds the printer's Spoolman secret.
        host = host_of(section.get("server"))
        if host is None:
            log.info(
                "[printer %s] Moonraker's [spoolman] section has no server, so spools picked "
                "on the printer aren't synced with 3D Print Log",
                self.printer,
            )
            return None
        self._spoolman_host = host
        return PRINTLOG if host == self.api_host else OWN

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

    def _echo(self, spool_id: int | None) -> bool:
        """Whether Moonraker's announcement answers one of the bridge's own posts, not a pick.

        Moonraker sets and announces posts in the order they were made, and announces only a
        change. So an announcement of a posted number answers the oldest such post, and the
        posts before it changed nothing and will never be announced: they're dropped too. An
        announcement of any other number is someone's pick, and leaves the posts pending.

        Moonraker announces a post before answering it, so a post answered and not announced
        had Moonraker's number already, and never will be. But the serve loop may hear the
        answer first, while the announcement waits behind a slow listener: so a post is dropped
        as unannounced only `ANSWER_GRACE_S` after its answer.
        """
        now = self.clock()
        self._posted = deque(post for post in self._posted if not self._lapsed(post, now))
        for i, post in enumerate(self._posted):
            if post.number == spool_id:
                for _ in range(i + 1):
                    self._posted.popleft()
                if not self._posted:
                    self.moonraker_spool = spool_id
                # Else it's the number of a later post already, which Moonraker will set.
                return True
        return False

    @staticmethod
    def _lapsed(post: _Post, now: float) -> bool:
        """Whether Moonraker answered the post long enough ago that it was never announced."""
        return post.answered_at is not None and now - post.answered_at >= ANSWER_GRACE_S

    def _follow(self, changed: bool, by_tool: bool = False) -> None:
        """Sets Moonraker's active spool to the active tool's when `changed` (the tool, or its
        spool in 3D Print Log), or for the connection's first sync, once all it needs is known.
        `by_tool`: the change is a tool change, logged at debug (a multi-colour print makes
        hundreds).
        """
        slot = self.active_slot
        if not self.enabled or self._call is None or slot is None or self._numbers is None:
            return
        first = not self._settled
        if first:
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
        post, previous = _Post(number), self.moonraker_spool
        self._posted.append(post)
        self.moonraker_spool = number
        level = logging.DEBUG if by_tool and not first else logging.INFO
        self._spawn(self._post(post, slot, previous, self._call, level))

    async def _post(
        self, post: _Post, slot: int, previous: int | None, call: Call, level: int
    ) -> None:
        number = post.number
        if self._posts is None:
            self._posts = asyncio.Lock()  # made here, in the running loop (Python 3.9)
        async with self._posts:  # set in the order the tools and spools changed
            try:
                await call("server.spoolman.post_spool_id", {"spool_id": number})
            except Exception as e:
                if call is not self._call:
                    return  # a connection since gone; the one since read Moonraker's spool again
                if post in self._posted:
                    self._posted.remove(post)
                if self.moonraker_spool == number:
                    self.moonraker_spool = previous
                if isinstance(e, ConnectionLost):
                    # Moonraker went away; on reconnect its active spool is read again, and set
                    # to the active tool's when no print is under way.
                    log.debug(
                        "[printer %s] Moonraker is out of reach, so its active spool wasn't set "
                        "to spool number %d: %s",
                        self.printer, number, e,
                    )  # fmt: skip
                    return
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
            post.answered_at = self.clock()
        log.log(
            level,
            "[printer %s] set Moonraker's active spool to spool number %d, T%d's in 3D Print Log",
            self.printer, number, slot,
        )  # fmt: skip

    # -- the user's own Spoolman (#38) ---------------------------------------------------------

    def _follow_own(self, status: Mapping[str, Any]) -> None:
        """Assigns the spools active in the user's own Spoolman that changed: each tool's, when
        the macros keep them, else the active spool, the first time once Klipper has said which
        tool is active and whether a print is under way."""
        tools = tool_spools(status)
        self.per_tool = bool(tools)
        if not self._settled:
            if self._busy is None or (not tools and self.active_slot is None):
                return
            self._settled = True
            if self._busy:
                self._seen_tools.update(tools)
                if not tools:
                    self._seen_active = self.moonraker_spool
                log.info(
                    "[printer %s] a print is under way, so the spools active in Spoolman are "
                    "assigned in 3D Print Log from their next change",
                    self.printer,
                )
                return
            if not tools:
                spool = self.moonraker_spool
                if spool is not None and spool != self._seen_active:
                    self._seen_active = spool
                    assert self.active_slot is not None  # settled needs it without tools
                    self._assign_later(self.active_slot, spool)
                return
        for slot, spool in sorted(tools.items()):
            if slot in self._seen_tools and self._seen_tools[slot] == spool:
                continue
            self._seen_tools[slot] = spool
            if spool is not None:
                self._assign_later(slot, spool)

    def _own_pick(self, spool_id: int | None) -> None:
        """Moonraker's active spool changed, in the user's own Spoolman."""
        if spool_id is None:
            log.info(
                "[printer %s] the active spool was cleared in Spoolman; 3D Print Log's slots "
                "are left as they are",
                self.printer,
            )
            return
        if self.per_tool:
            log.debug(
                "[printer %s] Moonraker's active spool is now %d; each tool's spool is assigned "
                "from its macro",
                self.printer, spool_id,
            )  # fmt: skip
            return
        if not self._settled:
            return  # assigned once Klipper says which tool is active, and whether it prints
        assert self.active_slot is not None  # settled needs it without tools
        self._seen_active = spool_id
        self._assign_later(self.active_slot, spool_id)

    def _assign_later(self, slot: int, spool_id: int) -> None:
        # Where the job is now, not once Spoolman and 3D Print Log answer: a split falls here.
        during = self.point()
        log.info(
            "[printer %s] Spoolman spool %d is active on the printer, for T%d",
            self.printer, spool_id, slot,
        )  # fmt: skip
        self._spawn(self._assign(slot, spool_id, during, self._call))

    async def _assign(
        self, slot: int, spool_id: int, during: JobPoint | None, call: Call | None
    ) -> None:
        async with self._ordered():  # assigned in the order they were made
            spool = await self._spoolman_spool(spool_id, call)
            command = SpoolCommand(self.printer, slot, spoolman=spool, during=during)
            await self._send(command, call)

    async def _spoolman_spool(self, spool_id: int, call: Call | None) -> SpoolmanSpool:
        """The spool as the user's Spoolman describes it, read through Moonraker's proxy. With
        no answer, just its id, which 3D Print Log can still find it by."""
        params = {"request_method": "GET", "path": f"/v1/spool/{spool_id}"}
        try:
            if call is None:
                raise ConnectionLost("not connected to Moonraker")
            answer = await call("server.spoolman.proxy", {**params, "use_v2_response": True})
            body = answer
            if isinstance(answer, Mapping) and "response" in answer and "error" in answer:
                if answer.get("error"):
                    raise MoonrakerRpcError(0, str(answer["error"]))
                body = answer.get("response")
            if not isinstance(body, Mapping):
                raise MoonrakerRpcError(0, "not a spool")
        except Exception as e:
            log.info(
                "[printer %s] couldn't read Spoolman spool %d through Moonraker (%s); looking "
                "it up in 3D Print Log by its id alone",
                self.printer, spool_id, e,
            )  # fmt: skip
            return SpoolmanSpool(spool_id)
        return describe_spool(spool_id, body)

    # -- carrying out --------------------------------------------------------------------------

    def _ordered(self) -> asyncio.Lock:
        if self._in_order is None:
            self._in_order = asyncio.Lock()  # made here, in the running loop (Python 3.9)
        return self._in_order

    async def _carry_out(self, command: SpoolCommand, call: Call | None) -> None:
        async with self._ordered():  # picks are carried out in the order they were made
            await self._send(command, call)

    async def _send(self, command: SpoolCommand, call: Call | None) -> None:
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
