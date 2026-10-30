"""A small stand-in for the 3D Print Log API: the calls the bridge makes, with the same rules.

It follows the API's contract for connectors (checked against 3d-print-log-api main, Oct 2026):

- `POST /api/Prints` with an `externalSource`/`externalId` pair already used returns that print
  with 200 instead of a duplicate. A replay applies only the fields a connector owns (status,
  durations, filament totals, usage rows) and never moves a finished print back to Printing.
  Its usage rows name their spool as `filament: {id}`, the print's own shape; a `filamentId`
  there is ignored, as the API's `AddPrintDTO` has no such field.
- `POST /api/Prints/{id}/complete` sets the status and duration and merges usage rows: by
  `filamentId`, else by `slot` against rows with no filament, else in order against the
  unlinked rows left (per-slot usage, api#146). A matched row's actual weight, length and volume
  are all replaced; its estimate is kept. It refuses with 400 a `filamentId` sent twice, a slot
  sent twice among rows with no `filamentId`, and a row with no measurement (or a `source`
  naming one it didn't send). Every other field is left alone.
- `PATCH /api/Prints/{id}` changes only the fields sent.
- `GET /api/Prints/claimable?printerId=&createdAfter=` lists the user's prints on the printer
  in Printing with no external pair, created after the time, newest first, as
  `ClaimablePrintDto` (no `externalId` or `images`). `POST /api/Prints/{id}/claim` sets the
  pair, the connection and a missing `startDate`. The same pair again returns the print, before
  any status check. 409 (problem+json) for a print with another pair or not in Printing, and
  for a pair already on another print, whose id is `printId` in the body (api#165, PR for
  3d-print-log-api#165). `lookup = False` answers the lookup 400, as an API without it does
  (the path falls through to `GET /api/Prints/{id}`).
- `POST /api/Prints/{id}/image` adds an image from a multipart form (`image`, `isDefault`):
  jpeg, png, gif, webp or bmp by the part's content type, under 10 MB, at most 5 per print
  (the free plan; 20 with Pro), each refused with 400. `isDefault` makes it the only default.
  It isn't idempotent: the same image sent twice is two images.
- `POST /api/Prints/{id}/slicer-settings` records a connector's slicer settings (api#164, PR
  for 3d-print-log-api#188): `{configBlock?, header?, metadata?}`. 400 (problem+json) with
  neither, for a metadata field over its length or out of its range, for a block with no
  `; key = value` line and no metadata, and for metadata with no `slicer` and no block; 413
  for a `configBlock` over 512 KiB or a `header` over 64 KiB (UTF-8 bytes); 404 for a print
  that doesn't exist or isn't the user's (creator-only); 409 for a print whose settings came
  from a slicer plugin (its `slicer` is set and no connector recorded them). Sending again
  replaces what an earlier send recorded. The print gets its `slicer` (and `slicerVersion`), and
  the lookup's `hasSlicerSettings` turns true. `slicer_settings_route = False` answers 404 as an
  API without the endpoint does (no route), and `slicer_settings_status` answers every send with
  that status.
- File attachments (Pro): `GET /api/Subscription/me` says `isPro` (`pro`).
  `POST /api/Prints/{id}/files/upload-url` with `{fileName, contentType, sizeBytes}` answers a
  `sasUrl` on a separate fake blob store and the `blobPath` to confirm: 403 without Pro, 400 for
  a `sizeBytes` outside 1 byte to 200 MB, a sixth file on the print, or an extension other than
  `.gcode`, `.stl`, `.3mf`, `.obj` and `.mp4` (`.mp4` with the API PR for printlog-bridge#42).
  The store takes `PUT <sasUrl>` only with `x-ms-blob-type: BlockBlob`, as Azure does, and
  needs no API key. `POST /api/Prints/{id}/files/confirm` with
  `{blobPath, fileName, sizeBytes, contentType}` adds the attachment without looking at the
  store, as the API does, so confirming twice is two attachments. `GET /api/Prints/{id}/files`
  lists them.
- `PUT /api/Connections/{instanceId}` registers a connection or refreshes it, replacing every
  field but the software ones (`firmwareName`, `firmwareVersion`, `hostSoftwareName`,
  `hostSoftwareVersion`), which a null or missing value leaves as they were (api#282); `kind`
  and `displayName` are required and `printerId` must be the user's. A print
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
- `PUT /api/Printers/{id}` replaces the printer (`AddPrinterDTO`): every field left out is
  cleared, as AutoMapper maps a missing one to null, except `slotCount` (1-64) and `category`,
  which are kept. The loaded spools become the ones sent, by id: a spool kept keeps its slot and
  loaded time, a spool left out is unloaded, and one sent that wasn't loaded is loaded with no
  slot. `make`, `model` and `name` are required (`[Required]`, 400 when empty). There is no
  PATCH and no other way to change `slotCount` (api main, Oct 2026).
- The `GET` (`PrinterDetailDto`) has no `beamDiameter`, which the `PUT` takes, so a client
  can't send it back and a `PUT` clears it (`beam_diameter`).
- `GET /api/Filaments/by-tag/{uid}` finds the user's spool with that tag UID, in any case and
  with `:` `-` `.` `_` or spaces between bytes (`TagUid.Normalize`): 400 for a UID that isn't
  whole hex bytes (64 hex characters at most), 404 when none of the user's spools has it.
- `GET /api/Filaments/by-number/{n}` finds the user's spool with that `spoolNumber` (what
  Moonraker's Spoolman integration calls its spool id): 404 when none of theirs has it.
- `GET /api/Filaments/by-spoolman-id/{n}` finds the user's spool linked to that spool in their
  own Spoolman server (`spoolmanId`, #38): 404 when none of theirs is. With `spoolman_ids` off it
  is a 404 always, as an API without the field is.
- `AddPrint` on a printer with one slot makes the print's spools the printer's loaded list (so a
  print with no spool unloads it); on a printer with more slots it leaves the slots alone.

Device pairing and device tokens (api#148, PR 286, the "Contract for the bridge"):

- `GET /api/device-grants/pairing` (no credentials) answers `pairing` (the Auth0 tenant's
  parameters, `FakeAuth0.discovery()`), or 503 `device_pairing_unavailable` while it is None.
- `POST /api/device-grants` with `Authorization: Bearer <an access token FakeAuth0 issued>` and
  `{deviceId, name}`: 201 with a new grant, or 200 rotating the token of the grant with that
  `deviceId`. 400 `invalid_request` for a `deviceId` that isn't a UUID or a name outside 1 to 100
  characters or with control characters; 401 for a token FakeAuth0 didn't issue; 403 with no
  code for one older than 10 minutes, `insufficient_scope` for one with no device scope,
  `offline_access_not_allowed`, and `pairing_token_used` the second time (by `jti`); 409
  `device_grant_limit` past `max_grants`, and `device_grant_revoked` (once) for a `deviceId` in
  `revoke_during_repair`. The grant's scopes are the token's device scopes.
- `Authorization: Bearer pld_...` works on the endpoints of `DEVICE_SCOPES` whose scope the grant
  holds: 401 `invalid_device_token` for a token no grant has (revoked or rotated), 403
  `insufficient_scope` or `device_endpoint_not_allowed`, 400 `multiple_credentials` with an
  `X-Api-Key` too, and 429 `device_token_throttled` (`Retry-After: 60`) while `throttled`.
- `GET /api/Printers/summary` pages the user's printers (`pageNumber`, `pageSize`), and
  `POST /api/Printers` (`AddPrinterDTO`: `make`, `model`, `name` required) adds one: 201.

Bridges (api#288, "Contract for story 12"), only with a device token holding `bridge:sync`
(an API key gets 403 `device_token_required`):

- `PUT /api/bridges/me` registers or updates the token's bridge. Every field is optional; a
  missing one keeps what is stored, and a list replaces. 400 `invalid_check_in` over the caps
  (16 networks, 50 discovered, 8 MACs) or for a bad `agentVersion`/`mode`/`hostName`. A
  network or printer that breaks a rule is dropped and listed in `rejected` with its code
  (`address_not_private`, `invalid_kind`, `invalid_mac`, `network_not_private`,
  `network_too_large`, `duplicate_printer`...), and the rest stored. The answer is
  `{bridgeId, checkInSeconds, desiredPollSeconds, desired, rejected}`. `check_in_limited`
  answers 429 with `Retry-After`, as the connector write budget does.
- `GET /api/bridges/me/desired`: 404 `bridge_not_registered` before the first check-in; the
  `desired` object with `ETag: W/"<version>"`, or 304 for a matching `If-None-Match`.
- A test changes the desired list with `connect`, `disconnect` and `request_scan`, each giving it
  a new version, as the app's Bridges screen does.

Sealed credentials (api#289, `docs/bridge-credential-sealing.md`):

- The check-in's `publicKey` (strict: padded base64 DER SPKI of a P-256 point, 124 characters)
  and `publicKeyFingerprint` (must match) are taken on the check-in that registers the bridge;
  any other new key is held pending until `confirm_key`. A malformed one is named in `rejected`
  as `invalid_public_key`. The answer adds `publicKeyFingerprint` and
  `pendingPublicKeyFingerprint` (both left out with `keys_supported = False`, as an API before
  14a).
- `credentialIssues` with `credentialIssuesVersion` replaces the stored issues
  (`bridge["issues"]`) only when the version is current; every check-in's are in `check_ins`.
- `set_secret` seals a value to the bridge's current key as the web app would; `confirm_key`
  makes values sealed to the old key stale (not sent). Each changes the desired version.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import ipaddress
import itertools
import json
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

PRINTER_FIELDS = (
    "make",
    "model",
    "name",
    "description",
    "nozzleDiameter",
    "filamentDiameter",
    "beamDiameter",
    "isActive",
    "bedWidthMm",
    "bedHeightMm",
    "bedDepthMm",
    "screenResolutionXPixels",
    "screenResolutionYPixels",
    "hasHeatedBed",
    "hasHeatedChamber",
    "wattageW",
)
"""What a printer `PUT` (`AddPrinterDTO`) sets, beyond its id, slots, spools and category."""
NOT_IN_PRINTER_DETAIL = ("beamDiameter",)
"""What the `PUT` takes that the `GET` (`PrinterDetailDto`) doesn't return."""

PRINTING = 2
FINISHED = (3, 4, 5, 6)  # Success, Cancelled, Failed, PartialSuccess
IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 200 * 1024 * 1024
ATTACHMENT_EXTENSIONS = (".gcode", ".stl", ".3mf", ".obj", ".mp4")
ATTACHMENT_CONTAINER = "printattachments"
SAS_SIGNATURE = "SECRET-sas-signature"
"""The `sig` of every upload URL: whoever has it can write to the store, so it's never logged."""
CONNECTOR_OWNED = (
    "printTimeInSeconds",
    "estimatedPrintTimeInSeconds",
    "filamentUsageMg",
    "estimatedFilamentUsageMg",
)


DEVICE_SCOPES = {
    ("GET", "/api/Users/me"): None,
    ("GET", "/api/Users/me/user-settings"): None,
    ("GET", "/api/Subscription/me"): None,
    ("GET", "/api/Printers/summary"): "printers:read",
    ("GET", "/api/Printers/{id}"): "printers:read",
    ("GET", "/api/Printers/{id}/filament"): "printers:read",
    ("POST", "/api/Printers"): "printers:create",
    ("PUT", "/api/Printers/{id}"): "printers:slots",
    ("PUT", "/api/Printers/{id}/slots/{slot}"): "printers:slots",
    ("DELETE", "/api/Printers/{id}/slots/{slot}"): "printers:slots",
    ("GET", "/api/Filaments/by-tag/{uid}"): "filaments:read",
    ("GET", "/api/Filaments/by-number/{number}"): "filaments:read",
    ("GET", "/api/Prints/{id}"): "prints:read",
    ("GET", "/api/Prints/claimable"): "prints:read",
    ("GET", "/api/Prints/{id}/files"): "prints:read",
    ("POST", "/api/Prints"): "prints:write",
    ("PATCH", "/api/Prints/{id}"): "prints:write",
    ("POST", "/api/Prints/{id}/claim"): "prints:write",
    ("POST", "/api/Prints/{id}/complete"): "prints:write",
    ("POST", "/api/Prints/{id}/image"): "prints:write",
    ("POST", "/api/Prints/{id}/slicer-settings"): "prints:write",
    ("POST", "/api/Prints/{id}/files/upload-url"): "files:write",
    ("POST", "/api/Prints/{id}/files/confirm"): "files:write",
    ("PUT", "/api/Connections/{instance}"): "connections:write",
    ("PUT", "/api/bridges/me"): "bridge:sync",
    ("GET", "/api/bridges/me/desired"): "bridge:sync",
}
"""Where a device token works, and the scope it needs there (None: any grant)."""
_GRANT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("grant")
"""The grant of the device token the request being handled came with."""
ALL_DEVICE_SCOPES = frozenset(s for s in DEVICE_SCOPES.values() if s)
MAX_PAIRING_TOKEN_AGE_S = 10 * 60
UUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


class FakePrintLogApi:
    def __init__(self, *, api_key: str = "key-123", printers: tuple[int, ...] = (42,)) -> None:
        self.api_key = api_key
        self.pairing: dict[str, Any] | None = None
        """What pairing discovery answers; None is a server with pairing off (503)."""
        self.auth0: Any = None
        """The FakeAuth0 whose access tokens pairing accepts."""
        self.grants: dict[int, dict[str, Any]] = {}
        """Device grants by id: `deviceId`, `name`, `scopes`, `tokenHash`."""
        self.pairings: list[tuple[int, dict[str, Any]]] = []
        """Every pairing answer: its status and body (the token left out)."""
        self.max_grants = 25
        self.revoke_during_repair: set[str] = set()
        """Device ids whose next re-pair loses the race with a revoke (409 once)."""
        self.throttled = False
        """While True, every device-token request is answered 429 `device_token_throttled`."""
        self.credentials: list[tuple[str, str]] = []
        """The credential each request came with, and its path: "key", "device" or "both"."""
        self.bridges: dict[int, dict[str, Any]] = {}
        """Each checked-in bridge, by grant id: what its check-ins stored, and `desired`."""
        self.check_ins: list[dict[str, Any]] = []
        """Every check-in body received."""
        self.desired_polls: list[str | None] = []
        """Every desired poll's `If-None-Match`."""
        self.check_in_limited: float | None = None
        """When set, every check-in is answered 429 with this `Retry-After`."""
        self.check_in_seconds = 300
        self.desired_poll_seconds = 60
        self._entry_ids = itertools.count(1)
        self.created_printers: list[dict[str, Any]] = []
        """Every POST /api/Printers body received."""
        self._spent_pairing_tokens: set[str] = set()
        self._grant_ids = itertools.count(1)
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
        self.latest_agent_version: str | None = None
        self.keys_supported = True
        """False: an API from before sealed credentials (no key fields in the answer)."""
        """The `latestAgentVersion` connections answer with: the newest bridge release, which
        the API reads from GitHub, or None when it doesn't know one."""
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
        """Each printer's own fields (`PRINTER_FIELDS` and its category's nickname), when the
        user or a PUT has set them."""
        self._ids = itertools.count(1)
        self._image_ids = itertools.count(1)
        self.lookup = True
        """False answers the lookup for prints to adopt 400, as an API without it does."""
        self.lookups: list[dict[str, str]] = []
        """Every lookup's query."""
        self.claims: list[tuple[int, dict[str, Any]]] = []
        self.spoolman_ids = True
        """False is an API that doesn't store Spoolman ids: `by-spoolman-id` isn't a route."""
        """Every claim body received, by print id."""
        self.slicer_settings_route = True
        """False answers the slicer settings endpoint 404, as an API without it does."""
        self.slicer_settings_status: int | None = None
        """When set, every slicer settings send is answered with this status."""
        self.slicer_posts: list[tuple[int, dict[str, Any]]] = []
        """Every slicer settings body received, by print id, refused ones included."""
        self.slicer_records: dict[int, dict[str, Any]] = {}
        """The slicer settings each print has from a connector: the last body recorded."""
        self.pro = False
        """Whether the key owner has Pro, which file attachments need."""
        self.max_files = 5
        self.attachments: dict[int, list[dict[str, Any]]] = {}
        """Each print's attached files, as `GET /files` lists them."""
        self.upload_requests: list[dict[str, Any]] = []
        """Every upload-url body received."""
        self.confirms: list[dict[str, Any]] = []
        """Every confirm body received."""
        self.confirm_cut = False
        """While True, a confirm is carried out but cut off before its answer."""
        self.blobs: dict[str, bytes] = {}
        """What the store holds, by blob path (`printattachments/<print>/<name>`)."""
        self.blob_puts: list[dict[str, str]] = []
        """The headers of every PUT the store received."""
        self.blob_status: int | None = None
        """When set, the store answers every PUT with this status."""
        self._attachment_ids = itertools.count(1)
        self._failures: list[tuple[int, dict[str, str]]] = []
        self._server: TestServer | None = None
        self._store: TestServer | None = None
        self.store_port: int | None = None
        """The fake blob store's port, once started."""

    # -- serving -------------------------------------------------------------------------------

    async def start(self) -> str:
        app = web.Application(middlewares=[self._middleware])
        app.router.add_get("/api/device-grants/pairing", self._pairing_config)
        app.router.add_post("/api/device-grants", self._pair)
        app.router.add_get("/api/Users/me", self._me)
        app.router.add_get("/api/Users/me/user-settings", self._settings)
        app.router.add_get("/api/Printers/summary", self._printer_summary)
        app.router.add_post("/api/Printers", self._create_printer)
        app.router.add_get("/api/Printers/{id}", self._printer)
        app.router.add_put("/api/Printers/{id}", self._put_printer)
        app.router.add_get("/api/Printers/{id}/filament", self._loaded_filament)
        app.router.add_put("/api/Printers/{id}/slots/{slot}", self._load_slot)
        app.router.add_delete("/api/Printers/{id}/slots/{slot}", self._unload_slot)
        app.router.add_get("/api/Filaments/by-tag/{uid}", self._by_tag)
        app.router.add_get("/api/Filaments/by-number/{number}", self._by_number)
        app.router.add_get("/api/Filaments/by-spoolman-id/{number}", self._by_spoolman_id)
        app.router.add_post("/api/Prints", self._create)
        app.router.add_get("/api/Prints/claimable", self._claimable)
        app.router.add_post("/api/Prints/{id}/claim", self._claim)
        app.router.add_get("/api/Prints/{id}", self._get)
        app.router.add_patch("/api/Prints/{id}", self._patch)
        app.router.add_post("/api/Prints/{id}/complete", self._complete)
        app.router.add_post("/api/Prints/{id}/image", self._upload_image)
        app.router.add_post("/api/Prints/{id}/slicer-settings", self._slicer_settings)
        app.router.add_get("/api/Prints/{id}/files", self._files)
        app.router.add_post("/api/Prints/{id}/files/upload-url", self._upload_url)
        app.router.add_post("/api/Prints/{id}/files/confirm", self._confirm)
        app.router.add_get("/api/Subscription/me", self._subscription)
        app.router.add_put("/api/Connections/{instance}", self._put_connection)
        app.router.add_put("/api/bridges/me", self._check_in)
        app.router.add_get("/api/bridges/me/desired", self._get_desired)
        self._server = TestServer(app)
        await self._server.start_server()
        store = web.Application()
        store.router.add_put("/{path:.+}", self._put_blob)
        self._store = TestServer(store)
        await self._store.start_server()
        self.store_port = self._store.port
        return str(self._server.make_url("")).rstrip("/")

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()
        if self._store is not None:
            await self._store.close()

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
        if request.path.startswith("/api/device-grants"):
            return await handler(request)  # anonymous, or the pairing token's own checks
        bearer = request.headers.get("Authorization", "")
        device = bearer[len("Bearer ") :] if bearer.startswith("Bearer pld_") else None
        has_key = "X-Api-Key" in request.headers
        if device is not None and has_key:
            self.credentials.append(("both", request.path))
            return _coded(400, "multiple_credentials")
        if device is not None:
            self.credentials.append(("device", request.path))
            return await self._device_request(request, handler, device)
        self.credentials.append(("key", request.path))
        if request.headers.get("X-Api-Key") != self.api_key:
            return web.Response(status=401)
        if request.path.startswith("/api/bridges"):
            return _coded(403, "device_token_required")
        return await handler(request)

    async def _device_request(self, request: web.Request, handler, token: str):
        if self.throttled:
            return _coded(429, "device_token_throttled", headers={"Retry-After": "60"})
        grant = self.grant_for(token)
        if grant is None:
            return _coded(
                401,
                "invalid_device_token",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        route = request.match_info.route.resource
        key = (request.method, route.canonical if route is not None else request.path)
        if key not in DEVICE_SCOPES:
            return _coded(403, "device_endpoint_not_allowed")
        scope = DEVICE_SCOPES[key]
        if scope is not None and scope not in grant["scopes"]:
            return _coded(
                403,
                "insufficient_scope",
                headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{scope}"'},
            )
        grant["lastUsedAt"] = time.time()
        _GRANT.set(grant)
        return await handler(request)

    def grant_for(self, token: str) -> dict[str, Any] | None:
        hashed = hashlib.sha256(token.encode()).hexdigest()
        return next((g for g in self.grants.values() if g["tokenHash"] == hashed), None)

    def revoke(self, grant_id: int) -> None:
        """The user removes the bridge in the app: its token stops working at once."""
        del self.grants[grant_id]

    def issue_device_token(
        self, scopes: Any = None, device_id: str | None = None, name: str = "bridge"
    ) -> str:
        """A device token for a grant made as pairing would (all device scopes by default),
        without the device flow."""
        token = "pld_" + secrets.token_urlsafe(32)
        grant_id = next(self._grant_ids)
        self.grants[grant_id] = {
            "id": grant_id,
            "deviceId": device_id or str(uuid.uuid4()),
            "name": name,
            "scopes": sorted(ALL_DEVICE_SCOPES if scopes is None else scopes),
            "tokenHash": hashlib.sha256(token.encode()).hexdigest(),
        }
        return token

    def grant_by_device(self, device_id: str) -> dict[str, Any] | None:
        return next((g for g in self.grants.values() if g["deviceId"] == device_id), None)

    # -- bridges (api#288) ---------------------------------------------------------------------

    def bridge(self, grant_id: int | None = None) -> dict[str, Any]:
        """The only bridge (or the grant's)."""
        if grant_id is None:
            [grant_id] = self.bridges
        return self.bridges[grant_id]

    def _new_version(self, bridge: dict[str, Any]) -> None:
        bridge["desired"]["version"] = uuid.uuid4().hex

    def set_secret(
        self,
        entry_id: int,
        field: str,
        plaintext: str | None = None,
        *,
        sealed: str | None = None,
        grant_id: int | None = None,
    ) -> str:
        """The user enters a credential in the app: sealed to the bridge's current key (or
        `sealed` as given). Returns the sealed value."""
        from sealing import seal

        from printlog_bridge.core.credentials import CredentialKey

        bridge = self.bridge(grant_id)
        current = bridge.get("publicKey")
        assert current is not None, "the bridge has no confirmed key to seal to"
        if sealed is None:
            from cryptography.hazmat.primitives import serialization

            key = CredentialKey.__new__(CredentialKey)
            key.spki = base64.b64decode(current)
            assert serialization.load_der_public_key(key.spki)
            sealed = seal(key, bridge["id"], entry_id, field, plaintext or "")
        entry = next(p for p in bridge["desired"]["printers"] if p["id"] == entry_id)
        entry["secrets"] = [s for s in entry["secrets"] if s["field"] != field]
        entry["secrets"].append(
            {
                "field": field,
                "sealed": sealed,
                "keyFingerprint": bridge["publicKeyFingerprint"],
                "updatedAt": datetime.now(timezone.utc).isoformat(),
                "stale": False,
            }
        )
        bridge["issues"] = [i for i in bridge.get("issues", []) if i["entryId"] != entry_id]
        self._new_version(bridge)
        return sealed

    def confirm_key(self, grant_id: int | None = None) -> None:
        """The owner confirms the pending key: values sealed to the old one go stale."""
        bridge = self.bridge(grant_id)
        assert bridge.get("pendingPublicKey"), "no pending key"
        bridge["publicKey"] = bridge.pop("pendingPublicKey")
        bridge["publicKeyFingerprint"] = bridge.pop("pendingPublicKeyFingerprint")
        for entry in bridge["desired"]["printers"]:
            entry["secrets"] = []
        self._new_version(bridge)

    def _take_key(self, bridge: dict[str, Any], body: dict[str, Any], first: bool) -> list:
        key, given = body.get("publicKey"), body.get("publicKeyFingerprint")
        if key is None:
            return []
        fingerprint = _key_fingerprint(key)
        if fingerprint is None:
            return [{"field": "publicKey", "code": "invalid_public_key"}]
        if given is not None and given != fingerprint:
            return [{"field": "publicKeyFingerprint", "code": "invalid_public_key"}]
        if first:
            bridge["publicKey"], bridge["publicKeyFingerprint"] = key, fingerprint
        elif fingerprint == bridge.get("publicKeyFingerprint"):
            pass  # the current key again leaves a pending one alone
        else:
            bridge["pendingPublicKey"], bridge["pendingPublicKeyFingerprint"] = key, fingerprint
        return []

    def connect(
        self,
        kind: str,
        host: str,
        printer_id: int,
        *,
        port: int | None = None,
        serial: str | None = None,
        grant_id: int | None = None,
    ) -> int:
        """The user connects a printer to the bridge in the app. Returns the entry's id."""
        bridge = self.bridge(grant_id)
        entry_id = next(self._entry_ids)
        bridge["desired"]["printers"].append(
            {
                "id": entry_id,
                "printerId": printer_id,
                "printerName": f"Printer {printer_id}",
                "kind": kind,
                "host": host,
                "port": port,
                "serial": serial,
                "identityKey": f"{kind}:sn:{serial}" if serial else f"{kind}:ip:{host}:{port}",
                "macs": [],
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "secretFields": list(SECRET_FIELDS.get(kind, ())),
                "secrets": [],
                "credentialIssues": [],
            }
        )
        self._new_version(bridge)
        return entry_id

    def disconnect(self, entry_id: int, grant_id: int | None = None) -> None:
        bridge = self.bridge(grant_id)
        printers = bridge["desired"]["printers"]
        printers[:] = [p for p in printers if p["id"] != entry_id]
        self._new_version(bridge)

    def request_scan(self, grant_id: int | None = None) -> str:
        bridge = self.bridge(grant_id)
        when = datetime.now(timezone.utc).isoformat()
        bridge["desired"]["scanRequestedAt"] = when
        self._new_version(bridge)
        return when

    async def _check_in(self, request: web.Request) -> web.Response:
        grant = _GRANT.get()
        try:
            body = await request.json()
        except ValueError:
            return _problem(400, "invalid body")
        if not isinstance(body, dict):
            return _problem(400, "invalid body")
        self.check_ins.append(body)
        if self.check_in_limited is not None:
            return web.Response(status=429, headers={"Retry-After": str(self.check_in_limited)})
        networks, discovered = body.get("networks"), body.get("discovered")
        if (
            (isinstance(networks, list) and len(networks) > 16)
            or (isinstance(discovered, list) and len(discovered) > 50)
            or any(len(d.get("macs") or []) > 8 for d in discovered or [] if isinstance(d, dict))
            or not _matches(body.get("agentVersion"), r"[0-9A-Za-z.+-]{1,50}")
            or not _matches(body.get("mode"), r"[a-z][a-z0-9-]{0,19}")
            or not _text_ok(body.get("hostName"))
        ):
            return _coded(400, "invalid_check_in")
        issues = body.get("credentialIssues")
        if issues is not None and (
            not isinstance(issues, list)
            or len(issues) > 200
            or not body.get("credentialIssuesVersion")
        ):
            return _coded(400, "invalid_check_in")
        first = grant["id"] not in self.bridges
        bridge = self.bridges.setdefault(
            grant["id"],
            {
                "id": grant["id"],
                "desired": {"version": uuid.uuid4().hex, "scanRequestedAt": None, "printers": []},
                "networks": [],
                "discovered": [],
            },
        )
        rejected: list[dict[str, str]] = []
        if self.keys_supported:
            rejected.extend(self._take_key(bridge, body, first))
            if (
                issues is not None
                and body["credentialIssuesVersion"] == bridge["desired"]["version"]
            ):
                bridge["issues"] = issues
        for field in ("agentVersion", "mode", "hostName", "lastSweepAt"):
            if body.get(field) is not None:
                bridge[field] = body[field]
        if isinstance(networks, list):
            kept = []
            for i, text in enumerate(networks):
                code = _network_problem(text)
                if code:
                    rejected.append({"field": f"networks[{i}]", "code": code})
                else:
                    kept.append(str(ipaddress.ip_network(text, strict=False)))
            bridge["networks"] = kept
        if isinstance(discovered, list):
            kept, seen = [], set()
            for i, printer in enumerate(discovered):
                code = _printer_problem(printer)
                identity = _identity(printer) if code is None else None
                if code is None and identity in seen:
                    code = "duplicate_printer"
                if code:
                    rejected.append({"field": f"discovered[{i}]", "code": code})
                    continue
                seen.add(identity)
                kept.append(printer)
            bridge["discovered"] = kept
        answer = {
            "bridgeId": grant["id"],
            "checkInSeconds": self.check_in_seconds,
            "desiredPollSeconds": self.desired_poll_seconds,
            "desired": bridge["desired"],
            "rejected": rejected,
        }
        if self.keys_supported:
            answer["publicKeyFingerprint"] = bridge.get("publicKeyFingerprint")
            answer["pendingPublicKeyFingerprint"] = bridge.get("pendingPublicKeyFingerprint")
        return web.json_response(answer)

    async def _get_desired(self, request: web.Request) -> web.Response:
        grant = _GRANT.get()
        self.desired_polls.append(request.headers.get("If-None-Match"))
        bridge = self.bridges.get(grant["id"])
        if bridge is None:
            return _coded(404, "bridge_not_registered")
        etag = f'W/"{bridge["desired"]["version"]}"'
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})
        return web.json_response(bridge["desired"], headers={"ETag": etag})

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
        number: int | None = None,
        spoolman_id: int | None = None,
    ) -> str:
        """A spool in the user's inventory, as the app would add it. Returns its id. Its
        `spoolNumber` is `number`, or else the next after the highest so far, as the app's."""
        filament_id = str(uuid.uuid4())
        if number is None:
            number = 1 + max((f["spoolNumber"] for f in self.filaments.values()), default=0)
        self.filaments[filament_id] = {
            "id": filament_id,
            "spoolNumber": number,
            "displayName": display_name,
            "materialType": material,
            "colorHex": color_hex,
            "tagUids": sorted(normalize_tag(t) for t in tags),
            "spoolmanId": spoolman_id,
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

    def user_creates(
        self,
        title: str,
        *,
        file_name: str | None = None,
        printer_id: int = 42,
        created: str = "2026-10-10T09:00:00+00:00",
        status: int = PRINTING,
        usage: list[dict[str, Any]] | None = None,
        **fields: Any,
    ) -> int:
        """A print the user made themselves (the Slic3r uploader, or by hand). Returns its id.

        `usage` rows are given as a create sends them (`filament: {id}`, no slot by default)."""
        print_id = next(self._ids)
        self.prints[print_id] = {
            "id": print_id,
            "printerId": printer_id,
            "title": title,
            "fileName": file_name,
            "status": status,
            "startDate": None,
            "createdDate": created,
            "notes": None,
            "externalSource": None,
            "externalId": None,
            "connectionInstanceId": None,
            "connectionId": None,
            "printTimeInSeconds": None,
            "estimatedPrintTimeInSeconds": None,
            "filamentUsageMg": None,
            "estimatedFilamentUsageMg": None,
            "filamentUsage": [_row(r, _spool(r)) for r in usage or []],
            "images": [],
            **fields,
        }
        return print_id

    def by_external_id(self, external_id: str) -> dict[str, Any]:
        [found] = [p for p in self.prints.values() if p["externalId"] == external_id]
        return found

    # -- the API -------------------------------------------------------------------------------

    async def _pairing_config(self, _request: web.Request) -> web.Response:
        if self.pairing is None:
            return _coded(503, "device_pairing_unavailable")
        return web.json_response(self.pairing)

    async def _pair(self, request: web.Request) -> web.Response:
        answer = await self._pairing(request)
        body = json.loads(answer.text) if answer.text else None
        shown = {k: v for k, v in (body or {}).items() if k != "token"}
        self.pairings.append((answer.status, shown))
        return answer

    async def _pairing(self, request: web.Request) -> web.Response:
        if "X-Api-Key" in request.headers:
            return web.Response(status=401)
        bearer = request.headers.get("Authorization", "")
        token = bearer[len("Bearer ") :] if bearer.startswith("Bearer ") else ""
        claims = self.auth0.issued.get(token) if self.auth0 is not None and token else None
        if claims is None:
            return web.Response(status=401)
        if (
            claims["azp"] != self.auth0.client_id
            or time.time() - claims["iat"] > MAX_PAIRING_TOKEN_AGE_S
        ):
            return web.Response(status=403)
        scopes = claims["scope"].split()
        if "offline_access" in scopes:
            return _coded(403, "offline_access_not_allowed")
        device_scopes = sorted(s for s in scopes if s in ALL_DEVICE_SCOPES)
        if not device_scopes:
            return _coded(403, "insufficient_scope")
        if claims["jti"] in self._spent_pairing_tokens:
            return _coded(403, "pairing_token_used")
        body = await request.json()
        device_id, name = body.get("deviceId"), body.get("name")
        if (
            not isinstance(device_id, str)
            or not UUID.match(device_id)
            or not isinstance(name, str)
            or not 1 <= len(name) <= 100
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
        ):
            return _coded(400, "invalid_request")
        new_token = "pld_" + secrets.token_urlsafe(32)
        hashed = hashlib.sha256(new_token.encode()).hexdigest()
        grant = self.grant_by_device(device_id)
        if device_id in self.revoke_during_repair:
            self.revoke_during_repair.discard(device_id)
            if grant is not None:
                del self.grants[grant["id"]]
            self._spent_pairing_tokens.add(claims["jti"])
            return _coded(409, "device_grant_revoked")
        if grant is None:
            if len(self.grants) >= self.max_grants:
                return _coded(409, "device_grant_limit")
            grant = {"id": next(self._grant_ids), "deviceId": device_id, "createdAt": time.time()}
            self.grants[grant["id"]] = grant
            status = 201
        else:
            status = 200
        grant.update(name=name, scopes=device_scopes, tokenHash=hashed)
        self._spent_pairing_tokens.add(claims["jti"])
        return web.json_response(
            {
                "id": grant["id"],
                "deviceId": device_id,
                "name": name,
                "scopes": device_scopes,
                "createdAt": "2026-10-27T18:04:11+00:00",
                "token": new_token,
                "tokenType": "Bearer",
            },
            status=status,
        )

    async def _printer_summary(self, request: web.Request) -> web.Response:
        page = int(request.query.get("pageNumber", "1"))
        size = int(request.query.get("pageSize", "10"))
        printers = sorted(self.printers, reverse=True)
        items = [
            {
                "id": p,
                **{k: self._fields(p)[k] for k in ("name", "make", "model", "isActive")},
                "slotCount": self.slot_counts.get(p, 1),
            }
            for p in printers[(page - 1) * size : page * size]
        ]
        return web.json_response(
            {
                "paging": {"totalCount": len(printers), "currentPage": page, "pageSize": size},
                "items": items,
            }
        )

    async def _create_printer(self, request: web.Request) -> web.Response:
        body = await request.json()
        if any(not (body.get(f) or "").strip() for f in ("make", "model", "name")):
            return _problem(400, "One or more validation errors occurred.")
        printer_id = max(self.printers, default=0) + 100
        self.printers.add(printer_id)
        self.loaded[printer_id] = []
        if body.get("slotCount"):
            self.slot_counts[printer_id] = int(body["slotCount"])
        fields = {k: v for k, v in body.items() if k in PRINTER_FIELDS}
        self.user_edits_printer(printer_id, **fields)
        self.created_printers.append(body)
        return web.json_response(self._printer_detail(printer_id), status=201)

    async def _me(self, _request: web.Request) -> web.Response:
        return web.json_response({"id": 7, "displayName": "Maker"})

    async def _settings(self, _request: web.Request) -> web.Response:
        return web.json_response(self.settings)

    async def _printer(self, request: web.Request) -> web.Response:
        printer_id = int(request.match_info["id"])
        if printer_id not in self.printers:
            return web.Response(status=404)
        return web.json_response(self._printer_detail(printer_id))

    def _fields(self, printer_id: int) -> dict[str, Any]:
        fields = {k: None for k in PRINTER_FIELDS}
        fields.update(
            make="Snapmaker",
            model="U1",
            name=f"Printer {printer_id}",
            nozzleDiameter=0.4,
            filamentDiameter=1.75,
            isActive=True,
            category="FDM",
        )
        fields.update(self._printer_fields.get(printer_id, {}))
        return fields

    def user_edits_printer(self, printer_id: int, **fields: Any) -> None:
        """What the user sets on the printer in the app (`category` is its nickname)."""
        self._printer_fields.setdefault(printer_id, {}).update(fields)

    def beam_diameter(self, printer_id: int) -> float | None:
        """The printer's `beamDiameter`, which only a PUT sets and no GET shows."""
        return self._fields(printer_id)["beamDiameter"]

    def _printer_detail(self, printer_id: int) -> dict[str, Any]:
        fields = self._fields(printer_id)
        detail: dict[str, Any] = {"id": printer_id}
        detail.update(
            (k, v)
            for k, v in fields.items()
            if k in PRINTER_FIELDS and k not in NOT_IN_PRINTER_DETAIL
        )
        detail["category"] = {"nickname": fields["category"]}
        detail["slotCount"] = self.slot_counts.get(printer_id, 1)
        detail["loadedFilaments"] = self._loaded_list(printer_id)
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
                return web.Response(status=400, text=f"The {field.title()} field is required.")
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
        fields = {k: body.get(k) for k in PRINTER_FIELDS}
        fields["isActive"] = bool(body.get("isActive"))
        fields["category"] = body.get("category") or self._fields(printer_id)["category"]
        self._printer_fields[printer_id] = fields
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

    async def _by_number(self, request: web.Request) -> web.Response:
        number = request.match_info["number"]
        for filament in self.filaments.values():
            if number.isdigit() and filament["spoolNumber"] == int(number):
                return web.json_response(filament)
        return web.Response(status=404)

    async def _by_spoolman_id(self, request: web.Request) -> web.Response:
        number = request.match_info["number"]
        for filament in self.filaments.values():
            if self.spoolman_ids and number.isdigit() and filament["spoolmanId"] == int(number):
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
                existing["filamentUsage"] = [_row(r, _spool(r)) for r in body["filamentUsage"]]
            return web.json_response(existing, status=200)

        spools = [_spool(r) for r in body.get("filamentUsage") or []]
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
            "createdDate": datetime.now(timezone.utc).isoformat(),
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
            "filamentUsage": [_row(r, _spool(r)) for r in body.get("filamentUsage") or []],
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
            "latestAgentVersion": self.latest_agent_version,
        }
        for field in SOFTWARE_FIELDS:
            # Like the API (api#282, #80): missing or null keeps what was stored.
            kept = (existing or {}).get(field)
            connection[field] = body[field] if body.get(field) is not None else kept
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

    async def _subscription(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "active" if self.pro else None,
                "isPro": self.pro,
                "maxFilesPerPrint": self.max_files if self.pro else 0,
                "maxFileStorageBytes": 50 * 1024**3 if self.pro else 0,
                "usedFileStorageBytes": sum(
                    f["sizeBytes"] for files in self.attachments.values() for f in files
                ),
            }
        )

    async def _files(self, request: web.Request) -> web.Response:
        print_id = int(request.match_info["id"])
        if print_id not in self.prints:
            return web.Response(status=404)
        return web.json_response(self.attachments.get(print_id, []))

    async def _upload_url(self, request: web.Request) -> web.Response:
        print_id = int(request.match_info["id"])
        body = await request.json()
        self.upload_requests.append(body)
        problem = _file_problem(body)
        if problem:
            return web.Response(status=400, text=problem)
        if print_id not in self.prints:
            return web.Response(status=404)
        if not self.pro:
            return web.Response(status=403, text="File attachments require a Pro subscription.")
        if len(self.attachments.get(print_id, [])) >= self.max_files:
            return web.Response(
                status=400, text=f"Maximum of {self.max_files} files per print allowed."
            )
        name = str(body["fileName"])
        extension = name[name.rfind(".") :].lower() if "." in name else ""
        if extension not in ATTACHMENT_EXTENSIONS:
            return web.Response(status=400, text=f"File type '{extension}' is not supported.")
        blob = f"{print_id}/{uuid.uuid4()}{extension}"
        assert self._store is not None
        sas = self._store.make_url(f"/{ATTACHMENT_CONTAINER}/{blob}").with_query(
            {"sv": "2025-05-05", "sp": "cw", "sig": SAS_SIGNATURE}
        )
        return web.json_response({"sasUrl": str(sas), "blobPath": f"{ATTACHMENT_CONTAINER}/{blob}"})

    async def _put_blob(self, request: web.Request) -> web.Response:
        self.blob_puts.append(dict(request.headers))
        if self.blob_status is not None:
            return web.Response(status=self.blob_status)
        if request.query.get("sig") != SAS_SIGNATURE:
            return web.Response(status=403, text="AuthenticationFailed")
        if request.headers.get("x-ms-blob-type") != "BlockBlob":
            return web.Response(status=400, text="MissingRequiredHeader")
        self.blobs[request.match_info["path"]] = await request.read()
        return web.Response(status=201)

    async def _confirm(self, request: web.Request) -> web.Response:
        print_id = int(request.match_info["id"])
        body = await request.json()
        self.confirms.append(body)
        problem = _file_problem(body) or (None if body.get("blobPath") else "BlobPath is required.")
        if problem:
            return web.Response(status=400, text=problem)
        if print_id not in self.prints:
            return web.Response(status=404)
        if not self.pro:
            return web.Response(status=403, text="File attachments require a Pro subscription.")
        if not str(body["blobPath"]).startswith(ATTACHMENT_CONTAINER + "/"):
            return web.Response(status=400, text="Invalid blob path.")
        files = self.attachments.setdefault(print_id, [])
        row = {
            "id": next(self._attachment_ids),
            "originalFileName": body["fileName"],
            "sizeBytes": body["sizeBytes"],
            "contentType": body["contentType"],
            "displayOrder": len(files),
            "blobPath": body["blobPath"],  # not in the API's answer; for the tests
        }
        files.append(row)
        if self.confirm_cut:
            if request.transport is not None:
                request.transport.close()
            raise web.HTTPServiceUnavailable()  # never reaches the bridge: the line is cut
        return web.json_response({k: v for k, v in row.items() if k != "blobPath"})

    def attached(self, print_id: int) -> list[bytes]:
        """What's in the store for each file attached to the print, in order."""
        return [self.blobs.get(f["blobPath"], b"") for f in self.attachments.get(print_id, [])]

    def default_image(self, print_id: int) -> bytes | None:
        """The bytes of the image shown for the print, if it has one."""
        for image in self.prints[print_id]["images"]:
            if image["isDefault"]:
                return self.images[image["id"]]
        return None

    async def _claimable(self, request: web.Request) -> web.Response:
        if not self.lookup:
            return web.Response(status=400, text="The value 'claimable' is not valid.")
        query = dict(request.query)
        self.lookups.append(query)
        printer_id = int(query["printerId"])
        after = datetime.fromisoformat(query["createdAfter"]) if "createdAfter" in query else None
        found = [
            p
            for p in self.prints.values()
            if p["printerId"] == printer_id
            and p["status"] == PRINTING
            and not p["externalSource"]
            and not p["externalId"]
            and (after is None or datetime.fromisoformat(p["createdDate"]) > after)
        ]
        found.sort(key=lambda p: (p["createdDate"], p["id"]), reverse=True)
        return web.json_response(
            [
                {
                    "id": p["id"],
                    "title": p["title"],
                    "printerId": p["printerId"],
                    "status": p["status"],
                    "source": p.get("source", 0),
                    "fileName": p["fileName"],
                    "fileHash": p.get("fileHash"),
                    "createdDate": p["createdDate"],
                    "startDate": p["startDate"],
                    "hasSlicerSettings": bool(p.get("slicer")),
                    "filamentUsage": sorted(
                        p["filamentUsage"],
                        key=lambda r: r["slot"] if r["slot"] is not None else 1 << 31,
                    ),
                }
                for p in found[: int(query.get("limit", 20))]
            ]
        )

    async def _claim(self, request: web.Request) -> web.Response:
        found = self.prints.get(int(request.match_info["id"]))
        if found is None:
            return web.Response(status=404)
        body = await request.json()
        self.claims.append((found["id"], body))
        source, external_id = body.get("externalSource"), body.get("externalId")
        if not (source or "").strip() or not (external_id or "").strip():
            return web.Response(status=400, text="externalSource and externalId are both required")
        if found["externalSource"] == source and found["externalId"] == external_id:
            return web.json_response(found)  # a replay, before the status is checked
        if found["externalSource"] or found["externalId"]:
            return _conflict("The print is already linked to another job.")
        if found["status"] != PRINTING:
            return _conflict("Only a print that is Printing can be claimed.")
        holder = next(
            (
                p["id"]
                for p in self.prints.values()
                if p["externalSource"] == source and p["externalId"] == external_id
            ),
            None,
        )
        if holder is not None:
            return _conflict("The job is already logged as another print.", holder)
        found["externalSource"] = source
        found["externalId"] = external_id
        connection = self.connections.get((body.get("connectionInstanceId") or "").strip())
        if connection is not None:
            found["connectionInstanceId"] = body["connectionInstanceId"]
            found["connectionId"] = connection.get("id")
        found["startDate"] = found["startDate"] or body.get("startDate")
        return web.json_response(found)

    async def _slicer_settings(self, request: web.Request) -> web.Response:
        if not self.slicer_settings_route:
            return web.Response(status=404)
        print_id = int(request.match_info["id"])
        body = await request.json()
        self.slicer_posts.append((print_id, body))
        if self.slicer_settings_status is not None:
            return web.Response(status=self.slicer_settings_status)
        block, header, metadata = body.get("configBlock"), body.get("header"), body.get("metadata")
        if not (block or "").strip() and metadata is None:
            return _problem(400, "One or more validation errors occurred.")
        problem = _metadata_problem(metadata or {})
        if problem:
            return _problem(400, problem)
        if len((block or "").encode()) > SLICER_BLOCK_BYTES:
            return _problem(413, "configBlock is over 512 KiB.")
        if len((header or "").encode()) > SLICER_HEADER_BYTES:
            return _problem(413, "header is over 64 KiB.")
        # Parsed before the print is looked up, as `PrintSlicerSettingsService.Record` does.
        has_settings = bool(block) and SETTING_LINE.search(block) is not None
        slicer = (metadata or {}).get("slicer")
        if not has_settings:
            if metadata is None:
                return _problem(400, "configBlock holds no slicer settings.")
            if not (slicer or "").strip():
                return _problem(400, "metadata.slicer is required when there is no configBlock.")
        found = self.prints.get(print_id)
        if found is None:
            return _problem(404, "Not Found")
        if found.get("slicer") and print_id not in self.slicer_records:
            return _problem(409, "This print's slicer settings were recorded by a slicer plugin.")
        replaced = print_id in self.slicer_records
        self.slicer_records[print_id] = body
        named = next((name for name in SLICERS if name in (header or "")), None)
        found["slicer"] = (named or slicer or "other").lower()
        found["slicerVersion"] = (metadata or {}).get("slicerVersion")
        return web.json_response(
            {
                "printId": print_id,
                "slicer": found["slicer"],
                "slicerVersion": found["slicerVersion"],
                "source": "configBlock" if has_settings else "metadata",
                "replaced": replaced,
            }
        )

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
        single_slot = self.slot_counts.get(found["printerId"], 1) <= 1
        _merge_actual_usage(found["filamentUsage"], usage, single_slot)
        return web.json_response(found)


def _merge_actual_usage(
    rows: list[dict[str, Any]], usage: list[dict[str, Any]], single_slot: bool
) -> None:
    """`PrintService.MergeActualUsage`: by filament first. A filament no row has yet, on a slot,
    takes that slot's unlinked row and links it (#162). A row with no filament takes the unlinked
    row for its slot, else the next unlinked row in order. Slot-only rows claim theirs first.

    On a printer with one slot, an unlinked row with no slot counts as slot 0 in both slot
    passes, after any row already on slot 0, and is stamped slot 0 when taken (api#168,
    708fb32)."""
    unlinked = [r for r in rows if r["filament"] is None]

    def take_on_slot(slot: int) -> dict[str, Any] | None:
        """`TakeUnlinkedRowOnSlot`."""
        row = next((r for r in unlinked if r["slot"] == slot), None)
        if row is None and single_slot and slot == 0:
            row = next((r for r in unlinked if r["slot"] is None), None)
        if row is not None:
            unlinked.remove(row)
            if single_slot and row["slot"] is None:
                row["slot"] = 0
        return row

    matches: dict[int, dict[str, Any] | None] = {}
    for n, incoming in enumerate(usage):
        if not incoming.get("filamentId") and incoming.get("slot") is not None:
            matches[n] = take_on_slot(incoming["slot"])
    for n, incoming in enumerate(usage):
        if incoming.get("filamentId") and incoming.get("slot") is not None:
            if any((r["filament"] or {}).get("id") == incoming["filamentId"] for r in rows):
                continue
            matches[n] = take_on_slot(incoming["slot"])
    for n, incoming in enumerate(usage):
        if incoming.get("filamentId"):
            match = next(
                (r for r in rows if (r["filament"] or {}).get("id") == incoming["filamentId"]),
                None,
            ) or matches.get(n)
            if match is not None:
                match["filament"] = {"id": incoming["filamentId"]}
        elif incoming.get("slot") is not None:
            match = matches[n]
        else:
            match = unlinked.pop(0) if unlinked else None
        if match is None:
            rows.append(_row(incoming, incoming.get("filamentId")))
        else:
            # All three values are replaced, so an earlier completion's can't outlive it.
            match["source"] = _source(incoming)
            for field in ("amountMg", "lengthInM", "volumeMl"):
                match[field] = incoming.get(field)
            match["actualIsEstimate"] = bool(incoming.get("actualIsEstimate"))


def _file_problem(body: dict[str, Any]) -> str | None:
    """What the API's model validation refuses in an upload-url or confirm body."""
    if not body.get("fileName") or len(str(body["fileName"])) > 255:
        return "FileName is required (255 characters at most)."
    if not body.get("contentType") or len(str(body["contentType"])) > 100:
        return "ContentType is required (100 characters at most)."
    size = body.get("sizeBytes")
    if not isinstance(size, int) or not 1 <= size <= MAX_FILE_BYTES:
        return "SizeBytes must be between 1 and 209715200."
    return None


def _conflict(detail: str, print_id: int | None = None) -> web.Response:
    problem: dict[str, Any] = {"status": 409, "title": "Print cannot be claimed", "detail": detail}
    if print_id is not None:
        problem["printId"] = print_id
    return web.json_response(problem, status=409, content_type="application/problem+json")


SLICER_BLOCK_BYTES = 512 * 1024
SLICER_HEADER_BYTES = 64 * 1024
SETTING_LINE = re.compile(r"^[ \t]*;[ \t]*[A-Za-z0-9_]+[ \t]*=", re.MULTILINE)
SLICERS = ("AnycubicSlicerNext", "OrcaSlicer", "PrusaSlicer", "BambuStudio")
SLICER_TEXT_LIMITS = {"slicer": 50, "slicerVersion": 50, "filamentName": 200, "filamentType": 100}
SLICER_NUMBER_LIMITS = {
    "layerHeightMm": (0, 100),
    "firstLayerHeightMm": (0, 100),
    "nozzleDiameterMm": (0, 100),
    "firstLayerNozzleTemperatureC": (-100, 1000),
    "firstLayerBedTemperatureC": (-100, 1000),
}


def _metadata_problem(metadata: dict[str, Any]) -> str | None:
    """`SlicerMetadataDto`'s `[StringLength]` and `[Range]` rules."""
    for field, limit in SLICER_TEXT_LIMITS.items():
        value = metadata.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            return f"The field {field} must be a string with a maximum length of {limit}."
    for field, (low, high) in SLICER_NUMBER_LIMITS.items():
        value = metadata.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"The JSON value for {field} could not be converted."
        if not low <= value <= high:
            return f"The field {field} must be between {low} and {high}."
    return None


_PRIVATE = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7")
)
BRIDGE_KINDS = ("moonraker", "octoprint", "prusalink", "bambu", "sdcp", "duet", "anycubic")
DEFAULT_BRIDGE_PORTS = {"moonraker": 7125, "bambu": 8883, "prusalink": 80, "octoprint": 80,
                        "sdcp": 3030, "duet": 80, "anycubic": 18910}  # fmt: skip


def _matches(value: Any, pattern: str) -> bool:
    return value is None or (isinstance(value, str) and re.fullmatch(pattern, value) is not None)


def _text_ok(value: Any, limit: int = 100) -> bool:
    if value is None:
        return True
    return (
        isinstance(value, str) and len(value) <= limit and not re.search(r"[\x00-\x1f\x7f]", value)
    )


def _private(address: Any) -> bool | None:
    """True or False for an IP literal; None for anything else."""
    try:
        ip = ipaddress.ip_address(str(address))
    except ValueError:
        return None
    if "%" in str(address):
        return None
    return any(ip in n for n in _PRIVATE)


def _network_problem(text: Any) -> str | None:
    try:
        network = ipaddress.ip_network(str(text), strict=False)
    except ValueError:
        return "invalid_network"
    if not any(network.subnet_of(n) for n in _PRIVATE if n.version == network.version):
        return "network_not_private"
    if network.prefixlen < (24 if network.version == 4 else 64):
        return "network_too_large"
    return None


def _clean_mac(text: Any) -> str | None:
    digits = re.sub(r"[:-]", "", str(text).lower())
    if not re.fullmatch(r"[0-9a-f]{12}", digits):
        return None
    if digits in ("000000000000", "ffffffffffff") or int(digits[1], 16) & 1:
        return None
    return digits


def _printer_problem(printer: Any) -> str | None:
    if not isinstance(printer, dict):
        return "invalid_text"
    if printer.get("kind") not in BRIDGE_KINDS:
        return "invalid_kind"
    private = _private(printer.get("address"))
    if private is None:
        return "invalid_address"
    if not private:
        return "address_not_private"
    port = printer.get("port")
    if port is not None and (not isinstance(port, int) or not 0 < port < 65536):
        return "invalid_port"
    if any(_clean_mac(m) is None for m in printer.get("macs") or []):
        return "invalid_mac"
    for field, code in (("serial", "invalid_serial"), ("mainboardId", "invalid_mainboard_id")):
        if printer.get(field) is not None and not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,64}", str(printer[field])
        ):
            return code
    if not _text_ok(printer.get("model")) or not _text_ok(printer.get("name")):
        return "invalid_text"
    return None


def _identity(printer: dict[str, Any]) -> str:
    kind = printer["kind"]
    port = printer.get("port") or DEFAULT_BRIDGE_PORTS[kind]
    if printer.get("serial"):
        return f"{kind}:sn:{str(printer['serial']).upper()}"
    if printer.get("mainboardId"):
        return f"{kind}:mb:{str(printer['mainboardId']).lower()}"
    macs = sorted(_clean_mac(m) for m in printer.get("macs") or [])
    if macs:
        return f"{kind}:mac:{macs[0]}:{port}"
    return f"{kind}:ip:{printer['address']}:{port}"


def _coded(status: int, code: str, headers: dict[str, str] | None = None) -> web.Response:
    """A problem details answer with the API's `code` extension, as `DeviceTokenErrors` sends."""
    return web.json_response(
        {"status": status, "title": code.replace("_", " "), "code": code},
        status=status,
        headers=headers,
        content_type="application/problem+json",
    )


def _problem(status: int, title: str) -> web.Response:
    return web.json_response(
        {"status": status, "title": title}, status=status, content_type="application/problem+json"
    )


TAG_SEPARATORS = re.compile(r"[:\-._\s]")
HEX = re.compile(r"[0-9A-F]+")


def normalize_tag(uid: str) -> str | None:
    """`TagUid.Normalize`: uppercase hex without separators, whole bytes, 64 characters at most."""
    cleaned = TAG_SEPARATORS.sub("", uid).upper()
    if not cleaned or len(cleaned) % 2 or len(cleaned) > 64 or not HEX.fullmatch(cleaned):
        return None
    return cleaned


MEASUREMENTS = {1: "amountMg", 2: "lengthInM", 3: "volumeMl"}  # Weight, Length, Volume
SOFTWARE_FIELDS = ("firmwareName", "firmwareVersion", "hostSoftwareName", "hostSoftwareVersion")
"""A check-in's software fields (api#282), which a null or missing value leaves as they were."""


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


def _spool(sent: dict[str, Any]) -> str | None:
    """The spool a create's usage row names: `filament.id`, as `AddPrintDTO` reads it."""
    return (sent.get("filament") or {}).get("id")


def _row(sent: dict[str, Any], filament_id: str | None) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "slot": sent.get("slot"),
        "filament": {"id": filament_id} if filament_id else None,
        "estimatedAmountMg": sent.get("estimatedAmountMg"),
        "estimatedLengthInM": sent.get("estimatedLengthInM"),
        "estimatedVolumeMl": sent.get("estimatedVolumeMl"),
        "estimatedSource": sent.get("estimatedSource"),
        "amountMg": sent.get("amountMg"),
        "lengthInM": sent.get("lengthInM"),
        "volumeMl": sent.get("volumeMl"),
        "source": sent.get("source"),
        "actualIsEstimate": bool(sent.get("actualIsEstimate")),
        "notes": sent.get("notes"),
    }


SECRET_FIELDS = {
    "bambu": ("access_code",),
    "prusalink": ("password", "username", "api_key"),
    "moonraker": ("moonraker_api_key",),
    "octoprint": ("api_key",),
    "duet": ("password",),
}
_SPKI_PREFIX = "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE"


def _key_fingerprint(key: Any) -> str | None:
    """The fingerprint of a public key in the check-in's strict shape, or None."""
    if not isinstance(key, str) or len(key) != 124 or not key.startswith(_SPKI_PREFIX):
        return None
    try:
        spki = base64.b64decode(key, validate=True)
    except ValueError:
        return None
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), spki[-65:])
    except ValueError:
        return None
    return hashlib.sha256(spki).hexdigest()
