"""Reading a binary G-code (`.bgcode`) file's metadata, thumbnails and slicer settings, never
its G-code.

The format (libbgcode's `doc/specifications.md`) is a 10-byte file header (`GCDE`, version 1,
checksum type) and then blocks, in this order: file metadata (optional), printer metadata,
thumbnails (optional), print metadata, slicer metadata, and the G-code. So everything the bridge
wants is in front of the first G-code block, and a reader can stop there: `BgcodeReader` takes
the file's bytes as they arrive and says when it has seen that block.

Each block is a header (type, compression, uncompressed size, and the compressed size when
compressed), its parameters (2 bytes for metadata and G-code, 6 for a thumbnail), its data, and a
4-byte CRC32 when the file has checksums. Metadata is `key=value` lines ("INI encoding"). A
block may be stored as is, with Deflate, or with Heatshrink (window 11 or 12, lookahead 4);
PrusaSlicer 2.9 deflates the print and slicer metadata, stores the rest of the metadata and the
thumbnails as is, and packs the G-code with Heatshrink 12/4 and MeatPack (its files in
`tests/fixtures/gcode/`). A block in a compression or
encoding the reader doesn't know is skipped, and said so in `problems`.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

from printlog_bridge.adapters.common.slicer_meta import parse_ini

MAGIC = b"GCDE"
FILE_HEADER_BYTES = 10

FILE_METADATA, GCODE, SLICER_METADATA, PRINTER_METADATA, PRINT_METADATA, THUMBNAIL = range(6)
METADATA_BLOCKS = {
    FILE_METADATA: "file",
    PRINTER_METADATA: "printer",
    PRINT_METADATA: "print",
    SLICER_METADATA: "slicer",
}
NONE, DEFLATE, HEATSHRINK_11_4, HEATSHRINK_12_4 = range(4)
PNG, JPG, QOI = range(3)
MAX_BLOCK_BYTES = 16 * 1024 * 1024
"""A metadata or thumbnail block bigger than this isn't read: no real one comes near it."""


class NotBgcode(ValueError):
    """The bytes don't start with a binary G-code file header."""


@dataclass
class Thumbnail:
    format: int
    width: int
    height: int
    data: bytes


@dataclass
class BgcodeInfo:
    """What was read in front of the G-code."""

    metadata: dict[str, dict[str, str]] = field(default_factory=dict)
    """Each metadata block's pairs, by block: `file`, `printer`, `print`, `slicer`."""
    thumbnails: list[Thumbnail] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    """Blocks that couldn't be read, in words for the log."""
    complete: bool = False
    """The first G-code block was reached: nothing more of the file is needed."""


class BgcodeReader:
    """Reads a `.bgcode` file's blocks from its bytes, fed as they arrive."""

    def __init__(self) -> None:
        self.info = BgcodeInfo()
        self._buffer = bytearray()
        self._checksum_bytes: int | None = None

    @property
    def done(self) -> bool:
        return self.info.complete

    def feed(self, data: bytes) -> bool:
        """Takes the next bytes of the file. True once the G-code is reached. Raises `NotBgcode`
        when the file doesn't start like one."""
        if self.done:
            return True
        self._buffer += data
        if self._checksum_bytes is None:
            if len(self._buffer) < FILE_HEADER_BYTES:
                if not MAGIC.startswith(bytes(self._buffer[:4])):
                    raise NotBgcode("not a binary G-code file")
                return False
            magic, _version, checksum = struct.unpack_from("<4sIH", self._buffer)
            if magic != MAGIC:
                raise NotBgcode("not a binary G-code file")
            self._checksum_bytes = 4 if checksum == 1 else 0
            del self._buffer[:FILE_HEADER_BYTES]
        while self._next_block():
            pass
        return self.done

    def _next_block(self) -> bool:
        """Reads one whole block from the buffer, if it's all there."""
        if len(self._buffer) < 8:
            return False
        kind, compression, size = struct.unpack_from("<HHI", self._buffer)
        if kind == GCODE:
            self.info.complete = True
            self._buffer.clear()
            return False
        header = 8
        stored = size
        if compression != NONE:
            if len(self._buffer) < 12:
                return False
            (stored,) = struct.unpack_from("<I", self._buffer, 8)
            header = 12
        parameters = 6 if kind == THUMBNAIL else 2
        total = header + parameters + stored + (self._checksum_bytes or 0)
        if stored > MAX_BLOCK_BYTES or size > MAX_BLOCK_BYTES:
            raise NotBgcode(f"a block of {stored} bytes: not a file the bridge can read")
        if len(self._buffer) < total:
            return False
        params = bytes(self._buffer[header : header + parameters])
        data = bytes(self._buffer[header + parameters : header + parameters + stored])
        del self._buffer[:total]
        self._block(kind, compression, size, params, data)
        return True

    def _block(self, kind: int, compression: int, size: int, params: bytes, data: bytes) -> None:
        name = METADATA_BLOCKS.get(kind)
        if name is None and kind != THUMBNAIL:
            self.info.problems.append(f"an unknown block type {kind} was skipped")
            return
        what = f"{name} metadata" if name else "a thumbnail"
        try:
            raw = decompress(compression, data, size)
        except ValueError as e:
            self.info.problems.append(f"{what} couldn't be read: {e}")
            return
        if kind == THUMBNAIL:
            image_format, width, height = struct.unpack("<HHH", params)
            self.info.thumbnails.append(Thumbnail(image_format, width, height, raw))
            return
        (encoding,) = struct.unpack("<H", params)
        if encoding != 0:
            self.info.problems.append(f"{what} is in an unknown encoding ({encoding})")
            return
        self.info.metadata.setdefault(name, {}).update(parse_ini(raw))  # type: ignore[arg-type]


def decompress(compression: int, data: bytes, size: int) -> bytes:
    """A block's data as stored, uncompressed. Raises ValueError for anything that isn't."""
    if compression == NONE:
        out = data
    elif compression == DEFLATE:
        try:
            out = zlib.decompress(data)
        except zlib.error:
            try:
                out = zlib.decompress(data, -15)  # a raw stream, without zlib's header
            except zlib.error as e:
                raise ValueError(f"broken Deflate data ({e})") from None
    elif compression in (HEATSHRINK_11_4, HEATSHRINK_12_4):
        window = 11 if compression == HEATSHRINK_11_4 else 12
        out = heatshrink_decode(data, window, 4, size)
    else:
        raise ValueError(f"unknown compression {compression}")
    if len(out) != size:
        raise ValueError(f"{len(out)} bytes where the block says {size}")
    return out


def heatshrink_decode(data: bytes, window_bits: int, lookahead_bits: int, limit: int) -> bytes:
    """Heatshrink's LZSS: a 1 bit and 8 bits is a literal byte; a 0 bit, `window_bits` of
    index and `lookahead_bits` of count copies count + 1 bytes from index + 1 bytes back. Bits
    are read from each byte's most significant first; the last byte's spare bits are padding.
    Stops at `limit` bytes of output."""
    out = bytearray()
    total_bits = len(data) * 8
    position = 0

    def bits(count: int) -> int | None:
        nonlocal position
        if position + count > total_bits:
            return None
        value = 0
        for _ in range(count):
            byte = data[position >> 3]
            value = (value << 1) | ((byte >> (7 - (position & 7))) & 1)
            position += 1
        return value

    while len(out) < limit:
        tag = bits(1)
        if tag is None:
            break
        if tag:
            literal = bits(8)
            if literal is None:
                break
            out.append(literal)
            continue
        index = bits(window_bits)
        count = bits(lookahead_bits)
        if index is None or count is None:
            break
        offset = index + 1
        if offset > len(out):
            raise ValueError("broken Heatshrink data (a reference before the start)")
        for _ in range(count + 1):
            out.append(out[-offset])
    return bytes(out[:limit])
