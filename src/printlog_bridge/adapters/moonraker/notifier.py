"""An older way of logging prints still set up: a Moonraker notifier that posts to 3D Print Log.

Before the bridge, 3D Print Log's guide had users add a `[notifier ...]` section to
moonraker.conf whose Apprise URL posts each finished job to the API
(`jsons://api.3dprintlog.com/api/Moonraker/notifier?+X-Api-Key=...`). With the bridge running
too, both log the print. The bridge can't remove the section (moonraker.conf is the user's), so
it reads Moonraker's config on every connect, and while such a section is there it says which
one to remove, in the log, `printlog-bridge status` and `doctor`.

Only the section's name and file are ever shown: its URL holds the user's API key.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from printlog_bridge.core.status import PrinterReport

from .session import Call, MoonrakerListener, MoonrakerRpcError

log = logging.getLogger(__name__)

TOPIC = "notifier"
"""The `PrinterReport.warn` topic."""
HOST = "3dprintlog.com"
"""A notifier URL to this host, or one under it, posts to 3D Print Log."""
DEFAULT_FILE = "moonraker.conf"
"""Where a section is said to be when Moonraker doesn't list its files (before v0.8)."""

_URL_SEPARATORS = re.compile(r"[\s,]+")
"""What Apprise and Moonraker allow between several URLs in one `url` option."""


@dataclass(frozen=True)
class OldNotifier:
    section: str
    """The section's name, as in moonraker.conf: `notifier 3d_print_log`."""
    file: str
    """The file it's in, relative to Klipper's config folder: `moonraker.conf`."""


def old_notifiers(answer: Mapping[str, Any]) -> list[OldNotifier]:
    """The `[notifier ...]` sections in a `server.config` answer that post to 3D Print Log."""
    config = answer.get("config")
    if not isinstance(config, Mapping):
        return []
    files = _files_of(answer.get("files"))
    return [
        OldNotifier(section, files.get(section, DEFAULT_FILE))
        for section, options in config.items()
        if _is_notifier(section)
        and isinstance(options, Mapping)
        and any(_posts_to_printlog(url) for url in _urls(options.get("url")))
    ]


def message(found: Sequence[OldNotifier]) -> str:
    """What the user is told to do, naming each section and its file, never its URL."""
    sections = [f"[{n.section}] in {n.file}" for n in found]
    listed = sections[-1]
    if len(sections) > 1:
        listed = ", ".join(sections[:-1]) + " and " + listed
    one = len(found) == 1
    logs, it = ("logs", "that section") if one else ("log", "each of those sections")
    return (
        f"Moonraker's {listed} also {logs} prints to 3D Print Log, so each print can be logged "
        f"twice. The bridge logs them itself: remove {it} (its [notifier ...] line and every "
        "line up to the next section) and restart Moonraker."
    )


class NotifierCheck(MoonrakerListener):
    """A MoonrakerListener that looks for an old 3D Print Log notifier on every connect.

    Moonraker reads its config only when it starts, so a section removed is gone after the
    restart that follows, which is a reconnect here, and the warning is cleared then.
    """

    def __init__(self, printer: str, report: PrinterReport) -> None:
        self.printer = printer
        self.report = report
        self.found: list[OldNotifier] = []
        """What the last read found, so it's logged when it changes rather than on every
        reconnect."""

    async def on_connected(self, call: Call) -> None:
        try:
            answer = await call("server.config", None)
        except (MoonrakerRpcError, asyncio.TimeoutError) as e:
            # Only a check: what's known stays as it was, and the next connect tries again.
            log.debug("[printer %s] couldn't read Moonraker's config: %s", self.printer, e)
            return
        found = old_notifiers(answer) if isinstance(answer, Mapping) else []
        if found == self.found:
            return
        self.found = found
        if found:
            text = message(found)
            log.warning("[printer %s] %s", self.printer, text)
            self.report.warn(TOPIC, text)
        else:
            log.info(
                "[printer %s] Moonraker no longer has a notifier logging prints to 3D Print Log",
                self.printer,
            )
            self.report.warn(TOPIC, None)

    async def on_klippy_state(self, state: str) -> None:
        pass

    async def on_status(self, status: Mapping[str, Any], changed: Mapping[str, Any]) -> None:
        pass

    async def on_history(self, action: str, job: Mapping[str, Any]) -> None:
        pass

    async def on_gcode_response(self, line: str) -> None:
        pass


def _is_notifier(section: object) -> bool:
    return isinstance(section, str) and section.split(maxsplit=1)[:1] == ["notifier"]


def _urls(value: object) -> list[str]:
    if isinstance(value, str):
        return [u for u in _URL_SEPARATORS.split(value) if u]
    if isinstance(value, (list, tuple)):
        return [u for item in value for u in _urls(item)]
    return []


def _posts_to_printlog(url: str) -> bool:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return host is not None and (host == HOST or host.endswith("." + HOST))


def _files_of(files: object) -> dict[str, str]:
    """Each section's file, from `server.config`'s `files`: `[{filename, sections}]`."""
    out: dict[str, str] = {}
    if not isinstance(files, list):
        return out
    for entry in files:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("filename"), str):
            continue
        for section in entry.get("sections") or []:
            if isinstance(section, str):
                out.setdefault(section, entry["filename"])
    return out
