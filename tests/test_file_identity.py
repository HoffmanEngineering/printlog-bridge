"""Each print carries a stable identity of its file (#40), so 3D Print Log can tell a reprint of
the same file from a print of a re-sliced one."""

from __future__ import annotations

import re
from dataclasses import asdict

from captures import INFIMECH, U1, capture
from fake_printlog_api import FakePrintLogApi
from test_job_logger import STARTED_AT, finished, run, started

from printlog_bridge.adapters.moonraker.jobs import started as job_started
from printlog_bridge.core.state import JobRecord, PrinterState

UUID = "473a41d2-15f2-434c-aa61-d1d6ec5a4bfd"
RESLICED_UUID = "0b5e9f6a-3c1d-4e2f-9a8b-7c6d5e4f3a2b"
SIZE = 1_629_418
MODIFIED = 1_706_359_465.4947228
IDENTITY = "5c0ffee5" * 5


def job(job_id: str = "000001", start_time: float = 1_791_000_000.0, **metadata) -> dict:
    return {
        "job_id": job_id,
        "filename": "parts/bracket.gcode",
        "status": "in_progress",
        "start_time": start_time,
        "metadata": metadata,
    }


def identity(**metadata) -> str | None:
    return job_started("voron", job(**metadata)).file_identity


def real_identities(printer: str) -> dict[str, str | None]:
    return {
        j["job_id"]: job_started("voron", j).file_identity
        for j in capture(printer, "history")["jobs"]
    }


class TestRealPrinters:
    """Real history (tests/fixtures/moonraker): Moonraker gave one file a new uuid between two
    of its jobs, so the uuid can't be what identifies it."""

    def test_two_u1_jobs_of_one_file_share_its_identity_though_their_uuids_differ(self):
        found = real_identities(U1)

        assert found["00000B"] is not None
        assert found["00000B"] == found["00000C"]  # Chillet_PLA_9h38m.gcode, twice
        others = [found[j] for j in ("00000A", "00000D", "00000E")]
        assert len({found["00000B"], *others}) == 4

    def test_the_infimechs_three_bolt_jobs_share_its_identity(self):
        found = real_identities(INFIMECH)

        assert found["00001A"] is not None
        assert found["00001A"] == found["00001C"] == found["00001D"]
        # A re-slice of the bolt (6m55s) and another file are other files.
        assert len({found["00001A"], found["000019"], found["00001B"]}) == 3


class TestFromSizeAndModifiedTime:
    """What Moonraker itself checks to tell whether a file changed. An upload always sets a new
    modified time, so a re-sliced file gets a new identity."""

    def test_is_20_bytes_of_lowercase_hex(self):
        assert re.fullmatch(r"[0-9a-f]{40}", identity(uuid=UUID, size=SIZE, modified=MODIFIED))

    def test_two_jobs_of_the_same_file_get_the_same_identity(self):
        first = job_started("voron", job("000001", size=SIZE, modified=MODIFIED))
        again = job_started("voron", job("000002", 1_791_090_000.0, size=SIZE, modified=MODIFIED))

        assert first.file_identity is not None
        assert first.file_identity == again.file_identity

    def test_ignores_the_uuid_which_moonraker_may_give_one_file_anew(self):
        assert identity(uuid=UUID, size=SIZE, modified=MODIFIED) == identity(
            uuid=RESLICED_UUID, size=SIZE, modified=MODIFIED
        )
        assert identity(uuid=UUID, size=SIZE, modified=MODIFIED) == identity(
            size=SIZE, modified=MODIFIED
        )

    def test_a_resliced_file_gets_a_new_identity(self):
        before = identity(uuid=UUID, size=SIZE, modified=MODIFIED)
        after = identity(uuid=RESLICED_UUID, size=SIZE + 812, modified=MODIFIED + 3600)

        assert before != after

    def test_a_file_uploaded_again_gets_a_new_identity_even_at_the_same_size(self):
        assert identity(size=SIZE, modified=MODIFIED) != identity(size=SIZE, modified=MODIFIED + 1)

    def test_a_file_changed_in_place_gets_a_new_identity(self):
        assert identity(size=SIZE, modified=MODIFIED) != identity(size=SIZE + 1, modified=MODIFIED)

    def test_an_integer_modified_time_is_the_same_time(self):
        assert identity(size=SIZE, modified=1_706_359_465) == identity(
            size=SIZE, modified=1_706_359_465.0
        )


class TestWithoutSizeOrModifiedTime:
    """A history entry that lacks either falls back on Moonraker's uuid, and else has none."""

    def test_the_uuid_identifies_the_file(self):
        assert identity(uuid=UUID) is not None
        assert identity(uuid=UUID, size=SIZE) == identity(uuid=UUID, modified=MODIFIED)
        assert identity(uuid=UUID) != identity(uuid=RESLICED_UUID)

    def test_is_the_same_whatever_the_uuids_case_or_surrounding_space(self):
        assert identity(uuid=f" {UUID.upper()} ") == identity(uuid=UUID)

    def test_does_not_send_the_uuid_itself(self):
        assert UUID.replace("-", "") not in identity(uuid=UUID)

    def test_is_never_the_identity_of_a_file_by_its_size_and_time(self):
        assert identity(uuid=UUID) != identity(size=SIZE, modified=MODIFIED)

    def test_without_a_uuid_either_there_is_none(self):
        assert identity() is None
        assert identity(size=SIZE) is None
        assert identity(modified=MODIFIED) is None
        assert identity(uuid="  ", size=SIZE) is None

    def test_values_that_are_not_numbers_count_as_missing(self):
        assert identity(size="1629418", modified=MODIFIED) is None
        assert identity(size=SIZE, modified=True) is None
        assert identity(size=SIZE, modified=float("nan")) is None
        assert identity(uuid=UUID, size="1629418", modified=MODIFIED) == identity(uuid=UUID)


class TestSent:
    def test_a_new_print_carries_it_as_its_file_hash(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started(file_identity=IDENTITY))

        [created] = api.created
        assert created["fileHash"] == IDENTITY

    def test_without_one_no_file_hash_is_sent(self, tmp_path):
        api = FakePrintLogApi()

        run(api, tmp_path, started())

        [created] = api.created
        assert "fileHash" not in created

    def test_an_adopted_print_is_claimed_with_it(self, tmp_path):
        api = FakePrintLogApi()
        print_id = api.user_creates("Shelf bracket", file_name="shelf_bracket.gcode")

        run(api, tmp_path, started(file_name="shelf_bracket.gcode", file_identity=IDENTITY))

        [(claimed, body)] = api.claims
        assert claimed == print_id
        assert body["fileHash"] == IDENTITY
        assert api.prints[print_id]["fileHash"] == IDENTITY

    def test_a_start_queued_before_the_bridge_knew_of_it_is_still_logged(self, tmp_path):
        # A job recorded by an older bridge has no identity in its saved start.
        state = PrinterState.load(tmp_path, "voron")
        saved = {**asdict(started()), "started_at": STARTED_AT.isoformat()}
        del saved["file_identity"]
        state.jobs["00000A"] = JobRecord(started_at=STARTED_AT.isoformat(), start=saved)
        state.save()
        api = FakePrintLogApi()

        run(api, tmp_path, started(), finished())

        [created] = api.created
        assert "fileHash" not in created
        assert len(api.completions) == 1
