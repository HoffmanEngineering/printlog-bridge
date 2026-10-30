"""A job's file on a Duet: what RepRapFirmware read of it when it was uploaded.

The board parses each G-code file's slicer comments itself, and says what it found through
`rr_fileinfo?name=<file>` (standalone) or `/machine/fileinfo/<file>` (SBC): `filament` (the
slicer's mm per extruder), `printTime` (the slicer's estimate, seconds) or `simulatedTime`,
`generatedBy` (`PrusaSlicer 2.9.2+win64 on ...`), `layerHeight`, `size` and `thumbnails`
(`width`, `height`, `fmt`, `offset` in the file and `size` of each embedded image's base64 text).
So the file itself is never downloaded. The thumbnail is read, only when the sink asks for it, a
piece of base64 at a time with `rr_thumbnail` (standalone; `client.py`), or comes with the file
info (SBC, `readThumbnailContent=true`).

ASSUMPTION: the fields are from Duet3D's "HTTP requests" and "Object Model" pages
(`GCodeFileInfo`), not a recorded board. Grams aren't among them: the slicer's estimate is
in mm only.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from printlog_bridge.adapters.common.timeouts import bounded
from printlog_bridge.core.events import SlicerMetadata
from printlog_bridge.core.images import ImageUnavailable

from .model import number

log = logging.getLogger(__name__)

THUMBNAIL_FORMATS = ("png", "jpeg", "jpg")
"""What 3D Print Log takes; RRF also lists QOI thumbnails, which it doesn't."""
MAX_THUMBNAIL_TEXT = 700 * 1024
"""The largest thumbnail read, as base64 text (about 512 KB of image)."""
THUMBNAIL_TIMEOUT_S = 10.0
_GENERATED_BY = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_ .-]*?)[ _]v?(\d[\w.+-]*)")


@dataclass(frozen=True)
class JobFile:
    duration_s: float | None = None
    mm: Mapping[int, float] = field(default_factory=dict)
    """The slicer's mm per extruder, extruders that use nothing left out."""
    size: int | None = None
    thumbnail: Callable[[], Awaitable[bytes | None]] | None = field(
        default=None, compare=False, repr=False
    )
    metadata: SlicerMetadata | None = None


class Source(Protocol):
    async def file_info(self, path: str) -> Mapping[str, Any] | None: ...

    async def thumbnail(self, path: str, entry: Mapping[str, Any]) -> bytes | None: ...


class DuetFiles:
    def __init__(self, printer: str, source: Source, enabled: bool = True) -> None:
        self.printer = printer
        self.source = source
        self.enabled = enabled

    async def read(self, path: str) -> JobFile:
        """What the board knows of the file at `path`; empty when it can't be had."""
        if not self.enabled:
            return JobFile()
        try:
            info = await self.source.file_info(path)
        except Exception as e:  # never holds up the job's start
            log.info("[printer %s] couldn't read the job's file info: %s", self.printer, e)
            return JobFile()
        if not isinstance(info, Mapping):
            return JobFile()
        return file_from(info, path, self.source)


def file_from(info: Mapping[str, Any], path: str, source: Source | None = None) -> JobFile:
    mm: dict[int, float] = {}
    filament = info.get("filament")
    for i, value in enumerate(filament if isinstance(filament, list) else []):
        amount = number(value)
        if amount is not None and amount > 0:
            mm[i] = round(amount, 1)
    duration = number(info.get("printTime")) or number(info.get("simulatedTime"))
    size = number(info.get("size"))
    thumbnail = None
    entry = best_thumbnail(info.get("thumbnails"))
    if entry is not None and source is not None:

        async def fetch() -> bytes | None:
            try:
                return await bounded(source.thumbnail(path, entry), THUMBNAIL_TIMEOUT_S)
            except asyncio.TimeoutError:
                raise ImageUnavailable("the board took too long to send the thumbnail") from None
            except Exception as e:  # the board's gone, or busy: no thumbnail
                raise ImageUnavailable(f"the board didn't send the thumbnail ({e})") from None

        thumbnail = fetch
    return JobFile(
        duration_s=duration if duration and duration > 0 else None,
        mm=mm,
        size=int(size) if size and size > 0 else None,
        thumbnail=thumbnail,
        metadata=metadata_from(info),
    )


def best_thumbnail(entries: Any) -> Mapping[str, Any] | None:
    """The largest PNG or JPEG thumbnail that isn't too big to read."""
    best = None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        fmt = str(entry.get("fmt") or entry.get("format") or "").lower()
        width, height = number(entry.get("width")) or 0, number(entry.get("height")) or 0
        size = number(entry.get("size")) or 0
        if fmt not in THUMBNAIL_FORMATS or not 0 < size <= MAX_THUMBNAIL_TEXT:
            continue
        if best is None or width * height > best[0]:
            best = (width * height, entry)
    return best[1] if best else None


def metadata_from(info: Mapping[str, Any]) -> SlicerMetadata | None:
    slicer = version = None
    generated = info.get("generatedBy")
    if isinstance(generated, str):
        match = _GENERATED_BY.match(generated)
        if match:
            slicer, version = match.group(1).strip(), match.group(2)
        elif generated.strip():
            slicer = generated.strip()[:100]
    layer = number(info.get("layerHeight"))
    first = number(info.get("firstLayerHeight"))
    if slicer is None and not layer:
        return None
    return SlicerMetadata(
        slicer=slicer,
        slicer_version=version,
        layer_height_mm=layer if layer and layer > 0 else None,
        first_layer_height_mm=first if first and first > 0 else None,
    )
