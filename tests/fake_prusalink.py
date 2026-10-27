"""A fake PrusaLink printer for the tests: its local HTTP API as Prusa-Firmware-Buddy serves it
(MK4/MK4S, MK3.5/3.9, Core One, MINI, XL), and a `.bgcode` file builder.

Built from Prusa's published sources, not a recorded printer (#17):

- Prusa-Link-Web `spec/openapi.yaml`: the endpoints and their fields.
- Prusa-Firmware-Buddy `lib/WUI/link_content/basic_gets.cpp` (`/api/version`, `/api/v1/info`,
  `get_job_v1`: no `meta`, `refs.download` is the file's short path under `/usb/`, thumbnails
  under `/thumb/l/`), `nhttp/status_renderer.cpp` (`job` only while there is one),
  `src/state/printer_state.cpp` (`has_job`: PRINTING, PAUSED and ATTENTION in a print, never
  FINISHED or STOPPED), and `nhttp/req_parser.cpp` / `status_page.cpp` for the authentication:
  Digest without `qop`, realm `Printer API`, user `maker`, a nonce good for 300 s and `stale=true`
  after that; `X-Api-Key` with the same password works too. `send_file.cpp` has no `Range`.
- libbgcode `doc/specifications.md` for the file format.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import struct
import zlib
from collections.abc import Mapping, Sequence

from aiohttp import web

PASSWORD = "Xk3pT9vQ2mWz"
USER = "maker"
REALM = "Printer API"
FIRMWARE = "6.2.1+8899"
HAS_JOB = frozenset({"PRINTING", "PAUSED", "ATTENTION"})

MAGIC = b"GCDE"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-thumbnail"


# --- .bgcode files ---------------------------------------------------------------------------


def heatshrink_encode(data: bytes, window_bits: int = 12, lookahead_bits: int = 4) -> bytes:
    """A plain greedy Heatshrink encoder, so the reader is tested against back references."""
    bits: list[int] = []

    def put(value: int, count: int) -> None:
        bits.extend((value >> (count - 1 - i)) & 1 for i in range(count))

    window = 1 << window_bits
    longest = 1 << lookahead_bits
    i = 0
    while i < len(data):
        best_length, best_offset = 0, 0
        for offset in range(1, min(window, i) + 1):
            length = 0
            while (
                length < longest
                and i + length < len(data)
                and data[i + length] == data[i - offset + length]
            ):
                length += 1
            if length > best_length:
                best_length, best_offset = length, offset
        if best_length >= 2:
            put(0, 1)
            put(best_offset - 1, window_bits)
            put(best_length - 1, lookahead_bits)
            i += best_length
        else:
            put(1, 1)
            put(data[i], 8)
            i += 1
    while len(bits) % 8:
        bits.append(0)
    return bytes(int("".join(str(b) for b in bits[n : n + 8]), 2) for n in range(0, len(bits), 8))


def _block(kind: int, payload: bytes, params: bytes, compression: int, checksum: bool) -> bytes:
    if compression == 0:
        stored = payload
        header = struct.pack("<HHI", kind, 0, len(payload))
    else:
        if compression == 1:
            stored = zlib.compress(payload)
        else:
            stored = heatshrink_encode(payload, 11 if compression == 2 else 12, 4)
        header = struct.pack("<HHII", kind, compression, len(payload), len(stored))
    block = header + params + stored
    if checksum:
        block += struct.pack("<I", zlib.crc32(block) & 0xFFFFFFFF)
    return block


def _ini(pairs: Mapping[str, str]) -> bytes:
    return "".join(f"{k}={v}\n" for k, v in pairs.items()).encode()


def make_bgcode(
    *,
    printer: Mapping[str, str] | None = None,
    print_meta: Mapping[str, str] | None = None,
    slicer: Mapping[str, str] | None = None,
    producer: str = "PrusaSlicer 2.9.2",
    thumbnails: Sequence[tuple[int, int, int, bytes]] = ((0, 313, 173, PNG),),
    compression: Mapping[str, int] | None = None,
    checksum: bool = True,
    gcode: bytes = b"SECRET-GCODE G1 X10 Y10 E.5\n" * 20,
) -> bytes:
    """A `.bgcode` file as PrusaSlicer writes one: file, printer, thumbnail, print and slicer
    metadata blocks, then G-code. `compression` sets a block's (`file`, `printer`, `print`,
    `slicer`, `thumbnail`): 0 none, 1 Deflate, 2/3 Heatshrink. PrusaSlicer 2.9 deflates the
    print and slicer blocks."""
    compress = {"print": 1, "slicer": 1, **(compression or {})}
    if printer is None:
        printer = {
            "printer_model": "MK4S",
            "filament_type": "PLA",
            "nozzle_diameter": "0.4",
            "layer_height": "0.2",
            "temperature": "215",
            "bed_temperature": "60",
            "filament used [g]": "12.34",
            "filament used [mm]": "4130.20",
            "estimated printing time (normal mode)": "1h 2m 3s",
        }
    if print_meta is None:
        print_meta = {
            "filament used [mm]": "4130.20",
            "filament used [g]": "12.34",
            "estimated printing time (normal mode)": "1h 2m 3s",
        }
    if slicer is None:
        slicer = {"layer_height": "0.2", "printhost_apikey": "SECRET-KEY", "infill": "15%"}
    out = MAGIC + struct.pack("<IH", 1, 1 if checksum else 0)
    two = struct.pack("<H", 0)
    out += _block(0, _ini({"Producer": producer}), two, compress.get("file", 0), checksum)
    out += _block(3, _ini(printer), two, compress.get("printer", 0), checksum)
    for image_format, width, height, data in thumbnails:
        params = struct.pack("<HHH", image_format, width, height)
        out += _block(5, data, params, compress.get("thumbnail", 0), checksum)
    out += _block(4, _ini(print_meta), two, compress.get("print", 0), checksum)
    out += _block(2, _ini(slicer), two, compress.get("slicer", 0), checksum)
    out += _block(1, gcode, two, 0, checksum)
    return out


def make_gcode(grams: str = "12.34", mm: str = "4130.20", size: int = 0) -> bytes:
    """A plain `.gcode` file as PrusaSlicer writes one: its estimates and settings after the
    G-code. `size` pads the G-code to about that many bytes."""
    head = b"; generated by PrusaSlicer 2.9.2+win64 on 2026-10-27 at 10:00:00 UTC\n\n"
    body = b"G1 X10 Y10 E.5 ; SECRET-GCODE\n" * max(1, size // 30)
    tail = (
        f"; filament used [mm] = {mm}\n; filament used [g] = {grams}\n"
        "; estimated printing time (normal mode) = 1h 2m 3s\n\n"
        "; prusaslicer_config = begin\n; layer_height = 0.2\n; printhost_apikey = SECRET-KEY\n"
        "; printer_model = MINI\n; prusaslicer_config = end\n"
    ).encode()
    return head + body + tail


# --- the printer ---------------------------------------------------------------------------


class FakePrusaLink:
    """Buddy's PrusaLink: `/api/version`, `/api/v1/info`, `/api/v1/status`, `/api/v1/job`,
    `/usb/...` downloads and `/thumb/l/...` thumbnails, behind Digest (or X-Api-Key)."""

    def __init__(self, password: str = PASSWORD) -> None:
        self.password = password
        self.state = "IDLE"
        self.job: dict | None = None
        """`id`, `display_name`, `path` (the short path under /usb), `progress`,
        `time_printing`, `time_remaining`, and `meta` when the printer gives it."""
        self.files: dict[str, bytes] = {}
        self.thumbnails: dict[str, bytes] = {}
        self.version: dict = {
            "api": "2.0.0",
            "server": "2.1.2",
            "nozzle_diameter": 0.4,
            "text": "PrusaLink",
            "hostname": "mk4s",
            "firmware": FIRMWARE,
            "printer": "1.3.1",
            "capabilities": {"upload-by-put": True},
        }
        self.api_key_only = False
        """Firmware that asks for X-Api-Key only (WWW-Authenticate: ApiKey)."""
        self.down = False
        """Answers every request with 503, as if the printer were off and a proxy in front."""
        self.requests: list[str] = []
        self.methods: set[str] = set()
        self.downloads: list[str] = []
        self.unauthorized = 0
        self.downloaded_bytes = 0
        self.chunk_delay = 0.0
        """A pause after each 4 KB of a download, as a printer's network would make: without
        one, loopback socket buffers (megabytes on Linux) take a whole test file at once, so
        `downloaded_bytes` can't show where the bridge stopped reading."""
        self._nonces: set[str] = set()
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def start(self) -> str:
        app = web.Application(middlewares=[self._auth])
        app.router.add_get("/api/version", self._version)
        app.router.add_get("/api/v1/info", self._info)
        app.router.add_get("/api/v1/status", self._status)
        app.router.add_get("/api/v1/job", self._job)
        app.router.add_get("/usb/{path:.*}", self._download)
        app.router.add_get("/thumb/l/usb/{path:.*}", self._thumbnail)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return f"127.0.0.1:{self.port}"

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    def expire_nonces(self) -> None:
        """Every nonce handed out so far is now too old (Buddy keeps one 300 s)."""
        self._nonces.clear()

    def print_file(self, job_id: int, name: str, data: bytes, **job) -> None:
        """Starts printing `name` from the USB drive (its short name is the same)."""
        self.files[name] = data
        self.state = "PRINTING"
        self.job = {
            "id": job_id,
            "display_name": name,
            "path": f"/usb/{name}",
            "progress": 0.0,
            "time_printing": 0,
            "time_remaining": 3723,
            **job,
        }

    def end(self, state: str) -> None:
        """The job ends `FINISHED`, `STOPPED` or `ERROR`; Buddy drops it from `status`."""
        self.state = state

    def requests_of(self, path: str) -> list[str]:
        return [r for r in self.requests if r == path]

    # --- handlers --------------------------------------------------------------------------

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        self.requests.append(request.path)
        self.methods.add(request.method)
        if self.down:
            raise web.HTTPServiceUnavailable()
        if request.headers.get("X-Api-Key") == self.password:
            return await handler(request)
        if not self.api_key_only:
            stale = False
            header = request.headers.get("Authorization", "")
            if header.startswith("Digest "):
                fields = dict(re.findall(r'(\w+)="?([^",]*)"?', header[7:]))
                if self._digest_ok(request, fields):
                    if fields.get("nonce") in self._nonces:
                        return await handler(request)
                    stale = True
            self.unauthorized += 1
            nonce = secrets.token_hex(8)
            self._nonces.add(nonce)
            challenge = (
                f'Digest realm="{REALM}", nonce="{nonce}", stale={"true" if stale else "false"}'
            )
        else:
            self.unauthorized += 1
            challenge = f'ApiKey realm="{REALM}"'
        return web.Response(
            status=401, text="Unauthorized", headers={"WWW-Authenticate": challenge}
        )

    def _digest_ok(self, request: web.Request, fields: Mapping[str, str]) -> bool:
        def md5(text: str) -> str:
            return hashlib.md5(text.encode()).hexdigest()

        if fields.get("username") != USER or fields.get("realm") != REALM:
            return False
        ha1 = md5(f"{USER}:{REALM}:{self.password}")
        ha2 = md5(f"{request.method}:{request.path_qs}")
        return fields.get("response") == md5(f"{ha1}:{fields.get('nonce')}:{ha2}")

    async def _version(self, request: web.Request) -> web.Response:
        return web.json_response(self.version)

    async def _info(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "nozzle_diameter": 0.4,
                "mmu": False,
                "serial": "10589-3742441632035021",
                "hostname": "mk4s",
                "min_extrusion_temp": 170,
            }
        )

    def has_job(self) -> bool:
        return self.job is not None and self.state in HAS_JOB

    async def _status(self, request: web.Request) -> web.Response:
        body: dict = {}
        if self.has_job():
            job = self.job or {}
            body["job"] = {
                "id": job["id"],
                "progress": job["progress"],
                "time_remaining": job["time_remaining"],
                "time_printing": job["time_printing"],
            }
        body["storage"] = {"path": "/usb/", "name": "usb", "read_only": False}
        body["printer"] = {
            "state": self.state,
            "temp_bed": 60.0,
            "target_bed": 60.0,
            "temp_nozzle": 215.0,
            "target_nozzle": 215.0,
            "axis_z": 1.2,
            "flow": 100,
            "speed": 100,
            "fan_hotend": 5000,
            "fan_print": 3000,
        }
        return web.Response(text=json.dumps(body), content_type="application/json")

    async def _job(self, request: web.Request) -> web.Response:
        if not self.has_job():
            return web.Response(status=204)
        job = self.job or {}
        path = job["path"]
        data = self.files.get(job["display_name"], b"")
        file = {
            "refs": {
                "icon": f"/thumb/s{path}",
                "thumbnail": f"/thumb/l{path}",
                "download": path,
            },
            "name": path.rsplit("/", 1)[-1],
            "display_name": job["display_name"],
            "path": "/usb",
            "size": len(data),
            "m_timestamp": 1793000000,
        }
        if "meta" in job:
            file["meta"] = job["meta"]
        return web.json_response(
            {
                "id": job["id"],
                "state": "PAUSED" if self.state == "ATTENTION" else self.state,
                "progress": job["progress"],
                "time_remaining": job["time_remaining"],
                "time_printing": job["time_printing"],
                "file": file,
            }
        )

    async def _download(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["path"]
        data = self.files.get(name)
        if data is None:
            raise web.HTTPNotFound()
        self.downloads.append(name)
        response = web.StreamResponse(
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(len(data))}
        )
        await response.prepare(request)
        try:
            for n in range(0, len(data), 4096):
                await response.write(data[n : n + 4096])
                self.downloaded_bytes += min(4096, len(data) - n)
                if self.chunk_delay:
                    await asyncio.sleep(self.chunk_delay)
        except (ConnectionResetError, RuntimeError):
            pass  # the bridge stopped reading
        return response

    async def _thumbnail(self, request: web.Request) -> web.Response:
        data = self.thumbnails.get(request.match_info["path"])
        if data is None:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="image/png")
