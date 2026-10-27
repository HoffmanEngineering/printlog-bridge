"""`trace = true`: a record of every message between the bridge and each printer (#17).

With tracing on, every JSON-RPC message the bridge sends to or receives from Moonraker goes, one
per line, into `state_dir/traces/<printer>-<YYYYMMDD>.jsonl`:

    {"t": 1791167314.36, "dir": "recv", "msg": {"jsonrpc": "2.0", "method": "notify_...", ...}}

`dir` is `send` or `recv`, or `open` and `close` where a connection starts and ends. A trace of
a real print is what turns it into a regression test (`tests/trace_replay.py`). Traces stay on
this computer; `doctor`'s support bundle includes the latest.

Secrets are removed as in the support bundle: every key, token and password the bridge knows of,
passwords in URLs and query values named like a secret (a webcam's `?token=`), any field named
like a key, token, password or access code, the answer to a request for a token or key
(`access.*`), and Moonraker's config (`server.config`), whose notifier URLs hold tokens no
scrubber can find.

Recording never holds up the bridge: `record` only queues the message, and a thread of the
trace's own scrubs and writes it. When the disk can't keep up, messages are dropped rather than
queued without end. A day's file is split into parts of `file_bytes`, and the oldest parts are
deleted to keep each printer's traces under `max_bytes`. A disk error is logged once, never
raised: tracing is a diagnostic, and the bridge keeps logging prints without it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Optional

from printlog_bridge.core.secrets import REDACTED, scrub
from printlog_bridge.core.state import safe_file_name

log = logging.getLogger(__name__)

TRACE_DIR = "traces"
MAX_BYTES = 50 * 1024 * 1024
"""Each printer's traces, all days together."""
FILE_BYTES = 10 * 1024 * 1024
"""One file; a busy day continues in `<printer>-<YYYYMMDD>.1.jsonl`, `.2`, ..."""
QUEUE_LIMIT = 10_000
CLOSE_TIMEOUT_S = 5.0

SEND, RECEIVE = "send", "recv"
SECRET_ANSWERS = frozenset(
    {
        "access.oneshot_token",
        "access.get_api_key",
        "access.post_api_key",
        "access.login",
        "access.refresh_jwt",
        "access.user.create",
        "server.config",
    }
)
"""Moonraker methods whose answer is itself a token or key, or holds ones that can't be told
apart from the rest (moonraker.conf's Apprise notifier URLs, `tgram://<bot>:<token>/...`)."""

_SECRET_FIELD = re.compile(
    r'("[^"\\]*(?:key|token|secret|passw|access_code)[^"\\]*"\s*:\s*)"(?:[^"\\]|\\.)*"',
    re.IGNORECASE,
)
_SECRET_METHOD = re.compile(r'"method"\s*:\s*"(access\.[^"]+|server\.config)"')

Item = Optional[tuple[float, str, str]]


def trace_dir(state_dir: Path) -> Path:
    return state_dir / TRACE_DIR


def trace_files(state_dir: Path, printer: str | None = None) -> list[Path]:
    """A printer's trace files, oldest first; or every printer's, oldest first by time."""
    folder = trace_dir(state_dir)
    try:
        candidates = [p for p in folder.iterdir() if p.is_file() and p.suffix == ".jsonl"]
    except OSError:
        return []
    if printer is None:
        return sorted(candidates, key=_modified)
    pattern = _file_pattern(printer)
    parts = []
    for path in candidates:
        match = pattern.fullmatch(path.name)
        if match:
            parts.append(((match.group(1), int(match.group(2) or 0)), path))
    return [path for _, path in sorted(parts)]


class Trace:
    """One printer's trace. `record` and `mark` are safe to call from the event loop."""

    def __init__(
        self,
        state_dir: Path,
        printer: str,
        *,
        secrets: Iterable[str] = (),
        max_bytes: int = MAX_BYTES,
        file_bytes: int = FILE_BYTES,
        queue_limit: int = QUEUE_LIMIT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.folder = trace_dir(state_dir)
        self.printer = printer
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self.max_bytes = max_bytes
        self.file_bytes = file_bytes
        self.clock = clock
        self.dropped = 0
        self._queue: queue.Queue[Item] = queue.Queue(maxsize=queue_limit)
        self._closed = False
        self._file: IO[str] | None = None
        self._path: Path | None = None
        self._day: str | None = None
        self._size = 0
        self._failing = False
        self._secret_ids: set[Any] = set()
        self._thread = threading.Thread(
            target=self._run, name=f"printlog-bridge trace {printer}", daemon=True
        )
        self._thread.start()

    def record(self, direction: str, raw: str) -> None:
        """One message as sent or received: `direction` is SEND or RECEIVE, `raw` its JSON."""
        self._put((self.clock(), direction, raw))

    def mark(self, event: str) -> None:
        """A connection `open`ed or `close`d."""
        self._put((self.clock(), event, ""))

    def flush(self) -> None:
        """Waits until everything recorded so far is written. For tests."""
        self._queue.join()

    def close(self) -> None:
        """Writes what is queued (for a few seconds at most) and stops the thread."""
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(None, timeout=CLOSE_TIMEOUT_S)
        except queue.Full:
            return  # the disk is stuck; the daemon thread dies with the process
        self._thread.join(CLOSE_TIMEOUT_S)

    def _put(self, item: Item) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            if not self.dropped:
                log.warning(
                    "[printer %s] the trace can't keep up with Moonraker's messages, so some "
                    "are left out of it",
                    self.printer,
                )
            self.dropped += 1

    # -- the writer thread ---------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    self._close_file()
                    return
                t, direction, raw = item
                self._write(self._line(t, direction, raw), t)
                if self._queue.empty() and self._file is not None:
                    self._file.flush()
            except OSError as e:
                self._failed(e)
                self._close_file()
            except Exception:  # a bug here must not end tracing, let alone the bridge
                log.exception("[printer %s] couldn't trace a message", self.printer)
            finally:
                self._queue.task_done()

    def _line(self, t: float, direction: str, raw: str) -> str:
        if direction not in (SEND, RECEIVE):
            return json.dumps({"t": t, "dir": direction}) + "\n"
        text = self._scrubbed(direction, raw.replace("\r", " ").replace("\n", " "))
        try:
            json.loads(text)
        except ValueError:
            return json.dumps({"t": t, "dir": direction, "text": text}) + "\n"
        # The message is spliced in as received: parsing and dumping it again would cost more.
        return f'{{"t": {json.dumps(t)}, "dir": "{direction}", "msg": {text}}}\n'

    def _scrubbed(self, direction: str, text: str) -> str:
        if direction == SEND and ('"access.' in text or '"server.config"' in text):
            match = _SECRET_METHOD.search(text)
            if match and match.group(1) in SECRET_ANSWERS:
                with contextlib.suppress(ValueError, AttributeError):
                    self._secret_ids.add(json.loads(text).get("id"))
        elif direction == RECEIVE and self._secret_ids and '"result"' in text:
            try:
                message = json.loads(text)
            except ValueError:
                message = None
            if isinstance(message, dict) and message.get("id") in self._secret_ids:
                self._secret_ids.discard(message["id"])
                message["result"] = REDACTED
                text = json.dumps(message)
        text = _SECRET_FIELD.sub(rf'\1"{REDACTED}"', text)
        return scrub(text, self.secrets)

    def _write(self, line: str, t: float) -> None:
        day = datetime.fromtimestamp(t).strftime("%Y%m%d")
        size = len(line.encode("utf-8"))
        if self._file is None or day != self._day or self._size + size > self.file_bytes:
            self._open(day, size)
        assert self._file is not None
        self._file.write(line)
        self._size += size
        if self._failing:
            log.info("[printer %s] the trace is written again", self.printer)
            self._failing = False

    def _open(self, day: str, size: int) -> None:
        self._close_file()
        self.folder.mkdir(parents=True, exist_ok=True)
        today = [
            p
            for p in trace_files(self.folder.parent, self.printer)
            if _day_of(self.printer, p) == day
        ]
        part = _part_of(self.printer, today[-1]) if today else 0
        path = self._path_for(day, part)
        if path.exists() and path.stat().st_size + size > self.file_bytes:
            path = self._path_for(day, part + 1)
        self._prune(keep=path)
        self._file = path.open("a", encoding="utf-8", newline="\n")
        self._path = path
        self._day = day
        self._size = path.stat().st_size

    def _prune(self, keep: Path) -> None:
        """Deletes the oldest files until the rest, and a full new file, fit in `max_bytes`."""
        others = [p for p in trace_files(self.folder.parent, self.printer) if p != keep]
        sizes = {p: p.stat().st_size for p in others}
        current = keep.stat().st_size if keep.exists() else 0
        total = sum(sizes.values()) + max(current, self.file_bytes)
        for path in others:
            if total <= self.max_bytes:
                break
            path.unlink()
            total -= sizes[path]

    def _path_for(self, day: str, part: int) -> Path:
        name = safe_file_name(self.printer)
        suffix = f".{part}" if part else ""
        return self.folder / f"{name}-{day}{suffix}.jsonl"

    def _close_file(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None

    def _failed(self, error: OSError) -> None:
        if not self._failing:
            log.warning(
                "[printer %s] couldn't write the trace in %s (%s); tracing is paused until it "
                "can be written again",
                self.printer, self.folder, error,
            )  # fmt: skip
        self._failing = True


def _file_pattern(printer: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(safe_file_name(printer))}-(\d{{8}})(?:\.(\d+))?\.jsonl")


def _day_of(printer: str, path: Path) -> str | None:
    match = _file_pattern(printer).fullmatch(path.name)
    return match.group(1) if match else None


def _part_of(printer: str, path: Path) -> int:
    match = _file_pattern(printer).fullmatch(path.name)
    return int(match.group(2) or 0) if match else 0


def _modified(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
