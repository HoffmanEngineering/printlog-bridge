"""A Moonraker job's slicer settings (#36): the summary in its file's metadata, and the header and
settings block read from the file itself, by byte range.

Moonraker's metadata says where a file's G-code begins (`gcode_start_byte`) and ends
(`gcode_end_byte`) and how long it is (`size`). Only what lies outside that is read:

- the header, from the start of the file to where the G-code begins, at most 64 KB (the first
  4 KB when Moonraker doesn't say where that is), cut at `; HEADER_BLOCK_END`;
- the settings block, from the line break that ends the G-code's last line to the end of the
  file, at most its last 512 KB, less the first line it reads, which may be cut (Moonraker v0.9.3
  and older count `gcode_end_byte` in characters, so it can point into the settings). When
  Moonraker doesn't say where the G-code ends, or says it ends before it begins, the last 128 KB,
  of which only the comments and blank lines it ends with are kept.

A remote bridge asks Moonraker's file API for each range (`Range: bytes=...`); a local one reads
the same ranges from the gcodes folder. A file that isn't the size the job's metadata says was
replaced since the job started, and isn't read as its settings. Each read gives up after 10 s.
Nothing is read until the sink asks (`JobSlicerSettings.read`), which it never does with
`slicer_settings = false`.
"""

from __future__ import annotations

import asyncio
import logging
import math
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from printlog_bridge.core.events import SlicerFile, SlicerMetadata
from printlog_bridge.core.images import RangeUnavailable, fetch_range, read_range
from printlog_bridge.core.slicer_settings import (
    CONFIG_BLOCK_MAX_BYTES,
    HEADER_MAX_BYTES,
    config_block_from,
    header_from,
)

from .images import FETCH_TIMEOUT_S, MoonrakerImages, _http_base, _inside
from .session import Call, ConnectionLost, MoonrakerRpcError, bounded

log = logging.getLogger(__name__)

GCODE_EXTENSIONS = (".gcode", ".g", ".gco")
"""Only these are read: `.ufp` is a zip, and `.bgcode` is binary."""
HEADER_FALLBACK_BYTES = 4 * 1024
"""The header read when Moonraker doesn't say where the G-code begins: a slicer's header
block, or PrusaSlicer's first line, is a few hundred bytes."""
TAIL_FALLBACK_BYTES = 128 * 1024
"""The tail read when Moonraker doesn't say where the G-code ends: real settings blocks are
tens of kilobytes."""

UNKNOWN = "unknown"
"""What Moonraker's metadata says for a slicer, or its version, it couldn't identify (QIDI
Studio's files, for one). Left out, so the API reads the slicer from the file or reports none."""

_TEXT = {"slicer": "slicer", "slicer_version": "slicer_version",
         "filament_name": "filament_name", "filament_type": "filament_type"}  # fmt: skip
_NUMBERS = {
    "layer_height": "layer_height_mm",
    "first_layer_height": "first_layer_height_mm",
    "nozzle_diameter": "nozzle_diameter_mm",
    "first_layer_extr_temp": "first_layer_nozzle_temperature_c",
    "first_layer_bed_temp": "first_layer_bed_temperature_c",
}
"""Moonraker's metadata has `chamber_temp`, `layer_count` and `object_height` too, which the API
has no fields for."""


def slicer_metadata(metadata: Mapping[str, Any]) -> SlicerMetadata | None:
    """The slicer's summary from Moonraker's metadata for a file, or None when it has none."""
    values: dict[str, Any] = {}
    for key, name in _TEXT.items():
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            values[name] = value.strip()
    for name in ("slicer", "slicer_version"):
        # Moonraker's word for a slicer it couldn't identify, not a name (#81).
        if values.get(name, "").lower() == UNKNOWN:
            del values[name]
    for key, name in _NUMBERS.items():
        value = metadata.get(key)
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if number and math.isfinite(value):
            values[name] = float(value)
    return SlicerMetadata(**values) if values else None


@dataclass(frozen=True)
class _Plan:
    size: int
    header: tuple[int, int] | None
    """`[start, end)` of the header, or None when there's none to read."""
    tail: tuple[int, int] | None
    tail_mid_line: bool
    """The tail read may begin inside a line, which is dropped."""
    tail_after_gcode: bool
    """The tail read may begin inside the G-code (Moonraker didn't say where it ends)."""


def _plan(metadata: Mapping[str, Any]) -> _Plan | None:
    """The byte ranges to read, from the metadata's `size`, `gcode_start_byte` and
    `gcode_end_byte`. None without a size."""
    size = _whole(metadata.get("size"))
    if size is None or size <= 0:
        return None
    start = _whole(metadata.get("gcode_start_byte"))
    header_end = min(HEADER_MAX_BYTES, start) if start is not None and start <= size else None
    if header_end is None:
        header_end = min(HEADER_FALLBACK_BYTES, size)
    end = _whole(metadata.get("gcode_end_byte"))
    if end is not None and (start is not None and end < start):
        end = None  # inconsistent: it can't end before it begins
    if end is not None and 0 < end <= size:
        # From the line break that ends the G-code's last line, one byte before `end`, so a
        # read that begins mid-line can be told and that line dropped: Moonraker v0.9.3 and
        # older count `gcode_end_byte` in characters, which after non-ASCII text or CRLF line
        # endings lands inside the settings, and a cut line could carry half a secret no
        # scrubber recognises. With an exact `end`, all that is dropped is that line break.
        first = max(end - 1, size - CONFIG_BLOCK_MAX_BYTES)
        tail = (first, size) if end < size else None
        mid_line, after_gcode = True, False
    else:
        first = max(0, size - TAIL_FALLBACK_BYTES)
        tail = (first, size)
        mid_line, after_gcode = first > 0, True
    return _Plan(
        size=size,
        header=(0, header_end) if header_end > 0 else None,
        tail=tail,
        tail_mid_line=mid_line,
        tail_after_gcode=after_gcode,
    )


class MoonrakerSlicerFiles:
    """Reads a job's file for its slicer settings, from where `images` reads its thumbnails:
    Moonraker's file API in remote mode (its address kept current there), the gcodes folder in
    local mode."""

    def __init__(
        self, printer: str, images: MoonrakerImages, timeout: float = FETCH_TIMEOUT_S
    ) -> None:
        self.printer = printer
        self.images = images
        self.timeout = timeout

    async def read(self, call: Call, job: Mapping[str, Any]) -> SlicerFile | None:
        """The header and settings block of the job's file, or None when neither can be had."""
        filename = str(job.get("filename") or "")
        if not filename.lower().endswith(GCODE_EXTENSIONS):
            return None
        normalized = posixpath.normpath(filename)
        if normalized.startswith(("../", "/")) or normalized == "..":
            return None  # never outside the gcodes folder
        try:
            return await bounded(self._read(call, filename, job), self.timeout)
        except asyncio.TimeoutError:
            log.info("[printer %s] %s: not read within %gs", self.printer, filename, self.timeout)
        except (RangeUnavailable, MoonrakerRpcError, ConnectionLost) as e:
            log.info("[printer %s] %s: its slicer settings can't be read: %s",
                     self.printer, filename, e)  # fmt: skip
        return None

    async def _read(self, call: Call, filename: str, job: Mapping[str, Any]) -> SlicerFile | None:
        metadata = job.get("metadata") or {}
        if not isinstance(metadata, Mapping) or "size" not in metadata:
            # Older history entries, and some forks, keep less of the metadata.
            try:
                metadata = await call("server.files.metadata", {"filename": filename}) or {}
            except MoonrakerRpcError:
                return None
        plan = _plan(metadata)
        if plan is None:
            return None
        if self.images.url is None:
            root = await _gcodes_root_or_none(call)
            if root is None:
                return None
            path = (root / filename).resolve()
            if not _inside(path, root.resolve()):
                return None

            async def get(start: int, end: int) -> bytes:
                return await read_range(path, start, end, plan.size)

        else:
            url = f"{_http_base(self.images.url)}/server/files/gcodes/{quote(filename)}"
            api_key = self.images.api_key
            headers = {"X-Api-Key": api_key} if api_key else None

            async def get(start: int, end: int) -> bytes:
                return await fetch_range(
                    url, start, end, size=plan.size, timeout=self.timeout, headers=headers
                )

        header = header_from(await get(*plan.header)) if plan.header is not None else None
        block = None
        if plan.tail is not None:
            block = config_block_from(
                await get(*plan.tail),
                starts_mid_line=plan.tail_mid_line,
                after_gcode=plan.tail_after_gcode,
            )
        if header is None and block is None:
            return None
        return SlicerFile(header=header, config_block=block)


async def _gcodes_root_or_none(call: Call) -> Path | None:
    for root in await call("server.files.roots", None) or []:
        if root.get("name") == "gcodes" and root.get("path"):
            return Path(root["path"])
    return None


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return int(value)
