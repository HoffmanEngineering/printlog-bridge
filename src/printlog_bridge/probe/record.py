"""The probe's trace: every send (checked against the allow-list first), every answer and every
passive message, with timestamps and size caps."""

from __future__ import annotations

import base64
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

from printlog_bridge.probe import allowlist

MAX_BODY = 64 * 1024
"""An HTTP body is kept up to this many bytes."""
MAX_MESSAGE = 16 * 1024
"""A websocket, MQTT or UDP message is kept up to this many bytes."""
MAX_PER_CHANNEL = 300
"""Messages kept per channel (one port's websocket, the MQTT broker...); later ones are counted."""
MAX_EVENTS = 5000


def payload(data: bytes | str, cap: int = MAX_MESSAGE) -> dict[str, Any]:
    """`data` as JSON: text when it is UTF-8, else base64; cut at `cap` bytes."""
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    kept = raw[:cap]
    out: dict[str, Any] = {"size": len(raw)}
    try:
        text = kept.decode("utf-8")
    except UnicodeDecodeError as e:
        # Cut in the middle of a character: keep what decodes, when that is nearly all of it.
        if len(raw) > cap and e.start >= len(kept) - 3:
            text = kept[: e.start].decode("utf-8")
        else:
            text = None
    if text is not None and _printable(text):
        out["text"] = text
    else:
        out["base64"] = base64.b64encode(kept).decode("ascii")
    if len(raw) > cap:
        out["truncated"] = True
    return out


def _printable(text: str) -> bool:
    if not text:
        return True
    controls = sum(1 for c in text if ord(c) < 32 and c not in "\r\n\t")
    return controls <= len(text) // 20


class Recorder:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.started = clock()
        self.events: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.sent: list[tuple[str, Any]] = []
        """Every (rule, payload) sent, in order, for tests."""
        self._per_channel: Counter[str] = Counter()
        self.dropped: Counter[str] = Counter()

    def _add(self, channel: str, kind: str, data: dict[str, Any]) -> None:
        if kind == "receive":
            self._per_channel[channel] += 1
            if self._per_channel[channel] > MAX_PER_CHANNEL:
                self.dropped[channel] += 1
                return
        if len(self.events) >= MAX_EVENTS:
            self.dropped[channel] += 1
            return
        now = self.clock()
        event = {
            "t": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds"),
            "dt": round(now - self.started, 3),
            "channel": channel,
            "kind": kind,
        }
        event.update(data)
        self.events.append(event)

    def send(self, rule: str, channel: str, checked: Any, **data: Any) -> None:
        """Checks `checked` against `rule` (raising `allowlist.NotAllowed`), then records it.
        Call it before the bytes go out."""
        allowlist.check(rule, checked)
        self.sent.append((rule, checked))
        self._add(channel, "send", {"rule": rule, **data})

    def receive(self, channel: str, **data: Any) -> None:
        self._add(channel, "receive", data)

    def error(self, channel: str, message: str) -> None:
        self._add(channel, "error", {"error": message})

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def received(self, channel: str) -> int:
        """How many messages came in on `channel`, kept or not."""
        return self._per_channel[channel]
