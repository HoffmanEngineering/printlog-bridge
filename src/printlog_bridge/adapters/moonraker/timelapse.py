"""A job's moonraker-timelapse video, to attach to its print (#42).

moonraker-timelapse is a Moonraker component (`[timelapse]` in moonraker.conf) that takes frames
during a print and renders them into an MP4 in its `timelapse` file root: on its own when the
print is done (`autorender`, its default), or when `TIMELAPSE_RENDER` runs in the print's end
macro, before the job ends. It announces the render with `notify_timelapse_event`, the success as
`{"action": "render", "status": "success", "filename": "timelapse_<file>_<time>.mp4",
"printfile": "<the job's file, without folders>"}`.

When a job ends while the bridge watches, the watcher reports the video rendered for its file
as a JobAttachment: one already announced in the last `WAIT_S`, or the first announced in the
`WAIT_S` that follow. Nothing waits on it; the print is completed meanwhile. The component is
looked for in `server.info` on every connect: without it, nothing is asked for or fetched.
Remote mode downloads the video from Moonraker's file API, local mode copies it from the root.
Only a plain `.mp4` file name in that root is ever fetched.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import posixpath
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from printlog_bridge.core.events import AttachmentKind, EventSink, JobAttachment
from printlog_bridge.core.images import FileUnavailable, copy_file, download_file

from .images import _http_base, _inside
from .session import Call, ConnectionLost, MoonrakerRpcError

log = logging.getLogger(__name__)

COMPONENT = "timelapse"
ROOT = "timelapse"
"""The file root moonraker-timelapse keeps its videos in."""
WAIT_S = 10 * 60.0
"""How far apart a job's end and its video's render may be."""
FETCH_TIMEOUT_S = 10 * 60.0
VIDEO_SUFFIX = ".mp4"


class TimelapseWatcher:
    def __init__(
        self,
        printer: str,
        sink: EventSink,
        *,
        url: str | None,
        socket: Path | None,
        api_key: str | None = None,
        wait: float = WAIT_S,
        timeout: float = FETCH_TIMEOUT_S,
    ) -> None:
        self.printer = printer
        self.sink = sink
        self.url = url
        self.socket = socket
        self.api_key = api_key
        self.wait = wait
        self.timeout = timeout
        self.available = False
        """Whether Moonraker has the timelapse component, as of the last connect."""
        self._call: Call | None = None
        self._ended: tuple[str, str, float] | None = None
        """The job that ended last, its file without folders, and when (loop time)."""
        self._rendered: tuple[str, str, float] | None = None
        """The last video rendered, the job file it's of, and when (loop time)."""
        self._tasks: set[asyncio.Future] = set()

    async def on_connected(self, call: Call) -> None:
        self._call = call
        try:
            info = await call("server.info", None) or {}
        except MoonrakerRpcError:
            info = {}
        available = COMPONENT in (info.get("components") or [])
        if available and not self.available:
            log.info(
                "[printer %s] moonraker-timelapse found: each print's video is attached to it "
                "(Pro; `timelapse = false` turns this off)",
                self.printer,
            )
        self.available = available

    def job_ended(self, job_id: str, file_name: str) -> None:
        """A job ended while the bridge watched: its video is the one rendered for its file."""
        if not self.available:
            return
        now = asyncio.get_running_loop().time()
        name = posixpath.basename(file_name.replace("\\", "/"))
        rendered = self._rendered
        if rendered is not None and rendered[1] == name and now - rendered[2] <= self.wait:
            # Rendered by TIMELAPSE_RENDER in the end macro, before the job ended.
            self._rendered = None
            self._attach(job_id, rendered[0])
            return
        self._ended = (job_id, name, now)

    async def on_event(self, params: Any) -> None:
        """`notify_timelapse_event`."""
        event = params[0] if isinstance(params, list) and params else params
        if not isinstance(event, Mapping) or not self.available:
            return
        if event.get("action") != "render" or event.get("status") != "success":
            return
        video = str(event.get("filename") or "")
        if not _plain_video_name(video):
            log.warning(
                "[printer %s] moonraker-timelapse rendered %r, which isn't an .mp4 in its "
                "folder; not attaching it",
                self.printer, video,
            )  # fmt: skip
            return
        printfile = str(event.get("printfile") or "")
        now = asyncio.get_running_loop().time()
        ended = self._ended
        if ended is not None and now - ended[2] > self.wait:
            ended = self._ended = None  # too long after: this video can't be that job's
        if ended is not None and (not printfile or printfile == ended[1]):
            self._ended = None
            self._attach(ended[0], video)
        else:
            self._rendered = (video, printfile, now)

    def _attach(self, job_id: str, video: str) -> None:
        attachment = JobAttachment(
            self.printer,
            job_id,
            AttachmentKind.TIMELAPSE,
            video,
            functools.partial(self.fetch, video),
        )
        # Not awaited: fetching a video takes a while, and Moonraker's other news can't wait.
        task = asyncio.ensure_future(self.sink.emit(attachment))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def fetch(self, video: str, path: Path, max_bytes: int) -> bool:
        """Writes the video to `path`."""
        if not _plain_video_name(video):
            raise FileUnavailable(f"{video!r} isn't an .mp4 in the {ROOT} folder")
        if self.url is not None:
            headers = {"X-Api-Key": self.api_key} if self.api_key else None
            url = f"{_http_base(self.url)}/server/files/{ROOT}/{quote(video)}"
            await download_file(
                url, path, timeout=self.timeout, max_bytes=max_bytes, headers=headers
            )
            return True
        root = await self._root()
        source = (root / video).resolve()
        if not _inside(source, root.resolve()) or not source.is_file():
            raise FileUnavailable(f"{video} isn't in the {ROOT} folder")
        await copy_file(source, path, max_bytes=max_bytes)
        return True

    async def _root(self) -> Path:
        call = self._call
        if call is None:
            raise FileUnavailable("not connected to Moonraker")
        try:
            roots = await call("server.files.roots", None) or []
        except (MoonrakerRpcError, ConnectionLost) as e:
            raise FileUnavailable(f"Moonraker couldn't list its folders: {e}") from None
        for root in roots:
            if root.get("name") == ROOT and root.get("path"):
                return Path(root["path"])
        raise FileUnavailable(f"Moonraker has no {ROOT} folder")

    async def close(self) -> None:
        """Stops the fetches still running, and waits for them to stop."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _plain_video_name(name: str) -> bool:
    """A file name with no folder in it, as moonraker-timelapse names its videos."""
    return (
        bool(name)
        and name.lower().endswith(VIDEO_SUFFIX)
        and "/" not in name
        and "\\" not in name
        and not name.startswith(".")
    )
