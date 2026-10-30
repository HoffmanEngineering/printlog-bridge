"""What the bridge reads of an SDCP printer's job file: the slicer's estimates and thumbnail,
from the first `HEAD_BYTES` of the file. Never the toolpaths, and never more than once per job.

**The download is an ASSUMPTION** (#94): SDCP itself has no way to read a file, and neither
Elegoo's spec nor elegoo-link describes one. One community note (bjan/pycentauri,
`docs/PROTOCOL.md`, Apache-2.0) reports that the Centauri Carbon's web server on port 80 serves
`GET /downloadFile<path>`, `<path>` as the file list names it (`/local/Benchy.gcode`). It is a
plain GET with a `Range`; a printer that ignores the range is read no further than the range's
length. Anything else (no web server, a 404, a redirect, a timeout, a file that isn't text)
logs the job without usage, as before: never an error. `files = false` turns it off.

Elegoo Slicer (OrcaSlicer's fork) writes the estimates at the top of the file, in its header:
`; total filament length [mm] : 4130.20`, `; total filament weight [g] : 12.34` (comma separated
per tool on a multi-material print) and `; model printing time: ...; total estimated time: 1h 2m
3s`, then the thumbnails. PrusaSlicer's `; filament used [g] = ...` is read too, should it be
in the header.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from typing import Any
from urllib.parse import quote

import aiohttp

from printlog_bridge.adapters.common.slicer_meta import (
    JobFile,
    facts_from_meta,
    metadata_from,
    slicer_from_header,
    thumbnail_from,
)
from printlog_bridge.core.events import SlicerFile
from printlog_bridge.core.slicer_settings import header_from

log = logging.getLogger(__name__)

HTTP_PORT = 80
HEAD_BYTES = 256 * 1024
"""The header and the thumbnails after it; a large thumbnail is a few hundred KB of base64."""
READ_TIMEOUT_S = 15.0
"""The websocket waits while the file is read: well under the 60 s a silent client is dropped
after (`protocol.py`)."""
DOWNLOAD = "/downloadFile"
STORAGE = "/local"
"""Where a file named without a folder is: the printer's own storage."""
_KEYS = {
    "total filament weight [g]": "filament used [g]",
    "filament used [g]": "filament used [g]",
    "total filament length [mm]": "filament used [mm]",
    "filament used [mm]": "filament used [mm]",
    "total estimated time": "estimated printing time (normal mode)",
    "estimated printing time (normal mode)": "estimated printing time (normal mode)",
}
"""The header's names for the estimates, under the names `facts_from_meta` reads."""


def download_path(path: str) -> str | None:
    """Where the printer's web server serves the file `PrintInfo.Filename` names, quoted. None
    for a name that would leave the printer's storage."""
    path = (path or "").strip()
    if not path:
        return None
    if not path.startswith("/"):
        path = f"{STORAGE}/{path}"
    if ".." in path.split("/") or "\\" in path or posixpath.basename(path) == "":
        return None
    return DOWNLOAD + quote(path, safe="/")


def facts_from(head: bytes) -> JobFile:
    """The estimates, the settings header, the slicer and the thumbnail from a file's start."""
    header = header_from(head)
    if header is None:
        return JobFile()
    meta: dict[str, Any] = {}
    for line in header.splitlines():
        for part in line.lstrip(";").split(";"):
            key, sep, value = part.partition("=")
            if not sep:
                key, sep, value = part.partition(" : ") if " : " in part else part.partition(":")
            name = _KEYS.get(key.strip().lower())
            if sep and name and name not in meta:
                meta[name] = value.strip()
    found = facts_from_meta(meta)
    slicer, version = slicer_from_header(header)
    found.settings = SlicerFile(header=header, config_block=None)
    found.metadata = metadata_from({}, slicer, version)
    found.thumbnail = thumbnail_from(head)
    return found


class SdcpFiles:
    def __init__(
        self,
        printer: str,
        host: str,
        port: int = HTTP_PORT,
        *,
        enabled: bool = True,
        timeout: float = READ_TIMEOUT_S,
    ) -> None:
        self.printer = printer
        shown = f"[{host}]" if ":" in host else host
        self.base = f"http://{shown}:{port}"
        self.enabled = enabled
        self.timeout = timeout

    async def read(self, path: str) -> JobFile:
        """The job file at `path` (as `PrintInfo.Filename` names it). Empty when it can't be
        read."""
        where = download_path(path)
        if not self.enabled or where is None:
            return JobFile()
        try:
            head = await asyncio.wait_for(self._head(self.base + where), self.timeout)
        except asyncio.TimeoutError:
            log.warning(
                "[printer %s] the job's file didn't arrive within %gs: logged without the "
                "slicer's estimate", self.printer, self.timeout,
            )  # fmt: skip
            return JobFile()
        except (aiohttp.ClientError, OSError, ValueError) as e:
            log.warning(
                "[printer %s] can't read the job's file (%s): logged without the slicer's "
                "estimate", self.printer, e,
            )  # fmt: skip
            return JobFile()
        return facts_from(head)

    async def _head(self, url: str) -> bytes:
        """The first `HEAD_BYTES` of the file, never more."""
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10.0)
        headers = {"Range": f"bytes=0-{HEAD_BYTES - 1}"}
        async with aiohttp.ClientSession(timeout=timeout) as http:  # noqa: SIM117 - py39
            async with http.get(url, headers=headers, allow_redirects=False) as response:
                if response.status not in (200, 206):
                    raise ValueError(f"HTTP {response.status} for the job's file")
                data = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    data += chunk
                    if len(data) >= HEAD_BYTES:
                        break
                response.close()
                return bytes(data[:HEAD_BYTES])
