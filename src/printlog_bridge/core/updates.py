"""Whether a newer printlog-bridge is out, and what to tell the user (#39).

3D Print Log answers each connection check-in with `latestAgentVersion`, the newest release it
read from this repository's tags, so the bridge never asks anyone else. Local installs also get
updates through Moonraker's update manager; Docker and remote-mode users only hear of one here.
"""

from __future__ import annotations

import re
from typing import Any

from printlog_bridge import __version__

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?!\d)")

HOW_TO_UPDATE = (
    "In Docker: `docker compose pull && docker compose up -d`. On the printer: update "
    "printlog-bridge in Mainsail's or Fluidd's update manager."
)


def _parse(text: Any) -> tuple[int, int, int] | None:
    """The leading X.Y.Z of `text` (a `v` before it is allowed), or None."""
    if not isinstance(text, str):
        return None
    match = _VERSION.match(text.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def is_newer(latest: Any, current: str = __version__) -> bool:
    """Whether `latest` is a newer release than `current`. False when either isn't a version, so
    an answer the bridge doesn't understand never tells anyone to update. Only the first three
    numbers count: 0.2.0.dev1 is 0.2.0."""
    newest = _parse(latest)
    have = _parse(current)
    return newest is not None and have is not None and newest > have


def update_notice(latest: str, current: str = __version__) -> str:
    return f"printlog-bridge {latest} is available (this is {current}). {HOW_TO_UPDATE}"
