"""A stand-in for Auth0's device authorization flow (RFC 8628), as the bridge pairs with it.

`POST /oauth/device/code` (form: `client_id`, `audience`, `scope`) answers a device code, a user
code, `verification_uri` and `verification_uri_complete`, `expires_in` and `interval`. A wrong
client id is 401 `unauthorized_client`, as Auth0 answers.

`POST /oauth/token` (form: `grant_type` = the device code grant, `device_code`, `client_id`)
answers, until the user acts, 403 `authorization_pending`, and 429 `slow_down` for a poll sooner
than the interval (`enforce_interval`). Then 200 with an access token, 403 `access_denied`, or
403 `expired_token`. A test drives it with `approve`, `deny`, `expire`, or `script`: the answers
to give the next polls, in order (`pending`, `slow_down`, `approve`, `deny`, `expire`).

Each access token issued is recorded in `issued` with the claims the 3D Print Log API checks when
pairing (`scope`, `azp`, `jti`, `iat`), so `FakePrintLogApi` can accept exactly these.
"""

from __future__ import annotations

import itertools
import time
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLIENT_ID = "native-client-id"
AUDIENCE = "https://api.3dprintlog.com/device"


class FakeAuth0:
    def __init__(self, *, interval: int = 5, expires_in: int = 900) -> None:
        self.interval = interval
        self.expires_in = expires_in
        self.client_id = CLIENT_ID
        self.audience = AUDIENCE
        self.authorizations: list[dict[str, str]] = []
        """Every device code request's form."""
        self.polls: list[dict[str, str]] = []
        """Every token request's form."""
        self.issued: dict[str, dict[str, Any]] = {}
        """Every access token issued, with its claims."""
        self.script: list[str] = []
        """The answers to the next polls, in order; once used up, `default` decides."""
        self.default = "pending"
        self.enforce_interval = False
        self.offline_access = False
        """Issue tokens that carry `offline_access` (a misconfigured tenant)."""
        self.grant_scope: str | None = None
        """The scope tokens carry; None grants what was asked for."""
        self._codes: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._server: TestServer | None = None
        self.base_url = ""

    async def start(self) -> str:
        app = web.Application()
        app.router.add_post("/oauth/device/code", self._device_code)
        app.router.add_post("/oauth/token", self._token)
        self._server = TestServer(app)
        await self._server.start_server()
        self.base_url = str(self._server.make_url("")).rstrip("/")
        return self.base_url

    async def close(self) -> None:
        if self._server is not None:
            await self._server.close()

    def discovery(self) -> dict[str, Any]:
        """What the API's `GET /api/device-grants/pairing` answers for this tenant."""
        return {
            "issuer": self.base_url + "/",
            "deviceAuthorizationEndpoint": self.base_url + "/oauth/device/code",
            "tokenEndpoint": self.base_url + "/oauth/token",
            "clientId": self.client_id,
            "audience": self.audience,
            "scopes": [
                "connections:write",
                "files:write",
                "filaments:read",
                "printers:create",
                "printers:read",
                "printers:slots",
                "prints:read",
                "prints:write",
            ],
        }

    # -- driving it ------------------------------------------------------------------------------

    def approve(self) -> None:
        self.default = "approve"

    def deny(self) -> None:
        self.default = "deny"

    def expire(self) -> None:
        self.default = "expire"

    @property
    def user_codes(self) -> list[str]:
        return [c["user_code"] for c in self._codes.values()]

    # -- the endpoints ---------------------------------------------------------------------------

    async def _device_code(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        self.authorizations.append(form)
        if form.get("client_id") != self.client_id:
            return _error(401, "unauthorized_client")
        if form.get("audience") != self.audience:
            return _error(403, "access_denied", "Service not found")
        n = next(self._ids)
        device_code = f"device-code-{n}"
        user_code = f"ABCD-{n:04d}"
        self._codes[device_code] = {
            "user_code": user_code,
            "scope": form.get("scope", ""),
            "state": "pending",
            "last_poll": None,
        }
        return web.json_response(
            {
                "device_code": device_code,
                "user_code": user_code,
                "verification_uri": self.base_url + "/activate",
                "verification_uri_complete": f"{self.base_url}/activate?user_code={user_code}",
                "expires_in": self.expires_in,
                "interval": self.interval,
            }
        )

    async def _token(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        self.polls.append(form)
        if form.get("grant_type") != DEVICE_CODE_GRANT or form.get("client_id") != self.client_id:
            return _error(400, "invalid_request")
        code = self._codes.get(form.get("device_code", ""))
        if code is None:
            return _error(403, "invalid_grant")
        if code["state"] in ("denied", "expired", "used"):
            return _error(403, "access_denied" if code["state"] == "denied" else "expired_token")
        answer = self.script.pop(0) if self.script else self.default
        now = time.monotonic()
        too_soon = code["last_poll"] is not None and now - code["last_poll"] < self.interval
        code["last_poll"] = now
        if answer == "slow_down" or (self.enforce_interval and too_soon):
            return _error(429, "slow_down")
        if answer == "pending":
            return _error(403, "authorization_pending")
        if answer == "deny":
            code["state"] = "denied"
            return _error(403, "access_denied", "User denied the request")
        if answer == "expire":
            code["state"] = "expired"
            return _error(403, "expired_token")
        code["state"] = "used"
        n = next(self._ids)
        token = f"auth0-access-token-{n}"
        scope = self.grant_scope if self.grant_scope is not None else code["scope"]
        if self.offline_access:
            scope = f"{scope} offline_access".strip()
        self.issued[token] = {
            "scope": scope,
            "azp": self.client_id,
            "aud": self.audience,
            "jti": f"jti-{n}",
            "iat": time.time(),
        }
        return web.json_response(
            {"access_token": token, "token_type": "Bearer", "expires_in": 600, "scope": scope}
        )


def _error(status: int, error: str, description: str | None = None) -> web.Response:
    body = {"error": error}
    if description:
        body["error_description"] = description
    return web.json_response(body, status=status)
