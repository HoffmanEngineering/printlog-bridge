"""A print's title from its file name, as a person would have typed it."""

from __future__ import annotations

import re

MAX_TITLE_LENGTH = 100
"""The API's limit on a print title."""

UNTITLED = "Untitled print"

EXTENSIONS = (".3mf", ".gcode", ".gco", ".g", ".bgcode", ".ufp")

FILAMENT_TYPES = frozenset(
    {"PLA", "PLA+", "PETG", "PET", "ABS", "ASA", "TPU", "PA", "PC", "PVA", "HIPS", "PP", "PA-CF",
     "PLA-CF", "PETG-CF", "NYLON", "FLEX"}
)  # fmt: skip

_DURATION = re.compile(r"^(?=\d)(\d+d)?(\d+h)?(\d+m)?(\d+s)?$")
_LAYER_HEIGHT = re.compile(r"^\d+(\.\d+)?mm$")


def title_from_file_name(file_name: str) -> str:
    """`shelf_bracket_0.2mm_PLA_MK4_1h2m.gcode` -> `Shelf Bracket`.

    Strips the folder, the extensions, and the suffixes PrusaSlicer and OrcaSlicer add by default.
    Those always end with the print time, so nothing is stripped from a name that doesn't, and a
    file the user named themselves keeps all of its words.
    """
    words = _words(file_name)
    title = " ".join(w[:1].upper() + w[1:] for w in words)[:MAX_TITLE_LENGTH].strip()
    return title or UNTITLED


def same_file(a: str, b: str) -> bool:
    """Whether two file names are the same model, as `title_from_file_name` reads them: folders,
    extensions, the slicer's suffix, case, and `_`, `-` and spaces don't count.

    `parts/benchy_0.2mm_PLA_MK4_1h2m.gcode` is the same file as `Benchy.bgcode`."""
    key = [w.lower() for w in _words(a)]
    return bool(key) and key == [w.lower() for w in _words(b)]


def _words(file_name: str) -> list[str]:
    name = re.split(r"[/\\]", file_name)[-1]
    while True:
        extension = next((e for e in EXTENSIONS if name.lower().endswith(e)), None)
        if extension is None:
            break
        name = name[: -len(extension)]

    parts = name.split("_")
    if len(parts) > 1 and _DURATION.match(parts[-1]):
        parts.pop()
        layer = next((i for i, p in enumerate(parts) if i > 0 and _LAYER_HEIGHT.match(p)), None)
        if layer is not None:
            parts = parts[:layer]  # PrusaSlicer: _{layer_height}mm_{filament}_{printer}
        elif len(parts) > 1 and parts[-1].upper() in FILAMENT_TYPES:
            parts.pop()  # OrcaSlicer: _{filament}

    return " ".join(parts).replace("-", " ").split()
