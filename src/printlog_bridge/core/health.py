"""The problems the connection check-in reports: how many, and the last one, for each printer.

`ProblemLog` is a logging handler on the bridge's own logger. It counts every warning and error
the bridge logs, by printer (a message starting `[printer <name>]` is that printer's, any other
is every printer's), and keeps the last one, scrubbed. The check-in sends the count since the
last check-in that got through, and the last problem, so 3D Print Log can show a bridge that is
failing (#37).

What leaves the printer is scrubbed harder than the log: no host name, IP address, path, file
name, quoted text, printer name or secret, only what went wrong ("Moonraker at <url> didn't
answer"). docs/privacy.md lists it; tests/test_privacy.py and tests/test_health.py hold it.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from printlog_bridge.core.secrets import REDACTED

MAX_PROBLEM_CHARS = 300

_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"]*[^\s'\":,;.)\]]")
_USERINFO = re.compile(r"[^\s@/'\"]+:[^\s@/'\"]+@")
"""A user and password without a scheme, as in `user:password@host`."""
_QUOTED = re.compile(r"(?<!\w)'[^'\n]*'(?!\w)|(?<!\w)\"[^\"\n]*\"(?!\w)")
"""Quoted text, which is how file names and repr()s turn up. A quote inside a word (`isn't`) is
an apostrophe, not the start of a quote."""
_PATH = re.compile(r"[^\s'\"]*[\\/][^\s'\"]*[^\s'\":,;.)\]]|[^\s'\"]*[\\/]")
_IPV6_BRACKETED = re.compile(r"\[[0-9A-Fa-f.]*:[0-9A-Fa-f:.]*\](?::\d{1,5})?")
_IPV6 = re.compile(r"(?<![\w:])[0-9A-Fa-f]*:[0-9A-Fa-f:]*:[0-9A-Fa-f]*(?![\w:])")
_IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?(?![\w.])")
_HOST_PORT = re.compile(r"(?<![\w.<>-])[A-Za-z0-9][\w.-]*:\d{1,5}(?!\w)")
_DOTTED = re.compile(r"(?<![\w.<-])[\w-]+(?:\.[\w-]+)*\.(?=[\w-]*[A-Za-z])[\w-]+(?![\w-])")
"""A host name or a file name: dotted, ending in letters (`u1.local`, `benchy.gcode`). A version
(`1.2.3`, `2.0.0.205_2026...`) ends in a digit and stays."""
_SPACES = re.compile(r"\s+")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def scrub_problem(message: str, printers: Iterable[str], secrets: Iterable[str] = ()) -> str:
    """`message` as the check-in may carry it: without its `[printer <name>]` prefix, and with
    every secret, URL, path, address, host, file name, quoted text and printer name replaced."""
    printers = sorted(printers, key=len, reverse=True)
    for printer in printers:
        prefix = f"[printer {printer}]"
        if message.startswith(prefix):
            message = message[len(prefix) :]
            break
    text = message
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = _URL.sub("<url>", text)
    text = _USERINFO.sub(f"{REDACTED}@", text)
    text = _QUOTED.sub("'<text>'", text)
    text = _PATH.sub("<path>", text)
    text = _IPV6_BRACKETED.sub("<ip>", text)
    text = _IPV4.sub("<ip>", text)
    text = _IPV6.sub("<ip>", text)
    text = _HOST_PORT.sub("<host>", text)
    text = _DOTTED.sub("<name>", text)
    for printer in printers:
        text = re.sub(rf"(?<![\w<-]){re.escape(printer)}(?![\w>-])", "<printer>", text)
    text = _SPACES.sub(" ", text).strip()
    if len(text) > MAX_PROBLEM_CHARS:
        text = text[: MAX_PROBLEM_CHARS - 1] + "…"
    return text


@dataclass(frozen=True)
class Problems:
    """One printer's problems, for its check-in."""

    count: int
    """Warnings and errors logged since the last check-in that got through."""
    last: str | None
    """The last one, scrubbed."""
    last_at: datetime | None


class ProblemLog(logging.Handler):
    def __init__(
        self,
        printers: Iterable[str],
        secrets: Iterable[str] = (),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__(logging.WARNING)
        self.printers = list(printers)
        self.secrets = list(secrets)
        self.clock = clock
        self._counts = {p: 0 for p in self.printers}
        self._last: dict[str, tuple[str, datetime]] = {}
        self._guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < self.level:
            return
        try:
            message = record.getMessage()
            mine = [p for p in self.printers if message.startswith(f"[printer {p}]")]
            scrubbed = scrub_problem(message, self.printers, self.secrets)
            now = self.clock()
        except Exception:  # counting a problem must never become one
            return
        with self._guard:
            for printer in mine[:1] or self.printers:
                self._counts[printer] += 1
                self._last[printer] = (scrubbed, now)

    def problems(self, printer: str) -> Problems:
        with self._guard:
            last, at = self._last.get(printer, (None, None))
            return Problems(self._counts.get(printer, 0), last, at)

    def reported(self, printer: str, count: int) -> None:
        """A check-in carrying `count` problems got through; those logged since stay counted."""
        with self._guard:
            if printer in self._counts:
                self._counts[printer] = max(0, self._counts[printer] - count)
