"""Objects cancelled mid-print with Klipper's `[exclude_object]`.

The slicer labels each object in the file (`EXCLUDE_OBJECT_DEFINE NAME=...`), and the user can
cancel one that failed (`EXCLUDE_OBJECT NAME=...`) while the rest print on. The print then uses
less filament than the slicer estimated, so the bridge notes which objects were cancelled.

Klipper's `exclude_object` lists the file's `objects` and the names in `excluded_objects`, and
clears both when it loads the next file, which some cancel macros do right away
(`SDCARD_RESET_FILE`). So the bridge collects them while the print runs rather than reading
them at the end. Klipper keeps each name upper-cased, the way it reports them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

PRINTING = ("printing", "paused")


class ExcludedObjects:
    """The objects of the print Klipper is running, or ran last, and which were cancelled."""

    def __init__(self) -> None:
        self.filename: str | None = None
        self.objects: list[str] = []
        self.excluded: list[str] = []
        self._state: str | None = None
        self._taken = False

    def update(self, status: Mapping[str, Any]) -> None:
        """Takes the subscribed objects' latest fields, after each status update."""
        stats = status.get("print_stats") or {}
        state = stats.get("state") or self._state
        filename = stats.get("filename") or None

        if state == "printing" and (
            self._state not in PRINTING or (filename is not None and filename != self.filename)
        ):
            # A print started, or was already running when the bridge first looked.
            self.filename = filename
            self.objects = []
            self.excluded = []
            self._taken = False

        # Also the update that ends the print: an object cancelled just before it is in it.
        if state in PRINTING or self._state in PRINTING:
            self._absorb(status.get("exclude_object") or {})
        self._state = state

    def take(self, filename: str) -> str | None:
        """The note for the job printing `filename`, once; None when nothing was cancelled."""
        if self._taken or filename != self.filename:
            return None
        self._taken = True
        return note(self.excluded, len(self.objects))

    def _absorb(self, exclude_object: Mapping[str, Any]) -> None:
        objects = [
            str(o["name"])
            for o in exclude_object.get("objects") or []
            if isinstance(o, Mapping) and o.get("name")
        ]
        if objects:  # a cleared list is Klipper moving on, not the print's objects
            self.objects = objects
        for name in exclude_object.get("excluded_objects") or []:
            if name and str(name) not in self.excluded:
                self.excluded.append(str(name))


def note(excluded: Sequence[str], total: int) -> str | None:
    """Such as "2 of 6 objects were cancelled: PART_3, PART_5"; None for none."""
    if not excluded:
        return None
    count = len(excluded)
    were = "was" if count == 1 else "were"
    if total >= count:
        what = f"{count} of {total} {'object' if total == 1 else 'objects'} {were}"
    else:
        what = f"{count} {'object' if count == 1 else 'objects'} {were}"
    return f"{what} cancelled: {', '.join(excluded)}"
