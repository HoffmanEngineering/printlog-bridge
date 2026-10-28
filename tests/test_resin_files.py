"""`adapters/common/resin_files.py`: the slicer's resin volume, layer count and print time from a
sliced resin file's header (#97).

The files here are built from the documented layouts (see the module), not copied from real
files: only the fields the reader uses are filled in, the rest left zero.
"""

from __future__ import annotations

import struct

import pytest

from printlog_bridge.adapters.common.resin_files import (
    ResinFile,
    read_resin_file,
)

CTB_MAGIC = 0x12FD0086
CBDDLP_MAGIC = 0x12FD0019
CTB_V4_MAGIC = 0x12FD0106
CTB_GKTWO_MAGIC = 0xFF220810
CTB_ENCRYPTED_MAGIC = 0x12FD0107


def ctb(
    magic: int = CTB_MAGIC,
    version: int = 3,
    layers: int = 1234,
    print_time_s: int = 7200,
    volume_ml: float | None = 41.5,
    params_at: int = 400,
) -> bytes:
    """A Chitu file: the 112-byte header, then the print parameters at `params_at`."""
    data = bytearray(params_at + 60 if volume_ml is not None else 112)
    struct.pack_into("<II", data, 0, magic, version)
    struct.pack_into("<f", data, 32, 0.05)  # layer height
    struct.pack_into("<I", data, 68, layers)
    struct.pack_into("<I", data, 76, print_time_s)
    if volume_ml is not None:
        struct.pack_into("<II", data, 84, params_at, 60)
        struct.pack_into("<f", data, params_at + 20, volume_ml)
        struct.pack_into("<f", data, params_at + 24, volume_ml * 1.1)  # grams, unused
    return bytes(data)


GOO_LAYERS_AT = 195310
GOO_TIME_AT = 195446
GOO_VOLUME_AT = 195450


def goo(
    version: bytes = b"V3.0",
    layers: int = 2048,
    print_time_s: int = 9000,
    volume_ml: float = 63.25,
    endian: str = ">",
) -> bytes:
    data = bytearray(195477 + 16)
    data[0:4] = version
    data[4:12] = bytes([0x07, 0, 0, 0, 0x44, 0x4C, 0x50, 0])
    data[12:21] = b"CHITUBOX\0"
    struct.pack_into(endian + "I", data, GOO_LAYERS_AT, layers)
    struct.pack_into(endian + "I", data, GOO_TIME_AT, print_time_s)
    struct.pack_into(endian + "f", data, GOO_VOLUME_AT, volume_ml)
    struct.pack_into(endian + "I", data, 195470, 195477)  # where the layers start
    return bytes(data)


def anycubic(
    version: int = 516,
    layers: int = 812,
    print_time_s: int = 5400,
    volume_ml: float = 22.75,
    header_at: int = 64,
    layerdef_at: int = 400,
) -> bytes:
    data = bytearray(layerdef_at + 32)
    data[0:8] = b"ANYCUBIC"
    struct.pack_into("<IIII", data, 12, version, 8, header_at, 0)
    struct.pack_into("<I", data, 36, layerdef_at)
    data[header_at : header_at + 6] = b"HEADER"
    struct.pack_into("<I", data, header_at + 12, 84)
    struct.pack_into("<f", data, header_at + 20, 0.05)  # layer height
    struct.pack_into("<f", data, header_at + 52, volume_ml)
    struct.pack_into("<I", data, header_at + 84, print_time_s)
    data[layerdef_at : layerdef_at + 8] = b"LAYERDEF"
    struct.pack_into("<II", data, layerdef_at + 12, 4, layers)
    return bytes(data)


class TestChitu:
    @pytest.mark.parametrize("version", [2, 3, 4, 5])
    def test_a_ctb_file_gives_its_volume_layers_and_time(self, version):
        info = read_resin_file(ctb(version=version))

        assert info == ResinFile("ctb", pytest.approx(41.5), 1234, 7200.0)

    @pytest.mark.parametrize("magic", [CBDDLP_MAGIC, CTB_V4_MAGIC, CTB_GKTWO_MAGIC])
    def test_the_other_chitu_magics_read_the_same(self, magic):
        info = read_resin_file(ctb(magic=magic))

        assert info is not None and info.volume_ml == pytest.approx(41.5)

    def test_a_file_without_print_parameters_has_no_volume(self):
        """A version 1 file (`.photon`) has none."""
        info = read_resin_file(ctb(version=1, volume_ml=None))

        assert info == ResinFile("ctb", None, 1234, 7200.0)

    def test_print_parameters_past_the_bytes_read_give_no_volume(self):
        data = ctb(params_at=4000)

        info = read_resin_file(data[:1000])

        assert info == ResinFile("ctb", None, 1234, 7200.0)

    def test_an_encrypted_ctb_is_not_read(self):
        """Chitubox's encrypted CTB (v4 and v5 from recent Chitubox) keeps its settings
        AES-encrypted with Chitubox's own key: the bridge doesn't decrypt it."""
        data = bytearray(ctb())
        struct.pack_into("<I", data, 0, CTB_ENCRYPTED_MAGIC)

        assert read_resin_file(bytes(data)) is None


class TestGoo:
    def test_a_goo_file_gives_its_volume_layers_and_time(self):
        assert read_resin_file(goo()) == ResinFile("goo", 63.25, 2048, 9000.0)

    def test_a_goo_v5_file_is_little_endian(self):
        data = goo(version=b"V5.1", endian="<")

        assert read_resin_file(data) == ResinFile("goo", 63.25, 2048, 9000.0)

    def test_a_goo_file_cut_short_of_its_header_is_not_read(self):
        assert read_resin_file(goo()[:GOO_VOLUME_AT]) is None


class TestAnycubic:
    @pytest.mark.parametrize("version", [1, 515, 516, 517, 518])
    def test_a_photon_workshop_file_gives_its_volume_layers_and_time(self, version):
        info = read_resin_file(anycubic(version=version))

        assert info == ResinFile("anycubic", 22.75, 812, 5400.0)

    def test_an_unknown_version_is_not_read(self):
        assert read_resin_file(anycubic(version=600)) is None

    def test_a_header_table_with_another_name_is_not_read(self):
        data = bytearray(anycubic())
        data[64:70] = b"PREVIE"

        assert read_resin_file(bytes(data)) is None

    def test_layers_past_the_bytes_read_are_left_out(self):
        info = read_resin_file(anycubic(layerdef_at=4000)[:1000])

        assert info == ResinFile("anycubic", 22.75, None, 5400.0)


class TestNonsense:
    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"\x00" * 10,
            b"; generated by PrusaSlicer\nG28\n" * 20,
            b"PK\x03\x04" + b"\x00" * 300,  # a zip: an SL1 or a 3MF
            bytes(range(256)) * 4,
            ctb()[:111],
            anycubic()[:40],
        ],
    )
    def test_anything_else_is_none(self, data):
        assert read_resin_file(data) is None

    def test_values_out_of_any_sensible_range_are_left_out(self):
        info = read_resin_file(ctb(layers=0, print_time_s=10**9, volume_ml=float("nan")))

        assert info is None

    def test_a_negative_volume_is_left_out_but_the_rest_kept(self):
        info = read_resin_file(goo(volume_ml=-3.0))

        assert info == ResinFile("goo", None, 2048, 9000.0)

    def test_offsets_pointing_anywhere_never_raise(self):
        data = bytearray(anycubic())
        struct.pack_into("<I", data, 20, 0xFFFFFFF0)
        struct.pack_into("<I", data, 36, 0xFFFFFFF0)

        assert read_resin_file(bytes(data)) is None

    def test_a_memoryview_reads_like_bytes(self):
        assert read_resin_file(memoryview(goo())) == read_resin_file(goo())
