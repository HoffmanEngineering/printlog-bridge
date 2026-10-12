"""Removing secrets from text the bridge writes down: the support bundle, and traces.

What the bridge knows to be a secret (its keys, from the config, the key file and the
environment) is replaced wherever it appears. So are passwords in URLs and query values named
like a secret, which can come from anywhere, such as a webcam URL Moonraker lists.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

REDACTED = "<redacted>"

URL_USERINFO = re.compile(r"(\w+://)[^/@\s\"'\\]+@")
_SECRET_QUERY_VALUE = re.compile(
    r"([?&][^=&\s\"'\\?]*(?:token|key|pass|pwd|secret|auth|sig)[^=&\s\"'\\?]*=)[^&#\s\"'<\\]+",
    re.IGNORECASE,
)
"""A query value named like a secret (`token`, `api_key`...), as in a webcam's URL."""


def scrub(text: str, secrets: Iterable[str]) -> str:
    """`text` without any of `secrets` (longest first), passwords in URLs, or secret-looking
    query values (`?token=...`, as a webcam URL can carry)."""
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    text = URL_USERINFO.sub(rf"\1{REDACTED}@", text)
    return _SECRET_QUERY_VALUE.sub(rf"\1{REDACTED}", text)
