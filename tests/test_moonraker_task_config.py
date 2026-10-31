"""What a Snapmaker U1 and a Creality K3 say they have loaded, and where the U1 sent each of the
slicer's filaments. The answers are real (`tests/fixtures/moonraker/u1/print_task_config.json`,
`tests/fixtures/moonraker/k3/box.json`)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from printlog_bridge.adapters.moonraker.task_config import (
    loaded,
    placed,
    placed_by_color,
    remapped,
    slot_notes,
    tool_map,
)
from printlog_bridge.adapters.moonraker.tools import SlicedFilament

FIXTURES = Path(__file__).parent / "fixtures" / "moonraker"


def answer(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["result"]["status"]


U1 = answer("u1/print_task_config.json")
K3 = answer("k3/box.json")


def u1_mapped(table_start: list[int]) -> dict:
    """The U1's answer with the slicer's first tools sent elsewhere, as its screen allows."""
    status = copy.deepcopy(U1)
    table = status["print_task_config"]["extruder_map_table"]
    table[: len(table_start)] = table_start
    return status


class TestToolMap:
    def test_the_u1s_table_has_a_head_per_gcode_tool(self):
        table = tool_map(U1)

        assert len(table) == 32
        assert table[:4] == [0, 1, 2, 3]

    @pytest.mark.parametrize(
        "config",
        [{}, {"extruder_map_table": []}, {"extruder_map_table": [0, -1]},
         {"extruder_map_table": [0, "1"]}, {"extruder_map_table": [True, 1]}],
    )  # fmt: skip
    def test_none_without_a_table_of_whole_numbers(self, config):
        assert tool_map({"print_task_config": config}) is None

    def test_none_on_a_printer_without_one(self):
        assert tool_map(K3) is None


class TestPlaced:
    def test_the_slicers_tools_go_to_the_heads_they_were_mapped_to(self):
        # T0 (30.2 g) printed on head 3, T1 on head 0, T2 on head 2, T3 on head 1.
        table = tool_map(u1_mapped([3, 0, 2, 1]))

        assert placed({0: 30.2, 1: 9.8, 2: 3.6, 3: 1.5}, table) == {
            3: 30.2,
            0: 9.8,
            2: 3.6,
            1: 1.5,
        }

    def test_two_tools_on_one_head_add_up(self):
        assert remapped({0: 5.0, 4: 2.0}, [1, 0, 2, 3, 1]) == {1: 7.0}

    def test_a_tool_past_the_table_keeps_its_number(self):
        assert remapped({5: 2.0}, [1, 0]) == {5: 2.0}

    def test_without_a_table_nothing_moves(self):
        assert placed({0: 30.2, 2: 3.6}, None) == {0: 30.2, 2: 3.6}

    def test_a_one_filament_file_sent_to_another_head_is_on_that_head(self):
        assert placed({}, [2, 1, 2, 3], single=13.27) == {2: 13.27}

    @pytest.mark.parametrize(("table", "single"), [([0, 1], 13.27), ([2, 1], None), (None, 13.0)])
    def test_a_one_filament_file_on_t0s_own_head_or_without_a_weight_stays_one_row(
        self, table, single
    ):
        assert placed({}, table, single=single) == {}


class TestLoaded:
    def test_what_each_u1_head_has_loaded(self):
        assert loaded(U1) == {
            0: ("#E2DEDB", "PLA SnapSpeed"),
            1: ("#000000", "PLA Basic"),
            2: ("#1E88E5", "PLA Basic"),
            3: ("#D81B60", "PLA Basic"),
        }

    def test_a_u1_head_with_no_filament_is_left_out(self):
        status = copy.deepcopy(U1)
        status["print_task_config"]["filament_exist"][1] = False

        assert sorted(loaded(status)) == [0, 2, 3]

    def test_what_each_k3_cfs_slot_has_loaded(self):
        """`same_material` names CFS slots T1A-T1D; slot A feeds nozzle 1, the bridge's slot 0."""
        assert loaded(K3) == {
            0: ("#FFFFFF", "PLA"),
            1: ("#000000", "PLA"),
            2: ("#FFF014", "PLA"),
            3: ("#FF1E1E", "PLA"),
        }

    def test_a_second_cfs_box_is_slots_4_to_7(self):
        status = {"box": {"same_material": [["000001", "00a3ff0", ["T2C"], "PETG"]]}}

        assert loaded(status) == {6: ("#0A3FF0", "PETG")}

    @pytest.mark.parametrize(
        "status",
        [{}, {"box": {"same_material": "x"}}, {"box": {"same_material": [["a", "b"]]}},
         {"box": {"same_material": [["000001", "0ffffff", ["T0A", "front"], "PLA"]]}}],
    )  # fmt: skip
    def test_nothing_it_cant_read(self, status):
        assert loaded(status) == {}


class TestSlotNotes:
    def test_u1(self):
        assert slot_notes(U1) == {
            0: "Slot 1 · White (#E2DEDB) · PLA SnapSpeed",
            1: "Slot 2 · Black (#000000) · PLA Basic",
            2: "Slot 3 · Blue (#1E88E5) · PLA Basic",
            3: "Slot 4 · Pink (#D81B60) · PLA Basic",
        }

    def test_k3(self):
        assert slot_notes(K3) == {
            0: "Slot 1 · White (#FFFFFF) · PLA",
            1: "Slot 2 · Black (#000000) · PLA",
            2: "Slot 3 · Yellow (#FFF014) · PLA",
            3: "Slot 4 · Red (#FF1E1E) · PLA",
        }


FUECOCO = [
    SlicedFilament(21.76, "#FF0000", "PLA"),
    SlicedFilament(10.33, "#FFFFFF", "PLA"),
    SlicedFilament(3.93, "#FFFF00", "PLA"),
    SlicedFilament(5.58, "#000000", "PLA"),
]


class TestPlacedByColor:
    """A K3 has no map of the G-code's tools, so each sliced filament goes to the CFS slot
    nearest in color: slot 0 white, 1 black, 2 yellow (#FFF014), 3 red (#FF1E1E)."""

    def test_places_each_filament_on_its_color(self):
        assert placed_by_color(FUECOCO, loaded(K3)) == ({0: 10.33, 1: 5.58, 2: 3.93, 3: 21.76}, [])

    def test_two_slots_of_one_color_are_a_tie(self):
        slots = {**loaded(K3), 4: ("#FFFFFF", "PLA")}

        by_slot, unplaced = placed_by_color(FUECOCO, slots)

        assert unplaced == [1]
        assert 0 not in by_slot and 4 not in by_slot

    def test_a_color_nothing_is_near_is_unplaced(self):
        blue = [SlicedFilament(4.0, "#1E88E5", "PLA"), *FUECOCO[1:]]

        assert placed_by_color(blue, loaded(K3)) == ({0: 10.33, 1: 5.58, 2: 3.93}, [0])

    def test_another_material_is_unplaced(self):
        petg = [SlicedFilament(21.76, "#FF0000", "PETG"), *FUECOCO[1:]]

        assert placed_by_color(petg, loaded(K3))[1] == [0]

    def test_the_materials_first_word_matches(self):
        slots = {3: ("#FF1E1E", "PLA Basic")}

        assert placed_by_color([FUECOCO[0], FUECOCO[0]], slots) == ({3: 43.52}, [])

    def test_two_filaments_of_one_color_add_up(self):
        reds = [FUECOCO[0], SlicedFilament(1.0, "#FE0101", "PLA")]

        assert placed_by_color(reds, loaded(K3)) == ({3: 22.76}, [])

    def test_nothing_loaded_leaves_them_all_unplaced(self):
        assert placed_by_color(FUECOCO, {}) == ({}, [0, 1, 2, 3])

    def test_a_filament_with_no_color_is_unplaced(self):
        assert placed_by_color([SlicedFilament(2.0, None, "PLA"), FUECOCO[1]], loaded(K3)) == (
            {0: 10.33},
            [0],
        )

    def test_a_filament_with_no_grams_is_left_out(self):
        assert placed_by_color([SlicedFilament(0.0, "#123456", "PLA"), FUECOCO[1]], loaded(K3)) == (
            {0: 10.33},
            [],
        )
