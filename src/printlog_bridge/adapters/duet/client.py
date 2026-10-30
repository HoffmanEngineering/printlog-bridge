"""A Duet's HTTP API: RepRapFirmware on the board itself ("standalone", Duet 2 and 3), or
DuetSoftwareFramework on a Raspberry Pi beside a Duet 3 ("SBC").

**Standalone** (docs.duet3d.com, "RepRapFirmware HTTP requests"):

- `rr_connect?password=<password>` opens a session. Without a password set on the board (M551),
  any is taken, and the bridge sends RRF's default, `reprap`. The answer's `err` is 0 (with
  `sessionTimeout` in ms and, from RRF 3.5, a `sessionKey` the bridge sends as `X-Session-Key`
  on every later request), 1 for a wrong password, 2 when every session is taken. The bridge
  never sends `time` (which sets the board's clock).
- A session lapses after `sessionTimeout` (8 s) without a request, which a poll every 30 s
  between jobs lets happen: the next request answers 401, and the client connects again and
  repeats it, once. 503 is a board too busy to answer (`Busy`).
- `rr_model?key=<key>&flags=d99vn` is one key of the object model in full, and
  `rr_model?flags=d99fn` the fields that change often, with `seqs`, a counter per key that moves
  when anything else in it changes. So each poll is one `rr_model`, plus a full read of each key
  whose counter moved (`boards`, `job`, `move`, `network`, `sbc`, `state`, `tools`; never
  `global`, the user's variables), merged into one model (`model.merge`). An array too long for
  one answer comes with `next`, read again with `a<next>` in the flags.
- `rr_fileinfo?name=<file>` and `rr_thumbnail?name=<file>&offset=<n>` for a job's file
  (`files.py`), and `rr_disconnect` when the bridge stops.

**SBC** (DuetSoftwareFramework's DuetWebServer, `MachineController`): when `rr_connect` is 404,
`/machine/connect?password=` opens a session (`sessionKey`, sent as `X-Session-Key`; 403 for a
wrong password), `/machine/model` (`/machine/status` before DSF 3.4) is the whole object model,
read at each poll, `/machine/fileinfo/<file>?readThumbnailContent=true` a file's facts with its
thumbnails, and `/machine/disconnect` ends it. DSF's websocket (`/machine`, patches pushed) isn't
used: polling the model keeps one code path for both. DSF 3.5 also answers `rr_` requests itself
(`isEmulated` in `rr_connect`'s answer), and then the standalone path is used.

Every request is a GET, and none changes anything on the printer: connecting and disconnecting
are the only ones that aren't reads.

ASSUMPTION throughout: from the documentation and DuetWebControl's `PollConnector` and
`RestConnector`, not a recorded board (#89).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import aiohttp

from printlog_bridge import __version__
from printlog_bridge.adapters.common.timeouts import bounded
from printlog_bridge.core.images import image_type
from printlog_bridge.core.trace import RECEIVE, SEND, Trace

from .files import MAX_THUMBNAIL_TEXT
from .model import merge

log = logging.getLogger(__name__)

DEFAULT_PASSWORD = "reprap"
"""RepRapFirmware's password when none is set (M551): the board then takes any."""
REQUEST_TIMEOUT_S = 10.0
DISCONNECT_TIMEOUT_S = 2.0
KEYS = ("boards", "job", "move", "network", "sbc", "state", "tools")
"""The object model's keys the bridge reads in full when their `seqs` move."""
FULL_FLAGS = "d99vn"
FREQUENT_FLAGS = "d99fn"
MAX_PAGES = 20
STANDALONE, SBC = "standalone", "sbc"
TRACED_KEYS = frozenset({*KEYS, "seqs"}) - {"network"}
"""What a trace keeps of the object model: what the bridge reads. Of `network` only its name
(it also holds the Wi-Fi network's name and the board's addresses), and nothing of `global`."""


class Refused(Exception):
    """The board turned down the password. The message is safe to log."""


class NotDuet(Exception):
    """What answered isn't a Duet."""


class Busy(Exception):
    """The board answered 503, or had no free session: try again later."""


class DuetClient:
    def __init__(
        self,
        base_url: str,
        *,
        password: str | None = None,
        trace: Trace | None = None,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.trace = trace
        self.timeout = timeout
        self.mode: str | None = None
        self.emulated = False
        """DSF answering `rr_` requests for the board."""
        self.session_key: str | None = None
        self.reconnects = 0
        self._http: aiohttp.ClientSession | None = None
        self._model: dict[str, Any] = {}
        self._seqs: dict[str, Any] = {}
        self._lock: asyncio.Lock | None = None
        self._model_path = "/machine/model"

    # -- sessions -------------------------------------------------------------------------

    async def connect(self) -> None:
        """Opens a session, standalone or SBC. Raises Refused, NotDuet, Busy, and what a
        board that can't be reached raises."""
        self.session_key = None
        password = self.password if self.password is not None else DEFAULT_PASSWORD
        status, body = await self._request("/rr_connect", {"password": password}, auth=False)
        if status == 404:
            await self._connect_sbc(password)
            return
        if status == 503:
            raise Busy("the board is busy (HTTP 503)")
        if status != 200 or not isinstance(body, Mapping) or "err" not in body:
            raise NotDuet(f"HTTP {status} from rr_connect")
        err = body.get("err")
        if err == 1:
            raise Refused("the board refused the password")
        if err == 2:
            raise Busy("the board has no free session (too many clients connected)")
        if err != 0:
            raise NotDuet(f"rr_connect answered err {err!r}")
        key = body.get("sessionKey")
        self.session_key = str(key) if isinstance(key, (int, str)) and key != "" else None
        self.emulated = body.get("isEmulated") is True
        self.mode = STANDALONE

    async def _connect_sbc(self, password: str) -> None:
        status, body = await self._request("/machine/connect", {"password": password}, auth=False)
        if status in (401, 403):
            raise Refused("DuetSoftwareFramework refused the password")
        if status == 503:
            raise Busy("DuetSoftwareFramework can't reach the board (HTTP 503)")
        if status == 404:
            raise NotDuet("neither rr_connect nor /machine/connect is there")
        if status != 200:
            raise NotDuet(f"HTTP {status} from /machine/connect")
        key = body.get("sessionKey") if isinstance(body, Mapping) else None
        self.session_key = str(key) if isinstance(key, (int, str)) and key != "" else None
        self.mode = SBC
        self._model = {}

    async def close(self) -> None:
        """Ends the session (bounded: a board that's gone doesn't hold up a stop) and the
        HTTP client."""
        if self.mode is not None and self._http is not None and not self._http.closed:
            path = "/machine/disconnect" if self.mode == SBC else "/rr_disconnect"
            with contextlib.suppress(Exception):
                await bounded(self._request(path, None, auth=True), DISCONNECT_TIMEOUT_S)
        self.mode = None
        self.session_key = None
        self._model, self._seqs = {}, {}
        if self._http is not None:
            await self._http.close()
            self._http = None

    # -- the object model -----------------------------------------------------------------

    async def model(self) -> dict[str, Any]:
        """The object model as it is now: what changed since the last call read again."""
        if self.mode == SBC:
            return await self._sbc_model()
        if not self._model:
            self._model = {}
            for key in KEYS:
                self._model[key] = await self._key(key)
            frequent = await self._frequent()
            self._seqs = dict(frequent.get("seqs") or {})
            merge(self._model, frequent)
            return self._model
        frequent = await self._frequent()
        seqs = frequent.get("seqs")
        seqs = seqs if isinstance(seqs, Mapping) else {}
        for key in KEYS:
            if key in seqs and seqs.get(key) != self._seqs.get(key):
                self._model[key] = await self._key(key)
        self._seqs = dict(seqs)
        merge(self._model, frequent)
        return self._model

    async def _frequent(self) -> Mapping[str, Any]:
        body = await self._get_model("", FREQUENT_FLAGS)
        result = body.get("result")
        if not isinstance(result, Mapping):
            raise NotDuet("rr_model answered no object model")
        return result

    async def _key(self, key: str) -> Any:
        body = await self._get_model(key, FULL_FLAGS)
        result = body.get("result")
        if not isinstance(result, list):
            return result
        items = list(result)
        for _ in range(MAX_PAGES):
            following = body.get("next")
            if not isinstance(following, int) or following <= len(items) - 1 or following == 0:
                break
            body = await self._get_model(key, f"{FULL_FLAGS}a{following}")
            more = body.get("result")
            if not isinstance(more, list) or not more:
                break
            items.extend(more)
        return items

    async def _get_model(self, key: str, flags: str) -> Mapping[str, Any]:
        status, body = await self._get("/rr_model", {"key": key, "flags": flags})
        if status != 200 or not isinstance(body, Mapping):
            raise NotDuet(f"HTTP {status} from rr_model")
        return body

    async def _sbc_model(self) -> dict[str, Any]:
        status, body = await self._get(self._model_path, None)
        if status == 404 and self._model_path == "/machine/model":
            self._model_path = "/machine/status"
            status, body = await self._get(self._model_path, None)
        if status != 200 or not isinstance(body, Mapping):
            raise NotDuet(f"HTTP {status} from {self._model_path}")
        self._model = dict(body)
        return self._model

    # -- files ----------------------------------------------------------------------------

    async def file_info(self, path: str) -> Mapping[str, Any] | None:
        if self.mode == SBC:
            status, body = await self._get(
                "/machine/fileinfo/" + quote(path, safe="/:"), {"readThumbnailContent": "true"}
            )
            return body if status == 200 and isinstance(body, Mapping) else None
        status, body = await self._get("/rr_fileinfo", {"name": path})
        if status != 200 or not isinstance(body, Mapping) or body.get("err") not in (0, None):
            return None
        return body

    async def thumbnail(self, path: str, entry: Mapping[str, Any]) -> bytes | None:
        """The thumbnail `entry` of `file_info` names: its base64 text with the file info
        (SBC), or read a piece at a time (standalone)."""
        if isinstance(entry.get("data"), str):
            return _decoded(entry["data"])
        if self.mode == SBC:
            return None
        offset = entry.get("offset")
        if not isinstance(offset, int):
            return None
        text = ""
        for _ in range(MAX_THUMBNAIL_TEXT // 512):
            status, body = await self._get("/rr_thumbnail", {"name": path, "offset": str(offset)})
            if status != 200 or not isinstance(body, Mapping) or body.get("err") not in (0, None):
                return None
            piece = body.get("data")
            if not isinstance(piece, str):
                return None
            text += piece
            if len(text) > MAX_THUMBNAIL_TEXT:
                return None
            following = body.get("next")
            if not isinstance(following, int) or following <= offset:
                break
            offset = following
        return _decoded(text)

    # -- HTTP -----------------------------------------------------------------------------

    async def _get(self, path: str, params: Mapping[str, str] | None) -> tuple[int, Any]:
        """A request in the session, connecting again once when the session has lapsed."""
        status, body = await self._request(path, params, auth=True)
        if status == 401:
            if self._lock is None:
                self._lock = asyncio.Lock()
            async with self._lock:
                self.reconnects += 1
                log.debug("session lapsed; connecting again")
                await self.connect()
            status, body = await self._request(path, params, auth=True)
        if status == 503:
            raise Busy(f"the board is busy (HTTP 503 from {path})")
        if status == 401:
            raise Refused("the board keeps refusing the bridge's session")
        return status, body

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                headers={"User-Agent": f"printlog-bridge/{__version__}"}
            )
        return self._http

    async def _request(
        self, path: str, params: Mapping[str, str] | None, *, auth: bool
    ) -> tuple[int, Any]:
        headers = {"X-Session-Key": self.session_key} if auth and self.session_key else {}
        if self.trace is not None:
            self.trace.record(SEND, json.dumps({"method": "GET", "path": path,
                                                "params": _traced_params(params)}))  # fmt: skip
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with self._session().get(
            self.base_url + path,
            params=dict(params) if params else None,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        ) as response:
            text = await response.text(errors="replace")
            status = response.status
        body: Any = None
        if text.strip():
            with contextlib.suppress(ValueError):
                body = json.loads(text)
        if self.trace is not None:
            self.trace.record(
                RECEIVE,
                json.dumps({"path": path, "status": status, "body": _traced_body(path, body)}),
            )
        return status, body


def _decoded(text: str) -> bytes | None:
    try:
        data = base64.b64decode(text.strip(), validate=False)
    except (binascii.Error, ValueError):
        return None
    return data if image_type(data) is not None else None


def _traced_params(params: Mapping[str, str] | None) -> dict[str, str] | None:
    """A request's parameters as a trace keeps them: never the password, and the model's `key`
    as `object` (a trace masks anything named like a key)."""
    if not params:
        return None
    kept = {k: v for k, v in params.items() if k != "password"}
    if "key" in kept:
        kept["object"] = kept.pop("key")
    return kept


def _traced_body(path: str, body: Any) -> Any:
    if not isinstance(body, Mapping):
        return body
    if path.endswith("connect"):
        return {k: v for k, v in body.items() if k != "sessionKey"}
    if path == "/rr_model":
        kept = {k: v for k, v in body.items() if k != "key"}
        kept["object"] = body.get("key")
        result = body.get("result")
        if body.get("key") == "":
            kept["result"] = _traced_model(result)
        elif body.get("key") == "network":
            kept["result"] = _network(result)
        return kept
    if path.startswith("/machine/model") or path.startswith("/machine/status"):
        return _traced_model(body)
    if path.startswith("/machine/fileinfo") or path == "/rr_fileinfo":
        thumbnails = body.get("thumbnails")
        if isinstance(thumbnails, list):
            body = dict(body)
            body["thumbnails"] = [
                {k: v for k, v in t.items() if k != "data"} if isinstance(t, Mapping) else t
                for t in thumbnails
            ]
        return body
    if path == "/rr_thumbnail":
        return {k: v for k, v in body.items() if k != "data"}
    return body


def _traced_model(result: Any) -> Any:
    if not isinstance(result, Mapping):
        return result
    kept: dict[str, Any] = {k: v for k, v in result.items() if k in TRACED_KEYS}
    if "network" in result:
        kept["network"] = _network(result["network"])
    return kept


def _network(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    return {"name": value.get("name")} if "name" in value else {}
