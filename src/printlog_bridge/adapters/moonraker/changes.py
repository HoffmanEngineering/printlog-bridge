"""Pauses for a filament change, from Klipper's runout sensors and the extruder (#44).

A print paused for a filament change may go on with another spool. When the user assigns it
with `PRINTLOG_SET_SPOOL` the bridge splits the tool's usage there (`core/swaps.py`). When they
don't, the print keeps one spool, and gets a note saying how to split it next time. So the
bridge watches for a filament change in two ways:

- a runout sensor, `[filament_switch_sensor <name>]` or `[filament_motion_sensor <name>]`,
  whose `filament_detected` goes from true to false while a job runs, and the job pausing
  before it has fed another `RUNOUT_WINDOW_MM` with that sensor still empty. A sensor that
  empties with no pause after it (an unload in the end G-code, an MMU's gate sensor) is no
  runout. The bridge asks on the console for the new spool as soon as the job pauses.
- filament unloaded while the job is paused: the extruder's position (`toolhead.position`,
  whose fourth value is the extruder's) going back by `UNLOAD_MM` or more, as an `M600` macro's
  unload does. A pause's own retract is a few millimetres at most. A tool change while paused
  (`ACTIVATE_EXTRUDER`) makes the position another extruder's, so that starts the count over.
  Klipper's filament count
  (`print_stats.filament_used`) stands still while paused, so what's unloaded and purged is
  never counted as printed.

Either way the change is reported (`FilamentChange`) once the job resumes, with the point it
was paused at; a job cancelled while paused reports nothing. Klipper doesn't say which tool a
sensor watches, so a sensor named for one (`e1_filament`, `extruder1`, `t1`; the Snapmaker U1
has `filament_motion_sensor e0_filament` to `e3_filament`) is that tool's, and any other the
active tool's.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from printlog_bridge.core.events import FilamentChange, JobPoint

SENSORS = ("filament_switch_sensor ", "filament_motion_sensor ")
"""The Klipper objects, by prefix, that are runout sensors."""
SENSOR_FIELDS = ["filament_detected", "enabled"]
UNLOAD_MM = 25.0
"""How far the extruder has to go back while paused to count as unloading the filament."""
RUNOUT_WINDOW_MM = 200.0
"""How much the job may feed after a sensor empties and still pause for it: Klipper pauses at
once by default, or after the sensor's `runout_distance`, often 0-100 mm."""
RUNOUT, UNLOAD = "runout", "unload"
TOOL_NAME = re.compile(r"(?:extruder|tool|e|t)(\d+)(?![0-9a-z])", re.IGNORECASE)


def is_sensor(name: str) -> bool:
    return name.startswith(SENSORS)


def sensor_slot(name: str) -> int | None:
    """The tool a sensor is named for (`filament_motion_sensor e1_filament` is T1), or None."""
    match = TOOL_NAME.match(name.split(" ", 1)[-1])
    return int(match.group(1)) if match else None


class FilamentChanges:
    """Turns status updates into the job's filament changes, and the prompts to show for them."""

    def __init__(self, printer: str) -> None:
        self.printer = printer
        self._state: str | None = None
        self._detected: dict[str, bool] = {}
        self._e: float | None = None
        self._extruder: str | None = None
        self._paused_at: JobPoint | None = None
        self._slot = 0
        self._reason: str | None = None
        self._unloaded = 0.0
        self._emptied: dict[str, tuple[int, float]] = {}
        """Each sensor that emptied while the job ran and not since refilled: its tool, and
        Klipper's filament count when it emptied."""

    def update(
        self,
        status: Mapping[str, Any],
        point: Callable[[], JobPoint | None],
        active: int,
    ) -> tuple[list[str], FilamentChange | None]:
        """What to tell the user on the console, and the filament change the job just resumed
        from, if it did. `point` is where the job is now; `active` the active tool."""
        prompts: list[str] = []
        change = None
        stats = status.get("print_stats") or {}
        state = stats.get("state") or self._state
        used = stats.get("filament_used")
        used = float(used) if isinstance(used, (int, float)) else 0.0
        running = state in ("printing", "paused")

        if running and self._state not in ("printing", "paused"):
            self._reset()  # a job started, or was already running when the bridge first looked
        if state == "paused" and self._state != "paused":
            self._paused_at = point()
            self._unloaded = 0.0
            self._slot = active

        for name, fields in status.items():
            if not is_sensor(name) or not isinstance(fields, Mapping):
                continue
            detected = fields.get("filament_detected")
            if not isinstance(detected, bool):
                continue
            was = self._detected.get(name)
            self._detected[name] = detected
            if detected or fields.get("enabled", True) is False:
                self._emptied.pop(name, None)
            elif running and was is True:
                slot = sensor_slot(name)
                self._emptied[name] = (active if slot is None else slot, used)
        for name, (_, at) in list(self._emptied.items()):
            if used - at > RUNOUT_WINDOW_MM:
                del self._emptied[name]  # the job printed on: not what it paused for

        if state == "paused" and self._emptied and self._reason is None:
            self._reason, self._slot = RUNOUT, next(iter(self._emptied.values()))[0]
            prompts.append(
                f"Spool ran out on T{self._slot}. Run PRINTLOG_SET_SPOOL to log the new one."
            )

        extruder = (status.get("toolhead") or {}).get("extruder")
        if extruder != self._extruder:
            self._e = None  # the position is now another extruder's
            self._extruder = extruder
        e = _extruder_position(status)
        if e is not None:
            if state == "paused" and self._state == "paused" and self._e is not None:
                self._unloaded += max(0.0, self._e - e)
                if self._unloaded >= UNLOAD_MM and self._reason is None:
                    self._reason = UNLOAD
                    prompts.append(
                        f"Filament unloaded from T{self._slot}. Run PRINTLOG_SET_SPOOL to log "
                        "the new spool."
                    )
            self._e = e

        if state == "printing" and self._state == "paused":
            if self._reason is not None and self._paused_at is not None:
                change = FilamentChange(self.printer, self._slot, self._paused_at, self._reason)
            self._reset()
        elif not running:
            self._reset()
        self._state = state
        return prompts, change

    def _reset(self) -> None:
        self._paused_at = None
        self._reason = None
        self._unloaded = 0.0
        self._emptied.clear()


def _extruder_position(status: Mapping[str, Any]) -> float | None:
    position = (status.get("toolhead") or {}).get("position")
    if isinstance(position, (list, tuple)) and len(position) >= 4:
        value = position[3]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None
