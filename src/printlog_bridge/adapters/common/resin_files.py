"""The slicer's resin volume, layer count and print time, read from the header of a sliced resin
file (#97), for any adapter of a resin printer that can fetch the job's file (or its start).

Pure functions on bytes: `read_resin_file(data)` takes the file, or as much of its start as the
adapter read (`HEADER_BYTES` is enough for the formats below), and returns a `ResinFile`, or None
when the bytes aren't a format it knows, are cut short of what it needs, or make no sense. It
never raises. A value that is out of any sensible range is left out (None) rather than guessed.

The format is told by its magic bytes, never by the file's extension. The layouts were worked
out from UVtools' format readers (github.com/sn4k3/UVtools, `UVtools.Core/FileFormats`); no code
of theirs is used. Offsets below are in bytes from the start of the file (or of a table).

**Chitu** (`.ctb`, `.cbddlp`, `.photon`, `.gktwo.ctb`), little-endian. A 112-byte header:

- 0: magic, one of 0x12FD0019 (cbddlp, photon), 0x12FD0086 (ctb v2-v3), 0x12FD0106 (ctb v4 and
  v5, unencrypted), 0xFF220810 (ctb for the UniFormation GKtwo); 4: version (u32);
- 68: layer count (u32); 76: print time, seconds (u32);
- 84: where the print parameters are (u32), 0 when there are none (version 1 files).

The print parameters: 20, the resin's volume in mL (f32); 24 its grams; 28 its cost.

Chitubox's *encrypted* CTB (magic 0x12FD0107: what recent Chitubox versions write for v4 and v5
printers, Elegoo's Saturn and Mars among them) keeps those settings AES-encrypted with
Chitubox's own key. It isn't read here: that would mean shipping their key.

**Elegoo GOO** (`.goo`, also Phrozen's `.prz`). A fixed layout: 4 bytes of version (`V3.0`,
`V5.1`), then the magic `07 00 00 00 44 4C 50 00`, names and two RGB565 previews (116x116 and
290x290, each followed by `0D 0A`). After them, 195310: layer count (u32); 195446: print time,
seconds (u32); 195450: the resin's volume in mL (f32). Version 3 is big-endian (as Elegoo's
published spec says); UVtools reads version 5's header little-endian, at the same offsets.

**Anycubic Photon Workshop** (`.pws`, `.pw0`, `.pwmx`, `.pwma`, `.pwms`, `.pwmo`, `.pwmb`,
`.pm3`, `.pm4n`, `.pm5`, `.dlp` and the rest of that family), little-endian. A file mark:

- 0: `ANYCUBIC`, padded with zeros to 12 bytes; 12: version (u32: 1, 515, 516, 517 or 518);
- 20: where the `HEADER` table is (u32); 36: where the `LAYERDEF` table is (u32).

Each table starts with its name (12 bytes, zero-padded) and its length (u32). In `HEADER`: 52,
the resin's volume in mL (f32); 84, print time, seconds (u32). In `LAYERDEF`: 16, the layer
count (u32).

Not read: Anycubic's `.photons` (an older layout with no volume), Creality's `.cxdlp`, zip-based
formats (Prusa's `.sl1`, `.zip` CTB, NanoDLP), and Uniformation's `.jxs`. None of them has been
needed yet; add one with a test built from its documented layout.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Callable, Union

HEADER_BYTES = 256 * 1024
"""How much of a file's start to read: a GOO header ends at 195,477 bytes; a Chitu or Anycubic
file's header and tables are usually within the first few kilobytes, ahead of the previews."""

MAX_VOLUME_ML = 100_000.0
"""3D Print Log takes a volume of up to 100,000 mL."""
MAX_LAYERS = 1_000_000
MAX_PRINT_TIME_S = 30 * 86400

Bytes = Union[bytes, bytearray, memoryview]


@dataclass(frozen=True)
class ResinFile:
    format: str
    """`ctb` (any Chitu file), `goo` or `anycubic`."""
    volume_ml: float | None
    """The slicer's estimate of resin used, in milliliters."""
    layer_count: int | None
    print_time_s: float | None
    """The slicer's estimate of the print's duration."""


def read_resin_file(data: Bytes) -> ResinFile | None:
    """What the file's header says, or None (see the module)."""
    try:
        data = bytes(data)
        for reader in _READERS:
            found = reader(data)
            if found is not None:
                return found if _anything(found) else None
    except Exception:  # a reader's bug must never reach the adapter
        return None
    return None


# -- Chitu ---------------------------------------------------------------------------------------

CHITU_MAGICS = frozenset({0x12FD0019, 0x12FD0086, 0x12FD0106, 0xFF220810})
CHITU_HEADER_SIZE = 112


def _chitu(data: bytes) -> ResinFile | None:
    magic = _u32(data, 0, "<")
    if magic not in CHITU_MAGICS or len(data) < CHITU_HEADER_SIZE:
        return None
    params = _u32(data, 84, "<") or 0
    volume = _f32(data, params + 20, "<") if params >= CHITU_HEADER_SIZE else None
    return ResinFile(
        "ctb",
        _volume(volume),
        _layers(_u32(data, 68, "<")),
        _seconds(_u32(data, 76, "<")),
    )


# -- GOO -----------------------------------------------------------------------------------------

GOO_MAGIC = bytes([0x07, 0x00, 0x00, 0x00, 0x44, 0x4C, 0x50, 0x00])
GOO_LAYER_COUNT, GOO_PRINT_TIME, GOO_VOLUME = 195310, 195446, 195450


def _goo(data: bytes) -> ResinFile | None:
    if data[4:12] != GOO_MAGIC or data[:1] != b"V" or not data[1:2].isdigit():
        return None
    if len(data) < GOO_VOLUME + 4:
        return None
    order = "<" if int(data[1:2]) >= 5 else ">"
    return ResinFile(
        "goo",
        _volume(_f32(data, GOO_VOLUME, order)),
        _layers(_u32(data, GOO_LAYER_COUNT, order)),
        _seconds(_u32(data, GOO_PRINT_TIME, order)),
    )


# -- Anycubic ------------------------------------------------------------------------------------

ANYCUBIC_MARK = b"ANYCUBIC"
ANYCUBIC_VERSIONS = frozenset({1, 515, 516, 517, 518})


def _anycubic(data: bytes) -> ResinFile | None:
    if data[:12] != ANYCUBIC_MARK.ljust(12, b"\0"):
        return None
    if _u32(data, 12, "<") not in ANYCUBIC_VERSIONS:
        return None
    header = _table(data, _u32(data, 20, "<"), b"HEADER", 88)
    if header is None:
        return None
    layers = None
    layerdef = _table(data, _u32(data, 36, "<"), b"LAYERDEF", 20)
    if layerdef is not None:
        layers = _layers(_u32(data, layerdef + 16, "<"))
    return ResinFile(
        "anycubic",
        _volume(_f32(data, header + 52, "<")),
        layers,
        _seconds(_u32(data, header + 84, "<")),
    )


def _table(data: bytes, at: int | None, name: bytes, size: int) -> int | None:
    """Where the table called `name` starts, when `at` is one within the bytes read."""
    if not at or at + size > len(data):
        return None
    if data[at : at + 12].rstrip(b"\0") != name:
        return None
    return at


_READERS: tuple[Callable[[bytes], ResinFile | None], ...] = (_chitu, _goo, _anycubic)


# -- values --------------------------------------------------------------------------------------


def _u32(data: bytes, at: int, order: str) -> int | None:
    if at < 0 or at + 4 > len(data):
        return None
    return int(struct.unpack_from(order + "I", data, at)[0])


def _f32(data: bytes, at: int, order: str) -> float | None:
    if at < 0 or at + 4 > len(data):
        return None
    return float(struct.unpack_from(order + "f", data, at)[0])


def _volume(value: float | None) -> float | None:
    if value is None or not math.isfinite(value) or not 0 < value <= MAX_VOLUME_ML:
        return None
    return round(value, 3)


def _layers(value: int | None) -> int | None:
    return value if value is not None and 0 < value <= MAX_LAYERS else None


def _seconds(value: int | None) -> float | None:
    return float(value) if value is not None and 0 < value <= MAX_PRINT_TIME_S else None


def _anything(found: ResinFile) -> bool:
    return any(v is not None for v in (found.volume_ml, found.layer_count, found.print_time_s))
