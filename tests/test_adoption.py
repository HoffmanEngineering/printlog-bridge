"""Adopting a print the user made themselves instead of creating a duplicate (#43)."""

from __future__ import annotations

import asyncio
import logging

import pytest
from fake_printlog_api import PRINTING, FakePrintLogApi
from test_job_logger import PNG, drain, finished, image, logging_to, run, started

from printlog_bridge.core.config import Adoption
from printlog_bridge.core.lifecycle import _usage_rows
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.titles import same_file

FINISH_ONLY = Adoption(create=False)


def uploaded(api: FakePrintLogApi, **kwargs) -> int:
    """What the Slic3r uploader makes: a print in Printing, with the slicer's estimate on a row
    with no slot."""
    kwargs.setdefault("file_name", "shelf_bracket_0.2mm_PLA_MK4_1h2m.gcode")
    kwargs.setdefault("estimatedPrintTimeInSeconds", 3900)
    kwargs.setdefault("usage", [{"estimatedAmountMg": 13000, "estimatedSource": 1}])
    return api.user_creates("Shelf bracket (uploaded)", **kwargs)


class TestAdopting:
    def test_an_uploaded_print_is_adopted_and_completed_instead_of_duplicated(
        self, tmp_path, caplog
    ):
        api = FakePrintLogApi()
        spool = api.add_spool()
        row = {"estimatedAmountMg": 13000, "estimatedSource": 1, "filament": {"id": spool}}
        print_id = uploaded(api, usage=[row])
        caplog.set_level(logging.INFO, "printlog_bridge")

        run(api, tmp_path, started(estimated_duration_s=3600), finished(), spools=True)

        assert list(api.prints) == [print_id]
        assert api.created == []
        print_ = api.prints[print_id]
        assert print_["status"] == 3
        assert print_["externalSource"] == "moonraker"
        assert print_["externalId"].endswith(":00000A")
        assert print_["title"] == "Shelf bracket (uploaded)"  # the user's, kept
        assert print_["estimatedPrintTimeInSeconds"] == 3900  # the uploader's, kept
        assert print_["printTimeInSeconds"] == 3725
        [row] = print_["filamentUsage"]
        assert row["filament"] == {"id": spool}
        assert row["estimatedAmountMg"] == 13000
        assert row["lengthInM"] == 4.3215
        assert f"job 00000A: adopted print {print_id} ('Shelf bracket (uploaded)')" in caplog.text

    def test_the_users_spool_wins_over_the_one_loaded(self, tmp_path):
        api = FakePrintLogApi()
        loaded, chosen = api.add_spool("Loaded"), api.add_spool("Chosen")
        api.user_loads(42, 0, loaded)
        print_id = uploaded(api, usage=[{"estimatedAmountMg": 1, "filament": {"id": chosen}}])

        run(api, tmp_path, started(), finished(), spools=True)

        [row] = api.prints[print_id]["filamentUsage"]
        assert row["filament"] == {"id": chosen}
        assert row["lengthInM"] == 4.3215

    def test_an_unslotted_row_with_no_spool_is_filled_not_duplicated(self, tmp_path):
        api = FakePrintLogApi()
        print_id = uploaded(api)

        run(api, tmp_path, started(), finished())

        [row] = api.prints[print_id]["filamentUsage"]
        assert row["estimatedAmountMg"] == 13000
        assert row["lengthInM"] == 4.3215

    def test_an_unslotted_row_takes_the_loaded_spool_and_is_filled(self, tmp_path):
        api = FakePrintLogApi()
        spool = api.add_spool()
        api.user_loads(42, 0, spool)
        print_id = uploaded(api)

        run(api, tmp_path, started(), finished(), spools=True)

        [row] = api.prints[print_id]["filamentUsage"]
        assert row["filament"] == {"id": spool}
        assert row["estimatedAmountMg"] == 13000
        assert row["lengthInM"] == 4.3215

    def test_a_multi_tool_print_fills_its_rows_by_slot(self, tmp_path):
        api = FakePrintLogApi()
        api.slot_counts[42] = 4
        print_id = uploaded(
            api,
            usage=[
                {"slot": 0, "estimatedAmountMg": 1000, "estimatedSource": 1},
                {"slot": 2, "estimatedAmountMg": 2000, "estimatedSource": 1},
            ],
        )

        run(api, tmp_path, started(), finished(filament_mm={0: 1000.0, 2: 2000.0}), spools=True)

        rows = {r["slot"]: r for r in api.prints[print_id]["filamentUsage"]}
        assert set(rows) == {0, 2}
        assert rows[0]["lengthInM"] == 1.0
        assert rows[2]["lengthInM"] == 2.0

    def test_the_newest_of_two_candidates_is_adopted(self, tmp_path, caplog):
        api = FakePrintLogApi()
        older = uploaded(api, created="2026-10-10T08:00:00+00:00")
        newer = uploaded(api, created="2026-10-10T09:15:00+00:00")
        caplog.set_level(logging.INFO, "printlog_bridge")

        run(api, tmp_path, started(), finished())

        assert api.prints[newer]["status"] == 3
        assert api.prints[older]["status"] == PRINTING
        assert api.prints[older]["externalId"] is None
        assert "2 prints could be adopted" in caplog.text

    def test_a_print_claimed_meanwhile_falls_back_to_the_next(self, tmp_path):
        api = FakePrintLogApi()
        older = uploaded(api, created="2026-10-10T08:00:00+00:00")
        newer = uploaded(api, created="2026-10-10T09:15:00+00:00")
        real_claimable = api._claimable

        async def racing(request):
            answer = await real_claimable(request)
            api.prints[newer]["externalId"] = "octoprint:other"  # claimed before our claim
            return answer

        api._claimable = racing

        run(api, tmp_path, started(), finished())

        assert api.prints[older]["status"] == 3
        assert api.created == []

    def test_a_print_finished_meanwhile_falls_back_to_the_next(self, tmp_path):
        api = FakePrintLogApi()
        older = uploaded(api, created="2026-10-10T08:00:00+00:00")
        newer = uploaded(api, created="2026-10-10T09:15:00+00:00")
        real_claimable = api._claimable

        async def racing(request):
            answer = await real_claimable(request)
            api.prints[newer]["status"] = 3  # the user marked it done before our claim
            return answer

        api._claimable = racing

        run(api, tmp_path, started(), finished())

        assert api.prints[older]["status"] == 3
        assert api.prints[newer]["externalId"] is None

    def test_a_restart_mid_job_finishes_the_adopted_print(self, tmp_path):
        api = FakePrintLogApi()
        print_id = uploaded(api)

        run(api, tmp_path, started())
        run(api, tmp_path, started(), finished())  # the bridge restarted; history replayed

        assert list(api.prints) == [print_id]
        assert api.prints[print_id]["status"] == 3
        assert len(api.claims) == 1

    def test_a_claim_whose_answer_was_lost_is_sent_again_not_duplicated(self, tmp_path):
        api = FakePrintLogApi()
        print_id = uploaded(api)
        run(api, tmp_path, lambda api: None)  # makes the state file
        state = PrinterState.load(tmp_path, "voron")
        # The claim reached the API, and the power went before the answer came back.
        api.prints[print_id]["externalSource"] = "moonraker"
        api.prints[print_id]["externalId"] = f"{state.instance_id}:00000A"
        run(api, tmp_path, started())
        job = PrinterState.load(tmp_path, "voron").jobs["00000A"]
        job.print_id, job.claiming, job.adopted = None, print_id, False
        state = PrinterState.load(tmp_path, "voron")
        state.jobs["00000A"] = job
        state.save()

        run(api, tmp_path, finished())

        assert list(api.prints) == [print_id]
        assert api.prints[print_id]["status"] == 3

    def test_a_job_already_logged_as_its_own_print_keeps_it(self, tmp_path):
        api = FakePrintLogApi()
        run(api, tmp_path, started())  # the bridge creates print 1 for the job
        [own] = api.prints
        theirs = uploaded(api, created="2026-10-10T09:20:00+00:00")
        # The power went before the print's id reached the state file.
        state = PrinterState.load(tmp_path, "voron")
        state.jobs["00000A"].print_id = None
        state.save()

        run(api, tmp_path, finished())

        assert api.prints[own]["status"] == 3
        assert api.prints[theirs]["status"] == PRINTING
        assert api.prints[theirs]["externalId"] is None

    def test_a_job_that_finished_while_the_bridge_was_away_adopts_too(self, tmp_path):
        api = FakePrintLogApi()
        print_id = uploaded(api)

        async def catch_up():  # history read at start: the start and the end queued together
            async with logging_to(api, tmp_path) as logger:
                try:
                    await logger.emit(started())
                    await logger.emit(finished())
                    await drain(logger)
                finally:
                    logger.close()

        asyncio.run(catch_up())

        assert list(api.prints) == [print_id]
        assert api.prints[print_id]["status"] == 3

    def test_the_thumbnail_does_not_replace_the_users_picture(self, tmp_path):
        api = FakePrintLogApi()
        print_id = uploaded(api, images=[{"id": 99, "isDefault": True, "displayOrder": 0}])

        run(api, tmp_path, started(), image())

        images = api.prints[print_id]["images"]
        assert len(images) == 2
        assert [i["isDefault"] for i in images] == [True, False]

    def test_without_a_picture_the_thumbnail_is_the_default(self, tmp_path):
        api = FakePrintLogApi()
        print_id = uploaded(api)

        run(api, tmp_path, started(), image())

        assert api.default_image(print_id) == PNG


class TestNotAdopting:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"file_name": "benchy.gcode"},
            {"printer_id": 43},
            {"externalSource": "octoprint", "externalId": "job-1"},
            {"created": "2026-10-09T21:00:00+00:00"},  # 12.5 hours before the job
            {"status": 3},
        ],
        ids=["other file", "other printer", "already claimed", "too old", "not printing"],
    )
    def test_a_print_that_does_not_match_gets_a_new_one(self, tmp_path, kwargs):
        api = FakePrintLogApi(printers=(42, 43))
        theirs = uploaded(api, **kwargs)

        run(api, tmp_path, started(), finished())

        assert len(api.prints) == 2
        assert api.prints[theirs]["externalId"] == kwargs.get("externalId")
        assert api.created

    def test_a_longer_window_adopts_an_older_print(self, tmp_path):
        api = FakePrintLogApi()
        print_id = uploaded(api, created="2026-10-09T21:00:00+00:00")

        run(api, tmp_path, started(), finished(), adoption=Adoption(window_s=24 * 60 * 60))

        assert list(api.prints) == [print_id]

    def test_with_adopt_prints_off_nothing_is_looked_up(self, tmp_path):
        api = FakePrintLogApi()
        uploaded(api)

        run(api, tmp_path, started(), finished(), adoption=Adoption(adopt=False))

        assert len(api.prints) == 2
        assert api.lookups == []

    def test_an_api_without_the_lookup_still_logs_the_job(self, tmp_path, caplog):
        api = FakePrintLogApi()
        api.lookup = False

        run(api, tmp_path, started(), finished())

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert "can't look up prints to adopt" in caplog.text


class TestFinishOnly:
    def test_a_matching_print_is_adopted(self, tmp_path):
        api = FakePrintLogApi()
        print_id = uploaded(api)

        run(api, tmp_path, started(), finished(), adoption=FINISH_ONLY)

        assert list(api.prints) == [print_id]
        assert api.prints[print_id]["status"] == 3

    def test_a_job_with_nothing_to_adopt_is_skipped_and_logged(self, tmp_path, caplog):
        api = FakePrintLogApi()
        caplog.set_level(logging.INFO, "printlog_bridge")

        run(api, tmp_path, started(), image(), finished(), adoption=FINISH_ONLY)

        assert api.prints == {}
        assert api.created == []
        assert "no print of yours to adopt, so it isn't logged" in caplog.text
        assert PrinterState.load(tmp_path, "voron").jobs["00000A"].finished

    def test_a_print_uploaded_after_the_job_started_is_adopted_when_it_ends(self, tmp_path):
        api = FakePrintLogApi()

        run(
            api,
            tmp_path,
            started(),
            lambda api: uploaded(api, created="2026-10-10T09:45:00+00:00"),
            finished(),
            adoption=FINISH_ONLY,
        )

        [print_] = api.prints.values()
        assert print_["status"] == 3
        assert print_["externalId"].endswith(":00000A")


class TestSameFile:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("parts/benchy_0.2mm_PLA_MK4_1h2m.gcode", "benchy.gcode"),
            ("benchy.bgcode", "Benchy.gcode.3mf"),
            ("shelf-bracket_PETG_2h5m.gcode", "Shelf Bracket"),
            (r"C:\prints\Shelf_Bracket.gcode", "shelf bracket.gcode"),
        ],
    )
    def test_matches(self, a, b):
        assert same_file(a, b)

    @pytest.mark.parametrize(
        ("a", "b"),
        [("benchy.gcode", "benchy_v2.gcode"), ("benchy.gcode", ""), ("a_b.gcode", "b_a.gcode")],
    )
    def test_differs(self, a, b):
        assert not same_file(a, b)


def test_a_bridge_made_print_keeps_its_slot_on_a_single_tool_printer():
    rows = [{"slot": 0, "filament": None}]

    usage = _usage_rows(finished(), rows, {}, single_tool=True)

    assert usage == [{"slot": 0, "lengthInM": 4.3215, "source": 2}]
