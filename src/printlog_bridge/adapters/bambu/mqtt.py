"""The part of MQTT 3.1.1 the bridge needs to listen to a Bambu Lab printer's own broker.

The printer runs an MQTT broker on port 8883 (TLS) for its slicer: it publishes its status on
`device/<serial>/report` and takes requests on `device/<serial>/request`. The bridge connects
with a clean session, subscribes to the reports, and publishes two requests (`pushall` and
`get_version`), all at QoS 0. So this is five packets (CONNECT, SUBSCRIBE, PUBLISH, PINGREQ,
DISCONNECT) and their answers, on asyncio streams: no thread, and no dependency to build on every
platform the Docker image is made for.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import struct
from collections.abc import Callable

MAX_PACKET_BYTES = 8 * 1024 * 1024
"""A whole status report is a few tens of KB; anything this big is a broken stream."""
CONNECT_TIMEOUT_S = 10.0

CONNACK_REASONS = {
    1: "unacceptable protocol version",
    2: "client id rejected",
    3: "broker unavailable",
    4: "bad user name or password",
    5: "not authorized",
}

_CONNECT, _CONNACK, _PUBLISH, _SUBSCRIBE, _SUBACK = 1, 2, 3, 8, 9
_PINGREQ, _PINGRESP, _DISCONNECT = 12, 13, 14


class MqttClosed(Exception):
    """The connection ended, or the broker stopped answering."""


class MqttRefused(Exception):
    """The broker answered CONNECT with a return code other than 0."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"refused ({code}: {CONNACK_REASONS.get(code, 'unknown reason')})")


class MqttClient:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, keepalive: int
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._keepalive = keepalive
        self._packet_id = 0
        self._pinger: asyncio.Task | None = None
        self.closed = False

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        context: ssl.SSLContext,
        *,
        client_id: str,
        username: str,
        password: str,
        keepalive: int = 60,
        verify: Callable[[bytes], None] | None = None,
        timeout: float = CONNECT_TIMEOUT_S,
    ) -> MqttClient:
        """Connects and logs in. `verify` is given the broker's certificate (DER) before the
        password is sent, and raises to stop there. Raises `MqttRefused` when the broker says
        no, and OSError, `ssl.SSLError` or `asyncio.TimeoutError` when it can't be reached."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context, server_hostname=None), timeout
        )
        try:
            if verify is not None:
                tls = writer.get_extra_info("ssl_object")
                verify((tls.getpeercert(binary_form=True) if tls else None) or b"")
            writer.write(_connect_packet(client_id, username, password, keepalive))
            await writer.drain()
            first, body = await asyncio.wait_for(_read_packet(reader), timeout)
            kind = first >> 4
            if kind != _CONNACK or len(body) < 2:
                raise MqttClosed(f"expected CONNACK, got packet type {kind}")
            if body[1] != 0:
                raise MqttRefused(body[1])
        except BaseException:
            writer.close()
            raise
        client = cls(reader, writer, keepalive)
        client._pinger = asyncio.ensure_future(client._ping())
        return client

    async def subscribe(self, topic: str) -> None:
        """Subscribes at QoS 0. The SUBACK is read by `receive` and ignored there."""
        self._packet_id = self._packet_id % 0xFFFF + 1
        body = struct.pack(">H", self._packet_id) + _text(topic) + b"\x00"
        await self._send(_SUBSCRIBE << 4 | 0x02, body)

    async def publish(self, topic: str, payload: bytes) -> None:
        await self._send(_PUBLISH << 4, _text(topic) + payload)

    async def receive(self) -> tuple[str, bytes]:
        """The next message published to a subscribed topic, as (topic, payload). Raises
        `MqttClosed` when the connection ends or the broker is silent for one and a half
        keepalive periods (the pings get answers, so silence means it's gone)."""
        while True:
            try:
                first, body = await asyncio.wait_for(
                    _read_packet(self._reader), self._keepalive * 1.5
                )
            except asyncio.TimeoutError:
                await self.close()
                raise MqttClosed("the printer stopped answering") from None
            except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError) as e:
                await self.close()
                raise MqttClosed(f"connection lost ({e.__class__.__name__})") from None
            if first >> 4 != _PUBLISH:
                continue  # SUBACK, PINGRESP
            length = struct.unpack(">H", body[:2])[0]
            topic = body[2 : 2 + length].decode("utf-8", errors="replace")
            offset = 2 + length + (2 if first & 0x06 else 0)  # a packet id at QoS 1 or 2
            return topic, body[offset:]

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._pinger is not None:
            self._pinger.cancel()
        with contextlib.suppress(Exception):
            self._writer.write(bytes([_DISCONNECT << 4, 0]))
        self._writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._writer.wait_closed(), 2)

    async def _send(self, first: int, body: bytes) -> None:
        if self.closed:
            raise MqttClosed("the connection is closed")
        try:
            self._writer.write(bytes([first]) + _length(len(body)) + body)
            await self._writer.drain()
        except (ConnectionError, ssl.SSLError, OSError) as e:
            await self.close()
            raise MqttClosed(f"connection lost ({e.__class__.__name__})") from None

    async def _ping(self) -> None:
        with contextlib.suppress(MqttClosed):
            while True:
                await asyncio.sleep(self._keepalive / 2)
                await self._send(_PINGREQ << 4, b"")


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """The packet's first byte (its type and flags) and its body."""
    first = (await reader.readexactly(1))[0]
    length, shift = 0, 0
    for _ in range(4):
        byte = (await reader.readexactly(1))[0]
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    else:
        raise MqttClosed("malformed packet length")
    if length > MAX_PACKET_BYTES:
        raise MqttClosed(f"a packet of {length} bytes is too large")
    return first, await reader.readexactly(length)


def _connect_packet(client_id: str, username: str, password: str, keepalive: int) -> bytes:
    flags = 0x80 | 0x40 | 0x02  # user name, password, clean session
    body = (
        _text("MQTT")
        + bytes([4, flags])
        + struct.pack(">H", keepalive)
        + _text(client_id)
        + _text(username)
        + _text(password)
    )
    return bytes([_CONNECT << 4]) + _length(len(body)) + body


def _text(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def _length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte, length = length % 128, length // 128
        out.append(byte | (0x80 if length else 0))
        if not length:
            return bytes(out)
