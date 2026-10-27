"""Slot numbers for a Bambu printer's spools, and what its AMS reads in each."""

from __future__ import annotations

from fake_bambu import idle_status, tray

from printlog_bridge.adapters.bambu.slots import (
    TrayReading,
    filament_slots,
    loaded_slot,
    readings,
    slot_count,
    slot_of_global,
    slot_of_tray,
)


def test_the_numbering():
    assert slot_of_global(254) == 0  # the external spool
    assert slot_of_tray(0, 0) == 1  # A1
    assert slot_of_tray(0, 3) == 4  # A4
    assert slot_of_tray(1, 0) == 5  # B1
    assert slot_of_tray(3, 3) == 16  # D4
    assert slot_of_global("5") == 6  # B2, as tray_now and ams_mapping number it
    assert slot_of_tray(128, 0) == 17  # the first AMS HT
    assert slot_of_tray(135, 0) == 24
    assert slot_of_global(255) == 25  # the H2D's second external spool
    assert slot_of_tray(4, 0) is None
    assert loaded_slot("255") is None  # nothing loaded


class TestFilamentSlots:
    def test_ams_mapping_names_each_filaments_tray(self):
        status = idle_status(ams_mapping=[2, 254, 5])

        assert filament_slots([1, 2, 3], status) == {1: 3, 2: 0, 3: 6}

    def test_ams_mapping2_names_unit_and_tray(self):
        status = idle_status(
            ams_mapping2=[{"ams_id": 1, "slot_id": 2}, {"ams_id": 255, "slot_id": 0}]
        )

        assert filament_slots([1, 2], status) == {1: 7, 2: 0}

    def test_one_filament_without_a_mapping_is_the_loaded_tray(self):
        status = idle_status()
        status["ams"]["tray_now"] = "1"

        assert filament_slots([1], status) == {1: 2}

    def test_one_filament_with_nothing_loaded_is_the_external_spool(self):
        assert filament_slots([1], idle_status()) == {1: 0}

    def test_several_filaments_without_a_mapping_are_trays_in_order(self):
        assert filament_slots([1, 3], idle_status()) == {1: 1, 3: 3}

    def test_several_filaments_without_an_ams_all_feed_from_the_external_spool(self):
        status = idle_status(ams={"ams": [], "tray_now": "254"})

        assert filament_slots([1, 2], status) == {1: 0, 2: 0}


def test_readings_of_each_tray_and_the_external_spool():
    found = readings(idle_status())

    assert found[1] == TrayReading("A1B2C3D4E5F60708", "PLA", "#FF0000")
    assert found[2] == TrayReading(None, "PETG", "#00FF00")  # no RFID tag
    assert found[3].empty and found[4].empty
    assert found[0].empty  # nothing on the external holder
    assert slot_count(idle_status()) == 5


def test_an_h2d_reports_its_second_external_spool():
    status = idle_status(
        vir_slot=[tray("254", "PLA", "FFFFFFFF", "0"), tray("255", "ABS", "000000FF", "0")]
    )

    assert readings(status)[25] == TrayReading(None, "ABS", "#000000")
    assert slot_count(status) == 26
