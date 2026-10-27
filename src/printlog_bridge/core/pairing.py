"""Pairing the bridge with a 3D Print Log account (#2, #101): no API key to copy by hand.

The contract is 3d-print-log-api#286's:

1. `GET /api/device-grants/pairing` says which Auth0 tenant and app to use, or 503
   `device_pairing_unavailable` when the server has pairing off (then `store-key` it is).
2. The bridge runs Auth0's device flow (RFC 8628) itself: it asks for a device code, shows the
   user code, the address and a QR code of `verification_uri_complete`, and polls the token
   endpoint at `interval` (5 s more after each `slow_down`) until the user approves it, denies
   it, or the code expires. It never asks for `offline_access`, so there is no refresh token.
3. It trades the Auth0 access token, once and at once (the API takes it for 10 minutes), at
   `POST /api/device-grants` for a device token (`pld_...`), sending its `deviceId` (one per
   install, core/device.py) and a name. Pairing again with the same `deviceId` rotates the token
   on the same grant. The Auth0 token is never stored or logged.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from printlog_bridge import __version__
from printlog_bridge.core.device import TOKEN_PREFIX, DeviceToken, device_id, write_device_token
from printlog_bridge.core.qr import render

log = logging.getLogger(__name__)

PAIRING_PATH = "/api/device-grants/pairing"
GRANTS_PATH = "/api/device-grants"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
BRIDGE_SCOPES = (
    "bridge:sync",
    "connections:write",
    "files:write",
    "filaments:read",
    "printers:read",
    "printers:slots",
    "prints:read",
    "prints:write",
)
"""What the bridge calls the API for (api#286's scope table). `files:write` is asked for even
without Pro, which the bridge can't know before it pairs: the API still checks Pro, and a user
who upgrades later doesn't have to pair again. `bridge:sync` (check-in, the printers connected in
the app, api#288) is asked for only when the server lists it, like every scope."""
SYNC_SCOPE = "bridge:sync"
CREATE_SCOPE = "printers:create"
"""Asked for only when `pair` may offer to create a printer."""
SLOW_DOWN_S = 5
DEFAULT_INTERVAL_S = 5
REQUEST_TIMEOUT_S = 15
NAME_LENGTH = 100
NAME_ENV = "PRINTLOG_BRIDGE_NAME"
"""The name the bridge pairs under, when `--name` isn't given (Docker: the container's host name
is a random id)."""

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class PairingError(Exception):
    """Pairing didn't work. The message says why, and what to do, in words for the user."""

    again = False
    """Whether starting the device flow over (a new code for the user to approve) may work."""


class PairingUnavailable(PairingError):
    """The server has pairing off (503 `device_pairing_unavailable`)."""


class PairingDenied(PairingError):
    again = True


class PairingExpired(PairingError):
    again = True


class ExchangeRefused(PairingError):
    def __init__(self, message: str, code: str | None = None, again: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.again = again


@dataclass(frozen=True)
class PairingConfig:
    issuer: str
    device_authorization_endpoint: str
    token_endpoint: str
    client_id: str
    audience: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: float
    interval: float

    def __repr__(self) -> str:  # never the device code, which is what polls for the token
        return f"DeviceAuthorization({self.user_code!r}, {self.verification_uri!r})"


@dataclass(frozen=True)
class PendingCode:
    """What the user needs to approve the bridge, for the local page (core/local_page.py). Never
    the device code, which is what polls for the token."""

    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_at: float
    """When the code stops working, on the PendingCodes clock (time.monotonic)."""


class PendingCodes:
    """The code this process's pairing is waiting on, if any. `pair` keeps it here while it
    waits; the local page reads it from its own thread."""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._code: PendingCode | None = None

    def show(self, authorization: DeviceAuthorization) -> None:
        code = PendingCode(
            authorization.user_code,
            authorization.verification_uri,
            authorization.verification_uri_complete,
            self.clock() + authorization.expires_in,
        )
        with self._lock:
            self._code = code

    def clear(self) -> None:
        with self._lock:
            self._code = None

    def get(self) -> PendingCode | None:
        """The code waiting for approval, or None (none, or it expired)."""
        with self._lock:
            code = self._code
        if code is None or self.clock() >= code.expires_at:
            return None
        return code


pending = PendingCodes()


def default_name(env: Mapping[str, str]) -> str:
    return (env.get(NAME_ENV) or "").strip() or f"printlog-bridge on {socket.gethostname()}"


def clean_name(name: str) -> str:
    """A name the API takes: 1 to 100 characters, no control characters."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:NAME_LENGTH].strip()
    return cleaned or "printlog-bridge"


def scopes_to_request(offered: Sequence[str], *, create: bool) -> list[str]:
    """The bridge's scopes that the server offers, and `printers:create` when it may create a
    printer. Never `offline_access`."""
    wanted = [*BRIDGE_SCOPES, CREATE_SCOPE] if create else list(BRIDGE_SCOPES)
    return [s for s in wanted if s in offered]


def instructions(authorization: DeviceAuthorization, stream: Any = None) -> str:
    """What the user reads (and scans) to approve the bridge."""
    minutes = max(1, round(authorization.expires_in / 60))
    complete = authorization.verification_uri_complete
    lines = [
        "To pair this bridge with your 3D Print Log account, scan the QR code with your phone, "
        "or open",
        f"  {authorization.verification_uri}",
        f"and enter the code  {authorization.user_code}",
    ]
    if complete:
        lines.append(f"(or open {complete})")
    lines.append(f"The code works for {minutes} minute{'' if minutes == 1 else 's'}.")
    lines.append("")
    lines.append(render(complete or authorization.verification_uri, stream=stream))
    return "\n".join(lines)


async def discover(session: aiohttp.ClientSession, api_url: str) -> PairingConfig:
    """The server's pairing parameters. Raises PairingUnavailable when it has pairing off, and
    PairingError when it can't be asked or its answer makes no sense."""
    url = api_url.rstrip("/") + PAIRING_PATH
    try:
        async with session.get(url) as response:
            if response.status in (404, 503):
                raise PairingUnavailable(
                    f"{api_url} doesn't offer pairing yet. Store an API key instead: create one "
                    "at https://www.3dprintlog.com/api-keys, then run `printlog-bridge store-key`."
                )
            if response.status != 200:
                raise PairingError(f"{api_url} answered {response.status} when asked how to pair")
            body = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise PairingError(f"can't reach {api_url} to pair: {e or type(e).__name__}") from None
    except ValueError:
        raise PairingError(f"{api_url} gave an answer about pairing that isn't JSON") from None
    try:
        config = PairingConfig(
            issuer=str(body["issuer"]),
            device_authorization_endpoint=str(body["deviceAuthorizationEndpoint"]),
            token_endpoint=str(body["tokenEndpoint"]),
            client_id=str(body["clientId"]),
            audience=str(body["audience"]),
            scopes=tuple(str(s) for s in body.get("scopes") or ()),
        )
    except (KeyError, TypeError, AttributeError):
        raise PairingError(
            f"{api_url} gave pairing parameters the bridge doesn't understand"
        ) from None
    for endpoint in (config.device_authorization_endpoint, config.token_endpoint):
        if not _secure(endpoint):
            raise PairingError(f"{api_url} named a sign-in address that isn't https: {endpoint}")
    return config


async def offered_scopes(api_url: str) -> tuple[str, ...]:
    """The scopes the server's pairing discovery lists. Raises PairingError."""
    async with aiohttp.ClientSession(
        headers={"User-Agent": f"printlog-bridge/{__version__}", "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
    ) as session:
        return (await discover(session, api_url)).scopes


async def start(
    session: aiohttp.ClientSession, config: PairingConfig, scopes: Sequence[str]
) -> DeviceAuthorization:
    """Asks Auth0 for a device code and the user code to show."""
    form = {"client_id": config.client_id, "audience": config.audience, "scope": " ".join(scopes)}
    status, body = await _post_form(session, config.device_authorization_endpoint, form)
    if status != 200:
        raise PairingError(f"the sign-in service refused to start pairing: {_oauth_error(body)}")
    try:
        return DeviceAuthorization(
            device_code=str(body["device_code"]),
            user_code=str(body["user_code"]),
            verification_uri=str(body["verification_uri"]),
            verification_uri_complete=body.get("verification_uri_complete") or None,
            expires_in=float(body.get("expires_in") or 900),
            interval=float(body.get("interval") or DEFAULT_INTERVAL_S),
        )
    except (KeyError, TypeError, ValueError):
        raise PairingError(
            "the sign-in service gave an answer the bridge doesn't understand"
        ) from None


async def wait_for_approval(
    session: aiohttp.ClientSession,
    config: PairingConfig,
    authorization: DeviceAuthorization,
    *,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> str:
    """Polls until the user approves, and returns Auth0's access token. Raises PairingDenied,
    PairingExpired, or PairingError for anything else the sign-in service says."""
    deadline = clock() + authorization.expires_in
    interval = authorization.interval
    form = {
        "grant_type": DEVICE_CODE_GRANT,
        "device_code": authorization.device_code,
        "client_id": config.client_id,
    }
    while True:
        await sleep(interval)
        if clock() >= deadline:
            raise PairingExpired("the code expired before it was approved")
        try:
            status, body = await _post_form(session, config.token_endpoint, form)
        except PairingError as e:  # the network, for a moment: keep polling until the deadline
            log.debug("polling for the approval failed: %s", e)
            continue
        if status == 200 and isinstance(body, Mapping) and body.get("access_token"):
            return str(body["access_token"])
        error = body.get("error") if isinstance(body, Mapping) else None
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += SLOW_DOWN_S
            continue
        if error == "access_denied":
            raise PairingDenied("the request was denied on the approval page")
        if error == "expired_token":
            raise PairingExpired("the code expired before it was approved")
        raise PairingError(f"the sign-in service refused the approval: {_oauth_error(body)}")


async def exchange(
    session: aiohttp.ClientSession, api_url: str, access_token: str, device: str, name: str
) -> tuple[Mapping[str, Any], bool]:
    """Trades the approval for the bridge's device token: the API's answer, and whether it
    rotated the token of this device's grant (a re-pair, 200) rather than adding one (201).
    Raises ExchangeRefused."""
    url = api_url.rstrip("/") + GRANTS_PATH
    try:
        async with session.post(
            url,
            json={"deviceId": device, "name": name},
            headers={"Authorization": f"Bearer {access_token}"},
        ) as response:
            status = response.status
            try:
                body = await response.json(content_type=None)
            except ValueError:
                body = None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise ExchangeRefused(
            f"couldn't reach {api_url} to finish pairing ({e or type(e).__name__}). The approval "
            "can be used only once: pair again.",
            again=True,
        ) from None
    code = body.get("code") if isinstance(body, Mapping) else None
    if status in (200, 201) and isinstance(body, Mapping):
        token = body.get("token")
        if isinstance(token, str) and token.startswith(TOKEN_PREFIX):
            return body, status == 200
        raise ExchangeRefused(f"{api_url} answered the pairing without a device token")
    raise _refusal(status, code)


def _refusal(status: int, code: str | None) -> ExchangeRefused:
    if code == "device_grant_limit":
        return ExchangeRefused(
            "this account already has the most paired devices it can have. Remove one you no "
            "longer use in 3D Print Log (Settings, Connected agents), then pair again.",
            code,
        )
    if code == "device_grant_revoked":
        return ExchangeRefused(
            "this bridge was removed in 3D Print Log while it was pairing again, so the approval "
            "no longer counts. Approve it once more.",
            code,
            again=True,
        )
    if code == "pairing_token_used":
        return ExchangeRefused(
            "that approval was already used. Approve the bridge again.", code, True
        )
    if code == "insufficient_scope":
        return ExchangeRefused(
            "the approval gave the bridge none of the permissions it needs. Pair again and allow "
            "them on the approval page.",
            code,
        )
    if code == "offline_access_not_allowed":
        return ExchangeRefused(
            "3D Print Log refused the approval because it came with offline access, which the "
            "sign-in service shouldn't grant. Report this to 3D Print Log support.",
            code,
        )
    if status == 401:
        return ExchangeRefused(
            "3D Print Log didn't accept the approval. Check that this computer's clock is right, "
            "then pair again.",
            code,
            again=True,
        )
    if status == 403:
        return ExchangeRefused(
            "3D Print Log refused the approval: it took too long (10 minutes at most) or came "
            "from another app. Pair again.",
            code,
            again=True,
        )
    if status == 400:
        return ExchangeRefused(f"3D Print Log refused the pairing request ({code or status})", code)
    return ExchangeRefused(
        f"3D Print Log answered {status} to the pairing. The approval can be used only once: pair "
        "again.",
        code,
        again=True,
    )


async def pair(
    api_url: str,
    state_dir: Path,
    *,
    name: str,
    create: bool,
    show: Callable[[DeviceAuthorization], None],
    say: Callable[[str], None],
    keep_trying: bool = False,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> tuple[DeviceToken, bool]:
    """Runs the whole pairing and stores the device token. Returns it, and whether this paired
    the bridge again (the API rotated its grant's token).

    `show` gets each code to approve; `say` what happened to it. With `keep_trying` (the first
    run of an unattended bridge), a code that expires or is denied is followed by a new one, for
    as long as it takes; otherwise the PairingError is raised."""
    async with aiohttp.ClientSession(
        headers={"User-Agent": f"printlog-bridge/{__version__}", "Accept": "application/json"},
        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
    ) as session:
        config = await discover(session, api_url)
        scopes = scopes_to_request(config.scopes, create=create)
        if not scopes:
            raise PairingError(f"{api_url} offers none of the permissions the bridge needs")
        device = device_id(state_dir)
        try:
            while True:
                try:
                    authorization = await start(session, config, scopes)
                    pending.show(authorization)
                    show(authorization)
                    access_token = await wait_for_approval(
                        session, config, authorization, sleep=sleep, clock=clock
                    )
                    pending.clear()
                    grant, repaired = await exchange(
                        session, api_url, access_token, device, clean_name(name)
                    )
                except PairingError as e:
                    if not (keep_trying and e.again):
                        raise
                    say(f"Pairing didn't finish: {e}. Here is a new code.")
                    continue
                break
        finally:
            pending.clear()
    stored = write_device_token(
        state_dir,
        token=str(grant["token"]),
        api_url=api_url,
        grant_id=grant.get("id") if isinstance(grant.get("id"), int) else None,
        name=str(grant.get("name") or name),
        scopes=[str(s) for s in grant.get("scopes") or ()],
    )
    return stored, repaired


async def _post_form(
    session: aiohttp.ClientSession, url: str, form: Mapping[str, str]
) -> tuple[int, Any]:
    try:
        async with session.post(url, data=dict(form)) as response:
            try:
                return response.status, await response.json(content_type=None)
            except ValueError:
                return response.status, None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise PairingError(f"can't reach the sign-in service: {e or type(e).__name__}") from None


def _oauth_error(body: Any) -> str:
    if not isinstance(body, Mapping):
        return "no reason given"
    error = body.get("error") or "no reason given"
    description = body.get("error_description")
    return f"{error} ({description})" if description else str(error)


def _secure(url: str) -> bool:
    """https, or http to this computer itself (a test tenant)."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return True
    if parts.scheme != "http" or not parts.hostname:
        return False
    if parts.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        return False
