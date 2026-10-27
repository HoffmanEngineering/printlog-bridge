"""Slot numbers for a Bambu Lab printer's spools, and what its AMS says is in each.

3D Print Log numbers a printer's slots from 0. A Bambu printer feeds from an external spool
holder and from up to four AMS units (four trays each), and some from AMS HT units (one tray
each) and, on the H2D, a second external spool. The numbering:

| Slot | Spool | Report |
| --- | --- | --- |
| 0 | the external spool | `vt_tray`, id 254 |
| 1-16 | AMS unit 0 trays 1-4 (A1-A4), unit 1 (B1-B4), ... | `ams.ams[].tray[]`, `tray_now` 0-15 |
| 17-24 | AMS HT units 128-135 | `ams.ams[]` with id 128 and up |
| 25 | the H2D's second external spool | id 255 |

So a printer without an AMS has one slot, 0, and one with an AMS has five: the external spool
and A1-A4, numbered as Bambu Studio shows them.

Which filament of a job feeds from which slot: the report's `ams_mapping` (one entry per
filament in the project's list: `ams * 4 + tray`, or 254/255 for an external spool) or newer
firmware's `ams_mapping2` (`{"ams_id", "slot_id"}`). Without either, a one-filament job uses
the tray loaded when it started (`tray_now`), and filament N of a multi-filament job is AMS tray
N, Bambu Studio's default mapping.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

EXTERNAL = 0
SECOND_EXTERNAL = 25
AMS_UNITS = 4
TRAYS_PER_AMS = 4
HT_FIRST_ID = 128
HT_UNITS = 8
HT_FIRST_SLOT = 1 + AMS_UNITS * TRAYS_PER_AMS  # 17


def slot_of_tray(ams_id: int, tray_id: int) -> int | None:
    """The slot of tray `tray_id` of AMS unit `ams_id` (an AMS, or an AMS HT)."""
    if 0 <= ams_id < AMS_UNITS and 0 <= tray_id < TRAYS_PER_AMS:
        return 1 + ams_id * TRAYS_PER_AMS + tray_id
    if HT_FIRST_ID <= ams_id < HT_FIRST_ID + HT_UNITS and tray_id == 0:
        return HT_FIRST_SLOT + ams_id - HT_FIRST_ID
    return None


def slot_of_global(value: Any) -> int | None:
    """The slot of a tray numbered across AMS units (`tray_now`, `ams_mapping`): `ams * 4 +
    tray`, 254 and 255 the external spools. None for 255 in `tray_now` ("nothing loaded"),
    which `slot_of_mapping` doesn't see, and anything else."""
    number = _int(value)
    if number is None:
        return None
    if number == 254:
        return EXTERNAL
    if number == 255:
        return SECOND_EXTERNAL
    if 0 <= number < AMS_UNITS * TRAYS_PER_AMS:
        return 1 + number
    return None


def loaded_slot(tray_now: Any) -> int | None:
    """The slot `tray_now` says is loaded; None for 255 (nothing) or anything unknown."""
    return None if _int(tray_now) == 255 else slot_of_global(tray_now)


def filament_slots(filament_ids: Iterable[int], status: Mapping[str, Any]) -> dict[int, int]:
    """Each filament's slot, by its 1-based id in the project's list."""
    ids = sorted(set(filament_ids))
    mapping2 = status.get("ams_mapping2")
    mapping = status.get("ams_mapping")
    out: dict[int, int] = {}
    for filament_id in ids:
        slot = None
        if isinstance(mapping2, list) and 0 < filament_id <= len(mapping2):
            entry = mapping2[filament_id - 1]
            if isinstance(entry, Mapping):
                ams_id, slot_id = _int(entry.get("ams_id")), _int(entry.get("slot_id"))
                if ams_id in (254, 255):
                    slot = EXTERNAL if slot_id == 0 else SECOND_EXTERNAL
                elif ams_id is not None and slot_id is not None:
                    slot = slot_of_tray(ams_id, slot_id)
        elif isinstance(mapping, list) and 0 < filament_id <= len(mapping):
            slot = slot_of_global(mapping[filament_id - 1])
        if slot is not None:
            out[filament_id] = slot
    if len(out) == len(ids):
        return out
    has_ams = bool(units(status))
    if len(ids) == 1:
        loaded = loaded_slot(_ams(status).get("tray_now"))
        return {ids[0]: loaded if loaded is not None else EXTERNAL}
    return {
        filament_id: out.get(filament_id, filament_id if has_ams else EXTERNAL)
        for filament_id in ids
    }


@dataclass(frozen=True)
class TrayReading:
    tag: str | None
    material: str | None
    color_hex: str | None

    @property
    def empty(self) -> bool:
        return self.tag is None and self.material is None and self.color_hex is None


def readings(status: Mapping[str, Any]) -> dict[int, TrayReading]:
    """What each slot holds, as the AMS and the external spool holder say: a Bambu spool's RFID
    tag (`tag_uid`), and the material and color (from the tag, or as the user set them on the
    printer). An empty tray reads empty."""
    out: dict[int, TrayReading] = {}
    for unit in units(status):
        ams_id = _int(unit.get("id"))
        trays = unit.get("tray")
        if ams_id is None or not isinstance(trays, list):
            continue
        for tray in trays:
            if not isinstance(tray, Mapping):
                continue
            slot = slot_of_tray(ams_id, _int(tray.get("id")) or 0)
            if slot is not None:
                out[slot] = _reading(tray)
    externals = [status.get("vt_tray")] + list(status.get("vir_slot") or [])
    for tray in externals:
        if isinstance(tray, Mapping):
            slot = slot_of_global(tray.get("id", 254))
            if slot is not None:
                out[slot] = _reading(tray)
    return out


def slot_count(status: Mapping[str, Any]) -> int:
    """How many slots the printer has, by what it reports: the highest slot plus one."""
    return max(readings(status), default=EXTERNAL) + 1


def units(status: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found = _ams(status).get("ams")
    return [u for u in found if isinstance(u, Mapping)] if isinstance(found, list) else []


def _ams(status: Mapping[str, Any]) -> Mapping[str, Any]:
    ams = status.get("ams")
    return ams if isinstance(ams, Mapping) else {}


def _reading(tray: Mapping[str, Any]) -> TrayReading:
    tag = str(tray.get("tag_uid") or "").strip().upper()
    material = str(tray.get("tray_type") or "").strip()
    color = str(tray.get("tray_color") or "").strip()
    has_spool = bool(material)
    return TrayReading(
        tag=tag if tag.strip("0") else None,  # all zeros: no tag
        material=material or None,
        color_hex=f"#{color[:6].upper()}" if has_spool and len(color) >= 6 else None,
    )


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
