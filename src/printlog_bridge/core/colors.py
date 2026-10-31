"""A filament's color in words, for the note on each usage row (`slot_note`).

The same rule as the Slic3r uploader's `FilamentColor.Describe` (PrintLog.SlicerSettings), so a
print logged by either reads alike: hue, saturation and value pick one of a dozen basic names,
and the hex stays beside it ("Red (#E72F1D)"). The uploader says "Grey"; this says "Gray".
"""

from __future__ import annotations

import colorsys
import re

HEX = re.compile(r"#?([0-9A-Fa-f]{6})")


def normalized(color: str | None) -> str | None:
    """`#RRGGBB` in capitals, or None for anything that isn't six hex digits."""
    match = HEX.fullmatch((color or "").strip())
    return f"#{match.group(1).upper()}" if match else None


def color_name(color: str | None) -> str | None:
    """A basic name for a `#RRGGBB` color ("Red", "Gray"...), or None when it isn't one."""
    hex_color = normalized(color)
    if hex_color is None:
        return None
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    hue, saturation, value = colorsys.rgb_to_hsv(r, g, b)
    hue *= 360
    if value < 0.15:
        return "Black"
    if saturation < 0.12:
        return "White" if value > 0.85 else "Gray"
    if hue < 15 or hue >= 345:
        return "Red"
    if hue < 45:
        return "Brown" if value < 0.6 else "Orange"
    if hue < 70:
        return "Yellow"
    if hue < 165:
        return "Green"
    if hue < 195:
        return "Teal"
    if hue < 255:
        return "Blue"
    if hue < 290:
        return "Purple"
    return "Purple" if value < 0.65 else "Pink"


def slot_note(slot: int, color: str | None = None, material: str | None = None) -> str:
    """The note on a usage row, "Slot 1 · Red (#E72F1D) · PLA" for slot 0, as the Slic3r
    uploader writes it: what was loaded in the slot, so each row can be told apart without
    decoding hex."""
    parts = [f"Slot {slot + 1}"]
    hex_color = normalized(color)
    if hex_color is not None:
        name = color_name(hex_color)
        parts.append(f"{name} ({hex_color})" if name else hex_color)
    if material and material.strip():
        parts.append(material.strip())
    return " · ".join(parts)
