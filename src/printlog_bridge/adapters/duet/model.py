"""Reading RepRapFirmware's object model (docs.duet3d.com, "Object Model"), the same JSON whether
it comes from the board itself (`rr_model`) or from DuetSoftwareFramework (`/machine/model`).

What the bridge reads of it:

- `state.status`: `idle`, `processing` (printing a file), `pausing`, `paused`, `resuming`,
  `cancelling`, `changingTool`, `busy`, `simulating`, `halted` (after an emergency stop), `off`,
  and while it starts or updates, `starting`, `updating`, `disconnected`.
- `job.file.fileName`: the file printing, null between jobs (`job.file` may be null too).
  `job.duration` (seconds printing, pauses left out) and `job.pauseDuration`, `job.filePosition`
  (bytes), `job.rawExtrusion` (mm fed in the job, all extruders). When a job ends,
  `job.lastFileName` keeps its file, and `lastFileCancelled` (the user cancelled it) and
  `lastFileAborted` (it stopped otherwise) how it ended.
- `move.extruders[]`: `rawPosition`, the mm each extruder has fed since the job started, before
  the extrusion factor (M221) is applied, and that `factor`. Counted from the start of each
  print, so a bridge that restarts mid-print still gets the job's whole total.
- `tools[]`: each tool's `number` and the `extruders` it drives (a toolchanger's T2 may well
  drive extruder 0).
- `boards[0]`: `firmwareName`, `firmwareVersion`, `name` (the board); `sbc.dsf.version` with an
  SBC; `network.name` (the name set by M550).

ASSUMPTION: from the documentation and DuetWebControl's sources, not a recorded board.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

UNDER_WAY = frozenset(
    {"processing", "pausing", "paused", "resuming", "cancelling", "changingTool", "busy",
     "halted"}
)  # fmt: skip
"""States a job carries on through."""
STARTS = frozenset({"processing", "pausing", "paused", "resuming"})
PAUSED = frozenset({"pausing", "paused", "resuming"})
SIMULATING = "simulating"
HALTED = "halted"


def merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Applies `patch` to `base` in place, as DuetWebControl does: objects field by field,
    arrays item by item when they are the same length (replaced when not), anything else
    replaced."""
    for key, value in patch.items():
        old = base.get(key)
        if isinstance(value, Mapping) and isinstance(old, dict):
            merge(old, value)
        elif isinstance(value, list) and isinstance(old, list) and len(value) == len(old):
            for i, item in enumerate(value):
                if isinstance(item, Mapping) and isinstance(old[i], dict):
                    merge(old[i], item)
                else:
                    old[i] = item
        else:
            base[key] = _copy(value)
    return base


def _copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def _get(model: Mapping[str, Any], *path: str) -> Any:
    value: Any = model
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def status(model: Mapping[str, Any]) -> str | None:
    value = _get(model, "state", "status")
    return value if isinstance(value, str) else None


def job_file(model: Mapping[str, Any]) -> str | None:
    """The file printing (`0:/gcodes/benchy.gcode`), or None."""
    name = _get(model, "job", "file", "fileName")
    return name if isinstance(name, str) and name else None


def job_number(model: Mapping[str, Any], name: str) -> float | None:
    return number(_get(model, "job", name))


def extruders(model: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = _get(model, "move", "extruders")
    return [e if isinstance(e, Mapping) else {} for e in value] if isinstance(value, list) else []


def raw_by_extruder(model: Mapping[str, Any]) -> dict[int, float] | None:
    """What each extruder has fed in the job, in mm, its extrusion factor applied; None when the
    board doesn't report `rawPosition` (older firmware)."""
    found: dict[int, float] = {}
    for i, extruder in enumerate(extruders(model)):
        raw = number(extruder.get("rawPosition"))
        if raw is None:
            continue
        factor = number(extruder.get("factor"))
        found[i] = raw * (factor if factor is not None and factor > 0 else 1.0)
    return found or None


def slots_of(model: Mapping[str, Any]) -> dict[int, int]:
    """Each extruder's slot in 3D Print Log: the number of the lowest-numbered tool that drives
    it, else its own index."""
    slots: dict[int, int] = {}
    tools = model.get("tools") if isinstance(model, Mapping) else None
    by_number = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, Mapping):
            continue
        tool_number = tool.get("number")
        drives = tool.get("extruders")
        if isinstance(tool_number, int) and tool_number >= 0 and isinstance(drives, list):
            by_number.append((tool_number, [e for e in drives if isinstance(e, int)]))
    for tool_number, drives in sorted(by_number):
        for e in drives:
            slots.setdefault(e, tool_number)
    for i in range(len(extruders(model))):
        slots.setdefault(i, i)
    return slots


def firmware(model: Mapping[str, Any]) -> tuple[str, str] | None:
    board = _get(model, "boards")
    board = board[0] if isinstance(board, list) and board else None
    if not isinstance(board, Mapping):
        return None
    name = board.get("firmwareName")
    version = board.get("firmwareVersion")
    if not isinstance(version, str) or not version.strip():
        return None
    return (name.strip() if isinstance(name, str) and name.strip() else "RepRapFirmware",
            version.strip())  # fmt: skip


def board_name(model: Mapping[str, Any]) -> str | None:
    board = _get(model, "boards")
    board = board[0] if isinstance(board, list) and board else None
    name = board.get("name") if isinstance(board, Mapping) else None
    return name.strip() or None if isinstance(name, str) else None


def board_id(model: Mapping[str, Any]) -> str | None:
    board = _get(model, "boards")
    board = board[0] if isinstance(board, list) and board else None
    value = board.get("uniqueId") if isinstance(board, Mapping) else None
    return value.strip() or None if isinstance(value, str) else None


def dsf_version(model: Mapping[str, Any]) -> str | None:
    value = _get(model, "sbc", "dsf", "version")
    return value.strip() or None if isinstance(value, str) else None


def machine_name(model: Mapping[str, Any]) -> str | None:
    value = _get(model, "network", "name")
    return value.strip() or None if isinstance(value, str) else None


def tool_count(model: Mapping[str, Any]) -> int | None:
    tools = model.get("tools") if isinstance(model, Mapping) else None
    return len(tools) if isinstance(tools, list) and tools else None
