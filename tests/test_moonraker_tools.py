"""Splitting Klipper's per-print `filament_used` by the tool that fed it.

The status updates below follow what Klipper sends while printing (checked against Klipper's
`print_stats.py` and `toolhead.py`): `print_stats.filament_used` is the millimetres of filament
fed since the print started, across every extruder, and `toolhead.extruder` names the active
one: `extruder`, `extruder1`, `extruder2`... on a toolchanger, an IDEX or a Snapmaker U1 (T0-T3).
A new print resets `filament_used` to 0 (`print_stats.reset()` when the file is loaded) before
the state goes to `printing`.
"""

from __future__ import annotations

import pytest

from printlog_bridge.adapters.moonraker.tools import (
    ToolUsage,
    active_slot,
    estimated_grams_by_slot,
    grams_by_slot_from_file,
    per_slot,
    slot_of,
)


def status(state="printing", used=None, tool="extruder", filename="cube.gcode", nozzle=None):
    stats = {"state": state, "filename": filename}
    if used is not None:
        stats["filament_used"] = used
    result = {"print_stats": stats, "toolhead": {"extruder": tool}}
    if nozzle is not None:
        result["nozzle_manager"] = {"selected": nozzle}
    return result


class TestActiveSlot:
    def test_is_the_active_extruder(self):
        assert active_slot({"toolhead": {"extruder": "extruder2"}}) == 2

    def test_a_creality_k3_names_its_nozzle_while_toolhead_stays_on_extruder(self):
        """The K3's stock firmware prints T1-T3 without switching Klipper's active extruder
        (seen in a trace of a four-color print: `toolhead.extruder` was `extruder` throughout),
        and keeps the printing nozzle, 0-3, in `nozzle_manager.selected`."""
        status = {"toolhead": {"extruder": "extruder"}, "nozzle_manager": {"selected": 3}}

        assert active_slot(status) == 3

    @pytest.mark.parametrize("selected", [-1, None, "1", True, 1.5])
    def test_no_nozzle_selected_is_no_slot_not_the_extruder(self, selected):
        """-1 is the K3's idle answer, and what a swap between nozzles may pass through; its
        `toolhead.extruder` is always `extruder`, so falling back to it would give slot 0
        whatever was purged between nozzles. No slot keeps the last one."""
        status = {"toolhead": {"extruder": "extruder"}, "nozzle_manager": {"selected": selected}}

        assert active_slot(status) is None

    def test_a_nozzle_manager_without_selected_leaves_it_to_the_extruder(self):
        status = {"toolhead": {"extruder": "extruder1"}, "nozzle_manager": {"nozzles": []}}

        assert active_slot(status) == 1

    def test_is_none_when_nothing_says(self):
        assert active_slot({}) is None
        assert active_slot({"toolhead": {"extruder": "my_tool"}}) is None


class TestSlotOf:
    @pytest.mark.parametrize(
        ("name", "slot"),
        [("extruder", 0), ("extruder1", 1), ("extruder2", 2), ("extruder3", 3), ("extruder10", 10)],
    )
    def test_klippers_extruder_names_are_slots_in_order(self, name, slot):
        assert slot_of(name) == slot

    @pytest.mark.parametrize("name", [None, "", "extruder_stepper belt", "my_tool", "extruderX"])
    def test_anything_else_is_not_a_slot(self, name):
        assert slot_of(name) is None


class TestToolUsage:
    def test_attributes_each_delta_to_the_extruder_active_at_that_update(self):
        usage = ToolUsage()
        usage.update(status("standby", 0.0))
        usage.update(status("printing", 0.0))
        usage.update(status(used=100.0, tool="extruder"))
        usage.update(status(used=250.0, tool="extruder1"))
        usage.update(status(used=300.0, tool="extruder3"))
        usage.update(status(used=320.0, tool="extruder"))

        assert usage.take("cube.gcode") == {0: 120.0, 1: 150.0, 3: 50.0}

    def test_a_tool_change_without_new_usage_moves_later_usage_to_the_new_tool(self):
        usage = ToolUsage()
        usage.update(status("printing", 0.0))
        usage.update(status(used=50.0))
        usage.update({"toolhead": {"extruder": "extruder2"}, "print_stats": {}})
        usage.update(status(used=80.0, tool="extruder2"))

        assert usage.take("cube.gcode") == {0: 50.0, 2: 30.0}

    def test_a_new_print_starts_from_zero(self):
        usage = ToolUsage()
        usage.update(status("printing", 0.0, filename="first.gcode"))
        usage.update(status(used=500.0, filename="first.gcode"))
        usage.update(status("complete", 500.0, filename="first.gcode"))
        usage.update(status("standby", 0.0, filename="second.gcode"))
        usage.update(status("printing", 0.0, filename="second.gcode"))
        usage.update(status(used=40.0, tool="extruder1", filename="second.gcode"))

        assert usage.take("first.gcode") == {}
        assert usage.take("second.gcode") == {1: 40.0}

    def test_a_print_already_running_counts_only_what_it_saw(self):
        """The bridge started mid-print: what came before is unknown, not slot 0's."""
        usage = ToolUsage()
        usage.update(status(used=900.0, tool="extruder2"))
        usage.update(status(used=1000.0, tool="extruder2"))

        assert usage.take("cube.gcode") == {2: 100.0}

    def test_a_reconnect_mid_print_carries_on(self):
        usage = ToolUsage()
        usage.update(status("printing", 0.0))
        usage.update(status(used=100.0))
        usage.update(status(used=180.0, tool="extruder1"))  # resubscribed after a reconnect

        assert usage.take("cube.gcode") == {0: 100.0, 1: 80.0}

    def test_a_retraction_mid_print_is_not_a_new_print(self):
        # filament_used follows the E position, so it drops while a retraction is in effect.
        usage = ToolUsage()
        usage.update(status("printing", 0.0))
        usage.update(status(used=1000.0))
        usage.update(status(used=2000.0, tool="extruder1"))
        usage.update(status(used=1999.2, tool="extruder1"))  # retracted
        usage.update(status("paused", 1998.0, tool="extruder1"))  # retracted further on pause
        usage.update(status(used=2100.0, tool="extruder1"))

        assert usage.take("cube.gcode") == pytest.approx({0: 1000.0, 1: 1100.0})

    def test_a_new_file_while_away_starts_over(self):
        usage = ToolUsage()
        usage.update(status("printing", 0.0, filename="first.gcode"))
        usage.update(status(used=500.0, filename="first.gcode"))
        # The bridge missed the end of first.gcode and the start of second.gcode.
        usage.update(status(used=40.0, tool="extruder1", filename="second.gcode"))
        usage.update(status(used=60.0, tool="extruder1", filename="second.gcode"))

        assert usage.take("first.gcode") == {}
        assert usage.take("second.gcode") == {1: 60.0}

    def test_is_taken_once(self):
        usage = ToolUsage()
        usage.update(status("printing", 0.0))
        usage.update(status(used=100.0))

        assert usage.take("cube.gcode") == {0: 100.0}
        assert usage.take("cube.gcode") == {}

    def test_a_single_tool_printer_has_one_slot(self):
        usage = ToolUsage()
        usage.update(status("printing", 0.0))
        for used in (10.0, 20.0, 30.0):
            usage.update(status(used=used))

        assert usage.take("cube.gcode") == {0: 30.0}

    def test_an_unknown_extruder_name_keeps_the_last_known_slot(self):
        usage = ToolUsage()
        usage.update(status("printing", 0.0, tool="extruder1"))
        usage.update(status(used=10.0, tool=""))

        assert usage.take("cube.gcode") == {1: 10.0}

    def test_a_creality_k3_follows_the_selected_nozzle(self):
        """Whatever slot a G-code tool was mapped to when the print was sent, the nozzle that
        prints is the one fed, so its slot is the one used."""
        usage = ToolUsage()
        usage.update(status("printing", 0.0, nozzle=-1))
        usage.update(status(used=100.0, nozzle=3))
        usage.update(status(used=101.0, nozzle=-1))  # between nozzles: stays on the last one
        usage.update(status(used=150.0, nozzle=0))
        usage.update(status(used=170.0, nozzle=2))

        assert usage.take("cube.gcode") == {3: 101.0, 0: 49.0, 2: 20.0}


class TestPerSlot:
    def test_scales_what_was_tracked_to_moonrakers_total(self):
        split = per_slot(1000.0, {0: 396.0, 1: 297.0, 2: 198.0, 3: 99.0}, {})

        assert split == pytest.approx({0: 400.0, 1: 300.0, 2: 200.0, 3: 100.0})
        assert sum(split.values()) == pytest.approx(1000.0)

    def test_a_tool_that_only_retracted_gets_nothing(self):
        assert per_slot(100.0, {0: 100.0, 1: -2.0}, {}) == {0: 100.0}

    def test_without_tracking_splits_by_the_slicers_estimate(self):
        """A job that ended while the bridge was away."""
        assert per_slot(1000.0, {}, {0: 30.0, 2: 10.0}) == pytest.approx({0: 750.0, 2: 250.0})

    def test_with_nothing_to_go_on_it_is_all_slot_0(self):
        assert per_slot(1234.5, {}, {}) == {0: 1234.5}

    def test_nothing_used_is_slot_0_with_nothing(self):
        assert per_slot(0, {1: 5.0}, {1: 3.0}) == {0: 0.0}


class TestEstimatedGramsBySlot:
    def test_orca_and_prusaslicer_list_a_weight_per_filament(self):
        """`; filament used [g] = 12.31, 0.00, 4.52, 1.07`, as Moonraker's `filament_weights`."""
        metadata = {"filament_weights": [12.31, 0.0, 4.52, 1.07], "filament_weight_total": 17.9}

        assert estimated_grams_by_slot(metadata) == {0: 12.31, 2: 4.52, 3: 1.07}

    def test_a_multi_filament_file_using_one_tool_names_that_tool(self):
        assert estimated_grams_by_slot({"filament_weights": [0.0, 0.0, 8.2, 0.0]}) == {2: 8.2}

    def test_the_snapmaker_u1s_moonraker_calls_them_filament_weight(self):
        # Seen on a real U1 (Moonraker 2.0.0): tests/fixtures/moonraker/u1/history.json.
        metadata = {"filament_weight": [54.22, 67.09, 20.69, 0.0], "filament_weight_total": 142.0}

        assert estimated_grams_by_slot(metadata) == {0: 54.22, 1: 67.09, 2: 20.69}

    @pytest.mark.parametrize(
        "metadata",
        [
            {},
            {"filament_weight_total": 13.27},
            {"filament_weights": [13.27]},  # one filament: the single row stays as it was
            {"filament_weights": "12, 3"},
            # A single-extruder MMU (ERCF, Happy Hare, AFC): the weights are per gate, and every
            # gate feeds Klipper's one `extruder`.
            {"filament_weights": [12.0, 3.0], "mmu_print": 1},
        ],
    )
    def test_is_empty_without_a_weight_per_tool(self, metadata):
        assert estimated_grams_by_slot(metadata) == {}


class TestGramsBySlotFromFile:
    # The end of a real Creality Print 7.3 file for a Creality K3, whose metadata Moonraker
    # leaves without weights (`slicer: Unknown`).
    K3_TAIL = (
        "; filament used [mm] = 7295.43, 3462.26, 1318.99, 1870.45\n"
        "; filament used [cm3] = 17.55, 8.33, 3.17, 4.50\n"
        "; filament used [g] = 21.76, 10.33, 3.93, 5.58\n"
        "; filament cost = 0.44, 0.21, 0.08, 0.11\n"
        "; total filament used [g] = 41.60\n"
        "; total filament change = 410\n"
    )

    def test_reads_the_slicers_grams_per_filament(self):
        assert grams_by_slot_from_file(self.K3_TAIL) == {0: 21.76, 1: 10.33, 2: 3.93, 3: 5.58}

    def test_prusaslicer_and_orca_spacing_and_crlf(self):
        text = ";filament used [g]=12.31,0.00,4.52\r\n; total filament used [g] = 16.83\r\n"

        assert grams_by_slot_from_file(text) == {0: 12.31, 2: 4.52}

    def test_the_last_such_line_wins(self):
        """A slicer's line in the G-code is followed by its settings block's; keep the last."""
        text = "; filament used [g] = 1, 2\n; filament used [g] = 3, 4\n"

        assert grams_by_slot_from_file(text) == {0: 3.0, 1: 4.0}

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "; filament used [g] = 13.27\n",  # one filament: the single row stays as it was
            "; total filament used [g] = 41.60\n",
            "; filament used [mm] = 7295.43, 3462.26\n",
            "; filament used [g] = 12, abc\n",
            "G1 X10 ; filament used [g] = 1, 2\n",  # not a comment line of its own
        ],
    )
    def test_is_empty_without_a_weight_per_tool(self, text):
        assert grams_by_slot_from_file(text) == {}
