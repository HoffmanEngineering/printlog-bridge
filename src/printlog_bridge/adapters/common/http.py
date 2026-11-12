"""What the adapters' HTTP clients share: every request they send carries the printer's
credentials, so a path is only ever followed on the printer itself; and an answer is read only
up to a cap.

A gzipped answer can inflate a thousandfold, and aiohttp inflates all it has received at once,
before anything reads it, so a cap on what is read doesn't bound the memory spent. The clients'
sessions (`session`) leave answers compressed, and `inflated` inflates them a piece at a time,
as they are read: each cap then counts inflated bytes, and nothing is inflated past it.
"""

from __future__ import annotations

import zlib
from collections.abc import AsyncIterator

import aiohttp
from yarl import URL

from printlog_bridge import __version__

MAX_JSON_BYTES = 8 * 1024 * 1024
"""An API answer read whole, at most. A status is a few KB; OctoPrint's settings with many
plugins, or a Duet file's info with its thumbnails, some hundreds."""
CHUNK_BYTES = 64 * 1024
ACCEPT_ENCODING = "gzip, deflate"
"""The encodings `inflated` reads, and so all a client asks for."""


class TooLarge(aiohttp.ClientPayloadError):
    """The answer went on past the cap: not one the printer would send."""


def session() -> aiohttp.ClientSession:
    """A session for a printer's API: its answers are inflated by `inflated`, not by aiohttp."""
    return aiohttp.ClientSession(
        headers={
            "User-Agent": f"printlog-bridge/{__version__}",
            "Accept-Encoding": ACCEPT_ENCODING,
        },
        auto_decompress=False,
    )


async def inflated(
    response: aiohttp.ClientResponse, chunk_bytes: int = CHUNK_BYTES
) -> AsyncIterator[bytes]:
    """`response`'s body as it arrives, inflated, in pieces of at most `chunk_bytes`. Raises
    `aiohttp.ClientPayloadError` for an encoding it doesn't know or data that won't inflate."""
    encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if encoding in ("", "identity"):
        async for chunk in response.content.iter_chunked(chunk_bytes):
            yield chunk
        return
    if encoding not in ("gzip", "x-gzip", "deflate"):
        raise aiohttp.ClientPayloadError(f"an answer in an unknown encoding ({encoding})")
    inflater = zlib.decompressobj(32 + zlib.MAX_WBITS)  # a gzip or a zlib header
    try:
        async for chunk in response.content.iter_chunked(chunk_bytes):
            data = chunk
            while data:
                piece = inflater.decompress(data, chunk_bytes)
                if piece:
                    yield piece
                data = inflater.unconsumed_tail
        rest = inflater.flush()
    except zlib.error as e:
        raise aiohttp.ClientPayloadError(f"an answer that won't inflate ({e})") from None
    if rest:
        yield rest


async def read_capped(response: aiohttp.ClientResponse, limit: int) -> bytes:
    """`response`'s whole body, inflated. Raises `TooLarge` as soon as it passes `limit`
    bytes, before reading or inflating the rest."""
    data = bytearray()
    async for piece in inflated(response):
        data += piece
        if len(data) > limit:
            raise TooLarge(f"an answer of more than {limit} bytes")
    return bytes(data)


def url_on(base_url: str, path: str) -> str:
    """`path` on the server at `base_url` (which may have a path of its own, OctoPrint's behind
    a proxy). Raises `aiohttp.InvalidURL` for a path that would lead anywhere else: one not
    starting with `/`, or one that changes the URL's host, as `@host/x` would by making
    `base_url` the user name of another server."""
    if not path.startswith("/"):
        raise aiohttp.InvalidURL(path)
    url = base_url.rstrip("/") + path
    try:
        joined, base = URL(url), URL(base_url)
    except ValueError:
        raise aiohttp.InvalidURL(path) from None
    if _origin(joined) != _origin(base):
        raise aiohttp.InvalidURL(path)
    return url


def _origin(url: URL) -> tuple[object, ...]:
    return (url.scheme, url.host, url.port, url.user, url.password)
