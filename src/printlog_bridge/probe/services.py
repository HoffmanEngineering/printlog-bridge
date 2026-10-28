"""Identifying what answers on an open port: read-only requests only, each through the
recorder's allow-list check."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import ssl
import struct
import time
import uuid
from typing import Any, Callable

import aiohttp
from yarl import URL

from printlog_bridge import __version__
from printlog_bridge.probe import allowlist
from printlog_bridge.probe.record import MAX_BODY, Recorder, payload

CONNECT_TIMEOUT_S = 2.0
REQUEST_TIMEOUT_S = 5.0
BANNER_S = 1.5
USER_AGENT = f"printlog-bridge-probe/{__version__}"
MQTT_KEEPALIVE_S = 30
SDCP_PING_S = 25.0


def insecure_context() -> ssl.SSLContext:
    """TLS without checking the certificate: printers use self-signed ones, and the probe only
    records what answers. No client certificate is offered."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), 1)


async def tcp_check(rec: Recorder, address: str, port: int) -> dict[str, Any]:
    """Whether `port` accepts a connection. Sends nothing."""
    channel = f"tcp:{port}"
    rec.send("tcp-connect", channel, None)
    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port), CONNECT_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        return {"open": False, "result": "no answer (filtered or off)"}
    except OSError as e:
        return {"open": False, "result": _os_error(e)}
    await _close(writer)
    return {"open": True, "connectMs": round((time.monotonic() - start) * 1000)}


def _os_error(e: OSError) -> str:
    if isinstance(e, ConnectionRefusedError):
        return "refused (closed)"
    return f"{e.__class__.__name__}: {e.strerror or e}"


async def banner(rec: Recorder, address: str, port: int, seconds: float = BANNER_S) -> None:
    """Connects and records whatever the device sends before being asked anything."""
    channel = f"tcp:{port}"
    rec.send("tcp-connect", channel, None, note="waiting for a banner")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port), CONNECT_TIMEOUT_S
        )
    except (OSError, asyncio.TimeoutError) as e:
        rec.error(channel, f"connect failed: {e.__class__.__name__}")
        return
    try:
        data = await asyncio.wait_for(reader.read(4096), seconds)
        if data:
            rec.receive(channel, banner=payload(data))
        else:
            rec.receive(channel, banner=None, closed=True)
    except asyncio.TimeoutError:
        rec.receive(channel, banner=None, note=f"nothing sent within {seconds:g} s")
    except OSError as e:
        rec.error(channel, f"read failed: {e.__class__.__name__}")
    finally:
        await _close(writer)


def session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=None, connect=CONNECT_TIMEOUT_S),
        cookie_jar=aiohttp.DummyCookieJar(),
    )


async def http_get(
    rec: Recorder,
    client: aiohttp.ClientSession,
    scheme: str,
    address: str,
    port: int,
    path: str,
    seconds: float = REQUEST_TIMEOUT_S,
) -> dict[str, Any] | None:
    """GETs `path` and records the status, headers and up to `MAX_BODY` of the body (reading for
    at most `seconds`, as a webcam's stream never ends). Returns the recorded answer."""
    channel = f"{scheme}:{port}"
    rec.send("http-get", channel, f"GET {path}", path=path)
    host = f"[{address}]" if ":" in address else address
    url = URL(f"{scheme}://{host}:{port}{path}", encoded=True)
    deadline = time.monotonic() + seconds
    try:
        response = await asyncio.wait_for(
            client.get(url, allow_redirects=False, ssl=False if scheme == "https" else None),
            seconds,
        )
    except asyncio.TimeoutError:
        rec.error(channel, f"GET {path}: no answer within {seconds:g} s")
        return None
    except (aiohttp.ClientError, OSError, ValueError) as e:
        rec.error(channel, f"GET {path}: {e.__class__.__name__}: {e}")
        return None
    body = b""
    complete = True
    try:
        while len(body) < MAX_BODY:
            chunk = await asyncio.wait_for(
                response.content.read(MAX_BODY - len(body)), max(0.05, _remaining(deadline))
            )
            if not chunk:
                break
            body += chunk
        else:
            complete = response.content.at_eof()
    except asyncio.TimeoutError:
        complete = False
    except (aiohttp.ClientError, OSError) as e:
        rec.error(channel, f"GET {path}: reading the body: {e.__class__.__name__}")
        complete = False
    finally:
        response.close()
    headers: dict[str, str] = {}
    for name, value in response.headers.items():
        headers[name] = f"{headers[name]}, {value}" if name in headers else value
    answer = {
        "path": path,
        "status": response.status,
        "reason": response.reason,
        "headers": headers,
        "body": payload(body, MAX_BODY),
    }
    if not complete:
        answer["body"]["truncated"] = True
    rec.receive(channel, **answer)
    return answer


async def websocket(
    rec: Recorder,
    client: aiohttp.ClientSession,
    address: str,
    port: int,
    path: str,
    deadline: float,
    *,
    rule: str,
    protocols: tuple[str, ...] = (),
    on_open: Callable[[Callable[[str], Any]], Any] | None = None,
    on_text: Callable[[str, Callable[[str], Any]], Any] | None = None,
    every: tuple[float, str] | None = None,
) -> None:
    """Connects, records every message until `deadline`, and sends only what `on_open`,
    `on_text` and `every` (an interval and a text) send, each checked against `rule`."""
    channel = f"ws:{port}{path}"
    rec.send("ws-connect", channel, path, protocols=list(protocols))
    host = f"[{address}]" if ":" in address else address
    try:
        ws = await asyncio.wait_for(
            client.ws_connect(
                f"http://{host}:{port}{path}",
                protocols=protocols,
                autoping=True,
                heartbeat=None,
                max_msg_size=4 * 1024 * 1024,
            ),
            REQUEST_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        rec.error(channel, "the websocket handshake timed out")
        return
    except aiohttp.WSServerHandshakeError as e:
        rec.error(channel, f"handshake refused: HTTP {e.status} {e.message}")
        return
    except (aiohttp.ClientError, OSError) as e:
        rec.error(channel, f"couldn't connect: {e.__class__.__name__}: {e}")
        return
    rec.receive(channel, opened=True, protocol=ws.protocol)

    async def send(text: str) -> None:
        rec.send(rule, channel, text, message=text)
        await ws.send_str(text)

    try:
        if on_open is not None:
            await on_open(send)
        next_tick = time.monotonic() + every[0] if every else None
        while True:
            wait = _remaining(deadline)
            if next_tick is not None:
                wait = min(wait, max(0.0, next_tick - time.monotonic()))
            if wait <= 0 and _remaining(deadline) <= 0:
                break
            try:
                message = await asyncio.wait_for(ws.receive(), wait)
            except asyncio.TimeoutError:
                if every and next_tick is not None and time.monotonic() >= next_tick:
                    await send(every[1])
                    next_tick = time.monotonic() + every[0]
                continue
            if message.type == aiohttp.WSMsgType.TEXT:
                rec.receive(channel, message=payload(message.data))
                if on_text is not None:
                    await on_text(message.data, send)
            elif message.type == aiohttp.WSMsgType.BINARY:
                rec.receive(channel, message=payload(message.data))
            elif message.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                rec.receive(channel, closed=True, code=ws.close_code)
                break
            elif message.type == aiohttp.WSMsgType.ERROR:
                rec.error(channel, f"websocket error: {ws.exception()!r}")
                break
    except (aiohttp.ClientError, OSError, ConnectionError) as e:
        rec.error(channel, f"connection lost: {e.__class__.__name__}")
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(), 2)


# -- Creality ----------------------------------------------------------------------------------


async def creality(rec: Recorder, client, address: str, port: int, deadline: float) -> None:
    """Creality's stock websocket: its status pushes, and the documented `get` requests."""

    async def on_open(send) -> None:
        for request in allowlist.CREALITY_REQUESTS:
            await send(request)

    async def on_text(text: str, send) -> None:
        with contextlib.suppress(ValueError, AttributeError):
            if json.loads(text).get("ModeCode") == "heart_beat":
                await send(allowlist.CREALITY_HEARTBEAT_REPLY)

    await websocket(
        rec,
        client,
        address,
        port,
        "/",
        deadline,
        rule="creality-ws",
        protocols=("wsslicer",),
        on_open=on_open,
        on_text=on_text,
    )


# -- SDCP --------------------------------------------------------------------------------------


def sdcp_request(board: str, cmd: int, now: float | None = None) -> str:
    """An SDCP v3 request (cbd-tech spec, "Request"), compact JSON."""
    message = {
        "Id": uuid.uuid4().hex,
        "Data": {
            "Cmd": cmd,
            "Data": {},
            "RequestID": uuid.uuid4().hex,
            "MainboardID": board,
            "TimeStamp": int(now if now is not None else time.time()),
            "From": 0,
        },
        "Topic": f"sdcp/request/{board}",
    }
    return json.dumps(message, separators=(",", ":"))


async def sdcp(
    rec: Recorder,
    client,
    address: str,
    port: int,
    deadline: float,
    board: Callable[[], Any],
) -> None:
    """SDCP v3's websocket: what it pushes, status and attributes when the discovery reply named
    the mainboard (`board()`, awaited), and its ping."""

    async def on_open(send) -> None:
        mainboard = await board()
        if not mainboard:
            rec.note(
                f"port {port}: no SDCP discovery reply named a MainboardID, so no status "
                "request was sent; only what the printer pushed is recorded"
            )
            return
        for cmd in sorted(allowlist.SDCP_COMMANDS):
            await send(sdcp_request(mainboard, cmd))

    await websocket(
        rec,
        client,
        address,
        port,
        "/websocket",
        deadline,
        rule="sdcp-ws",
        on_open=on_open,
        every=(SDCP_PING_S, allowlist.SDCP_PING),
    )


# -- MQTT --------------------------------------------------------------------------------------

CONNACK_REASONS = {
    1: "unacceptable protocol version",
    2: "client id rejected",
    3: "broker unavailable",
    4: "bad user name or password (credentials required)",
    5: "not authorized (credentials required)",
}


def _mqtt_text(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def _mqtt_length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte, length = length % 128, length // 128
        out.append(byte | (0x80 if length else 0))
        if not length:
            return bytes(out)


def mqtt_connect_packet(client_id: str, keepalive: int = MQTT_KEEPALIVE_S) -> bytes:
    """CONNECT: clean session, no user name, password or will."""
    body = _mqtt_text("MQTT") + bytes([4, 0x02]) + struct.pack(">H", keepalive)
    body += _mqtt_text(client_id)
    return b"\x10" + _mqtt_length(len(body)) + body


def mqtt_subscribe_packet(packet_id: int = 1) -> bytes:
    body = struct.pack(">H", packet_id) + _mqtt_text(allowlist.MQTT_SUBSCRIBE_TOPIC) + b"\x00"
    return b"\x82" + _mqtt_length(len(body)) + body


PINGREQ = b"\xc0\x00"
DISCONNECT = b"\xe0\x00"


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first = (await reader.readexactly(1))[0]
    length, shift = 0, 0
    for _ in range(4):
        byte = (await reader.readexactly(1))[0]
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    if length > 8 * 1024 * 1024:
        raise ValueError(f"a packet of {length} bytes")
    return first, await reader.readexactly(length)


async def mqtt(rec: Recorder, address: str, port: int, tls: bool, deadline: float) -> None:
    """Connects without credentials. A broker that takes that is subscribed to `#` until
    `deadline`; one that refuses is recorded as needing credentials and left."""
    channel = f"mqtt:{port}"
    if tls:
        rec.send("tls-handshake", channel, None)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port, ssl=insecure_context() if tls else None),
            REQUEST_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        rec.error(channel, "connect (and TLS handshake) timed out")
        return
    except ssl.SSLError as e:
        rec.error(
            channel,
            f"TLS handshake failed: {e.reason or e} (mutual TLS needs a client "
            "certificate the probe doesn't have)",
        )
        return
    except OSError as e:
        rec.error(channel, f"couldn't connect: {_os_error(e)}")
        return
    if tls:
        tls_object = writer.get_extra_info("ssl_object")
        der = tls_object.getpeercert(binary_form=True) if tls_object else None
        rec.receive(
            channel,
            tls={
                "version": tls_object.version() if tls_object else None,
                "certificateSha256": hashlib.sha256(der).hexdigest() if der else None,
            },
        )

    async def send(packet: bytes) -> None:
        rec.send("mqtt", channel, packet, packet=payload(packet))
        writer.write(packet)
        await writer.drain()

    try:
        await send(mqtt_connect_packet(f"plb-probe-{uuid.uuid4().hex[:8]}"))
        first, body = await asyncio.wait_for(_read_packet(reader), REQUEST_TIMEOUT_S)
        if first >> 4 != 2 or len(body) < 2:
            rec.error(channel, f"expected CONNACK, got packet type {first >> 4}")
            return
        code = body[1]
        rec.receive(
            channel,
            connack=code,
            meaning="accepted without credentials"
            if code == 0
            else CONNACK_REASONS.get(code, "refused"),
        )
        if code != 0:
            return
        await send(mqtt_subscribe_packet())
        last_ping = time.monotonic()
        while _remaining(deadline) > 0:
            wait = min(_remaining(deadline), MQTT_KEEPALIVE_S / 2)
            try:
                first, body = await asyncio.wait_for(_read_packet(reader), wait)
            except asyncio.TimeoutError:
                first = None
            if time.monotonic() - last_ping >= MQTT_KEEPALIVE_S / 2:
                await send(PINGREQ)
                last_ping = time.monotonic()
            if first is None:
                continue
            kind = first >> 4
            if kind == 3:  # PUBLISH
                length = struct.unpack(">H", body[:2])[0]
                topic = body[2 : 2 + length].decode("utf-8", errors="replace")
                offset = 2 + length + (2 if first & 0x06 else 0)
                rec.receive(channel, topic=topic, message=payload(body[offset:]))
            elif kind == 9:
                rec.receive(channel, suback=list(body[2:]))
        with contextlib.suppress(OSError, ConnectionError):
            await send(DISCONNECT)
    except asyncio.TimeoutError:
        rec.error(channel, "no CONNACK within the timeout")
    except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError) as e:
        rec.receive(channel, closed=True, reason=e.__class__.__name__)
    finally:
        await _close(writer)


# -- Anycubic Photon ---------------------------------------------------------------------------


async def photon(rec: Recorder, address: str, port: int) -> None:
    """Anycubic Photon's TCP text protocol: `sysinfo` and `getstatus`, each answered with a
    line ending `,end`."""
    channel = f"tcp:{port}"
    for query in allowlist.PHOTON_QUERIES:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), CONNECT_TIMEOUT_S
            )
        except (OSError, asyncio.TimeoutError) as e:
            rec.error(channel, f"connect failed: {e.__class__.__name__}")
            return
        try:
            rec.send("photon-query", channel, query, message=query)
            writer.write(query.encode("ascii"))
            await writer.drain()
            answer = b""
            end = time.monotonic() + 3
            while not answer.rstrip().endswith(b",end") and _remaining(end) > 0:
                chunk = await asyncio.wait_for(reader.read(4096), _remaining(end))
                if not chunk:
                    break
                answer += chunk
            rec.receive(channel, query=query, message=payload(answer))
        except asyncio.TimeoutError:
            rec.receive(channel, query=query, message=payload(answer), note="timed out")
        except OSError as e:
            rec.error(channel, f"{query}: {e.__class__.__name__}")
        finally:
            await _close(writer)
