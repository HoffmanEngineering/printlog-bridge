"""Color names for usage notes: the Slic3r uploader's table (`FilamentColorTests`), so a print
logged by either names its colors alike. The one difference is American English: Gray."""

from __future__ import annotations

import pytest

from printlog_bridge.core.colors import color_name, normalized, slot_note


@pytest.mark.parametrize(
    ("color", "name"),
    [
        ("#E72F1D", "Red"),
        ("#080A0D", "Black"),
        ("#FFFFFF", "White"),
        ("#0078BF", "Blue"),
        ("#A6A9AA", "Gray"),
        ("#FFFF00", "Yellow"),
        ("#26A69A", "Teal"),
        ("#FF8000", "Orange"),
        ("#8B4513", "Brown"),
        ("#800080", "Purple"),
        ("#FF69B4", "Pink"),
        ("#00AE42", "Green"),
    ],
)
def test_names_the_nearest_basic_color(color, name):
    assert color_name(color) == name


def test_accepts_lowercase_hex_without_a_hash():
    assert color_name("15a95d") == "Green"


@pytest.mark.parametrize("color", [None, "", "   ", "#FFF", "#GGGGGG", "red", "#E2DEDBFF"])
def test_is_none_for_anything_but_six_hex_digits(color):
    assert color_name(color) is None
    assert normalized(color) is None


class TestSlotNote:
    def test_slot_color_and_material_as_the_uploader_writes_them(self):
        assert slot_note(0, "#e72f1d", "PLA") == "Slot 1 · Red (#E72F1D) · PLA"

    def test_a_snapmaker_u1s_loaded_filament(self):
        # print_task_config on a real U1: head 3 has 1E88E5, Snapmaker PLA Basic.
        assert slot_note(2, "1E88E5", "PLA Basic") == "Slot 3 · Blue (#1E88E5) · PLA Basic"

    def test_without_a_color_or_material_says_what_it_has(self):
        assert slot_note(1) == "Slot 2"
        assert slot_note(1, material="PETG") == "Slot 2 · PETG"
        assert slot_note(1, "#123") == "Slot 2"
