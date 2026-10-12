"""A small stand-in for the 3D Print Log API: the calls the bridge makes, with the same rules.

It follows the API's contract for connectors (checked against 3d-print-log-api main, Oct 2026):

- `POST /api/Prints` with an `externalSource`/`externalId` pair already used returns that print
  with 200 instead of a duplicate. A replay applies only the fields a connector owns (status,
  durations, filament totals, usage rows) and never moves a finished print back to Printing.
- `POST /api/Prints/{id}/complete` sets the status and duration and merges usage rows: by
  `filamentId`, else by slot against rows with no filament. Every other field is left alone.
- `PATCH /api/Prints/{id}` changes only the fields sent.
- `POST /api/Prints/{id}/image` adds an image from a multipart form (`image`, `isDefault`):
  jpeg, png, gif, webp or bmp by the part's content type, under 10 MB, at most 5 per print
  (the free plan; 20 with Pro), each refused with 400. `isDefault` makes it the only default.
  It isn't idempotent: the same image sent twice is two images.
- `PUT /api/Connections/{instanceId}` registers a connection or refreshes it, replacing every
  field; `kind` and `displayName` are required and `printerId` must be the user's. A print
  links to a connection only when it is created after the connection was registered.
"""

from __future__ import annotations

import itertools
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
        self.offline = False
        """While True, every request is cut off before an answer, as with the internet down."""
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
        return web.json_response({"id": printer_id, "name": f"Printer {printer_id}"})

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

        found["status"] = body["status"]
        if body.get("printTimeInSeconds") is not None:
            found["printTimeInSeconds"] = body["printTimeInSeconds"]
        rows = found["filamentUsage"]
        for incoming in body.get("filamentUsage") or []:
            if incoming.get("filamentId"):
                match = next(
                    (r for r in rows if (r["filament"] or {}).get("id") == incoming["filamentId"]),
                    None,
                )
            else:
                match = next(
                    (
                        r
                        for r in rows
                        if r["filament"] is None and r["slot"] == incoming.get("slot")
                    ),
                    None,
                )
            if match is None:
                rows.append(_row(incoming))
            else:
                for field in ("amountMg", "lengthInM", "volumeMl", "source"):
                    if incoming.get(field) is not None:
                        match[field] = incoming[field]
        return web.json_response(found)


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
