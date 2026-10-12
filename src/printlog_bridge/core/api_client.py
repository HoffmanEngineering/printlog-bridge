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


class ApiError(Exception):
    pass


class AuthRejected(ApiError):
    """The API refused the credentials. Retrying will not help; the user has to act."""


class ApiUnavailable(ApiError):
    """The API could not be reached or failed. Worth retrying later."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        """Seconds the API asked the bridge to wait (`Retry-After`), when it said."""


class ApiRejected(ApiError):
    """The API refused this request (400 or 403). Sending it again won't change the answer."""


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

    async def create_print(self, body: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """The print, and whether it is new. An `externalId` already used returns the original."""
        status, print_ = await self._send("POST", "/api/Prints", body)
        return print_, status == 201

    async def get_print(self, print_id: int) -> dict[str, Any] | None:
        return await self._request("GET", f"/api/Prints/{print_id}", missing_ok=True)

    async def complete_print(self, print_id: int, body: Mapping[str, Any]) -> dict[str, Any] | None:
        return await self._request(
            "POST", f"/api/Prints/{print_id}/complete", json=body, missing_ok=True
        )

    async def patch_print(self, print_id: int, body: Mapping[str, Any]) -> dict[str, Any] | None:
        return await self._request("PATCH", f"/api/Prints/{print_id}", json=body, missing_ok=True)

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
    ) -> tuple[int, Any]:
        if self._session is None:
            raise RuntimeError("use ApiClient as `async with ApiClient(...) as client`")

        try:
            async with self._session.request(
                method, self.base_url + path, json=json, params=params
            ) as response:
                self.server_time = _date(response.headers.get("Date")) or self.server_time
                if response.status == 401:
                    raise AuthRejected(f"the API rejected the key ({response.status})")
                if response.status == 404 and missing_ok:
                    return 404, None
                if response.status in (400, 403):
                    reason = (await response.text()).strip()[:300]
                    raise ApiRejected(
                        f"the API refused {method} {path} ({response.status})"
                        + (f": {reason}" if reason else "")
                    )
                if response.status >= 400:
                    raise ApiUnavailable(
                        f"the API answered {response.status} for {path}",
                        _retry_after(response.headers.get("Retry-After"), self.server_time),
                    )
                return response.status, await response.json()
        except aiohttp.ClientError as e:
            raise ApiUnavailable(f"can't reach {self.base_url}: {e}") from e
        except asyncio.TimeoutError as e:
            raise ApiUnavailable(
                f"{self.base_url} did not answer within {REQUEST_TIMEOUT_S}s"
            ) from e


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
