from __future__ import annotations

import pytest

from printlog_bridge.core.titles import title_from_file_name


@pytest.mark.parametrize(
    ("file_name", "title"),
    [
        ("benchy.gcode", "Benchy"),
        ("my_cool-benchy.gcode", "My Cool Benchy"),
        ("parts/brackets/corner_bracket.gcode", "Corner Bracket"),
        # PrusaSlicer's default:
        # {input_filename_base}_{layer_height}mm_{filament_type}_{printer_model}_{print_time}
        ("shelf_bracket_0.2mm_PLA_MK4_1h2m.gcode", "Shelf Bracket"),
        ("shelf_bracket_0.20mm_PETG_MINI_45m.bgcode", "Shelf Bracket"),
        # OrcaSlicer's default: {input_filename_base}_{filament_type[0]}_{print_time}
        ("cable_clip_PLA_33m12s.gcode", "Cable Clip"),
        ("cable_clip_PETG_1d2h3m.gcode", "Cable Clip"),
        # Snapmaker U1 and Bambu-style jobs
        ("Plate_1.gcode.3mf", "Plate 1"),
        ("gear.3mf", "Gear"),
        # The file's own words and capitals are kept
        ("3DBenchy.gcode", "3DBenchy"),
        ("benchy_v2.gcode", "Benchy V2"),
        ("PLA_test_tower.gcode", "PLA Test Tower"),
        ("print time 2h.gcode", "Print Time 2h"),
    ],
)
def test_strips_extensions_and_slicer_suffixes(file_name, title):
    assert title_from_file_name(file_name) == title


def test_is_capped_at_the_apis_100_characters():
    assert len(title_from_file_name("a" * 150 + ".gcode")) == 100


@pytest.mark.parametrize("file_name", ["", ".gcode", "_1h2m.gcode", "  .gcode"])
def test_never_returns_a_blank_title(file_name):
    assert title_from_file_name(file_name) == "Untitled print"
