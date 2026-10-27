"""A stand-in for a Bambu Lab printer on the LAN: its MQTT broker (TLS, port 8883) and its FTPS
server (implicit TLS, port 990), as documented by OpenBambuAPI and ha-bambulab (see
docs/adapters.md, "The Bambu adapter"). No real printer has been traced yet (#17).

The broker speaks the part of MQTT 3.1.1 a client of the printer uses: CONNECT (user `bblp`, the
access code as the password; a wrong one is answered with return code 5), SUBSCRIBE, PUBLISH at
QoS 0, PINGREQ and DISCONNECT. A request on `device/<serial>/request` is recorded, and answered as
the printer does: `pushall` with the whole status, `get_version` with its modules. `send(report)`
pushes a report to every subscriber, as a P1 or A1 pushes the fields that changed.

The FTPS server takes USER/PASS, PBSZ, PROT, TYPE, PASV/EPSV, SIZE, RETR, NLST and QUIT, over TLS
from the first byte, and serves the files in `files`. Both use a self-signed certificate made
with `openssl` (`certificate()`), as the printer's own is from a CA nobody else trusts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import ssl
import struct
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

SERIAL = "01S00C123456789"
ACCESS_CODE = "40917263"
CANCELLED_ERROR = 0x0300400C


def certificate(directory: Path, common_name: str = SERIAL) -> tuple[Path, Path]:
    """A self-signed certificate and its key, as the printer's has the serial as its name.
    Skips the test when `openssl` isn't installed."""
    cert, key = directory / f"{common_name}.crt", directory / f"{common_name}.key"
    if cert.exists():
        return cert, key
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("needs openssl to make a certificate")
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", f"/CN={common_name}", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )  # fmt: skip
    return cert, key


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    return context


def idle_status(**fields: Any) -> dict[str, Any]:
    """A full `push_status` report of an idle P1S with one AMS, as `pushall` answers."""
    status: dict[str, Any] = {
        "command": "push_status",
        "sequence_id": "1",
        "gcode_state": "IDLE",
        "gcode_file": "",
        "subtask_name": "",
        "gcode_start_time": "0",
        "task_id": "0",
        "subtask_id": "0",
        "mc_percent": 0,
        "mc_remaining_time": 0,
        "layer_num": 0,
        "total_layer_num": 0,
        "print_error": 0,
        "ams": {
            "ams": [
                {
                    "id": "0",
                    "tray": [
                        tray("0", "PLA", "FF0000FF", "A1B2C3D4E5F60708"),
                        tray("1", "PETG", "00FF00FF", "0000000000000000"),
                        {"id": "2"},
                        {"id": "3"},
                    ],
                }
            ],
            "tray_now": "255",
        },
        "vt_tray": tray("254", "", "00000000", "0000000000000000"),
    }
    status.update(fields)
    return status


def tray(tray_id: str, kind: str, color: str, tag: str) -> dict[str, Any]:
    return {
        "id": tray_id,
        "tray_type": kind,
        "tray_color": color,
        "tag_uid": tag,
        "tray_uuid": "0" * 32,
        "remain": 80,
    }


VERSION = {
    "info": {
        "command": "get_version",
        "sequence_id": "2",
        "module": [
            {"name": "ota", "sw_ver": "01.08.02.00", "hw_ver": "", "sn": SERIAL},
            {"name": "mc", "sw_ver": "00.00.31.38", "hw_ver": "MC07", "sn": "x"},
        ],
    }
}


class FakeBambu:
    """The printer's MQTT broker. `status` is what `pushall` answers with."""

    def __init__(
        self,
        cert: Path,
        key: Path,
        *,
        serial: str = SERIAL,
        access_code: str = ACCESS_CODE,
        status: Mapping[str, Any] | None = None,
    ) -> None:
        self.context = server_context(cert, key)
        self.serial = serial
        self.access_code = access_code
        self.status: dict[str, Any] = dict(status or idle_status())
        self.version: dict[str, Any] | None = VERSION
        self.refuse: int | None = None
        """A CONNACK return code to answer every CONNECT with (5: not authorized)."""
        self.answer_pushall = True
        self.requests: list[dict[str, Any]] = []
        self.connects: list[dict[str, Any]] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._clients: list[tuple[asyncio.StreamWriter, set[str]]] = []

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self.context)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        self.drop()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def drop(self) -> None:
        """Cuts every client off, as a printer rebooting or leaving the network does."""
        for writer, _ in self._clients:
            writer.close()
        self._clients.clear()

    @property
    def subscribed(self) -> bool:
        topic = f"device/{self.serial}/report"
        return any(topic in topics for _, topics in self._clients)

    def requests_of(self, command: str) -> list[dict[str, Any]]:
        return [
            r for r in self.requests
            if any(isinstance(v, dict) and v.get("command") == command for v in r.values())
        ]  # fmt: skip

    async def send(self, report: Mapping[str, Any]) -> None:
        """Pushes `{"print": report}` to every subscriber, and keeps its fields in `status`."""
        self.status.update(report)
        await self.publish({"print": dict(report)})

    async def publish(self, message: Mapping[str, Any]) -> None:
        topic = f"device/{self.serial}/report"
        packet = _publish(topic, json.dumps(message).encode())
        for writer, topics in list(self._clients):
            if topic in topics:
                writer.write(packet)
                with contextlib.suppress(ConnectionError):
                    await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        topics: set[str] = set()
        entry = (writer, topics)
        try:
            kind, body = await _read(reader)
            if kind != 1:
                return
            connect = _parse_connect(body)
            self.connects.append(connect)
            if self.refuse is not None:
                writer.write(bytes([0x20, 2, 0, self.refuse]))
                await writer.drain()
                return
            if connect["username"] != "bblp" or connect["password"] != self.access_code:
                writer.write(bytes([0x20, 2, 0, 5]))
                await writer.drain()
                return
            writer.write(bytes([0x20, 2, 0, 0]))
            await writer.drain()
            self._clients.append(entry)
            while True:
                kind, body = await _read(reader)
                if kind == 8:  # SUBSCRIBE
                    packet_id = body[:2]
                    topic_length = struct.unpack(">H", body[2:4])[0]
                    topics.add(body[4 : 4 + topic_length].decode())
                    writer.write(bytes([0x90, 3]) + packet_id + bytes([0]))
                elif kind == 3:  # PUBLISH, QoS 0
                    topic_length = struct.unpack(">H", body[:2])[0]
                    topic = body[2 : 2 + topic_length].decode()
                    if topic == f"device/{self.serial}/request":
                        await self._request(json.loads(body[2 + topic_length :]))
                elif kind == 12:  # PINGREQ
                    writer.write(bytes([0xD0, 0]))
                elif kind == 14:  # DISCONNECT
                    return
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError):
            pass
        finally:
            if entry in self._clients:
                self._clients.remove(entry)
            writer.close()

    async def _request(self, request: dict[str, Any]) -> None:
        self.requests.append(request)
        if request.get("pushing", {}).get("command") == "pushall" and self.answer_pushall:
            await self.publish({"print": dict(self.status)})
        elif request.get("info", {}).get("command") == "get_version" and self.version:
            await self.publish(self.version)


async def _read(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first = (await reader.readexactly(1))[0]
    length, shift = 0, 0
    while True:
        byte = (await reader.readexactly(1))[0]
        length += (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return first >> 4, await reader.readexactly(length)


def _parse_connect(body: bytes) -> dict[str, Any]:
    def text(at: int) -> tuple[str, int]:
        length = struct.unpack(">H", body[at : at + 2])[0]
        return body[at + 2 : at + 2 + length].decode(), at + 2 + length

    name, at = text(0)
    level, flags = body[at], body[at + 1]
    keepalive = struct.unpack(">H", body[at + 2 : at + 4])[0]
    client_id, at = text(at + 4)
    username = password = None
    if flags & 0x80:
        username, at = text(at)
    if flags & 0x40:
        password, at = text(at)
    return {
        "protocol": name,
        "level": level,
        "clean": bool(flags & 0x02),
        "keepalive": keepalive,
        "client_id": client_id,
        "username": username,
        "password": password,
    }


def _publish(topic: str, payload: bytes) -> bytes:
    encoded = topic.encode()
    body = struct.pack(">H", len(encoded)) + encoded + payload
    length, out = len(body), bytearray()
    while True:
        byte, length = length % 128, length // 128
        out.append(byte | (0x80 if length else 0))
        if not length:
            break
    return bytes([0x30]) + bytes(out) + body


class FakeFtps:
    """The printer's FTPS server: implicit TLS on every connection, read only. `files` maps a
    path such as `/cache/Benchy.3mf` to its bytes. Runs in threads of its own."""

    def __init__(self, cert: Path, key: Path, *, access_code: str = ACCESS_CODE) -> None:
        self.context = server_context(cert, key)
        self.access_code = access_code
        self.files: dict[str, bytes] = {}
        self.retrieved: list[str] = []
        self.logins = 0
        self.port = 0
        self._listener: socket.socket | None = None
        self._closed = threading.Event()

    def start(self) -> int:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()
        return self.port

    def close(self) -> None:
        self._closed.set()
        if self._listener is not None:
            self._listener.close()

    def _accept(self) -> None:
        assert self._listener is not None
        while not self._closed.is_set():
            try:
                raw, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(raw,), daemon=True).start()

    def _session(self, raw: socket.socket) -> None:
        try:
            control = self.context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        lines = control.makefile("rb")

        def reply(text: str) -> None:
            control.sendall(f"{text}\r\n".encode())

        passive: socket.socket | None = None
        user = None
        authed = False
        try:
            reply("220 Bambu FTP")
            for line in lines:
                command, _, argument = line.decode().rstrip("\r\n").partition(" ")
                command = command.upper()
                if command == "USER":
                    user = argument
                    reply("331 password please")
                elif command == "PASS":
                    if user == "bblp" and argument == self.access_code:
                        authed = True
                        self.logins += 1
                        reply("230 logged in")
                    else:
                        reply("530 Login incorrect.")
                elif command in ("PBSZ", "PROT", "TYPE", "NOOP"):
                    reply("200 ok")
                elif not authed:
                    reply("530 Please login with USER and PASS.")
                elif command in ("PASV", "EPSV"):
                    passive = socket.create_server(("127.0.0.1", 0))
                    port = passive.getsockname()[1]
                    if command == "EPSV":
                        reply(f"229 Entering Extended Passive Mode (|||{port}|)")
                    else:
                        reply(f"227 Entering Passive Mode (127,0,0,1,{port >> 8},{port & 255})")
                elif command == "SIZE":
                    data = self.files.get(argument)
                    reply(f"213 {len(data)}" if data is not None else "550 not found")
                elif command in ("RETR", "NLST") and passive is not None:
                    if command == "RETR" and argument not in self.files:
                        reply("550 not found")
                        continue
                    if command == "RETR":
                        self.retrieved.append(argument)
                        data = self.files[argument]
                    else:
                        data = "".join(f"{name}\r\n" for name in self.files).encode()
                    reply("150 opening data connection")
                    conn, _ = passive.accept()
                    passive.close()
                    passive = None
                    with self.context.wrap_socket(conn, server_side=True) as tls:
                        tls.sendall(data)
                        with contextlib.suppress(OSError, ValueError):
                            tls.unwrap()
                    reply("226 done")
                elif command == "QUIT":
                    reply("221 bye")
                    return
                else:
                    reply("502 not implemented")
        except (OSError, ssl.SSLError, UnicodeDecodeError):
            return
        finally:
            control.close()


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
"""Enough of a PNG for `image_type`."""


def make_3mf(
    plates: Mapping[int, list[tuple[int, str, float, float]]] | None = None,
    settings: Mapping[str, Any] | None = None,
    prediction: int = 3600,
) -> bytes:
    """A Bambu Studio 3MF: per plate, its filaments as (id, type, used_m, used_g), a preview
    of each plate, and the project's settings."""
    import io
    import zipfile

    plates = plates if plates is not None else {1: [(1, "PLA", 4.12, 12.34)]}
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>", "  <header>",
           '    <header_item key="X-BBL-Client-Type" value="slicer"/>',
           '    <header_item key="X-BBL-Client-Version" value="02.02.00.85"/>',
           "  </header>"]  # fmt: skip
    for index, filaments in plates.items():
        weight = sum(f[3] for f in filaments)
        xml += ["  <plate>", f'    <metadata key="index" value="{index}"/>',
                f'    <metadata key="prediction" value="{prediction}"/>',
                f'    <metadata key="weight" value="{weight:.2f}"/>']  # fmt: skip
        for filament_id, kind, used_m, used_g in filaments:
            xml.append(
                f'    <filament id="{filament_id}" type="{kind}" color="#FF0000" '
                f'used_m="{used_m}" used_g="{used_g}"/>'
            )
        xml.append("  </plate>")
    xml.append("</config>")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Metadata/slice_info.config", "\n".join(xml))
        for index in plates:
            z.writestr(f"Metadata/plate_{index}.png", PNG)
            z.writestr(f"Metadata/plate_{index}.gcode", "G28\n" * 1000)
        z.writestr(
            "Metadata/project_settings.config",
            json.dumps(dict(settings or {"layer_height": "0.2", "nozzle_diameter": ["0.4"]})),
        )
    return out.getvalue()
