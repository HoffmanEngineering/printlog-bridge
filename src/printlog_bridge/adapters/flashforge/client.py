"""The FlashForge HTTP API on port 8898 (`protocol.py`): one `POST` per request, with the serial
number and the check code in the body. Plain HTTP, as FlashForge's own apps send it.

Every request is bounded by `timeout`; anything not in `READ_ONLY` is refused before it is sent.
A trace keeps each request's path (never its body: it holds the check code) and of each answer
its status, `code`, `message` and, of `/detail`, only `TRACED_DETAIL`: never the printer's
addresses, its serial or its registration codes. `/gcodeThumb`'s image is left out.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
from collections.abc import Mapping
from typing import Any

import aiohttp

from printlog_bridge import __version__
from printlog_bridge.core.images import image_type
from printlog_bridge.core.secrets import scrub
from printlog_bridge.core.trace import RECEIVE, SEND, Trace

from .protocol import DETAIL, GCODE_LIST, GCODE_THUMB, PRODUCT, READ_ONLY

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 10.0
MAX_ANSWER_BYTES = 4 * 1024 * 1024
"""A thumbnail is a few hundred KB of base64; anything far beyond is not an answer."""
TRACED_DETAIL = frozenset(
    {
        "status", "printFileName", "printProgress", "printDuration", "estimatedTime",
        "printLayer", "targetPrintLayer", "estimatedRightWeight", "estimatedRightLen",
        "estimatedLeftWeight", "estimatedLeftLen", "errorCode", "firmwareVersion", "model",
        "pid", "nozzleModel", "hasMatlStation", "matlStationInfo", "indepMatlInfo",
        "rightFilamentType", "leftFilamentType", "hasRightFilament", "hasLeftFilament",
    }
)  # fmt: skip
"""What a trace keeps of `/detail`: what the bridge reads, and what a trace from a real printer
should show of the job. Never `ipAddr`, `macAddr`, `name`, `location`, `cameraStreamUrl` or the
registration codes."""


class Refused(Exception):
    """The printer turned the serial number or check code down. The message is safe to log."""


class NotFlashForge(Exception):
    """What answered isn't the FlashForge HTTP API (older firmware has none: a 404)."""


class FlashForgeClient:
    def __init__(
        self,
        base_url: str,
        *,
        serial: str,
        check_code: str,
        trace: Trace | None = None,
        timeout: float = REQUEST_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.serial = serial
        self.check_code = check_code
        self.trace = trace
        self.timeout = timeout
        self._http: aiohttp.ClientSession | None = None

    async def detail(self) -> Mapping[str, Any]:
        """`/detail`'s `detail`: the whole status."""
        answer = await self.post(DETAIL)
        detail = answer.get("detail")
        if not isinstance(detail, Mapping):
            raise NotFlashForge("no detail in /detail's answer")
        return detail

    async def product(self) -> Mapping[str, Any]:
        answer = await self.post(PRODUCT)
        product = answer.get("product")
        return product if isinstance(product, Mapping) else {}

    async def gcode_list(self) -> Mapping[str, Any]:
        return await self.post(GCODE_LIST)

    async def thumbnail(self, file_name: str) -> bytes | None:
        """The file's thumbnail from `/gcodeThumb`, or None when it has none."""
        answer = await self.post(GCODE_THUMB, {"fileName": file_name})
        data = answer.get("imageData")
        if not isinstance(data, str) or not data.strip():
            return None
        try:
            image = base64.b64decode(data.strip(), validate=False)
        except (binascii.Error, ValueError):
            return None
        return image if image_type(image) is not None else None

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None

    def safe_error(self, error: BaseException) -> str:
        """`error` as text safe to log, without the check code."""
        text = scrub(str(error), [self.check_code] if self.check_code else [])
        name = type(error).__name__
        return f"{name}: {text}" if text else name

    async def post(self, path: str, extra: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        """One read-only request. Raises Refused, NotFlashForge, and what a printer that can't
        be reached raises (aiohttp.ClientError, OSError, asyncio.TimeoutError)."""
        if path not in READ_ONLY:
            raise ValueError(f"{path} isn't a read-only request")
        body = {"serialNumber": self.serial, "checkCode": self.check_code, **(extra or {})}
        if self.trace is not None:
            traced = {k: v for k, v in (extra or {}).items()}
            self.trace.record(SEND, json.dumps({"method": "POST", "path": path, **traced}))
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                headers={"User-Agent": f"printlog-bridge/{__version__}"}
            )
        async with self._http.post(
            self.base_url + path,
            json=body,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            allow_redirects=False,
        ) as response:
            status = response.status
            raw = await response.content.read(MAX_ANSWER_BYTES + 1)
        answer: Any = None
        if len(raw) <= MAX_ANSWER_BYTES and raw.strip():
            with contextlib.suppress(ValueError):
                answer = json.loads(raw.decode("utf-8", errors="replace"))
        if self.trace is not None:
            self.trace.record(
                RECEIVE, json.dumps({"path": path, "status": status, "body": _traced(answer)})
            )
        if status == 404:
            raise NotFlashForge(f"HTTP 404 from {path}")
        if not isinstance(answer, Mapping) or "code" not in answer:
            raise NotFlashForge(f"HTTP {status} from {path}, not the FlashForge API's answer")
        if answer.get("code") != 0:
            message = answer.get("message")
            said = f" ({message.strip()[:80]})" if isinstance(message, str) and message else ""
            text = scrub(f"code {answer.get('code')}{said}", [self.check_code, self.serial])
            raise Refused(f"the printer refused the serial number or check code: {text}")
        return answer


def _traced(answer: Any) -> Any:
    if not isinstance(answer, Mapping):
        return answer
    kept: dict[str, Any] = {k: answer[k] for k in ("code", "message") if k in answer}
    detail = answer.get("detail")
    if isinstance(detail, Mapping):
        kept["detail"] = {k: v for k, v in detail.items() if k in TRACED_DETAIL}
    if isinstance(answer.get("product"), Mapping):
        kept["product"] = answer["product"]
    if "gcodeListDetail" in answer:
        kept["gcodeListDetail"] = answer["gcodeListDetail"]
    if "gcodeList" in answer:
        kept["gcodeList"] = answer["gcodeList"]
    if isinstance(answer.get("imageData"), str):
        kept["imageData"] = f"<{len(answer['imageData'])} characters>"
    return kept
