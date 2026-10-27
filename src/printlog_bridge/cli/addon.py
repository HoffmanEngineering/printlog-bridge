"""`printlog-bridge-addon`: how the Home Assistant app starts the bridge.

The app image (the Dockerfile's `addon` stage) runs this as root. Home Assistant's Supervisor
writes the options the user set in the app's Configuration tab to `/data/options.json`, and
`/data` (root's) is the app's private, persistent folder. This:

1. writes the bridge's config, `/data/printlog-bridge.cfg` (mode 600), from the options. A
   `printlog-bridge.cfg` the user put in the app's config folder (`/config` in the container)
   is used instead, for settings the options don't have;
2. passes the device name, the API key and `trace` in the environment, and the local page's
   address: the app's ingress port, answering the Supervisor only. The API key is never
   written to a file;
3. gives `/data` and the state directory to the image's own user, and runs `printlog-bridge run`
   as that user, as the normal image does.

Nothing here prints an option's value: the log is the app's Log tab.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from printlog_bridge import __version__
from printlog_bridge.core.api_client import REPAIR_NEEDED
from printlog_bridge.core.config import (
    API_KEY_ENV,
    CONFIG_FILE_NAME,
    CONFIG_PATH_ENV,
    STATE_DIR_ENV,
    STATUS_PAGE_CLIENTS_ENV,
    STATUS_PAGE_ENV,
    TRACE_ENV,
)
from printlog_bridge.core.device import DEVICE_TOKEN_FILE, write_private_file
from printlog_bridge.core.pairing import NAME_ENV
from printlog_bridge.core.status import STATUS_FILE, read_status

DATA_DIR = Path("/data")
USER_CONFIG = Path("/config") / CONFIG_FILE_NAME
"""The app's public config folder (`map: app_config`), which the user can reach over Samba or
an editor app."""
OPTIONS_FILE = "options.json"
INGRESS_PORT = 7180
"""config.yaml's `ingress_port`: the local page (core/local_page.py) is the app's ingress page."""
INGRESS_PROXY = "172.30.32.2"
"""The Supervisor's address, the only one the app answers on that port. The app is on the host's
network (for discovery), so the port is open on every address of the host: everyone else gets
403, as Home Assistant's ingress documentation asks."""
STATE_DIR_NAME = "state"
BRIDGE_UID = BRIDGE_GID = 1000
"""The image's `printlog` user (Dockerfile)."""

ADAPTER_FIELDS: Mapping[str, Mapping[str, str]] = {
    # The app's option -> the bridge's config option, for each adapter.
    "moonraker": {"url": "url", "api_key": "moonraker_api_key"},
    "bambu": {"host": "host", "serial": "serial", "access_code": "access_code"},
    "prusalink": {
        "host": "host", "password": "password", "api_key": "api_key", "username": "username"
    },
}  # fmt: skip
REQUIRED: Mapping[str, Sequence[str]] = {"moonraker": ("url",)}
"""Without `url`, a Moonraker printer is watched over the local socket, which an app has not.
The other adapters say themselves what they miss."""
PRINTER_FIELDS = ("name", "adapter", "printer_id")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_INLINE_COMMENT = re.compile(r"(^|\s)[#;]")
"""The bridge's config reads ` #` and ` ;` as the start of a comment, and so a value that
starts with # or ; (after `key = `)."""


class OptionsError(Exception):
    """The options can't make a working config. The message says what to fix, never a value."""


def render_config(options: Mapping[str, Any]) -> str:
    """The bridge's config for the app's options."""
    printers = options.get("printers") or []
    if not printers:
        raise OptionsError(
            "Add your printers in the app's Configuration tab (or put a printlog-bridge.cfg in "
            "the app's config folder); there is nothing to watch yet."
        )
    lines = ["# Written by the Home Assistant app from its options at every start.", "[bridge]"]
    lines.append("update_check = false")  # Home Assistant shows the app's updates
    api_url = _text(options, "api_url", "api_url")
    if api_url:
        lines.append(f"api_url = {api_url}")

    names: set[str] = set()
    for printer in printers:
        lines.append("")
        lines.extend(_printer(printer, names))
    return "\n".join(lines) + "\n"


def _printer(printer: Mapping[str, Any], names: set[str]) -> list[str]:
    name = _text(printer, "name", "a printer's name")
    if not name:
        raise OptionsError("every printer needs a name")
    if "]" in name or "[" in name:
        raise OptionsError(f"printer {name!r}: a name can't contain [ or ]")
    if name in names:
        raise OptionsError(f"two printers are called {name!r}; give each its own name")
    names.add(name)

    adapter = _text(printer, "adapter", f"printer {name!r}'s adapter") or "moonraker"
    fields = ADAPTER_FIELDS.get(adapter)
    if fields is None:
        known = ", ".join(sorted(ADAPTER_FIELDS))
        raise OptionsError(f"printer {name!r}: unknown adapter {adapter!r} (one of {known})")

    where = f"printer {name!r} ({adapter})"
    lines = [f"[printer {name}]", f"adapter = {adapter}"]
    printer_id = printer.get("printer_id")
    if printer_id not in (None, ""):
        lines.append(f"printer_id = {_text(printer, 'printer_id', where)}")
    for option in printer:
        if option in PRINTER_FIELDS or option in fields:
            continue
        if _text(printer, option, f"{where} {option}"):
            raise OptionsError(f"{where} doesn't use {option}; clear it")
    for option, config_option in fields.items():
        value = _text(printer, option, f"{where} {option}")
        if value:
            lines.append(f"{config_option} = {value}")
        elif option in REQUIRED.get(adapter, ()):
            raise OptionsError(f"{where} needs {option}, such as http://192.168.1.20:7125")
    return lines


def _text(options: Mapping[str, Any], key: str, what: str) -> str:
    """An option as one line of the config: stripped, or empty when unset."""
    value = options.get(key)
    if value is None:
        return ""
    text = str(value).strip()
    if _CONTROL.search(text):
        raise OptionsError(f"{what} can't contain line breaks or control characters")
    if _INLINE_COMMENT.search(text):
        raise OptionsError(
            f"{what} can't start with # or ;, or have one after a space: the bridge's config "
            "reads the rest as a comment. Change it (on the printer, for a password)."
        )
    return text


def bridge_env(options: Mapping[str, Any], base: Mapping[str, str]) -> dict[str, str]:
    """The bridge's environment: `base` with the options that go there."""
    env = {k: v for k, v in base.items() if not (k == API_KEY_ENV and not v.strip())}
    name = str(options.get("device_name") or "").strip()
    if name:
        env[NAME_ENV] = name
    key = str(options.get("api_key") or "").strip()
    if key:
        env[API_KEY_ENV] = key
    if options.get("trace"):
        env[TRACE_ENV] = "1"
    # Set here, so a printlog-bridge.cfg in the app's config folder can't open it to the LAN.
    env[STATUS_PAGE_ENV] = f"0.0.0.0:{INGRESS_PORT}"
    env[STATUS_PAGE_CLIENTS_ENV] = INGRESS_PROXY
    return env


Chown = Callable[[Path, int, int], None]


def prepare(
    data: Path,
    user_config: Path,
    env: Mapping[str, str],
    *,
    uid: int,
    gid: int,
    chown: Chown,
) -> tuple[Path, dict[str, str]]:
    """Writes the config into `data` and gives `data` to `uid`. Returns the config's path and
    the bridge's environment."""
    options = _read_options(data / OPTIONS_FILE)
    config_path = data / CONFIG_FILE_NAME
    state_dir = data / STATE_DIR_NAME

    if user_config.is_file():
        text = user_config.read_text(encoding="utf-8-sig")
        say(f"Using {user_config} from the app's config folder.")
        if options.get("printers"):
            say("It ignores the printers in the app's options; remove them or the file.")
    else:
        text = render_config(options)
        names = [str(p.get("name", "")).strip() for p in options["printers"]]
        count = f"{len(names)} printer{'s' if len(names) != 1 else ''}"
        say(f"Watching {count} from the app's options ({', '.join(names)}).")
    if str(options.get("api_key") or "").strip():
        say("Signing in with the API key from the app's options, unless the bridge is paired.")

    write_private_file(config_path, text)
    state_dir.mkdir(mode=0o700, exist_ok=True)
    _forget_a_refused_token(state_dir)
    chown(data, uid, gid)
    chown(config_path, uid, gid)
    for folder, dirs, files in os.walk(state_dir):
        chown(Path(folder), uid, gid)
        for item in dirs + files:
            chown(Path(folder) / item, uid, gid)

    bridge = bridge_env(options, env)
    bridge[CONFIG_PATH_ENV] = str(config_path)
    bridge[STATE_DIR_ENV] = str(state_dir)
    return config_path, bridge


def _forget_a_refused_token(state_dir: Path) -> None:
    """An app has no shell to run `printlog-bridge pair` in, so restarting it is the re-pair:
    when the bridge last stopped because 3D Print Log refused its device token (removed in the
    app, or replaced), and the token is no newer than that, it is deleted, and `run` pairs on
    its own again. `device_id` stays, so 3D Print Log keeps one device for the bridge."""
    token = state_dir / DEVICE_TOKEN_FILE
    status_file = state_dir / STATUS_FILE
    status = read_status(state_dir)
    if status is None or status.get("credentials_problem") != REPAIR_NEEDED:
        return
    try:
        if token.stat().st_mtime > status_file.stat().st_mtime:
            return  # paired again since
        token.unlink()
    except FileNotFoundError:
        return
    say("3D Print Log no longer accepts this bridge's device token, so the bridge pairs again.")


def _read_options(path: Path) -> dict[str, Any]:
    try:
        options = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise OptionsError(
            f"{path} not found: printlog-bridge-addon runs only as a Home Assistant app. "
            "Elsewhere, run `printlog-bridge run`."
        ) from None
    except (OSError, ValueError) as e:
        raise OptionsError(f"can't read {path}: {e.__class__.__name__}") from None
    if not isinstance(options, dict):
        raise OptionsError(f"{path} isn't a JSON object")
    return options


def say(message: str) -> None:
    print(f"printlog-bridge app: {message}", flush=True)


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _chown(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid, follow_symlinks=False)


def _drop_to(uid: int, gid: int) -> None:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def _exec(argv: list[str], env: dict[str, str]) -> None:
    os.execvpe(argv[0], argv, env)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--version"]:
        print(f"printlog-bridge {__version__} (Home Assistant app)")
        return 0
    if args:
        print("usage: printlog-bridge-addon [--version]", file=sys.stderr)
        return 2
    root = _is_root()
    try:
        _, env = prepare(
            DATA_DIR,
            USER_CONFIG,
            dict(os.environ),
            uid=BRIDGE_UID,
            gid=BRIDGE_GID,
            chown=_chown if root else (lambda *_: None),
        )
    except OptionsError as e:
        print(f"printlog-bridge app: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"printlog-bridge app: can't prepare {DATA_DIR}: {e}", file=sys.stderr)
        return 2
    if root:
        _drop_to(BRIDGE_UID, BRIDGE_GID)
    _exec(["printlog-bridge", "run"], env)
    return 0  # only reached when _exec is replaced in tests


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
