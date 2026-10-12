"""Getting a picture of a print from the printer: a file or a URL, bounded in size and time.

Adapters use these to fetch a job's slicer thumbnail or a webcam snapshot. Neither may hold up
logging the print, so every fetch has a time limit and a size limit, and fails with
`ImageUnavailable`, never anything else. Webcam URLs (and Moonraker's, in remote mode) can carry
a password or a token, so a URL only ever reaches a message, the log or the status file through
`mask_url`, and an error that might repeat one through `scrub_url`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from printlog_bridge import __version__

MAX_IMAGE_BYTES = 10 * 1024 * 1024
"""The API refuses larger images ("Image must be under 10MB")."""

_SECRET_QUERY = re.compile(r"(token|key|pass|pwd|secret|auth|sig|user)", re.IGNORECASE)
_CHUNK = 64 * 1024


class ImageUnavailable(Exception):
    """The picture couldn't be had. The message is safe to log: it never holds a password."""


def image_type(data: bytes) -> str | None:
    """The content type of an image the API accepts (jpeg, png, gif, webp, bmp), from its bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return None


EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def mask_url(url: str) -> str:
    """The URL with any user and password, and query values that look like secrets, hidden."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<an invalid URL>"
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(_SECRET_QUERY.search(k) for k, _ in pairs):
            query = urlencode(
                [(k, "***" if _SECRET_QUERY.search(k) else v) for k, v in pairs], safe="*"
            )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


async def fetch_image(
    url: str,
    *,
    timeout: float,
    max_bytes: int = MAX_IMAGE_BYTES,
    headers: Mapping[str, str] | None = None,
) -> bytes:
    """GETs one image. A user and password in the URL are sent as basic auth."""
    shown = mask_url(url)
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": f"printlog-bridge/{__version__}"},
        ) as session:
            data = await _download(session, url, shown, max_bytes, headers)
    except asyncio.TimeoutError:
        raise ImageUnavailable(f"{shown} didn't answer within {timeout:g}s") from None
    except (aiohttp.ClientError, OSError, ValueError) as e:
        raise ImageUnavailable(f"can't reach {shown}: {scrub_url(e, url)}") from None
    if image_type(bytes(data)) is None:
        raise ImageUnavailable(f"{shown} sent something that is not an image")
    return bytes(data)


async def _download(
    session: aiohttp.ClientSession,
    url: str,
    shown: str,
    max_bytes: int,
    headers: Mapping[str, str] | None,
) -> bytearray:
    async with session.get(url, headers=dict(headers or {})) as response:
        if response.status != 200:
            raise ImageUnavailable(f"{shown} answered {response.status}")
        if response.content_length is not None and response.content_length > max_bytes:
            raise ImageUnavailable(f"{shown} sent an image larger than {max_bytes} bytes")
        data = bytearray()
        async for chunk in response.content.iter_chunked(_CHUNK):
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ImageUnavailable(f"{shown} sent an image larger than {max_bytes} bytes")
        return data


async def read_image_file(path: Path, *, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
    """Reads an image file, refusing one larger than `max_bytes` before reading it."""

    def read() -> bytes:
        size = path.stat().st_size
        if size > max_bytes:
            raise ImageUnavailable(f"{path} is larger than {max_bytes} bytes")
        with path.open("rb") as f:
            data = f.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ImageUnavailable(f"{path} is larger than {max_bytes} bytes")
        return data

    try:
        data = await asyncio.get_running_loop().run_in_executor(None, read)
    except OSError as e:
        raise ImageUnavailable(f"can't read {path}: {e.strerror or e}") from None
    if image_type(data) is None:
        raise ImageUnavailable(f"{path} is not an image the API accepts")
    return data


def scrub_url(error: BaseException, url: str | None) -> str:
    """The error's message, without the URL's user, password or secret query values should it
    repeat them."""
    text = str(error) or type(error).__name__
    if not url:
        return text
    text = text.replace(url, mask_url(url))
    try:
        parts = urlsplit(url)
        secrets = [parts.password, parts.username]
        secrets += [
            value
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if _SECRET_QUERY.search(key)
        ]
    except ValueError:
        secrets = []
    for secret in secrets:
        if secret and len(secret) >= 3:
            text = text.replace(secret, "***")
    return text
