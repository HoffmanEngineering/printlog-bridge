"""What the bridge knows of the FlashForge Adventurer 5M series' and the AD5X's LAN API, and
where each fact comes from. **Built from community sources, not a recorded printer** (#88):
GhostTypes/ff-5mp-api-py (MIT: `flashforge/api/constants/endpoints.py`,
`models/machine_info.py`, `discovery/discovery.py`), GhostTypes/ff-5mp-hass (MIT), and the
facts only of GhostTypes/ff-5mp-api-ts (no license file: nothing of it is copied). Orca-Flashforge
(AGPL) wraps a vendor library and says nothing of the protocol; it wasn't used. Every point not
confirmed by more than one source is marked ASSUMPTION.

**The HTTP API**, port 8898 (firmware 2.x and newer on the 5M and 5M Pro, every AD5X): each
request is a `POST` with a JSON body naming the printer's serial number and its check code,
`{"serialNumber": "SN...", "checkCode": "..."}`, and each answer is `{"code": 0, "message":
"Success", ...}` with what was asked under `detail` or `product`. Another `code` is a refusal
(ASSUMPTION: the sources don't give the code for a wrong check code, so any other code is taken
as one). The bridge sends only `READ_ONLY`:

- `/detail`: the whole status (`View`), polled.
- `/product`: which controls the printer has (six flags); `check()` asks it as `doctor`'s test
  of the check code, as ff-5mp-api-py does.
- `/gcodeList`: the last files printed (at most 10). On the AD5X it adds `gcodeListDetail`, each
  file's tools with their IFS slot and grams (`filament_by_slot`).
- `/gcodeThumb` with `fileName`: a file's thumbnail as base64 `imageData`.

`/control` (lights, fans, pause, cancel, the IFS), `/printGcode` and `/uploadGcode` change the
printer, and are never sent: `FlashForgeClient.post` refuses anything not in `READ_ONLY`.

**The legacy TCP interface** on port 8899 (`~M115`, `~M119`, `~M27`) is not used: every source
sends `~M601 S1` first, which takes control of the printer from FlashPrint or Orca, and none
says whether the reads work without it. `doctor` only checks whether something listens there,
to say the firmware is too old for the HTTP API.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

HTTP_PORT = 8898
TCP_PORT = 8899
"""The legacy G-code port, never sent anything (see above)."""
MOONRAKER_PORT = 7125
"""Where Moonraker answers on a printer with the Klipper Mod, Forge-X or ZMOD firmware."""

DETAIL, PRODUCT, GCODE_LIST, GCODE_THUMB = "/detail", "/product", "/gcodeList", "/gcodeThumb"
READ_ONLY = frozenset({DETAIL, PRODUCT, GCODE_LIST, GCODE_THUMB})
"""Every path the bridge may send. Each answers what it is asked and changes nothing."""

PRINTING, PAUSING, PAUSED, PAUSE = "printing", "pausing", "paused", "pause"
UNDER_WAY = frozenset({PRINTING, PAUSING, PAUSED, PAUSE})
"""A job is printing or paused (`pause` is reported by the Creator 5 Pro)."""
BUSY = frozenset({"busy", "heating", "calibrate_doing", "downloading"})
"""Busy: a job carries on through these (heating again after a pause, ASSUMPTION), and none
starts one."""
COMPLETED, CANCEL, ERROR = "completed", "cancel", "error"
ENDS = frozenset({COMPLETED, CANCEL, ERROR})
"""How a job ended. ASSUMPTION: they stay until the next job or until dismissed on the screen,
with the job's file and progress still in the status."""
READY = "ready"

MODELS = {35: "Adventurer 5M", 36: "Adventurer 5M Pro", 38: "AD5X", 40: "Creator 5",
          41: "Creator 5 Pro"}  # fmt: skip
"""`/detail`'s `pid`, also the product id in a discovery answer."""
NO_SENSOR_C = -50.0


@dataclass(frozen=True)
class Slot:
    """An AD5X IFS slot as `/detail` reports it: `slotId` 1 to 4 is the bridge's slot 0 to 3."""

    slot: int
    loaded: bool
    material: str | None
    color_hex: str | None


@dataclass(frozen=True)
class View:
    """What `/detail` says of the printer and its job."""

    state: str | None
    path: str | None
    """The job's file (`printFileName`), as given."""
    name: str
    progress: float | None
    """Percent, from `printProgress` (0 to 1)."""
    duration_s: float | None
    """`printDuration`: seconds since the job started."""
    remaining_s: float | None
    """`estimatedTime`: seconds left (the firmware counts it down only while printing)."""
    layer: int | None
    layers: int | None
    estimated_g: float | None
    """`estimatedRightWeight`: the job's grams, the slicer's estimate as the printer read it
    from the file (ff-5mp-api-py scales it by progress for "used so far": so it is the job's
    total, not a measurement). ASSUMPTION, from that code: the TS sources call it remaining."""
    estimated_mm: float | None
    """`estimatedRightLen`, the same in millimeters."""
    error_code: str | None
    firmware: str | None
    model: str | None
    name_on_screen: str | None
    slots: tuple[Slot, ...] = field(default_factory=tuple)
    slot_count: int = 0
    current_slot: int | None = None
    """The IFS slot feeding now (bridge numbering), when one is."""

    @property
    def under_way(self) -> bool:
        return self.state in UNDER_WAY and self.path is not None

    @property
    def busy(self) -> bool:
        return self.state in BUSY

    @property
    def ended(self) -> bool:
        return self.state in ENDS


def view_of(detail: Mapping[str, Any]) -> View:
    state = detail.get("status")
    state = state.strip().lower() if isinstance(state, str) and state.strip() else None
    path = _text(detail.get("printFileName"))
    progress = _number(detail.get("printProgress"))
    if progress is not None:
        progress = min(max(progress * 100 if progress <= 1.0 else progress, 0.0), 100.0)
    layer, layers = _number(detail.get("printLayer")), _number(detail.get("targetPrintLayer"))
    pid = detail.get("pid")
    model = _text(detail.get("model")) or (
        MODELS.get(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None
    )
    slots, count, current = _station(detail)
    return View(
        state=state,
        path=path,
        name=posixpath.basename(path.replace("\\", "/")) if path else "",
        progress=progress,
        duration_s=_positive(detail.get("printDuration")),
        remaining_s=_positive(detail.get("estimatedTime")),
        layer=int(layer) if layer is not None else None,
        layers=int(layers) if layers else None,
        estimated_g=_positive(detail.get("estimatedRightWeight")),
        estimated_mm=_positive(detail.get("estimatedRightLen")),
        error_code=_text(detail.get("errorCode")),
        firmware=_text(detail.get("firmwareVersion")),
        model=model,
        name_on_screen=_text(detail.get("name")),
        slots=slots,
        slot_count=count,
        current_slot=current,
    )


def _station(detail: Mapping[str, Any]) -> tuple[tuple[Slot, ...], int, int | None]:
    """The IFS: `matlStationInfo.slotInfos`, each `slotId` (1 to 4), `hasFilament`,
    `materialName`, `materialColor` (`#RRGGBB`). `hasMatlStation` is left out by some firmware
    that still fills the slots, so a station is `slotCnt` above 0."""
    info = detail.get("matlStationInfo")
    if not isinstance(info, Mapping):
        return (), 0, None
    count = info.get("slotCnt")
    count = count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0
    slots = []
    for entry in info.get("slotInfos") or []:
        if not isinstance(entry, Mapping):
            continue
        slot_id = entry.get("slotId")
        if not isinstance(slot_id, int) or isinstance(slot_id, bool) or not 1 <= slot_id <= 16:
            continue
        loaded = entry.get("hasFilament") is True
        slots.append(
            Slot(
                slot=slot_id - 1,
                loaded=loaded,
                material=_material(entry.get("materialName")) if loaded else None,
                color_hex=_color(entry.get("materialColor")) if loaded else None,
            )
        )
    current = info.get("currentSlot")
    current = current - 1 if isinstance(current, int) and 1 <= current <= 16 else None
    return tuple(sorted(slots, key=lambda s: s.slot)), max(count, len(slots)), current


def filament_by_slot(answer: Any, path: str) -> dict[int, float]:
    """The grams per IFS slot of the file `path` from `/gcodeList`'s `gcodeListDetail` (AD5X
    only): each `gcodeToolDatas` entry's `slotId` (1 to 4; 0 is the direct feed, slot 0) and
    `filamentWeight`. Empty for another model or a file not listed.
    ASSUMPTION: from ff-5mp-api-py's models; the units (grams) are its."""
    if not isinstance(answer, Mapping):
        return {}
    name = posixpath.basename(path.replace("\\", "/"))
    for entry in answer.get("gcodeListDetail") or []:
        if not isinstance(entry, Mapping):
            continue
        file = _text(entry.get("gcodeFileName"))
        if file is None or posixpath.basename(file.replace("\\", "/")) != name:
            continue
        grams: dict[int, float] = {}
        for tool in entry.get("gcodeToolDatas") or []:
            if not isinstance(tool, Mapping):
                continue
            slot_id, weight = tool.get("slotId"), _positive(tool.get("filamentWeight"))
            if not isinstance(slot_id, int) or isinstance(slot_id, bool) or weight is None:
                continue
            slot = max(slot_id - 1, 0)
            grams[slot] = round(grams.get(slot, 0.0) + weight, 3)
        return grams
    return {}


_COLOR = re.compile(r"#?([0-9A-Fa-f]{6})")


def _color(value: Any) -> str | None:
    match = _COLOR.fullmatch(value.strip()) if isinstance(value, str) else None
    return f"#{match.group(1).upper()}" if match else None


def _material(value: Any) -> str | None:
    text = _text(value)
    return None if text in (None, "?") else text


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number else None


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None
