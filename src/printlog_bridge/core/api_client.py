"""The 3D Print Log API, as the bridge uses it.

Authenticates with one credential, never both (the API answers 400 `multiple_credentials`): the
device token `printlog-bridge pair` stored (`Authorization: Bearer pld_...`, #2) when there is
one, else a personal API key (`X-Api-Key`).

A device token the API no longer knows (401 `invalid_device_token`: revoked in the app, or
replaced by pairing again) raises RepairNeeded, and from then on every call raises it without
asking the API, until `printlog-bridge pair` stores a new token, which the next call picks up.
429 `device_token_throttled` is the API limiting guesses from this address: ApiUnavailable, to
wait out, never a reason to pair again.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import aiohttp

from printlog_bridge import __version__
from printlog_bridge.core.device import DeviceFileError, DeviceToken, read_device_token
from printlog_bridge.core.images import mask_url, scrub_url

if TYPE_CHECKING:
    from printlog_bridge.core.config import BridgeConfig

REQUEST_TIMEOUT_S = 15
UPLOAD_TIMEOUT_S = 30
"""An image upload carries up to 10 MB, and slicer settings up to 576 KB, so they get longer
than other requests."""
MIN_UPLOAD_BYTES_PER_S = 64 * 1024
"""An attachment's upload gets `UPLOAD_TIMEOUT_S` plus its size at this rate (512 kbit/s), so
100 MB has about 27 minutes on a slow uplink before it is given up and tried again."""


class ApiError(Exception):
    pass


class AuthRejected(ApiError):
    """The API refused the credentials. Retrying will not help; the user has to act."""


REPAIR_COMMAND = "printlog-bridge pair"
REPAIR_NEEDED = (
    "3D Print Log no longer accepts this bridge's device token: it was removed in the app, or "
    f"replaced by pairing this bridge again somewhere else. Re-pair needed: run `{REPAIR_COMMAND}`"
)


class RepairNeeded(AuthRejected):
    """The device token was revoked or replaced (401 `invalid_device_token`): only pairing the
    bridge again fixes it."""

    def __init__(self) -> None:
        super().__init__(REPAIR_NEEDED)


INVALID_DEVICE_TOKEN = "invalid_device_token"
DEVICE_TOKEN_THROTTLED = "device_token_throttled"
INSUFFICIENT_SCOPE = "insufficient_scope"
THROTTLED_RETRY_S = 60.0
"""How long to wait on a `device_token_throttled` without a `Retry-After` (the API sends 60)."""


class ApiUnavailable(ApiError):
    """The API could not be reached or failed. Worth retrying later."""

    def __init__(
        self, message: str, retry_after: float | None = None, status: int | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        """Seconds the API asked the bridge to wait (`Retry-After`), when it said."""
        self.status = status
        """The HTTP status the API answered with, or None when it couldn't be reached."""


class ApiRejected(ApiError):
    """The API refused this request (400, 403, 413, 415). Sending it again won't change that."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        """The HTTP status the API refused it with."""
        self.code = code
        """The problem's `code`, when the API sent one (`insufficient_scope`...)."""


class ClaimConflict(ApiError):
    """The print can't be claimed (409): it's linked to another job or no longer in Printing,
    or this job is already logged as another print, `print_id`."""

    def __init__(self, claimed: int, print_id: int | None) -> None:
        super().__init__(
            f"print {claimed} can't be claimed"
            + (f": the job is already print {print_id}" if print_id is not None else "")
        )
        self.print_id = print_id
        """The print this job is already logged as, when that's why."""


@dataclass(frozen=True)
class CurrentUser:
    id: int
    display_name: str | None


class ApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        device: DeviceToken | None = None,
        state_dir: Path | None = None,
        on_credentials: Callable[[str | None], None] | None = None,
    ) -> None:
        """`device`, when given, is the credential and `api_key` is never sent. `state_dir` is
        where a re-paired token is looked for after a RepairNeeded. `on_credentials` hears the
        credential problem when one starts (its message) and when it's over (None)."""
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._device = device
        self._state_dir = state_dir
        self._refused: str | None = None
        """The device token the API refused, while no other has replaced it."""
        self.on_credentials = on_credentials
        self._session: aiohttp.ClientSession | None = None
        self.server_time: datetime | None = None
        self.last_etag: str | None = None
        """The `ETag` of the last answer, if it had one."""
        """The API's clock, from the `Date` of its last answer, to tell when ours is off."""

    @classmethod
    def for_config(
        cls, config: BridgeConfig, on_credentials: Callable[[str | None], None] | None = None
    ) -> ApiClient:
        return cls(
            config.api_url,
            config.api_key,
            device=config.device,
            state_dir=config.state_dir,
            on_credentials=on_credentials,
        )

    @property
    def uses_device_token(self) -> bool:
        return self._device is not None

    @property
    def repair_needed(self) -> bool:
        return self._refused is not None

    def __repr__(self) -> str:
        return f"ApiClient({self.base_url!r})"

    async def __aenter__(self) -> ApiClient:
        self._session = aiohttp.ClientSession(
            headers={
                "User-Agent": f"printlog-bridge/{__version__}",
                "Accept": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def current_user(self) -> CurrentUser:
        body = await self._get_json("/api/Users/me")
        return CurrentUser(id=int(body["id"]), display_name=body.get("displayName"))

    async def user_settings(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/Users/me/user-settings")

    async def get_printer(self, printer_id: int) -> dict[str, Any] | None:
        """The printer, or None when it doesn't exist or isn't the key owner's."""
        try:
            return await self._request("GET", f"/api/Printers/{printer_id}", missing_ok=True)
        except ApiRejected:
            return None  # 403: another user's printer

    async def put_printer(self, printer_id: int, body: Mapping[str, Any]) -> dict[str, Any] | None:
        """Replaces the whole printer (every field; loaded spools left out are unloaded)."""
        return await self._request("PUT", f"/api/Printers/{printer_id}", json=body, missing_ok=True)

    async def loaded_filament(self, printer_id: int) -> list[dict[str, Any]] | None:
        """The spools loaded on the printer, in slot order. None when the printer is gone."""
        return await self._request("GET", f"/api/Printers/{printer_id}/filament", missing_ok=True)

    async def load_slot(
        self, printer_id: int, slot: int, filament_id: str, label: str | None = None
    ) -> list[dict[str, Any]]:
        """Loads the spool into the slot, unloading it from anywhere else. Returns what's loaded.

        Raises ApiRejected for a slot beyond the printer's `slotCount`, or another user's spool.
        """
        return await self._request(
            "PUT",
            f"/api/Printers/{printer_id}/slots/{slot}",
            json={"filamentId": filament_id, "slotLabel": label},
        )

    async def unload_slot(self, printer_id: int, slot: int) -> None:
        await self._request("DELETE", f"/api/Printers/{printer_id}/slots/{slot}")

    async def filament_by_tag(self, uid: str) -> dict[str, Any] | None:
        """The key owner's spool with this RFID/NFC tag, or None when none of theirs has it."""
        return await self._request("GET", f"/api/Filaments/by-tag/{uid}", missing_ok=True)

    async def filament_by_number(self, number: int) -> dict[str, Any] | None:
        """The key owner's spool with this `spoolNumber`, or None when none of theirs has it."""
        return await self._request("GET", f"/api/Filaments/by-number/{number}", missing_ok=True)

    async def filament_by_spoolman_id(self, spoolman_id: int) -> dict[str, Any] | None:
        """The key owner's spool linked to this spool in their own Spoolman server
        (`spoolmanId`), or None when none of theirs is, or the API doesn't store the link (or
        doesn't take a device token there)."""
        try:
            return await self._request(
                "GET", f"/api/Filaments/by-spoolman-id/{spoolman_id}", missing_ok=True
            )
        except ApiRejected as e:
            if self._device is not None and e.status == 403:
                return None
            raise

    async def create_print(self, body: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """The print, and whether it is new. An `externalId` already used returns the original."""
        status, print_ = await self._send("POST", "/api/Prints", body)
        return print_, status == 201

    async def claimable_prints(
        self, printer_id: int, created_after: datetime
    ) -> list[dict[str, Any]]:
        """The key owner's prints on the printer a connector could claim (api#165): in Printing,
        linked to no job, created after `created_after`. Newest first."""
        body = await self._request(
            "GET",
            "/api/Prints/claimable",
            params={"printerId": str(printer_id), "createdAfter": created_after.isoformat()},
        )
        return [p for p in body or [] if isinstance(p, Mapping)]

    async def claim_print(self, print_id: int, body: Mapping[str, Any]) -> dict[str, Any] | None:
        """Claims the print for this connector's job: sets its `externalSource`, `externalId`,
        connection, and `startDate` when it has none (api#165). Claiming it again with the same
        pair returns it, also once it's finished.

        None when it's gone. Raises ClaimConflict when it can't be claimed (409)."""
        status, print_ = await self._send(
            "POST", f"/api/Prints/{print_id}/claim", body, missing_ok=True, conflict_ok=True
        )
        if status == 409:
            other = (print_ or {}).get("printId") if isinstance(print_, Mapping) else None
            raise ClaimConflict(print_id, int(other) if other is not None else None)
        return None if status == 404 else print_

    async def get_print(self, print_id: int) -> dict[str, Any] | None:
        return await self._request("GET", f"/api/Prints/{print_id}", missing_ok=True)

    async def complete_print(self, print_id: int, body: Mapping[str, Any]) -> dict[str, Any] | None:
        return await self._request(
            "POST", f"/api/Prints/{print_id}/complete", json=body, missing_ok=True
        )

    async def patch_print(self, print_id: int, body: Mapping[str, Any]) -> dict[str, Any] | None:
        return await self._request("PATCH", f"/api/Prints/{print_id}", json=body, missing_ok=True)

    async def upload_print_image(
        self, print_id: int, data: bytes, content_type: str, file_name: str, is_default: bool
    ) -> dict[str, Any] | None:
        """Adds an image to the print (`isDefault` makes it the one shown for the print).

        None when the print is gone."""
        form = aiohttp.FormData()
        form.add_field("image", data, content_type=content_type, filename=file_name)
        form.add_field("isDefault", "true" if is_default else "false")
        status, body = await self._send(
            "POST",
            f"/api/Prints/{print_id}/image",
            data=form,
            missing_ok=True,
            timeout=UPLOAD_TIMEOUT_S,
        )
        return None if status == 404 else body

    async def post_slicer_settings(self, print_id: int, body: Mapping[str, Any]) -> int:
        """Records the print's slicer settings (api#164): `configBlock`, `header`, `metadata`.
        Sending again replaces what an earlier send recorded.

        Returns the status: 2xx when recorded, 409 when the print's settings came from a slicer
        plugin (the Cura plugin, the Slic3r uploader), which the API keeps, and 404 when the
        print is gone, or the API is older than the endpoint. Raises ApiRejected for 400 (nothing
        usable) and 413 (over the API's size caps), and ApiUnavailable for anything else."""
        status, _ = await self._send(
            "POST",
            f"/api/Prints/{print_id}/slicer-settings",
            body,
            missing_ok=True,
            conflict_ok=True,
            timeout=UPLOAD_TIMEOUT_S,
        )
        return status

    async def subscription(self) -> dict[str, Any]:
        """The key owner's plan: `isPro`, `maxFilesPerPrint`, the storage they have and use."""
        return await self._request("GET", "/api/Subscription/me")

    async def print_files(self, print_id: int) -> list[dict[str, Any]] | None:
        """The files attached to the print. None when the print is gone."""
        body = await self._request("GET", f"/api/Prints/{print_id}/files", missing_ok=True)
        return None if body is None else [f for f in body if isinstance(f, Mapping)]

    async def attachment_upload_url(
        self, print_id: int, file_name: str, content_type: str, size: int
    ) -> dict[str, Any] | None:
        """Where to upload a file for the print (`sasUrl`, good for 15 minutes) and the
        `blobPath` to confirm it with. None when the print is gone.

        Raises ApiRejected without Pro (403), or for a file the API won't take (400): a type it
        doesn't accept, too large, one file too many, or no storage left."""
        return await self._request(
            "POST",
            f"/api/Prints/{print_id}/files/upload-url",
            json={"fileName": file_name, "contentType": content_type, "sizeBytes": size},
            missing_ok=True,
        )

    async def confirm_attachment(
        self, print_id: int, blob_path: str, file_name: str, content_type: str, size: int
    ) -> dict[str, Any] | None:
        """Attaches the file uploaded to `blob_path` to the print. None when the print is gone."""
        return await self._request(
            "POST",
            f"/api/Prints/{print_id}/files/confirm",
            json={
                "blobPath": blob_path,
                "fileName": file_name,
                "contentType": content_type,
                "sizeBytes": size,
            },
            missing_ok=True,
        )

    async def upload_to_storage(self, url: str, path: Path, content_type: str) -> None:
        """PUTs the file to the storage URL `attachment_upload_url` gave, streamed from disk.

        Storage is another service (Azure Blob Storage), so the API key never goes with it, and
        the URL's signature, which lets anyone write there, never reaches a message. The URL
        must be https, unless the API itself is plain http (a development API)."""
        shown = mask_url(url)
        if urlsplit(url).scheme != "https" and urlsplit(self.base_url).scheme == "https":
            raise ApiRejected(f"the API gave an upload address that isn't https: {shown}")
        size = path.stat().st_size
        timeout = UPLOAD_TIMEOUT_S + size / MIN_UPLOAD_BYTES_PER_S
        try:
            async with aiohttp.ClientSession(
                headers={"User-Agent": f"printlog-bridge/{__version__}"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as session:
                with path.open("rb") as f:
                    async with session.put(
                        url,
                        data=f,
                        allow_redirects=False,  # the file goes only where the API said
                        headers={
                            "x-ms-blob-type": "BlockBlob",
                            "Content-Type": content_type,
                            "Content-Length": str(size),
                        },
                    ) as response:
                        if response.status in (400, 403, 404, 413):
                            raise ApiRejected(
                                f"storage refused the upload ({response.status})",
                                status=response.status,
                            )
                        if response.status >= 300:
                            raise ApiUnavailable(
                                f"storage answered {response.status} to the upload",
                                _retry_after(response.headers.get("Retry-After"), None),
                                status=response.status,
                            )
        except aiohttp.ClientError as e:
            raise ApiUnavailable(f"can't reach storage: {scrub_url(e, url)}") from None
        except asyncio.TimeoutError:
            raise ApiUnavailable(f"storage didn't take the upload within {timeout:g}s") from None

    async def put_connection(self, instance_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Registers the connection, or sends its heartbeat. Each call replaces every field."""
        return await self._request("PUT", f"/api/Connections/{instance_id}", json=body)

    async def printers(self) -> list[dict[str, Any]]:
        """Every active printer of the account (`id`, `name`, `make`, `model`, `slotCount`), all
        pages of `GET /api/Printers/summary`."""
        found: list[dict[str, Any]] = []
        page = 1
        while True:
            body = await self._request(
                "GET",
                "/api/Printers/summary",
                params={"pageNumber": str(page), "pageSize": str(PRINTERS_PAGE_SIZE)},
            )
            items = body.get("items") if isinstance(body, Mapping) else None
            items = [p for p in items or [] if isinstance(p, Mapping)]
            found.extend(items)
            total = (body.get("paging") or {}).get("totalCount") if isinstance(body, Mapping) else 0
            if not items or len(found) >= (total if isinstance(total, int) else 0):
                return found
            page += 1

    async def create_printer(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Adds a printer to the account (`AddPrinterDTO`: `name`, `make` and `model` are
        required). Needs the `printers:create` scope with a device token."""
        _, created = await self._send("POST", "/api/Printers", body)
        return created

    async def check_in(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """The bridge's check-in (`PUT /api/bridges/me`, `bridge:sync`): what it found on its
        network. The answer carries `checkInSeconds`, `desiredPollSeconds`, `desired` and
        `rejected`. Raises ApiRejected (`code` says why: `insufficient_scope`...)."""
        _, answer = await self._send("PUT", "/api/bridges/me", body)
        return answer if isinstance(answer, dict) else {}

    async def desired(self, etag: str | None) -> tuple[int, dict[str, Any] | None, str | None]:
        """`GET /api/bridges/me/desired`: the status (200, 304 unchanged, 404 not checked in
        yet), the desired object on a 200, and its `ETag`."""
        status, body = await self._send(
            "GET",
            "/api/bridges/me/desired",
            missing_ok=True,
            extra_headers={"If-None-Match": etag} if etag else None,
        )
        return status, body if isinstance(body, dict) else None, self.last_etag

    def use_device(self, device: DeviceToken) -> None:
        """Signs in with `device` from now on: the bridge paired again while running."""
        self._device = device
        self._refused = None

    def _credentials(self) -> dict[str, str]:
        """The one credential header to send. Raises RepairNeeded, without asking the API, while
        the refused device token is still the one stored."""
        if self._device is None:
            return {"X-Api-Key": self._api_key} if self._api_key else {}
        if self._refused is not None:
            stored = self._stored_device()
            if stored is None or stored.token == self._refused:
                raise RepairNeeded()
            self._device = stored
            self._refused = None
            if self.on_credentials is not None:
                self.on_credentials(None)
        return {"Authorization": f"Bearer {self._device.token}"}

    def _stored_device(self) -> DeviceToken | None:
        if self._state_dir is None:
            return None
        try:
            stored = read_device_token(self._state_dir)
        except DeviceFileError:
            return None
        if stored is None or stored.api_url != self.base_url:
            return None
        return stored

    def _refuse(self, token: str) -> None:
        if self._refused == token:
            return
        self._refused = token
        if self.on_credentials is not None:
            self.on_credentials(REPAIR_NEEDED)

    async def _get_json(self, path: str) -> Any:
        try:
            return await self._request("GET", path)
        except ApiRejected as e:
            raise AuthRejected(str(e)) from e

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        missing_ok: bool = False,
    ) -> Any:
        status, body = await self._send(method, path, json, params, missing_ok=missing_ok)
        return None if status == 404 else body

    async def _send(
        self,
        method: str,
        path: str,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        *,
        missing_ok: bool = False,
        conflict_ok: bool = False,
        data: aiohttp.FormData | None = None,
        timeout: float = REQUEST_TIMEOUT_S,
        extra_headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Any]:
        if self._session is None:
            raise RuntimeError("use ApiClient as `async with ApiClient(...) as client`")
        headers = {**(extra_headers or {}), **self._credentials()}
        sent = self._device.token if self._device is not None else None

        try:
            async with self._session.request(
                method,
                self.base_url + path,
                json=json,
                params=params,
                data=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                self.server_time = _date(response.headers.get("Date")) or self.server_time
                self.last_etag = response.headers.get("ETag")
                if response.status == 304:
                    return 304, None
                if response.status == 401:
                    code = await _code(response)
                    if sent is not None and code == INVALID_DEVICE_TOKEN:
                        self._refuse(sent)
                        raise RepairNeeded()
                    what = "device token" if sent is not None else "key"
                    raise AuthRejected(f"the API rejected the {what} ({response.status})")
                if response.status == 404 and missing_ok:
                    return 404, None
                if response.status == 409 and conflict_ok:
                    try:
                        return 409, await response.json(content_type=None)
                    except ValueError:
                        return 409, None
                if response.status in (400, 403, 413, 415):
                    reason = (await response.text()).strip()[:300]
                    code = _code_in(reason)
                    if sent is not None and code == INSUFFICIENT_SCOPE:
                        reason += (
                            ". The bridge was paired without this permission: run "
                            f"`{REPAIR_COMMAND}` to pair it again"
                        )
                    raise ApiRejected(
                        f"the API refused {method} {path} ({response.status})"
                        + (f": {reason}" if reason else ""),
                        status=response.status,
                        code=code,
                    )
                if (
                    response.status == 429
                    and sent is not None
                    and await _code(response) == DEVICE_TOKEN_THROTTLED
                ):
                    raise ApiUnavailable(
                        "3D Print Log is limiting device-token requests from this address "
                        "(device_token_throttled); the bridge waits and tries again",
                        _retry_after(response.headers.get("Retry-After"), self.server_time)
                        or THROTTLED_RETRY_S,
                        status=response.status,
                    )
                if response.status >= 400:
                    raise ApiUnavailable(
                        f"the API answered {response.status} for {path}",
                        _retry_after(response.headers.get("Retry-After"), self.server_time),
                        status=response.status,
                    )
                if response.status == 204:
                    return 204, None
                return response.status, await response.json()
        except aiohttp.ClientError as e:
            raise ApiUnavailable(f"can't reach {self.base_url}: {e}") from e
        except asyncio.TimeoutError as e:
            raise ApiUnavailable(f"{self.base_url} did not answer within {timeout:g}s") from e


PRINTERS_PAGE_SIZE = 100


async def _code(response: aiohttp.ClientResponse) -> str | None:
    """The `code` of a problem details answer, or None."""
    try:
        body = await response.json(content_type=None)
    except (ValueError, aiohttp.ClientError):
        return None
    code = body.get("code") if isinstance(body, Mapping) else None
    return code if isinstance(code, str) else None


def _code_in(text: str) -> str | None:
    try:
        body = jsonlib.loads(text)
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, Mapping) else None
    return code if isinstance(code, str) else None


def _retry_after(header: str | None, server_time: datetime | None) -> float | None:
    """`Retry-After` in seconds: either a number of seconds or an HTTP date (RFC 9110)."""
    if not header:
        return None
    header = header.strip()
    if header.isdigit():
        return float(header)
    when = _date(header)
    if when is None:
        return None
    return max(0.0, (when - (server_time or datetime.now(timezone.utc))).total_seconds())


def _date(header: str | None) -> datetime | None:
    if not header:
        return None
    try:
        parsed = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
