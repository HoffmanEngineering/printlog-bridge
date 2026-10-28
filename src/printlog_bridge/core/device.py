"""The bridge's own identity and its device token, in the state directory (#2, #103).

- `device_id`: a UUID made the first time the bridge pairs, and kept. It is this install's
  identity in 3D Print Log: pairing again sends the same id, so the API rotates the token on the
  same device grant instead of adding a second one. Not a secret.
- `device_token`: what `printlog-bridge pair` got from the API, as JSON (`token`, the `apiUrl` it
  was issued by, `grantId`, `name`, `scopes`, `pairedAt`). Mode 600, like the API key file, and
  never in a folder a printer's host serves. The token is used only with the API that issued it,
  so a changed `api_url` never sends it anywhere else.

Nothing here imports the rest of the bridge: the config loads it, and the container's health
check loads the config.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import math
import os
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

DEVICE_ID_FILE = "device_id"
DEVICE_TOKEN_FILE = "device_token"
TOKEN_PREFIX = "pld_"
PENDING_PAIRING_FILE = "pairing_pending.json"
"""The code a pairing waits on, while it waits (`write_pending_pairing`)."""


class DeviceFileError(Exception):
    """The device token file is there but can't be read or makes no sense."""


@dataclass(frozen=True)
class DeviceToken:
    token: str
    api_url: str
    """The API that issued it, and the only one it is sent to."""
    grant_id: int | None = None
    name: str | None = None
    scopes: tuple[str, ...] = ()
    paired_at: str | None = None

    def __repr__(self) -> str:  # never the token itself
        return f"DeviceToken(grant {self.grant_id}, {self.api_url!r})"

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": self.token,
                "apiUrl": self.api_url,
                "grantId": self.grant_id,
                "name": self.name,
                "scopes": list(self.scopes),
                "pairedAt": self.paired_at,
            }
        )


def read_device_id(state_dir: Path) -> str | None:
    try:
        text = (state_dir / DEVICE_ID_FILE).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, NotADirectoryError):
        return None
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return None  # damaged: a new id is made, and the old grant can be revoked in the app


def device_id(state_dir: Path) -> str:
    """This bridge's device id, made and stored the first time it's asked for."""
    found = read_device_id(state_dir)
    if found is not None:
        return found
    made = str(uuid.uuid4())
    write_private_file(state_dir / DEVICE_ID_FILE, made)
    return made


def read_device_token(state_dir: Path) -> DeviceToken | None:
    """The stored device token, or None when the bridge hasn't paired. Raises DeviceFileError
    when the file is there but unreadable or damaged."""
    path = state_dir / DEVICE_TOKEN_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise DeviceFileError(f"can't read the device token file {path}: {e}") from None
    try:
        data = json.loads(text)
        token = data["token"]
        api_url = data["apiUrl"]
    except (ValueError, KeyError, TypeError):
        raise DeviceFileError(
            f"{path} is damaged. Run `printlog-bridge pair` to pair the bridge again."
        ) from None
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        raise DeviceFileError(f"{path} holds no device token. Run `printlog-bridge pair`.")
    scopes = data.get("scopes")
    grant_id = data.get("grantId")
    return DeviceToken(
        token=token,
        api_url=str(api_url).rstrip("/"),
        grant_id=grant_id if isinstance(grant_id, int) else None,
        name=data.get("name") if isinstance(data.get("name"), str) else None,
        scopes=tuple(s for s in scopes if isinstance(s, str)) if isinstance(scopes, list) else (),
        paired_at=data.get("pairedAt") if isinstance(data.get("pairedAt"), str) else None,
    )


def write_device_token(
    state_dir: Path,
    *,
    token: str,
    api_url: str,
    grant_id: int | None,
    name: str | None,
    scopes: Sequence[str],
    now: datetime | None = None,
) -> DeviceToken:
    stored = DeviceToken(
        token=token,
        api_url=api_url.rstrip("/"),
        grant_id=grant_id,
        name=name,
        scopes=tuple(scopes),
        paired_at=(now or datetime.now(timezone.utc)).isoformat(),
    )
    write_private_file(state_dir / DEVICE_TOKEN_FILE, stored.to_json())
    return stored


def write_private_file(path: Path, text: str) -> None:
    """Writes `text` (and a newline) so only this user can read it (mode 600), replacing any
    earlier file in one step. A state directory it has to make is made mode 700."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    # A folder that hands down an ACL ignores that mode: on a Synology share the file came out
    # readable by everyone the share lets read. An explicit chmod replaces the ACL with mode 600.
    if hasattr(os, "fchmod"):
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)
            raise
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.strip() + "\n")
    os.replace(str(temporary), str(path))


@dataclass(frozen=True)
class PendingPairing:
    """What the user needs to approve the bridge: never the device code, which polls for the
    token."""

    user_code: str
    verification_uri: str
    verification_uri_complete: str | None


_USER_CODE = re.compile(r"[A-Za-z0-9-]{1,32}")
_URL_CHARS = re.compile(r"[!-~]{1,2000}")


def _approval_url(url: object) -> bool:
    """https (or http to this computer, a test tenant), in printable ASCII only: the address
    reaches a terminal (the one-line Docker install shows it), so nothing that could steer it."""
    if not isinstance(url, str) or not _URL_CHARS.fullmatch(url):
        return False
    parts = urlsplit(url)
    if not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme != "http":
        return False
    if parts.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        return False


def write_pending_pairing(
    state_dir: Path,
    user_code: str,
    verification_uri: str,
    verification_uri_complete: str | None,
    expires_in: float,
    *,
    now: Callable[[], float] = time.time,
) -> None:
    """Keeps the code a pairing waits on in the state directory, until `clear_pending_pairing`,
    for `printlog-bridge status` (which `docker exec` can run, unlike the local page's memory).
    Mode 600, like the token it leads to. A non-finite `expires_in` raises ValueError: the file
    would never expire."""
    if not math.isfinite(expires_in):
        raise ValueError(f"a pairing code must expire (expires_in {expires_in})")
    write_private_file(
        state_dir / PENDING_PAIRING_FILE,
        json.dumps(
            {
                "userCode": user_code,
                "verificationUri": verification_uri,
                "verificationUriComplete": verification_uri_complete,
                "expiresAt": now() + expires_in,
            }
        ),
    )


def clear_pending_pairing(state_dir: Path) -> None:
    with contextlib.suppress(FileNotFoundError, NotADirectoryError):
        (state_dir / PENDING_PAIRING_FILE).unlink()


def read_pending_pairing(
    state_dir: Path, *, now: Callable[[], float] = time.time
) -> PendingPairing | None:
    """The code waiting for approval, or None: none, expired, or not a code and an address."""
    try:
        data = json.loads((state_dir / PENDING_PAIRING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    code, uri, complete = (
        data.get("userCode"),
        data.get("verificationUri"),
        data.get("verificationUriComplete"),
    )
    expires = data.get("expiresAt")
    if not isinstance(code, str) or not _USER_CODE.fullmatch(code) or not _approval_url(uri):
        return None
    if not isinstance(expires, (int, float)) or not math.isfinite(expires) or now() >= expires:
        return None
    return PendingPairing(code, str(uri), complete if _approval_url(complete) else None)
