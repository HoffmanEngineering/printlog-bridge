"""Which spool is in each of a printer's slots, as 3D Print Log knows it (#6).

The bridge reads each printer's loaded spools from 3D Print Log when it starts, every five
minutes after, and again when a print is created or completed, so a spool changed in the app
between prints is on the next print's usage rows. `PRINTLOG_SET_SPOOL` assigns a spool from the
printer's console (`spool_command`), and so does a spool picked in Mainsail, Fluidd or
KlipperScreen when Moonraker's Spoolman integration points at 3D Print Log, by its spool number.
A reader in a slot, such as the Snapmaker U1's RFID reader, assigns the spool whose tag it reads
(`spool_reading`).

Assigning a spool is not queued like a print: it says at once whether it worked, and a command
that couldn't reach 3D Print Log is run again by the user rather than applied hours later, when
the spool may have moved. A tag the reader keeps reporting that couldn't be looked up is
retried with each refresh. One that no spool has is looked up again every
`UNKNOWN_TAG_RETRY_S` while it stays in the slot, so adding it to a spool in the app takes
effect without a restart, and at once when the spool is pulled out and put back (#35).

A spool assigned to a slot while a job runs is a swap (#44): `on_swap` hears of it, with the
point the job had reached, so the job's usage on that slot is split there.

An adapter can `watch_loaded` a printer: it hears each change to what's in the printer's slots,
whatever made it (a refresh, a command, a reader), with each slot's spool as 3D Print Log
describes it, `spoolNumber` included. That is how Moonraker's active spool follows the active
tool's spool when its Spoolman server is 3D Print Log (`adapters/moonraker/spoolman.py`).

What the bridge does here only ever touches 3D Print Log. Moonraker's Spoolman integration is
followed only when its server is 3D Print Log; a Spoolman server of the user's own, and its
active spool and usage reports, are left alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from printlog_bridge.core.api_client import (
    ApiClient,
    ApiError,
    ApiRejected,
    ApiUnavailable,
    AuthRejected,
)
from printlog_bridge.core.events import JobPoint, SpoolCommand, SpoolReading
from printlog_bridge.core.status import StatusBoard
from printlog_bridge.core.swaps import SpoolSwap

log = logging.getLogger(__name__)

REFRESH_INTERVAL_S = 5 * 60
UNKNOWN_TAG_RETRY_S = 5 * 60
"""How long a tag no spool has waits before it's looked up again, for as long as it stays in
the slot: at most one lookup per slot in that time, however many spools aren't tagged."""
MAX_SLOTS = 64
"""The API's highest `slotCount`."""
COLOR_TOLERANCE = 100.0
"""How far apart (RGB distance, 0-441) two colors may be and still count as the same: a tag's
"red" and the app's "red" are rarely the same hex."""
TAG_SEPARATORS = re.compile(r"[:\-._\s]")
HEX = re.compile(r"[0-9A-F]+")
COLOR = re.compile(r"#?([0-9A-Fa-f]{6})")
FORBIDDEN = 403
PRINTER_FIELDS_NOT_SENT = frozenset({"images", "loadedFilaments", "category", "slotCount"})
"""What `GET /api/Printers/{id}` returns that its `PUT` takes in another shape, or not at all."""
BAD_REQUEST = 400
REQUIRED_PRINTER_FIELDS = (("make", "Make"), ("model", "Model"), ("name", "Name"))
"""What the printer `PUT` requires (`[Required]`, not empty), with what the app calls each."""


class SlotsNotAdded(ApiRejected):
    """3D Print Log won't take the printer with more slots. The message says what to do."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status=BAD_REQUEST)


def normalize_tag(uid: str) -> str | None:
    """A tag UID as 3D Print Log stores it (the API's `TagUid.Normalize`): uppercase hex with no
    separators. None for anything that isn't whole hex bytes, or is over 32 bytes."""
    cleaned = TAG_SEPARATORS.sub("", uid).upper()
    if not cleaned or len(cleaned) % 2 or len(cleaned) > 64 or not HEX.fullmatch(cleaned):
        return None
    return cleaned


def color_distance(a: str | None, b: str | None) -> float | None:
    """How far apart two `#RRGGBB` colors are (0-441), or None when either isn't one."""
    rgb = [_rgb(a), _rgb(b)]
    if rgb[0] is None or rgb[1] is None:
        return None
    return math.dist(rgb[0], rgb[1])


def _rgb(color: str | None) -> tuple[int, int, int] | None:
    match = COLOR.fullmatch((color or "").strip())
    if match is None:
        return None
    value = int(match.group(1), 16)
    return (value >> 16 & 0xFF, value >> 8 & 0xFF, value & 0xFF)


def _hex(color: str | None) -> str | None:
    rgb = _rgb(color)
    return None if rgb is None else "#{:02X}{:02X}{:02X}".format(*rgb)


def _material_key(material: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", material.upper())


def _label(slot: int) -> str:
    return f"T{slot}"


LoadedWatcher = Callable[[Mapping[int, Mapping[str, Any]]], None]
"""Told a printer's spool (the API's filament summary) in each slot, when that changes."""


def _name(filament: Mapping[str, Any]) -> str:
    return str(filament.get("displayName") or filament.get("id"))


class Spools:
    """The spools loaded on each logged printer, kept in step with 3D Print Log. A SpoolSink."""

    def __init__(
        self,
        api: ApiClient,
        printers: Mapping[str, int],
        board: StatusBoard | None = None,
        interval: float = REFRESH_INTERVAL_S,
        retry_after: float = UNKNOWN_TAG_RETRY_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`printers` maps each `[printer <name>]` to its printer in 3D Print Log."""
        self.api = api
        self.printers = dict(printers)
        self.board = board
        self.interval = interval
        self.retry_after = retry_after
        self.clock = clock
        self._loaded: dict[str, dict[int, dict[str, Any]]] = {}
        """Each printer's spool (the API's filament summary) in each slot, as last read."""
        self._slot_counts: dict[str, int] = {}
        self._readings: dict[tuple[str, int], SpoolReading] = {}
        """What each slot's reader last said."""
        self._tags: dict[tuple[str, int], tuple[str, str | None]] = {}
        """The tag each slot's reader reported that was looked up, and the spool it found."""
        self._retry_at: dict[tuple[str, int], tuple[str, float]] = {}
        """The tag each slot's reader reported that was last asked about, and when, on `clock`,
        it may be asked about again: set before asking, so an answer that never came waits too.
        """
        self._rescheduled: asyncio.Event | None = None
        """Set when a retry is scheduled, to wake `run` for it."""
        self._refused_growth: dict[str, tuple[tuple[Any, ...], str]] = {}
        """Each printer whose slot-growing PUT the API refused: what the printer was then
        (`_growth_key`), and what to tell the user. Not sent again until that changes."""
        self._locks: dict[str, asyncio.Lock] = {}
        self._failing = False
        self._watchers: dict[str, list[LoadedWatcher]] = {}
        self.on_swap: Callable[[SpoolSwap], bool] | None = None
        """Told of each spool assigned to a slot while a job runs; True when that job's usage
        is split there (the job logger's `spool_swapped`)."""

    # -- what's loaded -------------------------------------------------------------------------

    def loaded(self, printer: str) -> dict[int, str]:
        """The spool (filament id) in each slot, as last read. No request is made."""
        return {slot: str(f["id"]) for slot, f in self._loaded.get(printer, {}).items()}

    def slot_count(self, printer: str) -> int | None:
        return self._slot_counts.get(printer)

    def watch_loaded(self, printer: str, watcher: LoadedWatcher) -> Callable[[], None]:
        """Tells `watcher` the printer's spool in each slot now, when it has been read, and
        again after each change to it. Returns what stops that.

        The watcher is called in the middle of whatever changed the slots: it must not block,
        and what it raises is logged and goes no further."""
        self._watchers.setdefault(printer, []).append(watcher)
        if printer in self._loaded:
            self._tell(watcher, printer, self._loaded[printer])

        def unwatch() -> None:
            with contextlib.suppress(ValueError):
                self._watchers.get(printer, []).remove(watcher)

        return unwatch

    async def current(self, printer: str) -> dict[int, str]:
        """The spool in each slot, read again now; what was last read when that fails."""
        try:
            return await self.fresh(printer)
        except Exception as e:
            log.debug("[printer %s] couldn't read its spools: %s", printer, e)
        return self.loaded(printer)

    async def fresh(self, printer: str) -> dict[int, str]:
        """The spool in each slot, read again now. Raises what the read raised when it fails,
        rather than answer with what was last read, up to `interval` old: on a single-tool
        printer, a print created with a spool unloaded in the app since then loads it again."""
        await self.refresh(printer)
        return self.loaded(printer)

    async def refresh(self, printer: str) -> None:
        """Reads the printer's slots and loaded spools. Raises ApiError when it can't."""
        printer_id = self.printers.get(printer)
        if printer_id is None:
            return
        detail = await self.api.get_printer(printer_id)
        if detail is None:
            # Deleted, or not the key owner's: doctor and the heartbeat say why. It has no
            # spools to link, rather than the ones last read.
            self._loaded.pop(printer, None)
            return
        self._remember(printer, detail.get("loadedFilaments") or [], detail.get("slotCount"))

    async def refresh_all(self) -> None:
        """Reads every printer's spools, and retries any tag a reader reported that wasn't
        looked up yet, or is due again. Raises ApiError when a printer's can't be read."""
        for printer in self.printers:
            await self.refresh(printer)
        await self.retry_tags()

    async def retry_tags(self) -> None:
        """Looks up each tag a reader reported that wasn't looked up yet, or is due again."""
        for reading in list(self._readings.values()):
            if self._tag_pending(reading):
                await self.spool_reading(reading)

    async def run(self) -> None:
        """Reads every printer's spools now and every `interval` after, until cancelled. In
        between, it wakes only when a tag no spool had is due to be looked up again: readings
        arrive only when a reader's view changes, so a tag left in its slot sends none."""
        self._rescheduled = asyncio.Event()  # made here, in the running loop (Python 3.9)
        next_refresh = self.clock()
        while True:
            if self.clock() >= next_refresh:
                try:
                    await self.refresh_all()
                except Exception as e:
                    level = logging.DEBUG if self._failing else logging.WARNING
                    log.log(level, "couldn't read the printers' spools from 3D Print Log: %s", e)
                    self._failing = True
                else:
                    if self._failing:
                        log.info("read the printers' spools from 3D Print Log again")
                    self._failing = False
                next_refresh = self.clock() + self.interval
            else:
                try:
                    await self.retry_tags()
                except Exception as e:
                    log.warning("couldn't look a spool's tag up in 3D Print Log: %s", e)
            self._rescheduled.clear()
            await self._sleep_until(self._next_wake(next_refresh))

    def _next_wake(self, next_refresh: float) -> float:
        """When `run` wakes next: at the next refresh, or when a tag that's still in its slot
        is due again; at once for one that fell due while `run` was busy, or whose refresh
        failed before it was retried. Forgets each retry whose tag is no longer in its slot."""
        live = {}
        for key, (uid, at) in self._retry_at.items():
            reading = self._readings.get(key)
            looked_up = self._tags.get(key)
            if (
                reading is not None
                and reading.printer in self.printers
                and normalize_tag(reading.tag or "") == uid
                and not (looked_up is not None and looked_up[0] == uid and looked_up[1])
            ):
                live[key] = (uid, at)
        self._retry_at = live
        return min([next_refresh, *(max(at, self.clock()) for _, at in live.values())])

    async def _sleep_until(self, wake: float) -> None:
        """Sleeps until `wake`, or until a retry is scheduled. Not `asyncio.wait_for`: before
        Python 3.12 it swallows a cancel that arrives with the wait's result, and the bridge
        then never finishes shutting down."""
        assert self._rescheduled is not None
        waiter = asyncio.ensure_future(self._rescheduled.wait())
        try:
            await asyncio.wait({waiter}, timeout=max(0.0, wake - self.clock()))
        finally:
            waiter.cancel()

    def _remember(
        self, printer: str, rows: Iterable[Mapping[str, Any]], slot_count: int | None = None
    ) -> None:
        if slot_count is not None:
            self._slot_counts[printer] = int(slot_count)
        count = self._slot_counts.get(printer, 1)
        loaded: dict[int, dict[str, Any]] = {}
        unslotted = []
        for row in rows:
            filament = row.get("filament") or {}
            if not filament.get("id"):
                continue
            if row.get("slot") is None:
                unslotted.append(filament)
            else:
                loaded.setdefault(int(row["slot"]), dict(filament))
        if count <= 1 and 0 not in loaded and unslotted:
            # A single-tool printer's spool loaded the old way, with no slot, is in slot 0.
            loaded[0] = dict(unslotted[0])
        self._set_loaded(printer, loaded)

    def _set_loaded(self, printer: str, loaded: dict[int, dict[str, Any]]) -> None:
        """Keeps what's in the printer's slots, and tells its watchers when that changed."""
        changed = self._loaded.get(printer) != loaded
        self._loaded[printer] = loaded
        if changed:
            for watcher in list(self._watchers.get(printer, [])):
                self._tell(watcher, printer, loaded)

    def _tell(self, watcher: LoadedWatcher, printer: str, loaded: Mapping[int, Any]) -> None:
        try:
            watcher({slot: dict(f) for slot, f in loaded.items()})
        except Exception:
            log.exception("[printer %s] a watcher of its spools failed", printer)

    # -- PRINTLOG_SET_SPOOL --------------------------------------------------------------------

    async def spool_command(self, command: SpoolCommand) -> str:
        printer_id = self.printers.get(command.printer)
        if printer_id is None:
            return f"printer {command.printer} isn't linked to a printer in 3D Print Log"
        slot = _label(command.slot)
        if not 0 <= command.slot < MAX_SLOTS:
            return f"there is no T{command.slot}: tools are numbered 0 to {MAX_SLOTS - 1}"
        try:
            async with self._lock(command.printer):
                before = self._before(command.printer, command.slot)
                answer = await self._carry_out(command, printer_id)
                if self._swapped(command.printer, command.slot, before, command.during):
                    answer += f". This print's {slot} usage is split here"
        except AuthRejected:
            answer = (
                "3D Print Log rejected the bridge's API key; nothing changed. Check it with "
                "`printlog-bridge doctor`."
            )
        except SlotsNotAdded as e:
            answer = f"nothing changed: {e}"
        except ApiRejected as e:
            if e.status == FORBIDDEN:
                answer = "that spool isn't one of yours in 3D Print Log; nothing changed"
            else:
                answer = f"3D Print Log refused it; nothing changed ({e})"
        except ApiUnavailable as e:
            answer = (
                f"couldn't reach 3D Print Log ({e}); nothing changed. Run it again once the "
                "bridge is back online."
            )
        except Exception:
            log.exception("[printer %s] couldn't assign %s's spool", command.printer, slot)
            answer = "an unexpected error; nothing changed. See the bridge's log."
        log.info("[printer %s] %s: %s", command.printer, _describe(command), answer)
        self._event(command.printer, f"{_describe(command)}: {answer}")
        return answer

    async def _carry_out(self, command: SpoolCommand, printer_id: int) -> str:
        slot = _label(command.slot)
        if command.filament_id is None and command.tag is None and command.spool_number is None:
            await self.api.unload_slot(printer_id, command.slot)
            loaded = dict(self._loaded.get(command.printer, {}))
            loaded.pop(command.slot, None)
            self._set_loaded(command.printer, loaded)
            return f"{slot} has no spool in 3D Print Log now"
        if command.tag is not None:
            uid = normalize_tag(command.tag)
            if uid is None:
                return (
                    f"{command.tag!r} isn't a tag UID; give its bytes in hex, like 04A23F11 or "
                    "04:A2:3F:11"
                )
            spool = await self.api.filament_by_tag(uid)
            if spool is None:
                return (
                    f"no spool in 3D Print Log has tag {uid}; nothing changed. Add the tag to "
                    "the spool in the app, then run it again."
                )
            filament_id = str(spool["id"])
        elif command.spool_number is not None:
            spool = await self.api.filament_by_number(command.spool_number)
            if spool is None:
                # A deleted spool, or a number from another Spoolman: nothing to load, and
                # nothing asks again until the next pick.
                number = command.spool_number
                return f"no spool in 3D Print Log has number {number}; nothing changed"
            filament_id = str(spool["id"])
        else:
            filament_id = str(command.filament_id)
            try:
                uuid.UUID(filament_id)
            except ValueError:
                return (
                    f"{filament_id!r} isn't a spool id; copy it from the spool's page in "
                    "3D Print Log"
                )
        loaded = await self._load(
            command.printer, printer_id, command.slot, filament_id, command.slot_count
        )
        return f"{slot} is now {_name(loaded)} in 3D Print Log"

    async def _load(
        self,
        printer: str,
        printer_id: int,
        slot: int,
        filament_id: str,
        slot_count: int | None,
    ) -> Mapping[str, Any]:
        """Loads the spool into the slot, first adding slots to the printer if it needs them.
        Returns the spool as loaded."""
        if printer not in self._slot_counts:
            await self.refresh(printer)
        if slot >= self._slot_counts.get(printer, 1):
            count = min(MAX_SLOTS, max(slot + 1, slot_count or 0))
            await self._grow(printer, printer_id, slot, count)
        rows = await self.api.load_slot(printer_id, slot, filament_id, _label(slot))
        self._remember(printer, rows)
        return self._loaded.get(printer, {}).get(slot) or {"id": filament_id}

    async def _grow(self, printer: str, printer_id: int, slot: int, count: int) -> None:
        """Sets the printer's `slotCount`, for `slot`. The API takes it only by replacing the
        printer (there is no PATCH, and loading a slot doesn't add one), so everything else
        the printer has is sent back as read, and its loaded spools stay loaded.

        What that can't keep: `beamDiameter`, which the GET doesn't return, is cleared; and a
        spool loaded or unloaded in the app between the GET and the PUT is undone or loaded
        again with no slot. Raises SlotsNotAdded, saying what to do in the app, when the API
        would refuse the printer or does."""
        detail = await self.api.get_printer(printer_id)
        if detail is None:
            raise ApiRejected(f"printer {printer_id} isn't in 3D Print Log", status=404)
        if int(detail.get("slotCount") or 1) >= count:
            self._slot_counts[printer] = int(detail["slotCount"])
            return
        needed = f"{_label(slot)} needs the printer to have {count} slots in 3D Print Log"
        empty = [
            label
            for key, label in REQUIRED_PRINTER_FIELDS
            if not str(detail.get(key) or "").strip()
        ]
        if empty:
            # Sent as it is, the API refuses it with a 400 that names a field, not what to do.
            one = len(empty) == 1
            raise SlotsNotAdded(
                f"{needed}, and the bridge can't add them while its {_join(empty)} "
                f"{'is' if one else 'are'} empty. Fill {'it' if one else 'them'} in on the "
                f"printer in the app, or set its slots to {count} there, then run it again"
            )
        key = _growth_key(detail)
        refused = self._refused_growth.get(printer)
        if refused is not None and refused[0] == key:
            # Refused before, and nothing the API checks has changed: a PUT would be refused
            # again, while replacing the whole printer (racing the app, clearing beamDiameter).
            raise SlotsNotAdded(refused[1])
        self._refused_growth.pop(printer, None)
        body = {k: v for k, v in detail.items() if k not in PRINTER_FIELDS_NOT_SENT}
        body["id"] = printer_id
        body["slotCount"] = count
        category = detail.get("category")
        if isinstance(category, Mapping) and category.get("nickname"):
            body["category"] = category["nickname"]
        body["loadedFilaments"] = [
            {"id": row.get("id"), "filamentId": (row.get("filament") or {}).get("id")}
            for row in detail.get("loadedFilaments") or []
            if (row.get("filament") or {}).get("id")
        ]
        try:
            updated = await self.api.put_printer(printer_id, body)
        except ApiRejected as e:
            if e.status != BAD_REQUEST:
                raise
            # The action first: the API's reason can be long, and the console shows 300 characters.
            message = (
                f"{needed}, and it refused them. Set its slots to {count} on the printer in the "
                f"app, then run it again ({e})"
            )
            self._refused_growth[printer] = (key, message)
            raise SlotsNotAdded(message) from e
        log.info("[printer %s] set its slot count in 3D Print Log to %d", printer, count)
        self._slot_counts[printer] = int((updated or {}).get("slotCount") or count)

    # -- readers -------------------------------------------------------------------------------

    async def spool_reading(self, reading: SpoolReading) -> None:
        key = (reading.printer, reading.slot)
        self._readings[key] = reading
        uid = normalize_tag(reading.tag or "")
        if uid is None:
            # The spool is out (or its tag can't be read): putting one back looks it up again.
            self._tags.pop(key, None)
            self._retry_at.pop(key, None)
        printer_id = self.printers.get(reading.printer)
        if printer_id is None or not self._tag_pending(reading):
            return
        assert uid is not None  # _tag_pending made sure
        slot = _label(reading.slot)
        retry = False
        try:
            async with self._lock(reading.printer):
                if not self._still_in_slot(key, uid) or not self._tag_pending(reading):
                    return  # pulled out, or looked up, while this waited for the lock
                retry = (self._retry_at.get(key) or ("",))[0] == uid
                # Whatever comes of it, the next try is no sooner than this.
                self._retry_at[key] = (uid, self.clock() + self.retry_after)
                if self._rescheduled is not None:
                    self._rescheduled.set()
                spool = await self.api.filament_by_tag(uid)
                if not self._still_in_slot(key, uid):
                    return  # pulled out while 3D Print Log answered: the answer is for no slot
                if spool is None:
                    first = self._tags.get(key) != (uid, None)
                    self._tags[key] = (uid, None)
                    if first:
                        log.info(
                            "[printer %s] %s's reader sees tag %s, which no spool in 3D Print "
                            "Log has; add it to the spool in the app to have it assigned",
                            reading.printer, slot, uid,
                        )  # fmt: skip
                    return
                filament_id = str(spool["id"])
                if self.loaded(reading.printer).get(reading.slot) != filament_id:
                    before = self._before(reading.printer, reading.slot)
                    await self._load(
                        reading.printer, printer_id, reading.slot, filament_id, reading.slot_count
                    )
                    self._swapped(reading.printer, reading.slot, before, reading.during)
                    what = f"{slot} is now {_name(spool)}, from its tag {uid}"
                    log.info("[printer %s] %s", reading.printer, what)
                    self._event(reading.printer, what)
                self._tags[key] = (uid, filament_id)
                self._retry_at.pop(key, None)
        except ApiError as e:
            # Not marked as looked up, so it's tried again after `retry_after`, not over and
            # over while 3D Print Log is down. Only the first try of a tag is worth a warning.
            level = logging.DEBUG if retry or self._failing else logging.WARNING
            log.log(
                level,
                "[printer %s] couldn't assign the spool with tag %s to %s: %s",
                reading.printer, uid, slot, e,
            )  # fmt: skip

    def _tag_pending(self, reading: SpoolReading) -> bool:
        """Whether the reading's tag is to be looked up now: not found yet, and not asked about
        within `retry_after`. Another tag in the slot is asked about at once."""
        uid = normalize_tag(reading.tag or "")
        if uid is None:
            return False
        key = (reading.printer, reading.slot)
        looked_up = self._tags.get(key)
        if looked_up is not None and looked_up[0] == uid and looked_up[1] is not None:
            return False  # found its spool
        asked = self._retry_at.get(key)
        return asked is None or asked[0] != uid or self.clock() >= asked[1]

    def _still_in_slot(self, key: tuple[str, int], uid: str) -> bool:
        reading = self._readings.get(key)
        return reading is not None and normalize_tag(reading.tag or "") == uid

    def mismatches(self, printer: str, slots: Iterable[int]) -> list[str]:
        """A note for each of `slots` whose reader saw a material or color other than its
        spool's in 3D Print Log. A tag that found its spool is trusted over what it says."""
        notes = []
        for slot in sorted(set(slots)):
            reading = self._readings.get((printer, slot))
            spool = self._loaded.get(printer, {}).get(slot)
            if reading is None or spool is None:
                continue
            uid = normalize_tag(reading.tag or "")
            looked_up = self._tags.get((printer, slot))
            if uid is not None and looked_up == (uid, str(spool["id"])):
                continue
            seen, assigned = [], []
            material, spool_material = reading.material, spool.get("materialType")
            if material and spool_material:
                a, b = _material_key(material), _material_key(spool_material)
                if a and b and not b.startswith(a):
                    seen.append(material)
                    assigned.append(spool_material)
            distance = color_distance(reading.color_hex, spool.get("colorHex"))
            if distance is not None and distance > COLOR_TOLERANCE:
                seen.append(f"color {_hex(reading.color_hex)}")
                assigned.append(f"color {_hex(spool.get('colorHex'))}")
            if seen:
                notes.append(
                    f"Spool check: the printer's reader saw {' and '.join(seen)} in "
                    f"{_label(slot)}, but its spool in 3D Print Log, {_name(spool)}, is "
                    f"{' and '.join(assigned)}. Check which spool this print used."
                )
        return notes

    # -- helpers -------------------------------------------------------------------------------

    def _before(self, printer: str, slot: int) -> Mapping[str, Any] | None | bool:
        """The slot's spool before a change, None for none, or False when the printer's spools
        were never read, so what was there isn't known."""
        if printer not in self._loaded:
            return False
        return self._loaded[printer].get(slot)

    def _swapped(
        self,
        printer: str,
        slot: int,
        before: Mapping[str, Any] | None | bool,
        during: JobPoint | None,
    ) -> bool:
        """Tells `on_swap` when the slot's spool changed while a job ran. Whether it split it.
        Not when the spool before isn't known: there'd be nothing to split from."""
        if before is False:
            return False
        after = self._loaded.get(printer, {}).get(slot)
        old = None if before is None else str(before["id"])
        new = None if after is None else str(after["id"])
        if during is None or self.on_swap is None or old == new:
            return False
        names = (None if f is None else _name(f) for f in (before, after))
        return self.on_swap(SpoolSwap(printer, slot, old, new, *names, during=during))

    def _lock(self, printer: str) -> asyncio.Lock:
        if printer not in self._locks:
            self._locks[printer] = asyncio.Lock()
        return self._locks[printer]

    def _event(self, printer: str, what: str) -> None:
        if self.board is not None:
            self.board.printer(printer).event(what)


def _join(words: list[str]) -> str:
    """`a`, `a and b`, `a, b and c`."""
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def _growth_key(detail: Mapping[str, Any]) -> tuple[Any, ...]:
    """What, changed on the printer, may have the API take a slot-growing PUT it refused: its
    slot count and its own fields (its loaded spools aren't checked)."""
    return tuple(
        (k, repr(v)) for k, v in sorted(detail.items()) if k not in ("loadedFilaments", "images")
    )


def _describe(command: SpoolCommand) -> str:
    slot = _label(command.slot)
    if command.tag is not None:
        return f"set {slot} to the spool with tag {command.tag}"
    if command.spool_number is not None:
        return f"set {slot} to spool number {command.spool_number}"
    if command.filament_id is not None:
        return f"set {slot} to spool {command.filament_id}"
    return f"clear {slot}"
