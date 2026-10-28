"""What adapters read from a slicer's file, shared by those that read files themselves
(PrusaLink, OctoPrint): the `; key = value` settings, the slicer's estimates in them, and the
summary 3D Print Log shows.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from printlog_bridge.core.events import SlicerFile, SlicerMetadata

_DURATION = re.compile(r"(\d+)\s*([dhms])")
_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


@dataclass
class JobFile:
    """What was read of a job's file. Empty when nothing could be."""

    grams: dict[int, float] = field(default_factory=dict)
    """The slicer's grams per tool (slot), tools that use nothing left out."""
    mm: dict[int, float] = field(default_factory=dict)
    duration_s: float | None = None
    printer_model: str | None = None
    metadata: SlicerMetadata | None = None
    settings: SlicerFile | None = None
    thumbnail: bytes | None = None
    gcode_span: tuple[int, int] | None = None
    """Where the G-code starts and ends in the file, in bytes, past the header and thumbnails
    and before the settings block, when the reader found out."""


def parse_ini(raw: bytes) -> dict[str, str]:
    """`key=value` lines (spaces around `=` allowed, as `; key = value` in a text file), split at
    the first `=`: a key such as `filament used [g]` has spaces of its own but never an `=`."""
    pairs: dict[str, str] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(";"):
            line = line[1:].strip()
        key, sep, value = line.partition("=")
        if sep and key.strip():
            pairs[key.strip()] = value.strip()
    return pairs


def facts_from_meta(meta: Mapping[str, Any]) -> JobFile:
    """The estimates in a file's metadata: PrusaSlicer's `filament used [g]` and `[mm]` (one
    value per tool, comma separated, in a `.bgcode` or a G-code file's comments; or a number
    and a `... per tool` list, as PrusaLink's `file.meta` has them), and the print time."""
    grams = _per_tool(meta, "filament used [g]")
    mm = _per_tool(meta, "filament used [mm]")
    duration = None
    seconds = meta.get("estimated_print_time")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
        duration = float(seconds)
    else:
        text = meta.get("estimated printing time (normal mode)")
        duration = duration_of(text) if isinstance(text, str) else None
    model = meta.get("printer_model")
    return JobFile(
        grams=grams,
        mm=mm,
        duration_s=duration,
        printer_model=str(model).strip() or None if model else None,
    )


def metadata_from(
    settings: Mapping[str, Any], slicer: str | None, version: str | None
) -> SlicerMetadata:
    """The summary 3D Print Log shows, from the slicer's settings."""
    return SlicerMetadata(
        slicer=slicer or None,
        slicer_version=version or None,
        layer_height_mm=first_number(settings.get("layer_height")),
        first_layer_height_mm=first_number(settings.get("first_layer_height")),
        nozzle_diameter_mm=first_number(settings.get("nozzle_diameter")),
        filament_name=first_text(settings.get("filament_settings_id")),
        filament_type=first_text(settings.get("filament_type")),
        first_layer_nozzle_temperature_c=first_number(settings.get("first_layer_temperature")),
        first_layer_bed_temperature_c=first_number(settings.get("first_layer_bed_temperature")),
    )


def duration_of(text: str) -> float | None:
    """PrusaSlicer's `1d 2h 3m 4s` in seconds."""
    parts = _DURATION.findall(text)
    if not parts:
        return None
    return float(sum(int(n) * _UNITS[unit] for n, unit in parts))


def _per_tool(meta: Mapping[str, Any], key: str) -> dict[int, float]:
    values = _numbers(meta.get(f"{key} per tool"))
    if not values:
        values = _numbers(meta.get(key))
    return {tool: round(v, 3) for tool, v in enumerate(values) if v > 0}


def _numbers(value: Any) -> list[float]:
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,;]", str(value))
    out = []
    for item in items:
        try:
            number = float(item)
        except (TypeError, ValueError):
            number = 0.0
        out.append(number if number == number else 0.0)  # NaN is nothing
    return out


def first_number(value: Any) -> float | None:
    numbers = _numbers(value)
    return numbers[0] if numbers else None


def first_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    first = re.split(r"[;,]", value)[0].strip().strip('"')
    return first or None
