"""Each print carries a stable identity of its file (#40), so 3D Print Log can tell a reprint of
the same file from a print of a re-sliced one."""

from __future__ import annotations

import re
from dataclasses import asdict

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


class TestFromMoonrakersMetadata:
    def test_is_20_bytes_of_lowercase_hex(self):
        assert re.fullmatch(r"[0-9a-f]{40}", identity(uuid=UUID, size=SIZE, modified=MODIFIED))

    def test_two_jobs_of_the_same_file_get_the_same_identity(self):
        first = job_started("voron", job("000001", 1_791_000_000.0, uuid=UUID, size=SIZE,
                                         modified=MODIFIED))  # fmt: skip
        again = job_started("voron", job("000002", 1_791_090_000.0, uuid=UUID, size=SIZE,
                                         modified=MODIFIED))  # fmt: skip

        assert first.file_identity == again.file_identity

    def test_a_resliced_file_gets_a_new_identity(self):
        # Moonraker extracts a new file's metadata, and gives it a new uuid, on every upload.
        before = identity(uuid=UUID, size=SIZE, modified=MODIFIED)
        after = identity(uuid=RESLICED_UUID, size=SIZE + 812, modified=MODIFIED + 3600)

        assert before != after

    def test_is_the_uuids_when_moonraker_gives_one(self):
        # The uuid already names one extraction of one file; size and time add nothing to it.
        assert identity(uuid=UUID, size=SIZE, modified=MODIFIED) == identity(uuid=UUID)

    def test_is_the_same_whatever_the_uuids_case_or_surrounding_space(self):
        assert identity(uuid=f" {UUID.upper()} ") == identity(uuid=UUID)

    def test_does_not_send_the_uuid_itself(self):
        found = identity(uuid=UUID)

        assert found is not None
        assert UUID.replace("-", "") not in found


class TestWithoutAUuid:
    """Older Moonraker history entries, and some forks, have no uuid: the file's size and
    modified time identify it instead. Re-slicing and uploading rewrites both."""

    def test_two_jobs_of_the_same_file_get_the_same_identity(self):
        first = job_started("voron", job("000001", size=SIZE, modified=MODIFIED))
        again = job_started("voron", job("000002", 1_791_090_000.0, size=SIZE, modified=MODIFIED))

        assert first.file_identity is not None
        assert first.file_identity == again.file_identity

    def test_a_resliced_file_gets_a_new_identity(self):
        assert identity(size=SIZE, modified=MODIFIED) != identity(
            size=SIZE + 812, modified=MODIFIED + 3600
        )

    def test_a_file_uploaded_again_gets_a_new_identity_even_at_the_same_size(self):
        assert identity(size=SIZE, modified=MODIFIED) != identity(size=SIZE, modified=MODIFIED + 1)

    def test_a_file_changed_in_place_gets_a_new_identity(self):
        assert identity(size=SIZE, modified=MODIFIED) != identity(size=SIZE + 1, modified=MODIFIED)

    def test_is_never_the_identity_of_a_file_with_that_uuid(self):
        assert identity(size=SIZE, modified=MODIFIED) != identity(uuid=UUID)

    def test_an_integer_modified_time_is_the_same_time(self):
        assert identity(size=SIZE, modified=1_706_359_465) == identity(
            size=SIZE, modified=1_706_359_465.0
        )

    def test_without_size_or_modified_time_there_is_none(self):
        assert identity() is None
        assert identity(size=SIZE) is None
        assert identity(modified=MODIFIED) is None

    def test_values_that_are_not_numbers_are_no_identity(self):
        assert identity(size="1629418", modified=MODIFIED) is None
        assert identity(size=SIZE, modified=True) is None
        assert identity(size=SIZE, modified=float("nan")) is None
        assert identity(uuid="", size=SIZE, modified=MODIFIED) == identity(
            size=SIZE, modified=MODIFIED
        )


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
