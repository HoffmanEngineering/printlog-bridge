"""A small stand-in for the 3D Print Log API: the calls the bridge makes, with the same rules.

It follows the API's contract for connectors (checked against 3d-print-log-api main, Oct 2026):

- `POST /api/Prints` with an `externalSource`/`externalId` pair already used returns that print
  with 200 instead of a duplicate. A replay applies only the fields a connector owns (status,
  durations, filament totals, usage rows) and never moves a finished print back to Printing.
- `POST /api/Prints/{id}/complete` sets the status and duration and merges usage rows: by
  `filamentId`, else by `slot` against rows with no filament, else in order against the
  unlinked rows left (per-slot usage, api#146). A matched row's actual weight, length and volume
  are all replaced; its estimate is kept. It refuses with 400 a `filamentId` sent twice, a slot
  sent twice among rows with no `filamentId`, and a row with no measurement (or a `source`
  naming one it didn't send). Every other field is left alone.
- `PATCH /api/Prints/{id}` changes only the fields sent.
- `POST /api/Prints/{id}/image` adds an image from a multipart form (`image`, `isDefault`):
  jpeg, png, gif, webp or bmp by the part's content type, under 10 MB, at most 5 per print
  (the free plan; 20 with Pro), each refused with 400. `isDefault` makes it the only default.
  It isn't idempotent: the same image sent twice is two images.
- `PUT /api/Connections/{instanceId}` registers a connection or refreshes it, replacing every
  field; `kind` and `displayName` are required and `printerId` must be the user's. A print
  links to a connection only when it is created after the connection was registered.

Spools and slots (api#146, PR 158; api#147, PR 159):

- `GET /api/Printers/{id}` has `slotCount` (1 unless set) and `loadedFilaments`, and
  `GET /api/Printers/{id}/filament` lists the loaded spools: by slot, unslotted ones last.
- `PUT /api/Printers/{id}/slots/{slot}` with `{filamentId, slotLabel?}` loads a spool. Whatever
  was in the slot is unloaded, and so is the spool from any other slot or printer. Loading the
  spool already there keeps its row and `loadedAt` and only updates the label. 400 for a slot
  outside `0..slotCount-1` or a label over 20 characters, 403 for a spool that isn't the
  user's, 404 for a printer that doesn't exist. Returns the loaded list.
- `DELETE /api/Printers/{id}/slots/{slot}` unloads the slot: 204, also when it was empty.
- `PUT /api/Printers/{id}` replaces the printer. `slotCount` (1-64) is kept when left out, and
  the loaded spools become the ones sent, by id: a spool kept keeps its slot and loaded time, a
  spool left out is unloaded. `make`, `model` and `name` are required.
- `GET /api/Filaments/by-tag/{uid}` finds the user's spool with that tag UID, in any case and
  with `:` `-` `.` `_` or spaces between bytes (`TagUid.Normalize`): 400 for a UID that isn't
  whole hex bytes (64 hex characters at most), 404 when none of the user's spools has it.
- `AddPrint` on a printer with one slot makes the print's spools the printer's loaded list (so a
  print with no spool unloads it); on a printer with more slots it leaves the slots alone.
"""

from __future__ import annotations

import itertools
import re
import uuid
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

PRINTING = 2
FINISHED = (3, 4, 5, 6)  # Success, Cancelled, Failed, PartialSuccess
IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
CONNECTOR_OWNED = (
    "printTimeInSeconds",
    "estimatedPrintTimeInSeconds",
    "filamentUsageMg",
    "estimatedFilamentUsageMg",
)


class FakePrintLogApi:
    def __init__(self, *, api_key: str = "key-123", printers: tuple[int, ...] = (42,)) -> None:
        self.api_key = api_key
        self.printers = set(printers)
        self.settings: list[dict[str, Any]] = []
        self.prints: dict[int, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.created: list[dict[str, Any]] = []
        """Every create body received, replays included."""
        self.completions: list[dict[str, Any]] = []
        self.connections: dict[str, dict[str, Any]] = {}
        self.heartbeats: list[tuple[str, dict[str, Any]]] = []
        """Every PUT /api/Connections body received, by instance id."""
        self.images: dict[int, bytes] = {}
        """Every image's bytes, by image id. A print's `images` lists their ids and defaults."""
        self.max_images = 5
        self.image_status: int | None = None
        """When set, every image upload is answered with this status, as a broken store would."""
        self.image_cut = False
        """While True, every image upload is cut off before an answer (a proxy resetting large
        bodies, an uplink too slow), while every other request is answered."""
        self.offline = False
        """While True, every request is cut off before an answer, as with the internet down."""
        self.filaments: dict[str, dict[str, Any]] = {}
        """The user's spools, by id (`add_spool`)."""
        self.slot_counts: dict[int, int] = {}
        """Each printer's `slotCount`, when it isn't 1."""
        self.loaded: dict[int, list[dict[str, Any]]] = {p: [] for p in printers}
        """Each printer's loaded spools: `{id, slot, slotLabel, loadedAt, filamentId}`."""
        self.printer_puts: list[dict[str, Any]] = []
        """Every PUT /api/Printers/{id} body received."""
        self.slot_puts: list[dict[str, Any]] = []
        """Every PUT /api/Printers/{id}/slots/{slot} body received."""
        self._loaded_at = itertools.count(1)
        self._printer_fields: dict[int, dict[str, Any]] = {}
        """What the last PUT /api/Printers/{id} set, beyond slots and spools."""
        self._ids = itertools.count(1)
        self._image_ids = itertools.count(1)
        self._failures: list[tuple[int, dict[str, str]]] = []
        self._server: TestServer | None = None

    # -- serving -------------------------------------------------------------------------------

    async def start(self) -> str:
        app = web.Application(middlewares=[self._middleware])
        app.router.add_get("/api/Users/me", self._me)
        app.router.add_get("/api/Users/me/user-settings", self._settings)
        app.router.add_get("/api/Printers/{id}", self._printer)
        app.router.add_put("/api/Printers/{id}", self._put_printer)
        app.router.add_get("/api/Printers/{id}/filament", self._loaded_filament)
        app.router.add_put("/api/Printers/{id}/slots/{slot}", self._load_slot)
        app.router.add_delete("/api/Printers/{id}/slots/{slot}", self._unload_slot)
        app.router.add_get("/api/Filaments/by-tag/{uid}", self._by_tag)
        app.router.add_post("/api/Prints", self._create)
        app.router.add_get("/api/Prints/{id}", self._get)
        app.router.add_patch("/api/Prints/{id}", self._patch)
        app.router.add_post("/api/Prints/{id}/complete", self._complete)
        app.router.add_post("/api/Prints/{id}/image", self._upload_image)
        app.router.add_put("/api/Connections/{instance}", self._put_connection)
        self._server = TestServer(app)
        await self._server.start_server()
        return str(self._server.make_url("")).rstrip("/")

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        if self.offline:
            if request.transport is not None:
                request.transport.close()
            raise web.HTTPServiceUnavailable()  # never reaches the bridge: the line is cut
        self.requests.append((request.method, request.path))
        if self._failures:
            status, headers = self._failures.pop(0)
            return web.Response(status=status, headers=headers)
        if request.headers.get("X-Api-Key") != self.api_key:
            return web.Response(status=401)
        return await handler(request)

    # -- driving it from a test ----------------------------------------------------------------

    def fail_next(
        self, count: int, status: int = 503, headers: dict[str, str] | None = None
    ) -> None:
        """The next `count` requests fail with `status` (and `headers`), as an outage would."""
        self._failures.extend([(status, headers or {})] * count)

    def user_edits(self, print_id: int, **fields: Any) -> None:
        """What the user changes in the app while the print runs."""
        self.prints[print_id].update(fields)

    def add_spool(
        self,
        display_name: str = "Spool",
        *,
        material: str = "PLA",
        color_hex: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> str:
        """A spool in the user's inventory, as the app would add it. Returns its id."""
        filament_id = str(uuid.uuid4())
        self.filaments[filament_id] = {
            "id": filament_id,
            "displayName": display_name,
            "materialType": material,
            "colorHex": color_hex,
            "tagUids": sorted(normalize_tag(t) for t in tags),
        }
        return filament_id

    def user_loads(self, printer_id: int, slot: int | None, filament_id: str) -> None:
        """The user loads a spool in the app (or loads it with no slot, as older apps do)."""
        self._unload_everywhere(filament_id)
        rows = self.loaded[printer_id]
        rows[:] = [r for r in rows if slot is None or r["slot"] != slot]
        rows.append(self._loaded_row(slot, None, filament_id))

    def slot_spools(self, printer_id: int) -> dict[int | None, str]:
        """What's loaded on the printer: filament id by slot."""
        return {r["slot"]: r["filamentId"] for r in self.loaded[printer_id]}

    def by_external_id(self, external_id: str) -> dict[str, Any]:
        [found] = [p for p in self.prints.values() if p["externalId"] == external_id]
        return found

    # -- the API -------------------------------------------------------------------------------

    async def _me(self, _request: web.Request) -> web.Response:
        return web.json_response({"id": 7, "displayName": "Maker"})

    async def _settings(self, _request: web.Request) -> web.Response:
        return web.json_response(self.settings)

    async def _printer(self, request: web.Request) -> web.Response:
        printer_id = int(request.match_info["id"])
        if printer_id not in self.printers:
            return web.Response(status=404)
        return web.json_response(self._printer_detail(printer_id))

    def _printer_detail(self, printer_id: int) -> dict[str, Any]:
        detail = {
            "id": printer_id,
            "make": "Snapmaker",
            "model": "U1",
            "name": f"Printer {printer_id}",
            "description": None,
            "nozzleDiameter": 0.4,
            "filamentDiameter": 1.75,
            "isActive": True,
            "category": {"nickname": "FDM"},
            "slotCount": self.slot_counts.get(printer_id, 1),
            "loadedFilaments": self._loaded_list(printer_id),
        }
        detail.update(self._printer_fields.get(printer_id, {}))
        return detail

    async def _put_printer(self, request: web.Request) -> web.Response:
        printer_id = int(request.match_info["id"])
        body = await request.json()
        self.printer_puts.append(body)
        if body.get("id") != printer_id:
            return web.Response(status=400, text="ID in route does not match body.")
        if printer_id not in self.printers:
            return web.Response(status=404)
        for field in ("make", "model", "name"):
            if not (body.get(field) or "").strip():
                return web.Response(status=400, text=f"The {field} field is required.")
        count = body.get("slotCount")
        if count is not None:
            if not 1 <= count <= 64:
                return web.Response(status=400, text="slotCount must be between 1 and 64.")
            self.slot_counts[printer_id] = count
        wanted = [r.get("filamentId") for r in body.get("loadedFilaments") or []]
        if any(f not in self.filaments for f in wanted):
            return web.Response(status=403, text="User does not have access to filament.")
        rows = self.loaded[printer_id]
        kept = [r for r in rows if r["filamentId"] in wanted]
        held = {r["filamentId"] for r in kept}
        rows[:] = kept + [self._loaded_row(None, None, f) for f in wanted if f not in held]
        self._printer_fields[printer_id] = {
            k: v
            for k, v in body.items()
            if k not in ("id", "slotCount", "loadedFilaments", "category")
        }
        return web.json_response(self._printer_detail(printer_id))

    async def _loaded_filament(self, request: web.Request) -> web.Response:
        printer_id = int(request.match_info["id"])
        if printer_id not in self.printers:
            return web.Response(status=404)
        return web.json_response(self._loaded_list(printer_id))

    async def _load_slot(self, request: web.Request) -> web.Response:
        printer_id, slot = int(request.match_info["id"]), int(request.match_info["slot"])
        if printer_id not in self.printers:
            return web.Response(status=404)
        body = await request.json()
        self.slot_puts.append(body)
        count = self.slot_counts.get(printer_id, 1)
        if not 0 <= slot < count:
            return web.Response(
                status=400,
                text=f"slot must be between 0 and {count - 1}; this printer has {count}.",
            )
        label = (body.get("slotLabel") or "").strip() or None
        if label is not None and len(label) > 20:
            return web.Response(status=400, text="The field SlotLabel must be at most 20.")
        filament_id = body.get("filamentId")
        if not filament_id:
            return web.Response(status=400, text="The FilamentId field is required.")
        if filament_id not in self.filaments:
            return web.Response(status=403, text="User does not have access to filament.")
        rows = self.loaded[printer_id]
        here = next((r for r in rows if r["slot"] == slot), None)
        if here is not None and here["filamentId"] == filament_id:
            here["slotLabel"] = label  # already there: keeps its row and loaded time
        else:
            self._unload_everywhere(filament_id)
            rows[:] = [r for r in rows if r["slot"] != slot]
            rows.append(self._loaded_row(slot, label, filament_id))
        return web.json_response(self._loaded_list(printer_id))

    async def _unload_slot(self, request: web.Request) -> web.Response:
        printer_id, slot = int(request.match_info["id"]), int(request.match_info["slot"])
        if printer_id not in self.printers:
            return web.Response(status=404)
        rows = self.loaded[printer_id]
        rows[:] = [r for r in rows if r["slot"] != slot]
        return web.Response(status=204)

    async def _by_tag(self, request: web.Request) -> web.Response:
        uid = normalize_tag(request.match_info["uid"])
        if uid is None:
            return web.Response(status=400, text="The tag UID must be hex bytes.")
        for filament in self.filaments.values():
            if uid in filament["tagUids"]:
                return web.json_response(filament)
        return web.Response(status=404)

    def _loaded_row(self, slot: int | None, label: str | None, filament_id: str) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "slot": slot,
            "slotLabel": label,
            "loadedAt": next(self._loaded_at),
            "filamentId": filament_id,
        }

    def _loaded_list(self, printer_id: int) -> list[dict[str, Any]]:
        rows = sorted(
            self.loaded[printer_id],
            key=lambda r: (r["slot"] is None, r["slot"] or 0, r["loadedAt"]),
        )
        return [
            {
                "id": r["id"],
                "slot": r["slot"],
                "slotLabel": r["slotLabel"],
                "loadedAt": f"2026-10-12T00:00:{r['loadedAt']:02d}+00:00",
                "filament": dict(self.filaments.get(r["filamentId"]) or {"id": r["filamentId"]}),
            }
            for r in rows
        ]

    def _unload_everywhere(self, filament_id: str) -> None:
        for rows in self.loaded.values():
            rows[:] = [r for r in rows if r["filamentId"] != filament_id]

    async def _create(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.created.append(body)
        if not (body.get("title") or "").strip():
            return web.Response(status=400, text="The Title field is required.")
        if body.get("printerId") not in self.printers:
            return web.Response(
                status=400, text="Selected printer does not belong to currently logged in user."
            )

        source, external_id = body.get("externalSource"), body.get("externalId")
        existing = next(
            (
                p
                for p in self.prints.values()
                if source and p["externalSource"] == source and p["externalId"] == external_id
            ),
            None,
        )
        if existing is not None:
            reopens = existing["status"] in FINISHED and body.get("status") in (1, PRINTING)
            if body.get("status") and not reopens:
                existing["status"] = body["status"]
            for field in CONNECTOR_OWNED:
                if body.get(field) is not None:
                    existing[field] = body[field]
            if body.get("filamentUsage"):
                existing["filamentUsage"] = [_row(r) for r in body["filamentUsage"]]
            return web.json_response(existing, status=200)

        spools = [r.get("filamentId") for r in body.get("filamentUsage") or []]
        if any(f and f not in self.filaments for f in spools):
            return web.Response(status=403, text="User does not have access to filament.")
        if self.slot_counts.get(body["printerId"], 1) <= 1:
            # The print's spools become the printer's loaded list; not on a multi-slot printer.
            # `setLoadedFilament`: a spool already loaded keeps its row, slot and loaded time.
            wanted = list(dict.fromkeys(f for f in spools if f))
            rows = self.loaded[body["printerId"]]
            kept = [r for r in rows if r["filamentId"] in wanted]
            held = {r["filamentId"] for r in kept}
            for filament_id in wanted:
                if filament_id not in held:
                    self._unload_everywhere(filament_id)
            rows[:] = kept + [self._loaded_row(None, None, f) for f in wanted if f not in held]

        print_id = next(self._ids)
        self.prints[print_id] = {
            "id": print_id,
            "printerId": body["printerId"],
            "title": body["title"],
            "fileName": body.get("fileName"),
            "status": body.get("status", 1),
            "startDate": body.get("startDate"),
            "notes": body.get("notes"),
            "viewStatus": body.get("viewStatus"),
            "allowComments": body.get("allowComments", False),
            "externalSource": source,
            "externalId": external_id,
            "connectionInstanceId": body.get("connectionInstanceId"),
            # Like the API: only a connection registered before the print was created links.
            "connectionId": (
                self.connections.get(body.get("connectionInstanceId") or "") or {}
            ).get("id"),
            "printTimeInSeconds": body.get("printTimeInSeconds"),
            "estimatedPrintTimeInSeconds": body.get("estimatedPrintTimeInSeconds"),
            "filamentUsageMg": body.get("filamentUsageMg"),
            "estimatedFilamentUsageMg": body.get("estimatedFilamentUsageMg"),
            "filamentUsage": [_row(r) for r in body.get("filamentUsage") or []],
            "images": [],
        }
        return web.json_response(self.prints[print_id], status=201)

    async def _put_connection(self, request: web.Request) -> web.Response:
        instance_id = request.match_info["instance"].strip()
        body = await request.json()
        self.heartbeats.append((instance_id, body))
        for field in ("kind", "displayName"):
            if not (body.get(field) or "").strip():
                return web.Response(status=400, text=f"{field} is required.")
        if body.get("printerId") is not None and body["printerId"] not in self.printers:
            return web.Response(status=400, text="The printer does not exist or is not yours.")

        existing = self.connections.get(instance_id)
        connection = {
            "id": existing["id"] if existing else str(uuid.uuid4()),
            "instanceId": instance_id,
            "kind": body["kind"].strip().lower(),
            "displayName": body["displayName"].strip(),
            "agentVersion": body.get("agentVersion"),
            "printerId": body.get("printerId"),
            "status": 1,
        }
        self.connections[instance_id] = connection
        return web.json_response(connection, status=200 if existing else 201)

    async def _upload_image(self, request: web.Request) -> web.Response:
        found = self.prints.get(int(request.match_info["id"]))
        if found is None:
            return web.Response(status=404)
        if self.image_cut:
            if request.transport is not None:
                request.transport.close()
            raise web.HTTPServiceUnavailable()  # never reaches the bridge: the line is cut
        if self.image_status is not None:
            return web.Response(status=self.image_status)
        form = await request.post()
        image = form.get("image")
        if not isinstance(image, web.FileField):
            return web.Response(status=400, text="Image file is required.")
        if (image.content_type or "").lower() not in IMAGE_TYPES:
            return web.Response(
                status=400, text="Only image files are accepted (jpeg, png, gif, webp, bmp)."
            )
        data = image.file.read()
        if len(data) > MAX_IMAGE_BYTES:
            return web.Response(status=400, text="Image must be under 10MB.")
        if len(found["images"]) >= self.max_images:
            return web.Response(
                status=400, text=f"Maximum of {self.max_images} images per print allowed"
            )
        flag = str(form.get("isDefault", "false")).strip().lower()
        if flag not in ("true", "false"):
            return web.Response(status=400, text="The value is not valid for isDefault.")
        is_default = flag == "true"
        if is_default:
            for other in found["images"]:
                other["isDefault"] = False
        image_id = next(self._image_ids)
        self.images[image_id] = data
        row = {
            "id": image_id,
            "isDefault": is_default,
            "displayOrder": max((i["displayOrder"] for i in found["images"]), default=-1) + 1,
            "contentType": image.content_type,
        }
        found["images"].append(row)
        return web.json_response(
            {k: row[k] for k in ("id", "isDefault", "displayOrder")}, status=201
        )

    def default_image(self, print_id: int) -> bytes | None:
        """The bytes of the image shown for the print, if it has one."""
        for image in self.prints[print_id]["images"]:
            if image["isDefault"]:
                return self.images[image["id"]]
        return None

    async def _get(self, request: web.Request) -> web.Response:
        found = self.prints.get(int(request.match_info["id"]))
        return web.json_response(found) if found else web.Response(status=404)

    async def _patch(self, request: web.Request) -> web.Response:
        found = self.prints.get(int(request.match_info["id"]))
        if found is None:
            return web.Response(status=404)
        body = await request.json()
        found.update({k: v for k, v in body.items() if v is not None and k != "clear"})
        return web.json_response(found)

    async def _complete(self, request: web.Request) -> web.Response:
        found = self.prints.get(int(request.match_info["id"]))
        if found is None:
            return web.Response(status=404)
        body = await request.json()
        self.completions.append(body)
        if body.get("status") not in FINISHED:
            return web.Response(status=400, text="status must be a finished status.")

        usage = body.get("filamentUsage") or []
        problem = _usage_problem(usage)
        if problem:
            return web.Response(status=400, text=problem)

        found["status"] = body["status"]
        if body.get("printTimeInSeconds") is not None:
            found["printTimeInSeconds"] = body["printTimeInSeconds"]
        rows = found["filamentUsage"]
        unlinked = [r for r in rows if r["filament"] is None]
        # Rows that name a slot claim theirs first, so an in-order match can't take it.
        by_slot = {}
        for incoming in usage:
            if not incoming.get("filamentId") and incoming.get("slot") is not None:
                match = next((r for r in unlinked if r["slot"] == incoming["slot"]), None)
                if match is not None:
                    unlinked.remove(match)
                by_slot[incoming["slot"]] = match
        for incoming in usage:
            if incoming.get("filamentId"):
                match = next(
                    (r for r in rows if (r["filament"] or {}).get("id") == incoming["filamentId"]),
                    None,
                )
            elif incoming.get("slot") is not None:
                match = by_slot[incoming["slot"]]
            else:
                match = unlinked.pop(0) if unlinked else None
            if match is None:
                rows.append(_row(incoming))
            else:
                # All three values are replaced, so an earlier completion's can't outlive it.
                match["source"] = _source(incoming)
                for field in ("amountMg", "lengthInM", "volumeMl"):
                    match[field] = incoming.get(field)
        return web.json_response(found)


TAG_SEPARATORS = re.compile(r"[:\-._\s]")
HEX = re.compile(r"[0-9A-F]+")


def normalize_tag(uid: str) -> str | None:
    """`TagUid.Normalize`: uppercase hex without separators, whole bytes, 64 characters at most."""
    cleaned = TAG_SEPARATORS.sub("", uid).upper()
    if not cleaned or len(cleaned) % 2 or len(cleaned) > 64 or not HEX.fullmatch(cleaned):
        return None
    return cleaned


MEASUREMENTS = {1: "amountMg", 2: "lengthInM", 3: "volumeMl"}  # Weight, Length, Volume


def _source(row: dict[str, Any]) -> int | None:
    """The measurement a usage row carries: the one named, or the first sent; None if absent."""
    if row.get("source") is not None:
        return row["source"] if row.get(MEASUREMENTS.get(row["source"], "")) is not None else None
    return next((s for s, f in MEASUREMENTS.items() if row.get(f) is not None), None)


def _usage_problem(usage: list[dict[str, Any]]) -> str | None:
    """`CompletePrintDto.Validate`'s rules for usage rows, with its messages."""
    linked = [r["filamentId"] for r in usage if r.get("filamentId")]
    if len(linked) != len(set(linked)):
        return "Each filamentId may appear at most once."
    slots = [r["slot"] for r in usage if not r.get("filamentId") and r.get("slot") is not None]
    if len(slots) != len(set(slots)):
        return "Each slot may appear at most once among rows with no filamentId."
    if any(_source(r) is None for r in usage):
        return (
            "Each usage row needs amountMg, lengthInM or volumeMl, and source must name one "
            "that was sent."
        )
    return None


def _row(sent: dict[str, Any]) -> dict[str, Any]:
    filament_id = sent.get("filamentId") or (sent.get("filament") or {}).get("id")
    return {
        "id": str(uuid.uuid4()),
        "slot": sent.get("slot"),
        "filament": {"id": filament_id} if filament_id else None,
        "estimatedAmountMg": sent.get("estimatedAmountMg"),
        "estimatedLengthInM": sent.get("estimatedLengthInM"),
        "estimatedSource": sent.get("estimatedSource"),
        "amountMg": sent.get("amountMg"),
        "lengthInM": sent.get("lengthInM"),
        "volumeMl": sent.get("volumeMl"),
        "source": sent.get("source"),
        "notes": sent.get("notes"),
    }
