"""A Moonraker job's pictures: the slicer's thumbnail, and a frame from the printer's webcam.

Moonraker extracts the thumbnails a slicer embeds (OrcaSlicer, PrusaSlicer, Cura, SuperSlicer)
into `.thumbs/` next to the file, and lists them in the file's metadata, which the job's history
entry carries. A local bridge reads the largest from the gcodes folder; a remote one downloads
it from Moonraker's file API. The snapshot is one GET of the first enabled webcam's
`snapshot_url` from `server.webcams.list`. Each gives up after 10 s.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from printlog_bridge.adapters.common.timeouts import bounded
from printlog_bridge.core.images import ImageUnavailable, fetch_image, read_image_file

from .session import Call, ConnectionLost, MoonrakerRpcError

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S = 10.0
MOONRAKER_PORT = 7125
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
"""Thumbnails are read only from files named as pictures, so no G-code is ever read."""
THUMBNAILS_TRIED = 3
"""How many thumbnails to try, largest first, when one can't be read."""


class MoonrakerImages:
    def __init__(
        self,
        printer: str,
        *,
        url: str | None,
        socket: Path | None,
        api_key: str | None = None,
        snapshots: bool = True,
        timeout: float = FETCH_TIMEOUT_S,
    ) -> None:
        self.printer = printer
        self.url = url
        self.socket = socket
        self.api_key = api_key
        self.snapshots = snapshots
        """False: the webcam is never asked for anything."""
        self.timeout = timeout

    async def thumbnail(self, call: Call, job: Mapping[str, Any]) -> bytes | None:
        """The largest thumbnail in the job's file, or None when it has none."""
        try:
            return await bounded(self._thumbnail(call, job), self.timeout)
        except asyncio.TimeoutError:
            raise ImageUnavailable(f"no thumbnail within {self.timeout:g}s") from None
        except (MoonrakerRpcError, ConnectionLost) as e:
            raise ImageUnavailable(f"Moonraker couldn't say: {e}") from None

    async def snapshot(self, call: Call) -> bytes | None:
        """One frame from the first enabled webcam, or None when there's none (or it's off)."""
        if not self.snapshots:
            return None
        try:
            return await bounded(self._snapshot(call), self.timeout)
        except asyncio.TimeoutError:
            raise ImageUnavailable(f"no snapshot within {self.timeout:g}s") from None
        except (MoonrakerRpcError, ConnectionLost) as e:
            raise ImageUnavailable(f"Moonraker couldn't list its webcams: {e}") from None

    async def _thumbnail(self, call: Call, job: Mapping[str, Any]) -> bytes | None:
        filename = str(job.get("filename") or "")
        metadata = job.get("metadata") or {}
        thumbnails = metadata.get("thumbnails")
        if thumbnails is None and filename:
            # Older history entries, and some forks, keep less of the metadata.
            try:
                thumbnails = (
                    await call("server.files.metadata", {"filename": filename}) or {}
                ).get("thumbnails")
            except MoonrakerRpcError:
                return None
        problem: ImageUnavailable | None = None
        for candidate in largest_first(thumbnails or [])[:THUMBNAILS_TRIED]:
            path = _thumbnail_path(filename, str(candidate["relative_path"]))
            if path is not None and not path.lower().endswith(IMAGE_EXTENSIONS):
                # Never the G-code itself, or any other file: only pictures are read.
                log.warning(
                    "[printer %s] ignoring a thumbnail that isn't a picture: %r",
                    self.printer, candidate["relative_path"],
                )  # fmt: skip
                continue
            if path is None:
                log.warning(
                    "[printer %s] ignoring a thumbnail outside the gcodes folder: %r",
                    self.printer, candidate["relative_path"],
                )  # fmt: skip
                continue
            try:
                return await self._read(call, path)
            except ImageUnavailable as e:
                problem = e
        if problem is not None:
            raise problem
        return None

    async def _read(self, call: Call, path: str) -> bytes:
        if self.url is None:
            root = await _gcodes_root(call)
            full = (root / path).resolve()
            if not _inside(full, root.resolve()):
                raise ImageUnavailable(f"{path} is outside the gcodes folder")
            return await read_image_file(full)
        headers = {"X-Api-Key": self.api_key} if self.api_key else None
        url = f"{_http_base(self.url)}/server/files/gcodes/{quote(path)}"
        return await fetch_image(url, timeout=self.timeout, headers=headers)

    async def _snapshot(self, call: Call) -> bytes | None:
        try:
            listed = await call("server.webcams.list", None)
        except MoonrakerRpcError as e:
            if e.code == -32601:  # a Moonraker without the webcam component
                return None
            raise
        for webcam in (listed or {}).get("webcams") or []:
            if webcam.get("enabled", True) is False or not webcam.get("snapshot_url"):
                continue
            url = snapshot_url(str(webcam["snapshot_url"]), printer_url=self.url)
            return await fetch_image(url, timeout=self.timeout)
        return None


def largest_first(thumbnails: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    usable = [t for t in thumbnails if isinstance(t, Mapping) and t.get("relative_path")]
    return sorted(
        usable,
        key=lambda t: (_number(t.get("width")) * _number(t.get("height")), _number(t.get("size"))),
        reverse=True,
    )


def snapshot_url(snapshot: str, *, printer_url: str | None) -> str:
    """Where to GET a webcam's `snapshot_url`. Mainsail and Fluidd give a path on their own
    web server (`/webcam/?action=snapshot`, through nginx), so a relative one is resolved
    against the printer's host on its web port: Moonraker's own port is dropped."""
    if urlsplit(snapshot).scheme in ("http", "https"):
        return snapshot
    if printer_url is None:
        return urljoin("http://127.0.0.1/", snapshot)
    parts = urlsplit(_http_base(printer_url))
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None and parts.port != MOONRAKER_PORT:
        host = f"{host}:{parts.port}"
    return urljoin(f"{parts.scheme}://{host}/", snapshot)


def _http_base(url: str) -> str:
    """The HTTP address for a configured Moonraker URL, which may be its WebSocket."""
    parts = urlsplit(url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    path = parts.path.rstrip("/")
    if path.endswith("/websocket"):
        path = path[: -len("/websocket")]
    return f"{scheme}://{parts.netloc}{path}"


def _thumbnail_path(filename: str, relative: str) -> str | None:
    """The thumbnail's path in the gcodes folder: relative to its file's own folder."""
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(filename), relative))
    if joined.startswith(("../", "/")) or joined in ("..", "."):
        return None
    return joined


async def _gcodes_root(call: Call) -> Path:
    for root in await call("server.files.roots", None) or []:
        if root.get("name") == "gcodes" and root.get("path"):
            return Path(root["path"])
    raise ImageUnavailable("Moonraker has no gcodes folder")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
