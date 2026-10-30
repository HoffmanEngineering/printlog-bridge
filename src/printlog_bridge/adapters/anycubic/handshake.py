"""The Anycubic LAN-mode handshake: `GET /info`, then a signed `POST /ctrl` whose answer, once
decrypted, holds the MQTT broker's address and this connection's user and password.

Checked against a Kobra S1 and a Photon P1 Max (October 2026, `tests/fixtures/anycubic/`), and
described by rvanderp3/kobra-connect (Apache-2.0, `handshake.py`), Nino6689/anycubic-lan (MIT,
`docs/PROTOCOL.md`) and modrzew/hass-anycubic-photon-p1 (MIT, `docs/protocol.md`). No code of
theirs is used.

1. `GET http://<host>:18910/info`, no authentication: the printer's model (`modelId`, `modelName`,
   `deviceType` `fdm` or `lcd`), its serial (`cn`), whether LAN mode is on (`ctrlType`: `lan` on
   the S1, `net` on the P1 Max; `cloud` when it's off) and a `token` of 32 characters that changes
   on every request.
2. `POST http://<host>:18910/ctrl?ts=<ms>&nonce=<n>&sign=<s>&did=<d>` with no body, where
   `sign = md5(md5(token[:16]) + ts + nonce)` in lowercase hex and `did` names the client (any
   text). The answer is `{"code": 200, "data": {"info": <base64>, "token": <16 characters>}}`.
3. `info` is AES-128-CBC with `token[16:]` as the key and the answer's `token` as the IV: JSON
   with `broker` (`mqtts://<ip>:9883` on the S1, `<ip>:8883` on the P1 Max), `username`,
   `password`, `deviceId` (the MQTT topics name it) and, on the P1, `clientId`, which the broker
   insists on. It may also carry a client certificate and key (`devicecrt`, `devicepk`) or a CA
   (`cacrt`); the bridge doesn't use them, as the broker doesn't ask for one.

Every value here but the model is a secret: the token, the answer and what it opens never reach a
log or a trace (`traced_info`), and the credentials live in memory for one connection only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from . import aes

HTTP_PORT = 18910
DEFAULT_BROKER_PORT = 9883
HTTP_TIMEOUT_S = 10.0
MAX_ANSWER_BYTES = 64 * 1024
DID = "printlogbridge"
"""The `did` the bridge names itself with. The printer doesn't check it."""
SECRET_INFO_FIELDS = frozenset({"token", "cn"})
"""What `/info` holds that a trace mustn't keep: the token, and the serial."""
INFO_TRACE_FIELDS = ("code", "message", "ctrlType", "deviceType", "modelId", "modelName",
                     "deviceName", "env", "zone")  # fmt: skip


class HandshakeError(Exception):
    """The printer didn't answer the handshake as an Anycubic printer in LAN mode does. The
    message is safe to log: it never holds the token or the credentials."""


class LanModeOff(HandshakeError):
    """`/info` says LAN mode isn't on (`ctrlType` other than `lan` or `net`)."""


@dataclass(frozen=True)
class Info:
    """What `/info` says. `token` and `serial` are secrets."""

    model_id: str
    model_name: str | None
    device_type: str | None
    """`fdm` or `lcd` (resin)."""
    ctrl_type: str | None
    serial: str | None = field(repr=False)
    token: str = field(repr=False)

    @property
    def resin(self) -> bool:
        return self.device_type == "lcd"


@dataclass(frozen=True)
class Credentials:
    """What `/ctrl` opens to: the broker and one connection's login. Never logged."""

    broker_host: str
    broker_port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    device_id: str = field(repr=False)
    client_id: str | None = field(default=None, repr=False)
    """The client id the broker insists on (the P1), or None when any will do."""


def parse_info(body: Any) -> Info:
    if not isinstance(body, Mapping):
        raise HandshakeError("/info didn't answer with a JSON object")
    token = body.get("token")
    model = body.get("modelId")
    if not isinstance(token, str) or len(token) < 32:
        raise HandshakeError("/info has no handshake token")
    if isinstance(model, bool) or not isinstance(model, (str, int)) or not str(model).strip():
        raise HandshakeError("/info has no modelId")

    def text(key: str) -> str | None:
        value = body.get(key)
        return value.strip() or None if isinstance(value, str) else None

    info = Info(
        model_id=str(model).strip(),
        model_name=text("modelName") or text("deviceName"),
        device_type=text("deviceType"),
        ctrl_type=text("ctrlType"),
        serial=text("cn"),
        token=token,
    )
    if info.ctrl_type not in ("lan", "net"):
        raise LanModeOff(
            f"the printer says LAN mode is off (ctrlType {info.ctrl_type or 'missing'}). Turn "
            "on LAN mode on the printer's screen"
        )
    return info


def traced_info(body: Any) -> dict[str, Any]:
    """What a trace keeps of `/info`: the model and the mode, never the token or the serial
    (nor the address or the MAC address, which it also holds)."""
    if not isinstance(body, Mapping):
        return {"kind": "not a JSON object"}
    return {k: body[k] for k in INFO_TRACE_FIELDS if k in body}


def signature(token: str, ts: str, nonce: str) -> str:
    keyed = hashlib.md5(token[:16].encode()).hexdigest()
    return hashlib.md5((keyed + ts + nonce).encode()).hexdigest()


def ctrl_query(token: str, *, ts: str | None = None, nonce: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time() * 1000))
    nonce = nonce or secrets.token_hex(8)
    return {"ts": ts, "nonce": nonce, "sign": signature(token, ts, nonce), "did": DID}


def open_credentials(token: str, answer: Any) -> Credentials:
    """The credentials in `/ctrl`'s answer, opened with `/info`'s token."""
    if not isinstance(answer, Mapping):
        raise HandshakeError("/ctrl didn't answer with a JSON object")
    code = answer.get("code")
    if code != 200:
        raise HandshakeError(
            f"the printer refused the bridge's /ctrl request (code {code!r}). Is LAN mode on?"
        )
    data = answer.get("data")
    sealed = data.get("info") if isinstance(data, Mapping) else None
    iv_text = data.get("token") if isinstance(data, Mapping) else None
    if not isinstance(sealed, str) or not isinstance(iv_text, str):
        raise HandshakeError("/ctrl's answer has no credentials")
    try:
        ciphertext = base64.b64decode(sealed, validate=False)
    except (binascii.Error, ValueError):
        raise HandshakeError("/ctrl's credentials aren't base64") from None
    iv = iv_text.encode()[:16].ljust(16, b"\x00")
    key = token[16:32].encode()
    if len(key) != 16 or not ciphertext or len(ciphertext) % 16:
        raise HandshakeError("/ctrl's credentials can't be opened")
    plain = aes.decrypt_cbc(ciphertext, key, iv)
    opened = _json_object(plain)
    if opened is None:
        raise HandshakeError("/ctrl's credentials didn't open (a changed token?)")
    host, port = _broker(opened.get("broker"))
    username, password = opened.get("username"), opened.get("password")
    device = opened.get("deviceId")
    if not all(isinstance(v, str) and v for v in (username, password, device)) or not host:
        raise HandshakeError("/ctrl's credentials lack the broker's user, password or device")
    client = opened.get("clientId")
    return Credentials(
        broker_host=host,
        broker_port=port,
        username=username,  # type: ignore[arg-type]
        password=password,  # type: ignore[arg-type]
        device_id=device,  # type: ignore[arg-type]
        client_id=client if isinstance(client, str) and client else None,
    )


def _json_object(plain: bytes) -> dict[str, Any] | None:
    """The JSON object at the start of `plain`, whatever padding follows: PKCS#7 (the FDM
    printers) or zeros (the P1)."""
    text = plain.decode("utf-8", errors="replace")
    start = text.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _broker(value: Any) -> tuple[str | None, int]:
    """`mqtts://<host>:<port>`, `mqtt://<host>`, or `<host>:<port>`."""
    if not isinstance(value, str) or not value.strip():
        return None, DEFAULT_BROKER_PORT
    text = value.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        return text or None, DEFAULT_BROKER_PORT
    number = int(port)
    return host or None, number if 0 < number < 65536 else DEFAULT_BROKER_PORT


async def _read_json(response: aiohttp.ClientResponse, what: str) -> Any:
    raw = b""
    while True:  # one read can return a single chunk (the S1 answers /ctrl chunked)
        part = await response.content.read(MAX_ANSWER_BYTES + 1 - len(raw))
        if not part:
            break
        raw += part
        if len(raw) > MAX_ANSWER_BYTES:
            raise HandshakeError(f"{what}'s answer is too large")
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        raise HandshakeError(f"{what} didn't answer with JSON") from None


async def get_info(
    http: aiohttp.ClientSession, base: str, *, timeout: float = HTTP_TIMEOUT_S
) -> tuple[Any, Info]:
    """`/info`'s raw JSON and what it says. Raises HandshakeError, and what aiohttp raises when
    the printer can't be reached (asyncio.TimeoutError past `timeout`)."""
    request_timeout = aiohttp.ClientTimeout(total=timeout)
    async with http.get(f"{base}/info", allow_redirects=False, timeout=request_timeout) as response:
        if response.status != 200:
            raise HandshakeError(f"/info answered HTTP {response.status}")
        body = await _read_json(response, "/info")
    return body, parse_info(body)


async def get_credentials(
    http: aiohttp.ClientSession, base: str, info: Info, *, timeout: float = HTTP_TIMEOUT_S
) -> tuple[Any, Credentials]:
    """`/ctrl`'s raw answer and the credentials it opens to."""
    request_timeout = aiohttp.ClientTimeout(total=timeout)
    async with http.post(
        f"{base}/ctrl",
        params=ctrl_query(info.token),
        data=b"",
        allow_redirects=False,
        timeout=request_timeout,
    ) as response:
        if response.status != 200:
            raise HandshakeError(f"/ctrl answered HTTP {response.status}")
        answer = await _read_json(response, "/ctrl")
    return answer, open_credentials(info.token, answer)


async def handshake(
    host: str, port: int = HTTP_PORT, *, timeout: float = HTTP_TIMEOUT_S
) -> tuple[Info, Credentials]:
    """`/info`, then `/ctrl`, on a session of their own, bounded by `timeout` each."""
    shown = f"[{host}]" if ":" in host else host
    base = f"http://{shown}:{port}"
    async with aiohttp.ClientSession() as http:
        _, info = await get_info(http, base, timeout=timeout)
        _, credentials = await get_credentials(http, base, info, timeout=timeout)
    return info, credentials
