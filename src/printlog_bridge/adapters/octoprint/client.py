"""OctoPrint's REST API and its push socket, with the API key.

Every request carries the key as `X-Api-Key`. OctoPrint answers 403 (1.5 and later, also 401 in
some versions) for a key it doesn't know, which is `Refused`. The push socket is SockJS's raw
WebSocket endpoint, `/sockjs/websocket`: one JSON message per frame, without SockJS's own
framing. It carries nothing about printing until it is authenticated with `{"auth":
"<user>:<session>"}`, the user's name and a session from `POST /api/login` with `passive` (which
logs nobody in: it only says who the key belongs to).

Apart from that login, the client only reads: GET requests and the socket's messages.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import quote

import aiohttp

from printlog_bridge import __version__
from printlog_bridge.core.trace import RECEIVE, SEND, Trace

from . import webhooks

REQUEST_TIMEOUT_S = 10.0
CHUNK_BYTES = 64 * 1024
SOCKET_PATH = "/sockjs/websocket"
HEARTBEAT_S = 30.0


class Refused(Exception):
    """OctoPrint turned down the API key (401 or 403). The message is safe to log."""


class OctoPrintClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        trace: Trace | None = None,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.trace = trace
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _session_for(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": f"printlog-bridge/{__version__}"}
            )
        return self._session

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key}

    async def get_json(self, path: str) -> tuple[int, Any]:
        """GET `path`: its status and JSON body (None for none). Raises `Refused`, and aiohttp's
        and asyncio's errors for a server that can't be reached."""
        return await self._request("GET", path)

    async def login(self) -> tuple[str, str]:
        """The user the key belongs to and a session for the socket (`POST /api/login` with
        `passive`). Raises `Refused`, or `ValueError` for an answer without them."""
        status, body = await self._request("POST", "/api/login", {"passive": True})
        if status != 200 or not isinstance(body, Mapping):
            raise ValueError(f"HTTP {status} from /api/login")
        name, session = body.get("name"), body.get("session")
        if not isinstance(name, str) or not isinstance(session, str) or not session:
            raise ValueError("/api/login named no user or session")
        return name, session

    async def _request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        session = self._session_for()
        if self.trace is not None:
            self.trace.record(SEND, json.dumps({"method": method, "path": path}))
        request = session.request(
            method,
            self.base_url + path,
            json=body,
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            allow_redirects=False,
        )
        async with request as response:
            text = await response.text(errors="replace")
            answer: Any = None
            if text.strip():
                with contextlib.suppress(ValueError):
                    answer = json.loads(text)
            if self.trace is not None:
                shown = traced(path, answer)
                self.trace.record(
                    RECEIVE, json.dumps({"path": path, "status": response.status, "body": shown})
                )
            if response.status in (401, 403):
                raise Refused(
                    f"OctoPrint refused the bridge's API key (HTTP {response.status} from {path})"
                )
            return response.status, answer

    @contextlib.asynccontextmanager
    async def stream(
        self, path: str, headers: Mapping[str, str] | None = None
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """GET `path` (a download) and yield the response, to read as it arrives. Leaving the
        block early closes the connection."""
        session = self._session_for()
        if self.trace is not None:
            self.trace.record(SEND, json.dumps({"method": "GET", "path": path}))
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=self.timeout, sock_read=30)
        request = session.get(
            self.base_url + path,
            headers={**self._headers, **(headers or {})},
            timeout=timeout,
            allow_redirects=False,
        )
        async with request as response:
            if self.trace is not None:
                self.trace.record(
                    RECEIVE, json.dumps({"path": path, "status": response.status, "body": None})
                )
            if response.status in (401, 403):
                raise Refused(
                    f"OctoPrint refused the bridge's API key (HTTP {response.status} from {path})"
                )
            yield response

    @contextlib.asynccontextmanager
    async def socket(self) -> AsyncIterator[aiohttp.ClientWebSocketResponse]:
        """The push socket, not yet authenticated."""
        url = "ws" + self.base_url[len("http") :] + SOCKET_PATH
        session = self._session_for()
        async with session.ws_connect(
            url,
            heartbeat=HEARTBEAT_S,
            max_msg_size=8 * 1024 * 1024,
        ) as ws:
            yield ws


def file_path(origin: str, path: str) -> str:
    """`/api/files/<origin>/<path>`, with the path's parts quoted."""
    return f"/api/files/{quote(origin, safe='')}/{quote(path)}"


def download_path(origin: str, path: str) -> str:
    """Where OctoPrint serves a local file: `/downloads/files/<origin>/<path>`."""
    return f"/downloads/files/{quote(origin, safe='')}/{quote(path)}"


def traced(path: str, answer: Any) -> Any:
    """What a trace keeps of `path`'s answer. The login's carries the session, so only its
    shape; the settings hold every plugin's settings (a Discord or Slack webhook's token, the
    Webhooks plugin's headers), so only what the bridge reads of them."""
    if path == "/api/login":
        return _without_session(answer)
    if path == "/api/settings":
        return _settings_read(answer)
    return answer


def _settings_read(settings: Any) -> Any:
    if not isinstance(settings, Mapping):
        return None
    webcam = settings.get("webcam") if isinstance(settings.get("webcam"), Mapping) else {}
    cams = []
    for cam in webcam.get("webcams") or ():  # type: ignore[union-attr]
        if isinstance(cam, Mapping):
            compat = cam.get("compat") if isinstance(cam.get("compat"), Mapping) else {}
            cams.append(
                {
                    "name": cam.get("name"),
                    "canSnapshot": cam.get("canSnapshot"),
                    "compat": {"snapshot": compat.get("snapshot")},  # type: ignore[union-attr]
                }
            )
    return {
        "webcam": {
            "webcamEnabled": webcam.get("webcamEnabled"),  # type: ignore[union-attr]
            "snapshotUrl": webcam.get("snapshotUrl"),  # type: ignore[union-attr]
            "webcams": cams,
        },
        "webhooks_to_3dprintlog": webhooks.posts_to_printlog(settings),
    }


def _without_session(answer: Any) -> Any:
    if not isinstance(answer, Mapping):
        return answer
    return {k: ("<session>" if k in ("session", "apikey") else v) for k, v in answer.items()}
