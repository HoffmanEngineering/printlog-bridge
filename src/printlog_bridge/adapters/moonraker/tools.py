"""Which tool fed the filament: Klipper's per-print total, split by the active extruder.

Klipper keeps one figure for a print, `print_stats.filament_used`: the millimetres fed since
it started, across every extruder (each tool change re-reads the new extruder's position, so
nothing is counted twice). Neither Klipper's extruder objects nor Moonraker's history entry say
how much each tool fed. So the bridge watches the total grow and gives each increase to the
extruder named in `toolhead.extruder` at that update: `extruder` is slot 0, `extruder1` slot 1,
and so on. That covers toolchangers (klipper-toolchanger), IDEX in its normal mode, and the
Snapmaker U1's four heads (T0-T3).

The Creality K3 on its stock firmware is different: it prints T1-T3 without switching Klipper's
active extruder (`toolhead.extruder` stays `extruder`), and keeps the nozzle that prints, 0-3,
in its own `nozzle_manager.selected` (-1 when none is). Each nozzle is fed by one slot of the
CFS (nozzle 1 from slot A, and so on, in Creality's own websocket), and the slicer's colors are
mapped to slots when the print is sent, so the nozzle, not the G-code's tool, is the slot used.
So a nozzle named there is the active slot; elsewhere it's the active extruder (`active_slot`).

What the bridge saw is then scaled to Moonraker's total for the job, which stays the source of
truth. When it saw nothing (the job ran while the bridge was away), the slicer's estimate per
tool decides the split: Moonraker's `filament_weights`, or the file's own `; filament used [g]`
line when Moonraker doesn't read the slicer's files (Creality Print). That estimate is by the
slicer's tool order, which a K3's mapping at print time can change. With no estimate per tool
either, it all goes to slot 0, as on a single-tool printer.

Not covered: an MMU on one extruder (ERCF, Happy Hare, AFC), where every gate feeds `extruder`,
and IDEX copy or mirror mode, where two heads print what Klipper counts once.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

EXTRUDER = re.compile(r"extruder(\d*)")
TOOL_MACRO = re.compile(r"gcode_macro t(\d+)", re.IGNORECASE)
"""A toolchanger's tool macro (`gcode_macro T1`). Fluidd and Mainsail keep each tool's spool
in the user's own Spoolman as its `spool_id` variable."""
PRINTING = ("printing", "paused")
FILE_GRAMS = re.compile(r"^;\s*filament used \[g\]\s*=\s*(.+?)\s*$", re.MULTILINE)
"""The slicer's grams per filament, in tool order (PrusaSlicer, OrcaSlicer, Creality Print)."""


def macro_slot(name: str) -> int | None:
    """The slot of a tool macro (`gcode_macro T1` is slot 1), or None for any other object."""
    match = TOOL_MACRO.fullmatch(name)
    return None if match is None else int(match.group(1))


def slot_of(extruder: str | None) -> int | None:
    """The slot for one of Klipper's extruder names, or None for anything else."""
    match = EXTRUDER.fullmatch(extruder or "")
    if match is None:
        return None
    return int(match.group(1) or 0)


def active_slot(status: Mapping[str, Any]) -> int | None:
    """The slot of the tool printing: on a Creality K3, its selected nozzle, else the active
    extruder; None when that doesn't say. A K3 with no nozzle selected (-1, idle or between
    nozzles) gives None, so the last slot is kept: its `toolhead.extruder` is always slot 0."""
    nozzles = status.get("nozzle_manager") or {}
    if "selected" in nozzles:
        selected = nozzles["selected"]
        if isinstance(selected, int) and not isinstance(selected, bool) and selected >= 0:
            return selected
        return None
    return slot_of((status.get("toolhead") or {}).get("extruder"))


class ToolUsage:
    """Millimetres fed per slot for the print Klipper is running, or ran last."""

    def __init__(self) -> None:
        self.filename: str | None = None
        self.slots: dict[int, float] = {}
        self._slot = 0
        self._state: str | None = None
        self._used: float | None = None
        self._taken = False

    def update(self, status: Mapping[str, Any]) -> None:
        """Takes the subscribed objects' latest fields, after each status update."""
        slot = active_slot(status)
        if slot is not None:
            self._slot = slot

        stats = status.get("print_stats") or {}
        state = stats.get("state")
        used = stats.get("filament_used")
        used = float(used) if isinstance(used, (int, float)) else None

        if (
            used is not None
            and self._used is not None
            and used < self._used
            and (state not in PRINTING or (stats.get("filename") or None) != self.filename)
        ):
            # Klipper loaded a new file: the total starts again from zero. While a print runs
            # the total also drops on every retraction (it follows the E position), so a drop
            # is only a new file when no print is running, or the file changed.
            self._restart(stats)
            if used > 0:
                self.slots[self._slot] = used
        elif state == "printing" and self._state not in PRINTING:
            # A print started, or was already running when the bridge first looked. What it
            # fed before then is unknown, and left for `per_slot` to make up.
            self._restart(stats)
        elif used is not None and self._used is not None and used != self._used:
            self.slots[self._slot] = self.slots.get(self._slot, 0.0) + used - self._used

        if used is not None:
            self._used = used
        if state is not None:
            self._state = state

    def take(self, filename: str) -> dict[int, float]:
        """What was fed per slot for the job printing `filename`, once; empty if not seen."""
        if self._taken or filename != self.filename:
            return {}
        self._taken = True
        return dict(self.slots)

    def _restart(self, stats: Mapping[str, Any]) -> None:
        self.filename = stats.get("filename") or None
        self.slots = {}
        self._taken = False


def per_slot(
    total: float, tracked: Mapping[int, float], estimated: Mapping[int, float]
) -> dict[int, float]:
    """`total` millimetres split in the proportions of `tracked`, else `estimated`, else slot 0."""
    total = float(total or 0)
    if total <= 0:
        return {0: 0.0}
    for weights in (tracked, estimated):
        positive = {slot: value for slot, value in weights.items() if value > 0}
        if positive:
            scale = total / sum(positive.values())
            return {slot: value * scale for slot, value in sorted(positive.items())}
    return {0: total}


def estimated_grams_by_slot(metadata: Mapping[str, Any]) -> dict[int, float]:
    """The slicer's grams per tool, from Moonraker's `filament_weights`, when it has them.

    PrusaSlicer and OrcaSlicer (and Snapmaker Orca) write `; filament used [g] = a, b, c` with
    one value per filament, in tool order; Moonraker lists them as `filament_weights`, and the
    Snapmaker U1's Moonraker (2.0.0) as `filament_weight` (seen on a real U1). A file
    with one filament gives nothing here, so its estimate stays one row as before. So does a
    single-extruder MMU print (`mmu_print`), whose filaments are gates, not tools.
    """
    weights = metadata.get("filament_weights")
    if not isinstance(weights, list):
        weights = metadata.get("filament_weight")
    if metadata.get("mmu_print") or not isinstance(weights, list) or len(weights) < 2:
        return {}
    by_slot = {}
    for slot, grams in enumerate(weights):
        if isinstance(grams, (int, float)) and grams > 0:
            by_slot[slot] = float(grams)
    return by_slot


def grams_by_slot_from_file(text: str) -> dict[int, float]:
    """The slicer's grams per tool from the end of a G-code file, for when Moonraker's metadata
    has none (it reads no weights from Creality Print's files). The last `; filament used [g]`
    line wins; one with a single filament, or a value that isn't a number, gives nothing."""
    lines = FILE_GRAMS.findall(text)
    if not lines:
        return {}
    try:
        weights = [float(value) for value in lines[-1].split(",")]
    except ValueError:
        return {}
    if len(weights) < 2:
        return {}
    return {slot: grams for slot, grams in enumerate(weights) if grams > 0}
