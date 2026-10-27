"""The vendored QR encoder, checked module for module against segno (a dev dependency only)."""

from __future__ import annotations

import io

import pytest

from printlog_bridge.core import qr

segno = pytest.importorskip("segno")

URL = "https://auth.3dprintlog.com/activate?user_code=QWER-TYUI"


def _segno_matrix(data: bytes, version: int, mask: int) -> list[list[bool]]:
    code = segno.make(data, error="m", version=version, mask=mask, mode="byte", boost_error=False)
    return [[bool(m) for m in row] for row in code.matrix]


@pytest.mark.parametrize("version", range(1, qr.MAX_VERSION + 1))
@pytest.mark.parametrize("mask", range(8))
def test_every_version_and_mask_matches_segno(version: int, mask: int) -> None:
    # A full code: segno pads a stream that ends on a byte boundary with an extra 0x00 codeword
    # (valid, but not the standard's pad), so shorter data is checked codeword by codeword below.
    data = bytes(65 + i % 58 for i in range(qr.capacity(version)))
    assert qr.encode(data, version=version, mask=mask) == _segno_matrix(data, version, mask)


def _read_codewords(matrix: list[list[bool]], version: int, mask: int) -> list[int]:
    """The codewords in `matrix`, read back in placement order and unmasked."""
    size = len(matrix)
    zeros = qr._draw(version, [0] * (qr._raw_modules(version) // 8), mask)
    ones = qr._draw(version, [255] * (qr._raw_modules(version) // 8), mask)
    bits = []
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vertical in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vertical if (right + 1) & 2 == 0 else vertical
                if zeros[y][x] != ones[y][x]:
                    bits.append(int(matrix[y][x] != zeros[y][x]))
        right -= 2
    return [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits) - 7, 8)]


def test_short_data_is_header_data_terminator_and_pads_with_segnos_error_correction() -> None:
    from segno import consts
    from segno.encoder import Buffer, make_blocks

    matrix = qr.encode(b"ABCDE")
    version = (len(matrix) - 17) // 4
    codewords = _read_codewords(matrix, version, _mask(matrix))
    data_length = qr._data_codewords(version)
    assert codewords[:9] == [0x40, 0x54, 0x14, 0x24, 0x34, 0x44, 0x50, 0xEC, 0x11]
    buffer = Buffer()
    for byte in codewords[:data_length]:
        buffer.append_bits(byte, 8)
    _, ecc = make_blocks(consts.ECC[version][consts.ERROR_LEVEL_M], buffer)
    assert codewords[data_length:] == list(ecc[0])


def _mask(matrix: list[list[bool]]) -> int:
    """The mask the format information names (bits 10 to 12, after unmasking with 0x5412)."""
    bits = sum(int(matrix[8][len(matrix) - 1 - i]) << i for i in range(8))
    bits |= sum(int(matrix[len(matrix) - 15 + i][8]) << i for i in range(8, 15))
    return ((bits ^ 0x5412) >> 10) & 0b111


def test_picks_the_smallest_version_that_fits() -> None:
    matrix = qr.encode(URL.encode())
    version = (len(matrix) - 17) // 4
    assert qr.capacity(version) >= len(URL) > qr.capacity(version - 1)


def test_picks_the_mask_that_scores_best() -> None:
    matrix = qr.encode(URL.encode())
    version = (len(matrix) - 17) // 4
    chosen = _mask(matrix)
    scores = [qr._penalty(qr.encode(URL.encode(), version=version, mask=m)) for m in range(8)]
    assert scores[chosen] == min(scores)
    assert matrix == qr.encode(URL.encode(), version=version, mask=chosen)


def test_too_long_is_refused() -> None:
    with pytest.raises(ValueError):
        qr.encode(b"x" * (qr.capacity(qr.MAX_VERSION) + 1))


def test_render_half_blocks_draws_the_light_modules() -> None:
    text = qr.render(URL, stream=io.StringIO())
    lines = text.splitlines()
    size = len(qr.encode(URL.encode())) + 2 * qr.QUIET_ZONE
    assert all(len(line) == size for line in lines)
    assert len(lines) == (size + 1) // 2
    # The quiet zone is light, so the first line is all full blocks.
    assert set(lines[0]) == {"█"}
    assert set("".join(lines)) <= {"█", "▀", "▄", " "}


class _Ascii(io.StringIO):
    encoding = "ascii"


def test_render_falls_back_to_ascii_when_the_stream_cant_show_blocks() -> None:
    text = qr.render(URL, stream=_Ascii())
    lines = text.splitlines()
    size = len(qr.encode(URL.encode())) + 2 * qr.QUIET_ZONE
    assert len(lines) == size
    assert all(len(line) == 2 * size for line in lines)
    assert set("".join(lines)) <= {"#", " "}
    text.encode("ascii")
