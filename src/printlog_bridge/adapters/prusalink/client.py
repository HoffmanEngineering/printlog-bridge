"""PrusaLink's local HTTP API, with its authentication.

Prusa-Firmware-Buddy (MK4/MK4S, MK3.5/3.9, Core One, MINI, XL) asks for HTTP Digest with the
user `maker` and the password the printer shows (`nhttp/req_parser.cpp`): realm `Printer API`, no
`qop`, so the answer is MD5(HA1:nonce:HA2) as in RFC 2069, and a nonce stays good for 300 s,
after which the printer answers `stale=true`. It also takes the same password as `X-Api-Key`,
and older firmware asks for that alone (`WWW-Authenticate: ApiKey`). PrusaLink on a Raspberry
Pi (MK3S) uses Digest too, and may offer `qop=auth`, which is answered when offered.

aiohttp has Digest only from 3.12 (the bridge supports 3.8), so it's done here: the nonce is
kept and reused, so a poll is one request rather than two, until the printer calls it stale.

The client only ever sends GET requests: it never writes to the printer.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

import aiohttp

from printlog_bridge import __version__
from printlog_bridge.core.trace import RECEIVE, SEND, Trace

DEFAULT_USER = "maker"
REQUEST_TIMEOUT_S = 10.0
CHUNK_BYTES = 64 * 1024
_CHALLENGE_FIELD = re.compile(r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')


class Refused(Exception):
    """The printer turned down the credentials (401 or 403). The message is safe to log."""


class PrusaLinkClient:
    def __init__(
        self,
        base_url: str,
        *,
        username: str = DEFAULT_USER,
        password: str | None = None,
        api_key: str | None = None,
        trace: Trace | None = None,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.api_key = api_key
        self.trace = trace
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._challenge: dict[str, str] | None = None
        self._nc = 0
        self._key_for_password = False
        """The printer asked for X-Api-Key: the password is sent as the key."""

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_json(self, path: str) -> tuple[int, Any]:
        """GET `path`: its status and its JSON body (None for no body or not JSON). Raises
        `Refused` for credentials the printer turns down, and aiohttp's and asyncio's errors for
        a printer that can't be reached."""
        async with self._get(path, aiohttp.ClientTimeout(total=self.timeout)) as response:
            text = await response.text(errors="replace")
            body: Any = None
            if text.strip():
                with contextlib.suppress(ValueError):
                    body = json.loads(text)
            if self.trace is not None:
                self.trace.record(
                    RECEIVE,
                    json.dumps({"path": path, "status": response.status, "body": body}),
                )
            return response.status, body

    @contextlib.asynccontextmanager
    async def stream(self, path: str) -> AsyncIterator[AsyncIterator[bytes]]:
        """GET `path` and yield its body's chunks as they arrive. Leaving the block early closes
        the connection, so the printer stops sending. Raises `FileNotFoundError` for anything
        but 200."""
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=self.timeout, sock_read=30)
        async with self._get(path, timeout) as response:
            if self.trace is not None:
                self.trace.record(
                    RECEIVE, json.dumps({"path": path, "status": response.status, "body": None})
                )
            if response.status != 200:
                raise FileNotFoundError(f"{path}: HTTP {response.status}")
            try:
                yield response.content.iter_chunked(CHUNK_BYTES)
            finally:
                response.close()

    @contextlib.asynccontextmanager
    async def _get(self, path: str, timeout: aiohttp.ClientTimeout):
        session = self._session_for()
        url = self.base_url + path
        for _attempt in range(3):
            if self.trace is not None:
                self.trace.record(SEND, json.dumps({"method": "GET", "path": path}))
            response = await session.get(
                url, headers=self._headers(path), timeout=timeout, allow_redirects=False
            )
            if response.status not in (401, 403):
                try:
                    yield response
                finally:
                    response.release()
                return
            challenge = response.headers.get("WWW-Authenticate", "")
            response.release()
            if self.trace is not None:  # the challenge, to see what a real printer asks for
                self.trace.record(
                    RECEIVE,
                    json.dumps({"path": path, "status": response.status, "challenge": challenge}),
                )
            if not self._answer(response.status, challenge):
                break
        raise Refused(self._refusal())

    def _session_for(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": f"printlog-bridge/{__version__}"}
            )
        return self._session

    def _headers(self, path: str) -> dict[str, str]:
        if self.api_key:
            return {"X-Api-Key": self.api_key}
        if self._key_for_password and self.password:
            return {"X-Api-Key": self.password}
        if self._challenge is not None and self.password is not None:
            self._nc += 1
            header = digest_header(
                "GET",
                path,
                self.username,
                self.password,
                self._challenge,
                nc=self._nc,
                cnonce=os.urandom(8).hex(),
            )
            return {"Authorization": header}
        return {}

    def _answer(self, status: int, challenge: str) -> bool:
        """Whether a 401's challenge is worth answering: a first or stale Digest nonce, or a
        printer asking for the key when only the password was given."""
        if status != 401 or self.api_key:
            return False
        scheme, _, rest = challenge.strip().partition(" ")
        if scheme.lower() == "digest" and self.password is not None:
            fields = parse_challenge(rest)
            algorithm = fields.get("algorithm", "MD5").upper()
            if algorithm != "MD5" or "nonce" not in fields:
                return False
            fresh = self._challenge is None or fields.get("stale", "").lower() == "true"
            if not fresh:
                return False  # the answer to this nonce was wrong: the password is
            self._challenge = fields
            self._nc = 0
            return True
        if scheme.lower() == "apikey" and self.password and not self._key_for_password:
            self._key_for_password = True
            return True
        return False

    def _refusal(self) -> str:
        self._challenge = None
        if self.api_key:
            return "the printer refused the api_key"
        return f"the printer refused the user {self.username!r} and its password"


def parse_challenge(text: str) -> dict[str, str]:
    """The fields of a `WWW-Authenticate: Digest ...` challenge."""
    return {
        m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
        for m in _CHALLENGE_FIELD.finditer(text)
    }


def digest_header(
    method: str,
    uri: str,
    username: str,
    password: str,
    challenge: Mapping[str, str],
    *,
    nc: int,
    cnonce: str,
) -> str:
    """The `Authorization` header answering a Digest challenge (MD5): with `qop=auth` when the
    server offers it (RFC 7616), else as RFC 2069 has it, which is what Buddy firmware expects."""

    def md5(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    realm, nonce = challenge.get("realm", ""), challenge.get("nonce", "")
    ha1 = md5(f"{username}:{realm}:{password}")
    ha2 = md5(f"{method}:{uri}")
    qops = [q.strip() for q in challenge.get("qop", "").split(",")]
    fields = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
    ]
    if "auth" in qops:
        count = f"{nc:08x}"
        response = md5(f"{ha1}:{nonce}:{count}:{cnonce}:auth:{ha2}")
        fields += ["qop=auth", f"nc={count}", f'cnonce="{cnonce}"']
    else:
        response = md5(f"{ha1}:{nonce}:{ha2}")
    fields.append(f'response="{response}"')
    if "opaque" in challenge:
        fields.append(f'opaque="{challenge["opaque"]}"')
    if "algorithm" in challenge:
        fields.append("algorithm=MD5")
    return "Digest " + ", ".join(fields)
