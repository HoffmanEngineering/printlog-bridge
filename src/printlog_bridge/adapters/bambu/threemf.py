"""What the bridge reads from a Bambu job's 3MF: the slicer's grams per filament, the plate's
thumbnail and the project's settings.

A 3MF is a ZIP. Bambu Studio (and OrcaSlicer, for Bambu printers) write:

- `Metadata/slice_info.config`, XML: one `<plate>` per sliced plate, with `<metadata>` items
  (`index`, `prediction` in seconds, `weight` in grams) and a `<filament>` per filament used,
  `id` (1-based, the project's filament list), `type`, `color`, `used_m` and `used_g`. The
  `<header>` has `X-BBL-Client-Version`.
- `Metadata/plate_<n>.png`, the plate's preview.
- `Metadata/project_settings.config`, JSON: the print, filament and printer settings, a value or
  a list of values (one per filament or extruder) for each key.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
import zipfile
from dataclasses import dataclass
from typing import IO, Any

SLICE_INFO = "Metadata/slice_info.config"
PROJECT_SETTINGS = "Metadata/project_settings.config"
MAX_ENTRY_BYTES = 16 * 1024 * 1024
"""Larger metadata is not what Bambu Studio writes: it's left alone."""
_PLATE_GCODE = re.compile(r"plate_(\d+)\.gcode$", re.IGNORECASE)


@dataclass(frozen=True)
class Filament:
    id: int
    """1-based, the filament's place in the project's list, which `ams_mapping` is indexed by."""
    type: str | None
    color: str | None
    used_g: float
    used_m: float


@dataclass(frozen=True)
class Plate:
    index: int
    prediction_s: float | None
    weight_g: float | None
    filaments: tuple[Filament, ...]


def plate_of(gcode_file: str, plate_idx: Any = None) -> int | None:
    """Which plate the printer is printing: `gcode_file` names it for a print sent from the
    slicer (`/data/Metadata/plate_3.gcode`), and some firmwares report `plate_idx`."""
    match = _PLATE_GCODE.search(gcode_file or "")
    if match:
        return int(match.group(1))
    try:
        index = int(plate_idx)
    except (TypeError, ValueError):
        return None
    return index if index > 0 else None


def read_plates(data: bytes) -> dict[int, Plate]:
    """The plates of a `slice_info.config`, by index. Empty when it can't be read."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return {}
    plates = {}
    for element in root.iter("plate"):
        meta = {m.get("key"): m.get("value") for m in element.findall("metadata")}
        index = _int(meta.get("index"))
        if index is None:
            continue
        filaments = []
        for item in element.findall("filament"):
            filament_id = _int(item.get("id"))
            if filament_id is None:
                continue
            filaments.append(
                Filament(
                    id=filament_id,
                    type=item.get("type") or None,
                    color=item.get("color") or None,
                    used_g=_float(item.get("used_g")) or 0.0,
                    used_m=_float(item.get("used_m")) or 0.0,
                )
            )
        plates[index] = Plate(
            index=index,
            prediction_s=_float(meta.get("prediction")),
            weight_g=_float(meta.get("weight")),
            filaments=tuple(filaments),
        )
    return plates


class ThreeMF:
    """A job's 3MF, open for reading the few entries the bridge wants."""

    def __init__(self, file: IO[bytes]) -> None:
        self.file = file
        self.zip = zipfile.ZipFile(file)
        info = self._entry(SLICE_INFO)
        self.plates = read_plates(info) if info else {}

    def plate(self, index: int | None) -> Plate | None:
        """Plate `index`, or the only plate sliced (a `.gcode.3mf` holds one) when the printer
        didn't say."""
        if index is not None:
            return self.plates.get(index)
        if len(self.plates) == 1:
            return next(iter(self.plates.values()))
        return None

    def thumbnail(self, index: int | None) -> bytes | None:
        plate = self.plate(index)
        return self._entry(f"Metadata/plate_{plate.index}.png") if plate else None

    def settings(self) -> dict[str, Any] | None:
        data = self._entry(PROJECT_SETTINGS)
        if data is None:
            return None
        try:
            settings = json.loads(data)
        except ValueError:
            return None
        return settings if isinstance(settings, dict) else None

    def close(self) -> None:
        self.zip.close()
        self.file.close()

    def _entry(self, name: str) -> bytes | None:
        try:
            info = self.zip.getinfo(name)
        except KeyError:
            return None
        if info.file_size > MAX_ENTRY_BYTES:
            return None
        try:
            return self.zip.read(info)
        except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
            return None


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _float(text: str | None) -> float | None:
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None
