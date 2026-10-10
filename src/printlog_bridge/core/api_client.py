"""The 3D Print Log API, as the bridge uses it.

Authenticates with a personal API key (`X-Api-Key`) until pairing (printlog-bridge#2) replaces
it with a device token.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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


@dataclass(frozen=True)
class CurrentUser:
    id: int
    display_name: str | None


class ApiClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

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

    async def _get_json(self, path: str) -> Any:
        if self._session is None:
            raise RuntimeError("use ApiClient as `async with ApiClient(...) as client`")

        try:
            async with self._session.get(self.base_url + path) as response:
                if response.status in (401, 403):
                    raise AuthRejected(f"the API rejected the key ({response.status})")
                if response.status >= 400:
                    raise ApiUnavailable(f"the API answered {response.status} for {path}")
                return await response.json()
        except aiohttp.ClientError as e:
            raise ApiUnavailable(f"can't reach {self.base_url}: {e}") from e
        except asyncio.TimeoutError as e:
            raise ApiUnavailable(
                f"{self.base_url} did not answer within {REQUEST_TIMEOUT_S}s"
            ) from e
