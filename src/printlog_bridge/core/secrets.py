"""Removing secrets from text the bridge writes down: the support bundle, and traces.

What the bridge knows to be a secret (its keys, from the config, the key file and the
environment) is replaced wherever it appears. So are passwords in URLs and query values named
like a secret, which can come from anywhere, such as a webcam URL Moonraker lists.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

REDACTED = "<redacted>"

URL_USERINFO = re.compile(r"(?<![\w+.-])([A-Za-z][\w+.-]*://)[^/@\s\"'\\]+@")
"""The user and password in a URL. The scheme can only start where a word does, so a long
word is scanned once, not once from each of its letters."""
_QUERY_VALUE = re.compile(r"([?&])([^=&\s\"'\\?#]*)=([^&#\s\"'<\\]+)")
"""Each `name=value` of a query. A name can't hold `?` or `&`, so no two of them overlap and
the text is scanned once, however long."""
SECRET_QUERY_NAME = re.compile(r"token|key|pass|pwd|secret|auth|sig", re.IGNORECASE)
"""A query value named like a secret (`token`, `api_key`...), as in a webcam's URL."""


def mask_query_values(text: str, names: re.Pattern[str], replacement: str) -> str:
    """`text` with the value of every query parameter whose name `names` finds replaced."""

    def mask(match: re.Match[str]) -> str:
        if not names.search(match.group(2)):
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}={replacement}"

    return _QUERY_VALUE.sub(mask, text)


def scrub(text: str, secrets: Iterable[str]) -> str:
    """`text` without any of `secrets` (longest first), passwords in URLs, or secret-looking
    query values (`?token=...`, as a webcam URL can carry)."""
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    text = URL_USERINFO.sub(rf"\1{REDACTED}@", text)
    return mask_query_values(text, SECRET_QUERY_NAME, REDACTED)
