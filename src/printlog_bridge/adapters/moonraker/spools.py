"""PRINTLOG_SET_SPOOL and PRINTLOG_CLEAR_SPOOL, and slot readers, through Moonraker (#6).

The macros (`klipper/printlog.cfg`) print a line to the console, such as

    // printlog: SET_SPOOL TOOL=2 TAG=04A23F11

and Moonraker sends every console line to every client connected to it, as
`notify_gcode_response`, so the bridge sees it in local and remote mode alike. It answers on the
console through `_PRINTLOG_RESPOND`, a macro from the same file.

Why a console line and not `action_call_remote_method`: Klipper raises an error when the remote
method isn't registered, which is the case whenever the bridge isn't connected (restarting,
updating, offline, or a remote bridge on a computer that's asleep). In a print's G-code that
error ends the print. Printing a line can't fail, so a macro can never stop a print; the cost is
that a line printed while the bridge is away is missed, and the macro's own message says to
check for the bridge's answer.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from printlog_bridge.core.events import JobPoint, SpoolCommand, SpoolSink

from .session import Call
from .tools import EXTRUDER, slot_of
from .u1_rfid import U1RfidReader

log = logging.getLogger(__name__)

COMMAND = re.compile(r"// printlog: (SET_SPOOL|CLEAR_SPOOL)((?: +[A-Z]+=\S+)*) *")
UNSAFE = re.compile(r"[#;*]")
"""What Klipper's releases take as the start of a comment (`;` `#` `*`), even in quotes, so it
can't be in an answer. `\\` becomes `/`: Klipper's shlex parse of `MSG="..."` reads it as an
escape (one before the closing quote leaves the quote open, and the answer is a malformed command
that never shows), and a Windows path stays readable. `"` becomes `'`, and a line break, which
would end the command, a space."""
ANSWER_PREFIX = "printlog-bridge: "
RESPOND_MACRO = "gcode_macro _PRINTLOG_RESPOND"
"""The Klipper object `printlog.cfg`'s answering macro is, when it's included."""
MAX_ANSWER = 300


def parse_command(printer: str, line: str, active: int = 0) -> SpoolCommand | None:
    """The command a console line carries, or None. Without TOOL, the slot is `active`."""
    match = COMMAND.fullmatch(line.strip())
    if match is None:
        return None
    args = dict(arg.split("=", 1) for arg in match.group(2).split())
    tool = args.get("TOOL")
    if tool is not None and not tool.isdigit():
        return None
    slot = int(tool) if tool is not None else active
    if match.group(1) == "CLEAR_SPOOL":
        return SpoolCommand(printer, slot)
    filament_id, tag = args.get("ID"), args.get("TAG")
    if (filament_id is None) == (tag is None):
        return None
    return SpoolCommand(printer, slot, filament_id=filament_id, tag=tag)


def answer_script(answer: str) -> str:
    """The G-code that shows the bridge's answer on the console."""
    safe = answer.replace('"', "'").replace("\\", "/")
    text = " ".join(UNSAFE.sub("", safe).split())[:MAX_ANSWER]
    return f'_PRINTLOG_RESPOND MSG="{ANSWER_PREFIX}{text}"'


class SpoolWatcher:
    """A MoonrakerListener that passes spool commands and slot readings to a SpoolSink.

    Each is handled in a task of its own, so the API's answer never holds up the connection.
    Each carries where the running job was when it came (`point`, the JobTracker's), so a spool
    changed mid-print splits the job's usage there.
    """

    def __init__(
        self,
        printer: str,
        spools: SpoolSink,
        reader: U1RfidReader | None = None,
        point: Callable[[], JobPoint | None] | None = None,
    ):
        self.printer = printer
        self.spools = spools
        self.reader = reader
        self.point = point or (lambda: None)
        self._call: Call | None = None
        self._active = 0
        self._tools: int | None = None
        self._tasks: set[asyncio.Future] = set()
        self._in_order: asyncio.Lock | None = None

    async def on_connected(self, call: Call) -> None:
        self._call = call
        self._tools = None

    async def on_klippy_state(self, state: str) -> None:
        if state == "ready":
            self._tools = None  # a config change may have added a tool

    async def on_status(self, status: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        slot = slot_of((status.get("toolhead") or {}).get("extruder"))
        if slot is not None:
            self._active = slot
        if self.reader is not None and "filament_detect" in changed:
            for reading in self.reader.update(status.get("filament_detect") or {}):
                during = self.point()
                if during is not None:
                    reading = replace(reading, during=during)
                self._spawn(self.spools.spool_reading(reading))

    async def on_history(self, action: str, job: Mapping[str, Any]) -> None:
        pass

    async def on_gcode_response(self, line: str) -> None:
        for each in line.splitlines():  # one response can hold several lines
            command = parse_command(self.printer, each, self._active)
            if command is not None:
                command = replace(command, during=self.point())
                self._spawn(self._carry_out(command, self._call))

    async def _carry_out(self, command: SpoolCommand, call: Call | None) -> None:
        if self._in_order is None:
            self._in_order = asyncio.Lock()  # made here, in the running loop (Python 3.9)
        async with self._in_order:  # commands are carried out in the order they were given
            tools = await self._tool_count(call)
            answer = await self.spools.spool_command(replace(command, slot_count=tools))
        if call is None:
            return
        try:
            await call("printer.gcode.script", {"script": answer_script(answer)})
        except Exception as e:
            # Most likely printlog.cfg isn't included, or Klipper is busy or down. The answer is
            # in the bridge's log and `printlog-bridge status` either way.
            log.debug("[printer %s] couldn't show the answer on the console: %s", self.printer, e)

    async def _tool_count(self, call: Call | None) -> int | None:
        """How many extruders Klipper has, so 3D Print Log can be told how many slots."""
        if self._tools is None and call is not None:
            try:
                objects = (await call("printer.objects.list", None))["objects"]
            except Exception:
                return None
            count = sum(1 for name in objects if EXTRUDER.fullmatch(str(name)))
            self._tools = count or None
        return self._tools

    def _spawn(self, work) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Future) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("[printer %s] a spool update failed", self.printer, exc_info=task.exception())
