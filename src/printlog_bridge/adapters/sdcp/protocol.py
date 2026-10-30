"""What the bridge knows of SDCP v3 (Smart Device Control Protocol), the local protocol of the
Elegoo Centauri Carbon, described in our own words.

Sources, read October 2026: the protocol's published description (cbd-tech,
`SDCP-Smart-Device-Control-Protocol-V3.0.0`, no license given, so nothing is copied from it),
Elegoo's own client library (elegooofficial/elegoo-link, Apache-2.0,
`src/lan/adapters/elegoo_fdm_cc/`), the printer's firmware source (elegooofficial/CentauriCarbon,
GPL-3.0, `print_stats_c.h`: read for facts only), and two community write-ups recorded from a
Centauri Carbon (WalkerFrederick/sdcp-centauri-carbon, firmware 1.1.29;
danielcherubini/elegoo-homeassistant, `sdcp/models/`). No printer was recorded for this
adapter: every point marked ASSUMPTION below wants checking against a trace (`trace = true`).

- **Discovery**: the text `M99999` to UDP port 3000 (broadcast) is answered by each printer with
  JSON naming it: `Data` holds `Name`, `MachineName` (the model), `MainboardID`, `MainboardIP`,
  `FirmwareVersion` and `ProtocolVersion`.
- **Transport**: a websocket at `ws://<printer>:3030/websocket`, with no authentication at all.
  The client's text `ping` is answered `pong`. ASSUMPTION (community write-up): the printer
  closes a socket after about 60 s with nothing from the client, so the bridge pings.
- **Requests** go out as `{"Id", "Data": {"Cmd", "Data", "RequestID", "MainboardID",
  "TimeStamp", "From"}, "Topic": "sdcp/request/<MainboardID>"}`. The bridge only ever sends
  Cmd 0 (push the status again) and Cmd 1 (push the attributes again), with empty `Data`. The
  others start, pause and stop prints, rename the printer or delete files: never sent.
  ASSUMPTION (community write-up): an empty `MainboardID` is accepted before the bridge has
  learned the printer's; it learns it from the first message and keeps it.
- **Pushes**: `{"Status": {...}, "MainboardID", "TimeStamp", "Topic": "sdcp/status/<id>"}` when
  the status changes or after Cmd 0, and `{"Attributes": {...}, ...}` likewise after Cmd 1.
  Answers to requests come on `sdcp/response/<id>` as `{"Data": {"Cmd", "Data": {"Ack": 0},
  "RequestID", ...}}`. ASSUMPTION: `TimeStamp` is the printer's clock in epoch seconds (the
  spec's example is), which the bridge uses only when it is plausibly synced.
- **Status**: `CurrentStatus` (a list: 0 idle, 1 printing, 2 file transfer...) and `PrintInfo`:
  `Status` (the print state, below), `Filename`, `TaskId`, `CurrentLayer`, `TotalLayer`,
  `CurrentTicks` and `TotalTicks` (print time so far and the estimate), `Progress` (percent).
  ASSUMPTION: ticks are seconds on the Centauri Carbon (elegoo-link and the Home Assistant
  integration both read them so; the resin spec says milliseconds). ASSUMPTION: a push may
  carry only what changed, so pushes are merged into the last full status (a full status after
  Cmd 0 resets nothing the bridge relies on).
- **The print state** (`PrintInfo.Status`) is the Centauri Carbon's own table, not the resin
  spec's: the firmware's `print_stats_state_s` and elegoo-link's agree on it (`PRINT_STATES`).
  The spec says it keeps its last value: after an end it stays `complete`, `stopped` or `error`
  until the next print, which is how the bridge tells how a job ended while it was away.

Not used: history (Cmd 320/321, task ids and details; its fields are described only for resin
printers), the camera (Cmd 386, which turns a stream on), and file transfer.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

WS_PORT = 3030
WS_PATH = "/websocket"
DISCOVERY_PORT = 3000
DISCOVERY_ASK = b"M99999"
PING, PONG = "ping", "pong"

STATUS_REFRESH, ATTRIBUTES = 0, 1
READ_ONLY_COMMANDS = frozenset({STATUS_REFRESH, ATTRIBUTES})
"""The only commands the bridge sends."""
FROM_LAN_PC = 0
"""`From`: a program on the local network (spec, "From")."""

PRINT_STATES: Mapping[int, str] = {
    0: "idle",
    1: "homing",
    2: "dropping",  # resin only
    3: "exposing",  # resin only
    4: "lifting",  # resin only
    5: "pausing",
    6: "paused",
    7: "stopping",
    8: "stopped",
    9: "complete",
    10: "checking file",
    11: "self-checking",
    12: "resuming",
    13: "printing",
    14: "error",
    15: "leveling",
    16: "preheating",
    17: "resonance testing",
    18: "starting",
    19: "leveling done",
    20: "preheating done",
    21: "homing done",
    22: "resonance testing done",
    23: "checking for foreign objects",
    24: "foreign object check done",
    25: "changing filament",
    26: "filament changed",
}
"""`PrintInfo.Status` on a Centauri Carbon. 23 to 26 are named for the firmware's
`print_stats_state_s` (elegoo-link names them for a resin feeder); either way they come up
during a print, not instead of one."""

PRINTING, PAUSING, PAUSED, RESUMING = 13, 5, 6, 12
STOPPED, COMPLETE, ERROR, IDLE = 8, 9, 14, 0
UNDER_WAY = frozenset({PRINTING, PAUSING, PAUSED, RESUMING})
"""A job is printing or paused: the only states a job starts in. Its file and task are the
running job's."""
BUSY = frozenset(PRINT_STATES) - UNDER_WAY - {STOPPED, COMPLETE, ERROR, IDLE}
"""The start routine (homing, preheating, leveling, "starting"), stopping, a filament change:
part of a running job, but never the start of one, since the last job's file and task may still
show (the spec: the print state keeps its last value)."""
ENDS = frozenset({STOPPED, COMPLETE, ERROR})


def request(cmd: int, board: str, now: float | None = None) -> str:
    """A read-only request, compact JSON. Raises ValueError for any command that isn't one."""
    if cmd not in READ_ONLY_COMMANDS:
        raise ValueError(f"the bridge never sends SDCP command {cmd}")
    message = {
        "Id": uuid.uuid4().hex,
        "Data": {
            "Cmd": cmd,
            "Data": {},
            "RequestID": uuid.uuid4().hex,
            "MainboardID": board,
            "TimeStamp": int(now if now is not None else time.time()),
            "From": FROM_LAN_PC,
        },
        "Topic": f"sdcp/request/{board}",
    }
    return json.dumps(message, separators=(",", ":"))


def kind_of(message: Mapping[str, Any]) -> str:
    """`status`, `attributes`, `response`, `notice`, `error` or `other`: the topic's second part,
    else what the message holds."""
    topic = message.get("Topic")
    if isinstance(topic, str):
        parts = topic.split("/")
        if len(parts) >= 2 and parts[0] == "sdcp" and parts[1]:
            return parts[1] if parts[1] in KINDS else "other"
    if isinstance(message.get("Status"), Mapping):
        return "status"
    if isinstance(message.get("Attributes"), Mapping):
        return "attributes"
    return "other"


KINDS = ("status", "attributes", "response", "notice", "error")


def board_of(message: Mapping[str, Any]) -> str | None:
    """The `MainboardID` a message names, at its top or in its `Data`."""
    for where in (message, message.get("Data")):
        if isinstance(where, Mapping):
            board = where.get("MainboardID")
            if isinstance(board, str) and board.strip():
                return board.strip()
    return None
