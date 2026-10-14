"""A spool changed partway through a print: its tool's usage is split between the spools (#44).

A swap is a new spool assigned to a slot while a job runs, by `PRINTLOG_SET_SPOOL` (paused or
printing) or by a reader in the slot seeing another tag, such as the Snapmaker U1's. `Spools`
reports each one with the point the job had reached (`JobPoint`), and the job logger keeps it in
the job's state, so a restart before the job ends keeps it too. When the job ends, the length
each slot fed before the swap goes to the old spool and the rest to the new one: one usage row
per spool, which the API's completion merges by spool, each on the slot it was fed from
(3d-print-log-api#162). The estimate stays on the row the print started with.

A pause for a filament change that no swap followed (a runout, or an M600 that unloaded the
filament, with no `PRINTLOG_SET_SPOOL`) keeps the slot's usage on one spool, and the print gets a
note saying how to split it next time. The bridge never guesses a spool.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from printlog_bridge.core.events import FilamentChange, JobPoint

SAME_POINT_MM = 1.0
"""How close (in millimetres fed) a swap may be to a filament change's pause and still count as
the spool loaded in it: a swap made during the pause is at the pause's point, give or take."""
NO_SPOOL = "no spool"


@dataclass(frozen=True)
class SpoolSwap:
    """A slot got another spool (or none) while a job ran. What `Spools` tells the job logger."""

    printer: str
    slot: int
    old: str | None
    """The spool (filament id) the slot had, or None when it had none."""
    new: str | None
    old_name: str | None
    new_name: str | None
    during: JobPoint


def swap_record(swap: SpoolSwap) -> dict[str, Any]:
    """The swap as the job's state file keeps it."""
    return {
        "slot": swap.slot,
        "fed_mm": float(swap.during.fed_mm.get(swap.slot, 0.0)),
        "old": swap.old,
        "new": swap.new,
        "old_name": swap.old_name,
        "new_name": swap.new_name,
        "progress": swap.during.progress,
        "layer": swap.during.layer,
    }


def change_record(change: FilamentChange) -> dict[str, Any]:
    """The filament change as the job's state file keeps it."""
    return {
        "slot": change.slot,
        "fed_mm": float(change.at.fed_mm.get(change.slot, 0.0)),
        "reason": change.reason,
        "progress": change.at.progress,
        "layer": change.at.layer,
    }


def on_slot(records: Iterable[Mapping[str, Any]], slot: int) -> list[Mapping[str, Any]]:
    """The records for `slot`, in the order the job reached them (a stable sort, so two at the
    same point keep the order they came in)."""
    return sorted((r for r in records if r.get("slot") == slot), key=lambda r: r["fed_mm"])


def segments(
    mm: float, first: str | None, swaps: Sequence[Mapping[str, Any]]
) -> list[tuple[str | None, float]]:
    """A slot's `mm` as (spool, length) pieces: `first` up to the first swap, then each swap's
    new spool up to the next. A swap past the end (the slot fed less than the bridge saw by
    then) is taken as at the end, so the pieces always add up to `mm`."""
    pieces: list[tuple[str | None, float]] = []
    spool, start = first, 0.0
    for swap in swaps:
        at = min(max(float(swap["fed_mm"]), start), mm)
        pieces.append((spool, at - start))
        spool, start = swap.get("new"), at
    pieces.append((spool, mm - start))
    return pieces


def swap_notes(swaps: Iterable[Mapping[str, Any]]) -> list[str]:
    """Such as "T0: Spool A → Spool B at 63% (layer 112)", one per swap, in order."""
    notes = []
    for swap in sorted(swaps, key=lambda r: (r["slot"], r["fed_mm"])):
        if swap.get("old") == swap.get("new"):
            continue
        old, new = (_spool_name(swap, end) for end in ("old", "new"))
        notes.append(f"T{swap['slot']}: {old} → {new}{_where(swap)}")
    return notes


def change_hints(
    changes: Iterable[Mapping[str, Any]], swaps: Sequence[Mapping[str, Any]]
) -> list[str]:
    """A note for each slot paused for a filament change that no swap followed."""
    hinted = []
    for change in sorted(changes, key=lambda r: (r["slot"], r["fed_mm"])):
        slot = change["slot"]
        explained = any(
            swap["slot"] == slot and swap["fed_mm"] >= change["fed_mm"] - SAME_POINT_MM
            for swap in swaps
        )
        if not explained and slot not in hinted:
            hinted.append(slot)
    return [
        f"T{slot} was paused for a filament change; run PRINTLOG_SET_SPOOL during the pause to "
        "split usage"
        for slot in hinted
    ]


def _spool_name(swap: Mapping[str, Any], end: str) -> str:
    if swap.get(end) is None:
        return NO_SPOOL
    return str(swap.get(f"{end}_name") or swap[end])


def _where(point: Mapping[str, Any]) -> str:
    progress, layer = point.get("progress"), point.get("layer")
    text = f" at {round(progress * 100)}%" if progress is not None else ""
    if layer is not None:
        text += f" (layer {layer})"
    return text
