"""Hiding the secrets in a URL, with nothing but the standard library.

Kept apart from `core.images` (which needs aiohttp) so that the config, and the container's
health check that reads it every minute, load without importing a network library.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET_QUERY = re.compile(r"(token|key|pass|pwd|secret|auth|sig|user)", re.IGNORECASE)
"""Query parameter names whose values are hidden."""


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
        if any(SECRET_QUERY.search(k) for k, _ in pairs):
            query = urlencode(
                [(k, "***" if SECRET_QUERY.search(k) else v) for k, v in pairs], safe="*"
            )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
