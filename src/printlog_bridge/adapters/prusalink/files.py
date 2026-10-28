"""What the bridge reads of a PrusaLink job's file: the slicer's estimates per tool, its
thumbnail, its settings. Never the toolpaths, and never more than once per job.

PrusaLink on a Raspberry Pi puts the file's metadata in `/api/v1/job` (`file.meta`, the keys of
Prusa-Link-Web's `PrintFileMetadata`), and then nothing is downloaded. Buddy firmware gives only
the file's name and `refs` (`basic_gets.cpp`), and serves the file without `Range`
(`send_file.cpp`), so the file is read from its start:

- A `.bgcode` file (PrusaSlicer's default for every Buddy printer) has its metadata, thumbnails
  and settings in front of its G-code (`bgcode.py`), so the download stops at the first G-code
  block: usually a few hundred kilobytes, at most `MAX_BGCODE_HEAD_BYTES`.
- A plain `.gcode` file has PrusaSlicer's estimates and settings after its G-code, so it is read
  to its end, keeping only its first `HEAD_BYTES` and last `TAIL_BYTES`. A file larger than
  `max_gcode_bytes` (the job's `file.size`) isn't read: it would take minutes over the printer's
  network, while it prints.

The thumbnail is the file's largest PNG or JPEG (a `.bgcode`'s), else the printer's
`refs.thumbnail`.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from collections.abc import Mapping
from typing import Any

import aiohttp

from printlog_bridge.adapters.common.slicer_meta import (
    JobFile,
    duration_of,
    facts_from_meta,
    metadata_from,
    parse_ini,
)
from printlog_bridge.core.events import SlicerFile
from printlog_bridge.core.images import image_type
from printlog_bridge.core.slicer_settings import (
    config_block_from,
    config_block_from_settings,
    header_from,
)
from printlog_bridge.core.status import PrinterReport

from .bgcode import JPG, PNG, BgcodeInfo, BgcodeReader, NotBgcode
from .client import PrusaLinkClient, Refused

log = logging.getLogger(__name__)

MAX_BGCODE_HEAD_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_GCODE_BYTES = 64 * 1024 * 1024
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 512 * 1024
MAX_THUMBNAIL_BYTES = 2 * 1024 * 1024
READ_TIMEOUT_S = 180.0
"""For the whole read: a plain G-code file over a MINI's network can take minutes."""
WARNING = "files"
REFUSED = (
    "the printer refused the bridge's credentials for the job's file, so prints are logged "
    "without the slicer's estimates"
)


class PrusaLinkFiles:
    def __init__(
        self,
        printer: str,
        client: PrusaLinkClient,
        report: PrinterReport,
        *,
        enabled: bool = True,
        max_gcode_bytes: int = DEFAULT_MAX_GCODE_BYTES,
        timeout: float = READ_TIMEOUT_S,
    ) -> None:
        self.printer = printer
        self.client = client
        self.report = report
        self.enabled = enabled
        self.max_gcode_bytes = max_gcode_bytes
        self.timeout = timeout
        self._key: tuple[Any, ...] | None = None
        self._file: JobFile | None = None

    async def read(self, job: Mapping[str, Any]) -> JobFile:
        """The file of `job` (`/api/v1/job`'s answer), read once per job."""
        file = job.get("file") if isinstance(job.get("file"), Mapping) else {}
        refs = file.get("refs") if isinstance(file.get("refs"), Mapping) else {}  # type: ignore[union-attr]
        key = (job.get("id"), refs.get("download"), file.get("display_name"))  # type: ignore[union-attr]
        if key == self._key and self._file is not None:
            return self._file
        found = JobFile()
        if self.enabled:
            try:
                found = await asyncio.wait_for(self._read(file, refs), self.timeout)  # type: ignore[arg-type]
            except Refused:
                log.warning("[printer %s] %s", self.printer, REFUSED)
                self.report.warn(WARNING, REFUSED)
            except asyncio.TimeoutError:
                log.warning(
                    "[printer %s] the job's file didn't arrive within %gs, so it is logged "
                    "without the slicer's estimates", self.printer, self.timeout,
                )  # fmt: skip
            except (aiohttp.ClientError, OSError, NotBgcode) as e:
                log.warning(
                    "[printer %s] can't read the job's file (%s), so it is logged without the "
                    "slicer's estimates", self.printer, e,
                )  # fmt: skip
            else:
                self.report.warn(WARNING, None)
        self._key, self._file = key, found
        return found

    async def _read(self, file: Mapping[str, Any], refs: Mapping[str, Any]) -> JobFile:
        meta = file.get("meta")
        download = refs.get("download")
        name = str(file.get("display_name") or file.get("name") or download or "")
        if isinstance(meta, Mapping) and meta:
            found = facts_from_meta(meta)
        elif not isinstance(download, str) or not download.startswith("/"):
            found = JobFile()
        elif is_bgcode(name):
            found = await self._read_bgcode(download)
        else:
            size = file.get("size")
            if isinstance(size, int) and size > self.max_gcode_bytes:
                log.info(
                    "[printer %s] %s is %d MB, over the %d MB the bridge reads for its "
                    "estimates: logged without them",
                    self.printer, name, size // 2**20, self.max_gcode_bytes // 2**20,
                )  # fmt: skip
                found = JobFile()
            else:
                found = await self._read_gcode(download)
        if found.thumbnail is None and isinstance(refs.get("thumbnail"), str):
            found.thumbnail = await self._thumbnail(refs["thumbnail"])
        return found

    async def _read_bgcode(self, path: str) -> JobFile:
        reader = BgcodeReader()
        read = 0
        async with self.client.stream(path) as chunks:
            async for chunk in chunks:
                read += len(chunk)
                if reader.feed(chunk) or read >= MAX_BGCODE_HEAD_BYTES:
                    break
        for problem in reader.info.problems:
            log.info("[printer %s] in the job's file, %s", self.printer, problem)
        return facts_from_bgcode(reader.info)

    async def _read_gcode(self, path: str) -> JobFile:
        head = bytearray()
        tail = bytearray()
        read = 0
        async with self.client.stream(path) as chunks:
            async for chunk in chunks:
                read += len(chunk)
                if read > self.max_gcode_bytes:
                    log.info(
                        "[printer %s] the job's file is over %d MB: logged without estimates",
                        self.printer, self.max_gcode_bytes // 2**20,
                    )  # fmt: skip
                    return JobFile()
                if len(head) < HEAD_BYTES:
                    head += chunk[: HEAD_BYTES - len(head)]
                tail += chunk
                if len(tail) > 2 * TAIL_BYTES:
                    del tail[: len(tail) - TAIL_BYTES]
        cut = len(tail) > TAIL_BYTES
        tail_bytes = bytes(tail[-TAIL_BYTES:])
        block = config_block_from(tail_bytes, starts_mid_line=cut, after_gcode=True)
        found = facts_from_meta(parse_ini(block.encode()) if block else {})
        header = header_from(bytes(head))
        if block or header:
            found.settings = SlicerFile(header=header, config_block=block)
        return found

    async def _thumbnail(self, path: str) -> bytes | None:
        data = bytearray()
        try:
            async with self.client.stream(path) as chunks:
                async for chunk in chunks:
                    data += chunk
                    if len(data) > MAX_THUMBNAIL_BYTES:
                        return None
        except (aiohttp.ClientError, OSError, Refused) as e:
            log.info("[printer %s] no thumbnail from the printer: %s", self.printer, e)
            return None
        return bytes(data) if image_type(bytes(data)) else None


def is_bgcode(name: str) -> bool:
    """`.bgcode`, or `.bgc` as Buddy's short (8.3) names have it."""
    return posixpath.splitext(name.lower())[1] in (".bgcode", ".bgc")


def facts_from_bgcode(info: BgcodeInfo) -> JobFile:
    printer = info.metadata.get("printer", {})
    found = facts_from_meta({**printer, **info.metadata.get("print", {})})
    slicer = info.metadata.get("slicer", {})
    if slicer:
        block = config_block_from_settings(slicer)
        if block:
            found.settings = SlicerFile(config_block=block)
    producer = info.metadata.get("file", {}).get("Producer", "")
    name, _, version = producer.strip().rpartition(" ")
    if not name:
        name, version = version, ""
    settings = {**printer, **slicer}
    found.metadata = metadata_from(settings, name, version)
    pictures = [t for t in info.thumbnails if t.format in (PNG, JPG) and image_type(t.data)]
    if pictures:
        found.thumbnail = max(pictures, key=lambda t: t.width * t.height).data
    return found


__all__ = ["JobFile", "PrusaLinkFiles", "duration_of", "facts_from_meta"]
