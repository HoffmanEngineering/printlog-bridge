"""The Snapmaker U1's RFID reader, from Klipper's `filament_detect` object.

Everything here comes from Snapmaker's published firmware (github.com/Snapmaker/u1-klipper,
`klippy/extras/filament_detect.py` and `filament_protocol.py`), not from a printer: none was at
hand. What it says, and what must still be checked on a real U1:

- `filament_detect.info` is a list with one entry per channel, four on the U1, and channel `n`
  feeds `extruder<n>` (T0-T3), so it is slot `n`.
- An entry is filled in only when a tag was read and its Snapmaker signature checked; a spool
  with no tag, or a tag that isn't Snapmaker's, reads as the empty entry: `VENDOR` is `NONE`.
- `CARD_UID` is the tag's UID, as a list of byte values (block 0's first 4 bytes on the M1
  tags Snapmaker uses), or 0 when no tag was read. The bridge writes those bytes as hex in
  order, `04A23F11`, which should be how a phone's NFC app shows the same tag. Unverified.
- `MAIN_TYPE` is the material (`PLA`, `PETG`, `PLA-CF`...), and `RGB_1` its first color as a
  24-bit number.

Kept apart from the rest of the adapter so a firmware that reports something else only needs
this file changed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from printlog_bridge.core.events import SpoolReading
from printlog_bridge.core.spools import normalize_tag

NO_VALUE = frozenset({"", "NONE", "Reserved"})


class U1RfidReader:
    """Turns `filament_detect` updates into a SpoolReading for each slot that changed."""

    def __init__(self, printer: str) -> None:
        self.printer = printer
        self._last: dict[int, SpoolReading] = {}

    def update(self, filament_detect: Mapping[str, Any]) -> list[SpoolReading]:
        """The readings that differ from the last ones reported, in slot order."""
        info = filament_detect.get("info")
        if not isinstance(info, Sequence) or isinstance(info, str):
            return []
        changed = []
        for slot, entry in enumerate(info):
            reading = channel_reading(self.printer, slot, entry, len(info))
            if self._last.get(slot) != reading:
                self._last[slot] = reading
                changed.append(reading)
        return changed


def channel_reading(printer: str, slot: int, entry: Any, slot_count: int) -> SpoolReading:
    if not isinstance(entry, Mapping) or str(entry.get("VENDOR") or "NONE") in NO_VALUE:
        return SpoolReading(printer, slot, slot_count=slot_count)
    material = str(entry.get("MAIN_TYPE") or "").strip()
    color = entry.get("RGB_1")
    return SpoolReading(
        printer,
        slot,
        tag=tag_uid(entry.get("CARD_UID")),
        material=None if material in NO_VALUE else material,
        color_hex=f"#{color:06X}" if isinstance(color, int) and 0 <= color <= 0xFFFFFF else None,
        slot_count=slot_count,
    )


def tag_uid(value: Any) -> str | None:
    """`CARD_UID` as hex: a list of byte values, in order. None when there's no UID."""
    if isinstance(value, str):
        return normalize_tag(value)
    if (
        isinstance(value, Sequence)
        and value
        and all(isinstance(b, int) and 0 <= b <= 0xFF for b in value)
    ):
        if not any(value):
            return None
        return "".join(f"{b:02X}" for b in value)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        text = f"{value:X}"
        return text.rjust(len(text) + len(text) % 2, "0")
    return None
