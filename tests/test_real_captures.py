"""The Moonraker adapter against what real printers answered (tests/fixtures/moonraker, REAL).

A Snapmaker U1 (Klipper 2.0.0.205, Moonraker 2.0.0, Snapmaker's forks) and an Infimech EX (the
vendor's Moonraker, reporting 9.9.9), captured 2026-10-12.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from captures import INFIMECH, U1, capture, job, moonraker_like
from fake_moonraker import eventually
from session_helpers import Recorder as SessionRecorder
from session_helpers import running
from test_moonraker_spools import Recorder

from printlog_bridge.adapters.moonraker.images import largest_first, snapshot_url
from printlog_bridge.adapters.moonraker.jobs import finished, started
from printlog_bridge.adapters.moonraker.session import Backoff, Listeners, MoonrakerSession
from printlog_bridge.adapters.moonraker.spools import SpoolWatcher
from printlog_bridge.adapters.moonraker.u1_rfid import U1RfidReader
from printlog_bridge.core.events import JobOutcome, SpoolReading


class TestU1RfidReader:
    """`filament_detect` as a U1 reports it: two Snapmaker spools in T0 and T1, T2 and T3
    empty. The colors match the slicer's for the same spools in the U1's history."""

    def readings(self) -> list[SpoolReading]:
        return U1RfidReader("u1").update(capture(U1, "status")["status"]["filament_detect"])

    def test_a_tag_read_is_its_card_uid_as_hex_with_material_and_color(self):
        first, second, _, _ = self.readings()

        assert first == SpoolReading(
            "u1", 0, tag="A05D1C3F", material="PLA", color_hex="#E2DEDB", slot_count=4
        )  # CARD_UID [160, 93, 28, 63]; PLA SnapSpeed, #E2DEDB in the slicer
        assert second == SpoolReading(
            "u1", 1, tag="909DB5E2", material="PLA", color_hex="#D93B90", slot_count=4
        )  # CARD_UID [144, 157, 181, 226]; PLA Full Spectrum, #D93B90 in the slicer

    def test_an_empty_slot_with_vendor_none_and_card_uid_0_is_an_empty_reading(self):
        _, _, third, fourth = self.readings()

        assert third == SpoolReading("u1", 2, slot_count=4)
        assert fourth == SpoolReading("u1", 3, slot_count=4)

    def test_the_same_status_again_is_no_new_reading(self):
        reader = U1RfidReader("u1")
        detect = capture(U1, "status")["status"]["filament_detect"]
        reader.update(detect)

        assert reader.update(detect) == []


@contextlib.asynccontextmanager
async def session_on(printer: str, listener):
    moonraker = moonraker_like(printer)
    base = await moonraker.start()
    session = MoonrakerSession(
        printer, url=base, listener=listener, backoff=Backoff(initial=0.01, maximum=0.05)
    )
    try:
        async with running(session):
            await eventually(lambda: moonraker.subscriptions)
            yield moonraker
    finally:
        await moonraker.close()


class TestObjects:
    def subscribed(self, printer: str) -> tuple[dict, dict]:
        recorder = SessionRecorder()

        async def scenario():
            async with session_on(printer, recorder) as moonraker:
                await eventually(lambda: recorder.status)
                return moonraker.subscriptions[0]

        return asyncio.run(scenario()), recorder.status

    def test_the_u1s_rfid_reader_is_subscribed_to(self):
        objects, status = self.subscribed(U1)

        assert set(objects) == {
            "print_stats", "toolhead", "virtual_sdcard", "webhooks", "filament_detect"
        }  # fmt: skip
        assert len(status["filament_detect"]["info"]) == 4
        assert status["toolhead"]["extruder"] == "extruder"

    def test_the_infimechs_filament_manager_is_left_alone(self):
        objects, status = self.subscribed(INFIMECH)

        assert set(objects) == {"print_stats", "toolhead", "virtual_sdcard", "webhooks"}
        assert "filament_manager" not in status

    @pytest.mark.parametrize(("printer", "tools"), [(U1, 4), (INFIMECH, 1)])
    def test_the_tool_count_is_its_extruders(self, printer, tools):
        # extruder..extruder3 on the U1, but not `tmc2209 extruder`, `extruder_offset_calibration`
        # or `filament_feed left`; one extruder on the Infimech.
        spools = Recorder()
        watcher = SpoolWatcher(printer, spools)

        async def scenario():
            async with session_on(printer, Listeners(watcher)) as moonraker:
                await moonraker.respond("// printlog: SET_SPOOL TOOL=0 ID=spool-1")
                await eventually(lambda: spools.commands)

        asyncio.run(scenario())

        assert spools.commands[0].slot_count == tools


class TestU1History:
    def test_a_four_filament_file_estimates_each_tool_it_uses(self):
        # Moonraker 2.0.0 on the U1 lists the slicer's grams per filament as `filament_weight`.
        start = started("u1", job(U1, "00000C"))

        assert start.estimated_filament_g_by_slot == {0: 54.22, 1: 67.09, 2: 20.69}
        assert start.estimated_filament_g == 142.0
        assert start.estimated_filament_mm == 44723.69

    def test_a_job_on_one_tool_of_four_is_that_tools(self):
        job_ = job(U1, "00000E")  # filament_weight [0.0, 0.0, 0.0, 40.5]: printed with T3

        assert started("u1", job_).estimated_filament_g_by_slot == {3: 40.5}
        assert finished("u1", job_).filament_mm == {3: pytest.approx(12753.98133)}

    def test_a_job_finished_while_away_is_split_by_the_estimate(self):
        end = finished("u1", job(U1, "00000C"))

        assert end.outcome is JobOutcome.COMPLETED
        assert sorted(end.filament_mm) == [0, 1, 2]
        assert sum(end.filament_mm.values()) == pytest.approx(45425.51351)
        assert end.filament_mm[1] > end.filament_mm[0] > end.filament_mm[2]

    def test_a_cancelled_job(self):
        assert finished("u1", job(U1, "00000B")).outcome is JobOutcome.CANCELLED

    def test_the_largest_thumbnail_is_tried_first(self):
        thumbnails = job(U1, "00000E")["metadata"]["thumbnails"]

        assert largest_first(thumbnails)[0]["relative_path"].endswith("-300x300.png")


class TestInfimechHistory:
    def test_a_single_filament_job_is_all_slot_0(self):
        job_ = job(INFIMECH, "00001D")  # nozzle_diameter is a string here: "0.4"

        start = started("infimech", job_)
        assert start.estimated_filament_g_by_slot == {}
        assert start.estimated_filament_g == 0.84
        assert finished("infimech", job_).filament_mm == {0: pytest.approx(328.74756)}

    def test_the_largest_thumbnail_is_tried_first(self):
        thumbnails = job(INFIMECH, "00001D")["metadata"]["thumbnails"]

        assert largest_first(thumbnails)[0]["relative_path"].endswith("-430x344.png")

    def test_the_webcam_snapshot_is_on_the_printers_web_port(self):
        [webcam] = capture(INFIMECH, "webcams")["webcams"]

        assert snapshot_url(webcam["snapshot_url"], printer_url="http://ex.local:7125") == (
            "http://ex.local/webcam5/snapshot"
        )
