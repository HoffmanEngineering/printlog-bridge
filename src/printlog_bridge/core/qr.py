"""A QR code as text, for pairing (#101): scan the code in the log to approve the bridge.

A small encoder of its own rather than a dependency: the pairing URL is short, so byte mode, error
correction level M and versions 1 to 10 (up to 213 bytes) are all it needs, and the bridge runs on
Python 3.9 on a printer's Pi, where every dependency is one more thing to install. It follows
ISO/IEC 18004 as Project Nayuki's reference encoder lays it out; tests/test_qr.py checks every
version and mask against segno.

`render` draws it with half blocks, two rows of modules to a line, so it fits a terminal, `docker
logs` and an SSH session. The light modules are the ones drawn, for the usual dark terminal; phone
scanners read the code either way round.
"""

from __future__ import annotations

from typing import TextIO

MAX_VERSION = 10
QUIET_ZONE = 2
"""Light modules around the code. The standard asks for 4; 2 scans fine and saves lines."""

_FORMAT_BITS_M = 0
"""Error correction level M in the format information."""
_ECC_PER_BLOCK = (0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
"""Level M's error correction codewords per block, by version."""
_BLOCKS = (0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
"""Level M's number of blocks, by version."""
_PENALTY_RUN, _PENALTY_BOX, _PENALTY_FINDER, _PENALTY_BALANCE = 3, 3, 40, 10
_FINDER_LIKE = ((1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0), (0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1))

Matrix = list[list[bool]]
"""Rows of modules, True for dark."""


def capacity(version: int) -> int:
    """How many bytes a code of `version` holds at level M."""
    if version < 1:
        return 0
    count_bits = 8 if version < 10 else 16
    return (_data_codewords(version) * 8 - 4 - count_bits) // 8


def encode(data: bytes, *, version: int | None = None, mask: int | None = None) -> Matrix:
    """The QR code for `data`: the smallest version that holds it (or `version`), with the mask
    that scores best (or `mask`). Raises ValueError when it doesn't fit in version 10."""
    if version is None:
        version = next((v for v in range(1, MAX_VERSION + 1) if capacity(v) >= len(data)), None)
        if version is None:
            raise ValueError(f"{len(data)} bytes is too long for a QR code here")
    elif not 1 <= version <= MAX_VERSION or capacity(version) < len(data):
        raise ValueError(f"{len(data)} bytes doesn't fit version {version}")
    codewords = _add_ecc(_data(data, version), version)
    if mask is not None:
        return _draw(version, codewords, mask)
    return min((_draw(version, codewords, m) for m in range(8)), key=_penalty)


def render(text: str, stream: TextIO | None = None) -> str:
    """The QR code for `text`, as lines of text for `stream` (half blocks, or `#` when the
    stream's encoding can't show them)."""
    matrix = encode(text.encode("utf-8"))
    size = len(matrix) + 2 * QUIET_ZONE

    def light(y: int, x: int) -> bool:
        y, x = y - QUIET_ZONE, x - QUIET_ZONE
        inside = 0 <= y < len(matrix) and 0 <= x < len(matrix)
        return not (inside and matrix[y][x])

    if not _can_show(stream, "█▀▄"):
        return "\n".join(
            "".join("##" if light(y, x) else "  " for x in range(size)) for y in range(size)
        )
    lines = []
    for y in range(0, size, 2):
        line = ""
        for x in range(size):
            top, bottom = light(y, x), y + 1 < size and light(y + 1, x)
            line += "█" if top and bottom else "▀" if top else "▄" if bottom else " "
        lines.append(line)
    return "\n".join(lines)


SVG_QUIET_ZONE = 4
"""The standard's quiet zone, for the local page's QR code (core/local_page.py)."""


def svg(text: str) -> str:
    """The QR code for `text` as an SVG element: a white square with a black path of one unit
    square per dark module. It holds only numbers, so nothing in `text` reaches the markup."""
    matrix = encode(text.encode("utf-8"))
    size = len(matrix) + 2 * SVG_QUIET_ZONE
    path = "".join(
        f"M{x + SVG_QUIET_ZONE} {y + SVG_QUIET_ZONE}h1v1h-1z"
        for y, row in enumerate(matrix)
        for x, dark in enumerate(row)
        if dark
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'shape-rendering="crispEdges" role="img" aria-label="QR code">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path d="{path}" fill="#000"/></svg>'
    )


def _can_show(stream: TextIO | None, characters: str) -> bool:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        characters.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


# -- codewords -----------------------------------------------------------------------------------


def _raw_modules(version: int) -> int:
    """Modules left for data and error correction once the function patterns are drawn."""
    result = (16 * version + 128) * version + 64
    if version >= 2:
        aligns = version // 7 + 2
        result -= (25 * aligns - 10) * aligns - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version: int) -> int:
    return _raw_modules(version) // 8 - _ECC_PER_BLOCK[version] * _BLOCKS[version]


def _data(data: bytes, version: int) -> list[int]:
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(length)))

    put(0b0100, 4)  # byte mode
    put(len(data), 8 if version < 10 else 16)
    for byte in data:
        put(byte, 8)
    capacity_bits = _data_codewords(version) * 8
    put(0, min(4, capacity_bits - len(bits)))
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < capacity_bits:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    return [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]


def _add_ecc(data: list[int], version: int) -> list[int]:
    blocks_count, ecc_len = _BLOCKS[version], _ECC_PER_BLOCK[version]
    raw = _raw_modules(version) // 8
    short_blocks = blocks_count - raw % blocks_count
    short_len = raw // blocks_count
    divisor = _rs_divisor(ecc_len)
    blocks = []
    k = 0
    for i in range(blocks_count):
        length = short_len - ecc_len + (0 if i < short_blocks else 1)
        block = data[k : k + length]
        k += length
        ecc = _rs_remainder(block, divisor)
        if i < short_blocks:
            block = [*block, -1]  # a gap, so every block lines up for interleaving
        blocks.append(block + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if i != short_len - ecc_len or j >= short_blocks:
                result.append(block[i])
    return result


def _rs_multiply(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _rs_multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _rs_multiply(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= _rs_multiply(coefficient, factor)
    return result


# -- the matrix ----------------------------------------------------------------------------------


def _alignment_positions(version: int) -> list[int]:
    if version == 1:
        return []
    count = version // 7 + 2
    step = (version * 8 + count * 3 + 5) // (count * 4 - 4) * 2
    size = version * 4 + 17
    return [6] + sorted(size - 7 - i * step for i in range(count - 1))


def _draw(version: int, codewords: list[int], mask: int) -> Matrix:
    size = version * 4 + 17
    modules = [[False] * size for _ in range(size)]
    function = [[False] * size for _ in range(size)]

    def put(x: int, y: int, dark: bool) -> None:
        modules[y][x] = dark
        function[y][x] = True

    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    put(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    positions = _alignment_positions(version)
    last = len(positions) - 1
    for i, cx in enumerate(positions):
        for j, cy in enumerate(positions):
            if (i, j) in ((0, 0), (0, last), (last, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
    _format(put, size, 0)  # reserved now, drawn once the mask is known
    if version >= 7:
        remainder = version
        for _ in range(12):
            remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
        bits = version << 12 | remainder
        for i in range(18):
            dark = (bits >> i) & 1 == 1
            a, b = size - 11 + i % 3, i // 3
            put(a, b, dark)
            put(b, a, dark)

    i = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vertical in range(size):
            for j in range(2):
                x = right - j
                upward = (right + 1) & 2 == 0
                y = size - 1 - vertical if upward else vertical
                if not function[y][x] and i < len(codewords) * 8:
                    modules[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2

    for y in range(size):
        for x in range(size):
            if not function[y][x] and _masked(mask, x, y):
                modules[y][x] = not modules[y][x]
    _format(put, size, mask)
    return modules


def _format(put, size: int, mask: int) -> None:
    data = _FORMAT_BITS_M << 3 | mask
    remainder = data
    for _ in range(10):
        remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
    bits = (data << 10 | remainder) ^ 0x5412

    def bit(i: int) -> bool:
        return (bits >> i) & 1 == 1

    for i in range(6):
        put(8, i, bit(i))
    put(8, 7, bit(6))
    put(8, 8, bit(7))
    put(7, 8, bit(8))
    for i in range(9, 15):
        put(14 - i, 8, bit(i))
    for i in range(8):
        put(size - 1 - i, 8, bit(i))
    for i in range(8, 15):
        put(8, size - 15 + i, bit(i))
    put(8, size - 8, True)


def _masked(mask: int, x: int, y: int) -> bool:
    if mask == 0:
        return (x + y) % 2 == 0
    if mask == 1:
        return y % 2 == 0
    if mask == 2:
        return x % 3 == 0
    if mask == 3:
        return (x + y) % 3 == 0
    if mask == 4:
        return (x // 3 + y // 2) % 2 == 0
    if mask == 5:
        return x * y % 2 + x * y % 3 == 0
    if mask == 6:
        return (x * y % 2 + x * y % 3) % 2 == 0
    return ((x + y) % 2 + x * y % 3) % 2 == 0


def _penalty(modules: Matrix) -> int:
    size = len(modules)
    columns = [[modules[y][x] for y in range(size)] for x in range(size)]
    score = 0
    for line in [*modules, *columns]:
        run = 1
        for a, b in zip(line, line[1:]):
            if a == b:
                run += 1
                continue
            score += _PENALTY_RUN + run - 5 if run >= 5 else 0
            run = 1
        score += _PENALTY_RUN + run - 5 if run >= 5 else 0
        padded = (0,) * 4 + tuple(int(m) for m in line) + (0,) * 4
        for i in range(len(padded) - 10):
            if padded[i : i + 11] in _FINDER_LIKE:
                score += _PENALTY_FINDER
    for y in range(size - 1):
        for x in range(size - 1):
            if modules[y][x] == modules[y][x + 1] == modules[y + 1][x] == modules[y + 1][x + 1]:
                score += _PENALTY_BOX
    dark = sum(map(sum, modules))
    total = size * size
    k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
    return score + k * _PENALTY_BALANCE
