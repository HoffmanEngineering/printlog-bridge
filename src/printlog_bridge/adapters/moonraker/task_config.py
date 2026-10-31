"""What a multi-material printer says it has loaded in each slot, and where it sent each of the
slicer's filaments.

Printers with several filaments let you map the slicer's filaments to any slot when you send a
print (a Snapmaker U1's screen, Creality Print for a K3's CFS, Bambu Studio's AMS mapping), so
the slicer's filament order says nothing about which spool fed what. Two printers say so
through Klipper (both read from real printers, `tests/fixtures/moonraker/`):

- **Snapmaker U1**, `print_task_config`: `extruder_map_table` has 32 entries, one per G-code
  tool (T0-T31): the head (0-3) that prints it. Per head, `filament_color_rgba` (`E2DEDBFF`),
  `filament_type` (`PLA`), `filament_sub_type` (`SnapSpeed`) and `filament_exist`.
- **Creality K3**, `box.same_material`: one entry per material and color loaded in the CFS,
  `[material id, "0" + RRGGBB, ["T1A", ...], "PLA"]`. Slot `T1A` (box 1, slot A) feeds nozzle 1,
  the bridge's slot 0. Nothing there maps the G-code's tools, so on a K3 the nozzle printing
  (`tools.active_slot`) is what places the filament.

`tool_map` re-keys the slicer's per-filament estimates to the slots they printed from;
`slot_notes` describes what is loaded (`core.colors.slot_note`), for each slot's usage row.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from printlog_bridge.core.colors import normalized, slot_note

U1_FIELDS = [
    "extruder_map_table",
    "filament_color_rgba",
    "filament_type",
    "filament_sub_type",
    "filament_exist",
]
"""What the bridge subscribes to of `print_task_config` (its calibration and timelapse flags
are left out)."""
K3_FIELDS = ["same_material"]
"""What the bridge subscribes to of a K3's `box`: not its temperatures, humidity or serials."""
CFS_SLOT = re.compile(r"T(\d+)([A-D])")
SLOTS_PER_BOX = 4


def tool_map(status: Mapping[str, Any]) -> list[int] | None:
    """The slot each G-code tool printed from (index: the tool), or None when the printer
    doesn't say. A table with anything but whole numbers from 0 isn't used."""
    table = (status.get("print_task_config") or {}).get("extruder_map_table")
    if not isinstance(table, list) or not table:
        return None
    if not all(
        isinstance(slot, int) and not isinstance(slot, bool) and slot >= 0 for slot in table
    ):
        return None
    return list(table)


def remapped(by_tool: Mapping[int, float], table: Sequence[int] | None) -> dict[int, float]:
    """The slicer's figures per tool, moved to the slots `table` sent each tool to. Tools mapped
    to one slot add up; a tool the table doesn't cover keeps its number."""
    if not table:
        return dict(by_tool)
    by_slot: dict[int, float] = {}
    for tool, value in by_tool.items():
        slot = table[tool] if 0 <= tool < len(table) else tool
        by_slot[slot] = by_slot.get(slot, 0.0) + value
    return by_slot


def loaded(status: Mapping[str, Any]) -> dict[int, tuple[str | None, str | None]]:
    """(color `#RRGGBB`, material) per slot, for the slots the printer says have filament."""
    found = _u1_loaded(status.get("print_task_config") or {})
    if found:
        return found
    return _k3_loaded(status.get("box") or {})


def slot_notes(status: Mapping[str, Any]) -> dict[int, str]:
    """The note on each loaded slot's usage row: "Slot 1 · White (#E2DEDB) · PLA SnapSpeed"."""
    return {
        slot: slot_note(slot, color, material) for slot, (color, material) in loaded(status).items()
    }


def _u1_loaded(config: Mapping[str, Any]) -> dict[int, tuple[str | None, str | None]]:
    colors = _list(config.get("filament_color_rgba"))
    types = _list(config.get("filament_type"))
    sub_types = _list(config.get("filament_sub_type"))
    exists = _list(config.get("filament_exist"))
    found: dict[int, tuple[str | None, str | None]] = {}
    for slot in range(max(len(colors), len(types))):
        if slot < len(exists) and exists[slot] is False:
            continue
        rgba = colors[slot] if slot < len(colors) else None
        color = normalized(rgba[:6]) if isinstance(rgba, str) else None
        material = _material(
            types[slot] if slot < len(types) else None,
            sub_types[slot] if slot < len(sub_types) else None,
        )
        if color is not None or material is not None:
            found[slot] = (color, material)
    return found


def _k3_loaded(box: Mapping[str, Any]) -> dict[int, tuple[str | None, str | None]]:
    found: dict[int, tuple[str | None, str | None]] = {}
    for entry in _list(box.get("same_material")):
        if not isinstance(entry, list) or len(entry) < 4:
            continue
        _, value, slots, material = entry[:4]
        color = normalized(value[-6:]) if isinstance(value, str) else None
        material = material.strip() if isinstance(material, str) and material.strip() else None
        for name in _list(slots):
            match = CFS_SLOT.fullmatch(name) if isinstance(name, str) else None
            if match is None or int(match.group(1)) < 1:
                continue
            slot = (int(match.group(1)) - 1) * SLOTS_PER_BOX + "ABCD".index(match.group(2))
            found[slot] = (color, material)
    return found


def _material(kind: Any, sub_type: Any) -> str | None:
    """The material from its type and sub-type ("PLA SnapSpeed"), or what of them there is."""
    parts = [p.strip() for p in (kind, sub_type) if isinstance(p, str) and p.strip()]
    if len(parts) == 2 and parts[1].lower() in parts[0].lower():
        parts = parts[:1]
    return " ".join(parts) or None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def placed(
    by_tool: Mapping[int, float], table: Sequence[int] | None, single: float | None = None
) -> dict[int, float]:
    """The slicer's grams per tool, in the slots they printed from. A file with one filament
    has no figures per tool, but its T0 can still go to another slot: then `single` (its total,
    or any positive weight when only the split matters) is put on that slot."""
    if by_tool:
        return remapped(by_tool, table)
    if table and table[0] != 0 and single:
        return {table[0]: single}
    return {}
