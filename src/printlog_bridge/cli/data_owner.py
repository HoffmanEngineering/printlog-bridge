"""In the Docker image, every `printlog-bridge` command starts as root, gives the data folder to
the user the bridge runs as, and switches to that user before doing anything else (#99).

That user is `PUID`/`PGID` when set (Unraid's convention: 99:100, nobody:users), else the data
folder's own owner when it isn't root (a Synology folder made in File Station belongs to the
DSM user, so nothing changes), else the image's `printlog` user, 1000:1000 (a folder Docker made
for a bind mount belongs to root). Whatever in the folder belongs to anyone else is handed over,
so `Permission denied: '/data/state'` can't happen whichever way the folder was made.

It runs only in the image, where `PRINTLOG_BRIDGE_DATA_DIR` names the folder, and only as root:
a container started with `user: "<uid>:<gid>"` (installs from before this) runs as that user,
exactly as before. `docker exec` runs as root too, and so switches the same way, so a command
run there never leaves a root-owned file behind.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

DATA_DIR_ENV = "PRINTLOG_BRIDGE_DATA_DIR"
"""Set by the Docker image to its data folder, /data."""
PUID_ENV, PGID_ENV = "PUID", "PGID"
IMAGE_UID = IMAGE_GID = 1000
"""The image's `printlog` user (Dockerfile)."""

Owner = Callable[[Path], "tuple[int, int]"]
Chown = Callable[[Path, int, int], None]


class OwnerError(Exception):
    """PUID or PGID isn't a user or group id. The message says which."""


def choose_owner(env: Mapping[str, str], folder: tuple[int, int]) -> tuple[int, int]:
    """The uid and gid to run as, given the environment and the data folder's owner."""
    puid = _id(env, PUID_ENV)
    if puid is not None:
        pgid = _id(env, PGID_ENV)
        return puid, puid if pgid is None else pgid
    if folder[0] != 0:
        return folder
    return IMAGE_UID, IMAGE_GID


def _id(env: Mapping[str, str], name: str) -> int | None:
    text = (env.get(name) or "").strip()
    if not text:
        return None
    if not text.isdigit():
        raise OwnerError(f"{name} must be a number (a user or group id, such as 99), not {text!r}")
    return int(text)


def hand_over(folder: Path, uid: int, gid: int, *, owner: Owner, chown: Chown) -> int:
    """Gives `folder` and everything in it that isn't already `uid:gid`'s to them, never
    following a symlink. Returns how many it changed."""
    changed = 0
    paths = [folder]
    for parent, dirs, files in os.walk(folder):
        paths.extend(Path(parent) / name for name in dirs + files)
    for path in paths:
        if owner(path) != (uid, gid):
            chown(path, uid, gid)
            changed += 1
    return changed


def drop_root(env: Mapping[str, str] | None = None) -> None:
    """In the image as root: hands the data folder over and switches user (see the module).
    Elsewhere, does nothing."""
    env = os.environ if env is None else env
    folder = Path(env.get(DATA_DIR_ENV) or "")
    if not env.get(DATA_DIR_ENV) or not _is_root() or not folder.is_dir():
        return
    try:
        uid, gid = choose_owner(env, _owner(folder))
    except OwnerError as e:
        print(f"printlog-bridge: {e}", file=sys.stderr)
        sys.exit(2)
    if uid == 0:
        return  # PUID=0: the user asked for root
    try:
        changed = hand_over(folder, uid, gid, owner=_owner, chown=_chown)
    except OSError as e:
        print(
            f"printlog-bridge: couldn't give {folder} to {uid}:{gid} ({e.strerror or e}). If the "
            f"bridge can't write it, set {PUID_ENV} and {PGID_ENV} to the folder's owner.",
            file=sys.stderr,
        )
    else:
        if changed:
            print(
                f"printlog-bridge: gave {folder} to {uid}:{gid}, the user the bridge runs as "
                f"({changed} file{'s' if changed != 1 else ''} or folder"
                f"{'s' if changed != 1 else ''} changed).",
                file=sys.stderr,
            )
    _switch_to(uid, gid)


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _owner(path: Path) -> tuple[int, int]:
    info = os.lstat(path)
    return info.st_uid, info.st_gid


def _chown(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid, follow_symlinks=False)


def _switch_to(uid: int, gid: int) -> None:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
