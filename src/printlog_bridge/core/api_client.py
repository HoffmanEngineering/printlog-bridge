"""The 3D Print Log API, as the bridge uses it.

Authenticates with a personal API key (`X-Api-Key`) until pairing (printlog-bridge#2) replaces
it with a device token.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any

import aiohttp

from printlog_bridge import __version__

REQUEST_TIMEOUT_S = 15
UPLOAD_TIMEOUT_S = 30
"""An image upload carries up to 10 MB, and slicer settings up to 576 KB, so they get longer
than other requests."""


class ApiError(Exception):
    pass


class AuthRejected(ApiError):
    """The API refused the credentials. Retrying will not help; the user has to act."""


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

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        """The HTTP status the API refused it with."""


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
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        self.server_time: datetime | None = None
        """The API's clock, from the `Date` of its last answer, to tell when ours is off."""

    def __repr__(self) -> str:
        return f"ApiClient({self.base_url!r})"

    async def __aenter__(self) -> ApiClient:
        self._session = aiohttp.ClientSession(
            headers={
                "X-Api-Key": self._api_key,
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

    async def put_connection(self, instance_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Registers the connection, or sends its heartbeat. Each call replaces every field."""
        return await self._request("PUT", f"/api/Connections/{instance_id}", json=body)

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
    ) -> tuple[int, Any]:
        if self._session is None:
            raise RuntimeError("use ApiClient as `async with ApiClient(...) as client`")

        try:
            async with self._session.request(
                method,
                self.base_url + path,
                json=json,
                params=params,
                data=data,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                self.server_time = _date(response.headers.get("Date")) or self.server_time
                if response.status == 401:
                    raise AuthRejected(f"the API rejected the key ({response.status})")
                if response.status == 404 and missing_ok:
                    return 404, None
                if response.status == 409 and conflict_ok:
                    try:
                        return 409, await response.json(content_type=None)
                    except ValueError:
                        return 409, None
                if response.status in (400, 403, 413, 415):
                    reason = (await response.text()).strip()[:300]
                    raise ApiRejected(
                        f"the API refused {method} {path} ({response.status})"
                        + (f": {reason}" if reason else ""),
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
