"""What the adapters' HTTP clients share: every request they send carries the printer's
credentials, so a path is only ever followed on the printer itself."""

from __future__ import annotations

import aiohttp
from yarl import URL


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
