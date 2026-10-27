"""A Bambu job's 3MF: finding it over FTPS (against `tests/fake_bambu.py`) and reading it."""

from __future__ import annotations

import io

import pytest
from fake_bambu import ACCESS_CODE, PNG, FakeFtps, certificate, make_3mf

from printlog_bridge.adapters.bambu.ftps import BambuFtps, FilesRefused, FileTooLarge, candidates
from printlog_bridge.adapters.bambu.threemf import Filament, ThreeMF, plate_of
from printlog_bridge.adapters.bambu.tls import CertificateChanged


@pytest.fixture
def ftps(tmp_path_factory):
    server = FakeFtps(*certificate(tmp_path_factory.mktemp("tls")))
    server.start()
    yield server
    server.close()


def client(server: FakeFtps, password: str = ACCESS_CODE, **options) -> BambuFtps:
    return BambuFtps("127.0.0.1", password, port=server.port, timeout=5, **options)


class TestCandidates:
    def test_a_lan_print_of_a_sliced_file(self):
        assert candidates("36mm", "36mm.gcode.3mf") == [
            "/cache/36mm.gcode.3mf",
            "/36mm.gcode.3mf",
            "/cache/36mm.3mf",
            "/36mm.3mf",
        ]

    def test_a_cloud_print_names_a_plate_on_the_ram_disk_which_is_skipped(self):
        assert candidates("Shadowbox", "/data/Metadata/plate_3.gcode") == [
            "/cache/Shadowbox.gcode.3mf",
            "/Shadowbox.gcode.3mf",
            "/cache/Shadowbox.3mf",
            "/Shadowbox.3mf",
        ]

    def test_a_full_path_is_tried_first(self):
        assert candidates("", "/sdcard/parts/Box.gcode.3mf")[:2] == [
            "/sdcard/parts/Box.gcode.3mf",
            "/cache/Box.gcode.3mf",
        ]


class TestFetch:
    def test_finds_the_file_in_the_cache_and_reads_it(self, ftps):
        ftps.files["/cache/Benchy.3mf"] = make_3mf()

        path, file = client(ftps).fetch(candidates("Benchy", "/data/Metadata/plate_1.gcode"))

        with file:
            assert path == "/cache/Benchy.3mf"
            assert file.read() == ftps.files["/cache/Benchy.3mf"]

    def test_the_fetched_file_opens_as_a_3mf(self, ftps):
        ftps.files["/Box.gcode.3mf"] = make_3mf()

        _, file = client(ftps).fetch(["/Box.gcode.3mf"])

        three = ThreeMF(file)
        assert three.plate(None) is not None
        three.close()

    def test_nothing_found_is_none(self, ftps):
        assert client(ftps).fetch(["/cache/missing.3mf"]) is None

    def test_a_wrong_access_code_is_refused(self, ftps):
        with pytest.raises(FilesRefused):
            client(ftps, password="00000000").fetch(["/a.3mf"])

    def test_a_file_over_the_limit_isnt_fetched(self, ftps):
        ftps.files["/big.3mf"] = b"x" * 2000

        with pytest.raises(FileTooLarge):
            client(ftps).fetch(["/big.3mf"], max_bytes=1000)
        assert ftps.retrieved == []

    def test_the_certificate_is_checked_before_logging_in(self, ftps):
        def refuse(der: bytes) -> None:
            assert der
            raise CertificateChanged("no")

        with pytest.raises(CertificateChanged):
            client(ftps, verify=refuse).probe()
        assert ftps.logins == 0

    def test_probe_logs_in(self, ftps):
        client(ftps).probe()
        assert ftps.logins == 1


class TestThreeMF:
    def test_reads_each_plates_filaments(self):
        three = ThreeMF(
            io.BytesIO(
                make_3mf({1: [(1, "PLA", 4.0, 12.0)], 2: [(1, "PLA", 1, 3), (3, "PETG", 2, 6)]})
            )
        )

        plate = three.plate(2)
        assert plate is not None
        assert plate.filaments == (
            Filament(1, "PLA", "#FF0000", 3.0, 1.0),
            Filament(3, "PETG", "#FF0000", 6.0, 2.0),
        )
        assert plate.prediction_s == 3600
        assert plate.weight_g == 9.0

    def test_with_one_plate_the_plate_needs_no_number(self):
        three = ThreeMF(io.BytesIO(make_3mf()))

        assert three.plate(None) == three.plate(1)
        assert three.thumbnail(None) == PNG

    def test_with_several_plates_an_unknown_plate_is_none(self):
        three = ThreeMF(io.BytesIO(make_3mf({1: [], 2: []})))

        assert three.plate(None) is None
        assert three.thumbnail(None) is None

    def test_reads_the_project_settings(self):
        three = ThreeMF(io.BytesIO(make_3mf(settings={"layer_height": "0.16"})))

        assert three.settings() == {"layer_height": "0.16"}

    def test_the_plate_comes_from_gcode_file_or_plate_idx(self):
        assert plate_of("/data/Metadata/plate_3.gcode") == 3
        assert plate_of("Box.gcode.3mf", 2) == 2
        assert plate_of("Box.gcode.3mf", "0") is None
        assert plate_of("", None) is None
