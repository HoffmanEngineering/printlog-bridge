"""Loads `printlog-bridge.cfg`.

```ini
[bridge]
api_key = ...            ; better: `printlog-bridge store-key` (see below), or PRINTLOG_API_KEY
api_url = https://api.3dprintlog.com
state_dir = ~/.local/share/printlog-bridge   ; or $PRINTLOG_BRIDGE_STATE_DIR when not set here

[printer voron]          ; one section per printer
printer_id = 42          ; the printer in 3D Print Log (from its page's URL)
adapter = moonraker      ; the default
url = http://voron.local:7125
```

The API key is looked up in this order: the `PRINTLOG_API_KEY` environment variable, the key
file `<state_dir>/api_key` (which `printlog-bridge store-key` and install.sh write, mode 600),
then `api_key` in `[bridge]`. In local mode the config sits in Klipper's config folder, which
Moonraker serves to anyone who can open Mainsail or Fluidd, so the key file is the default.
"""

from __future__ import annotations

import configparser
import contextlib
import ipaddress
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from printlog_bridge.core.adapter import Adapter

DEFAULT_API_URL = "https://api.3dprintlog.com"
ALLOW_HTTP_ON_LAN_ENV = "PRINTLOG_ALLOW_HTTP_ON_LAN"
DEFAULT_ADAPTER = "moonraker"
CONFIG_FILE_NAME = "printlog-bridge.cfg"

API_KEY_ENV = "PRINTLOG_API_KEY"
API_KEY_FILE = "api_key"
"""The key file's name in the state directory."""
CONFIG_PATH_ENV = "PRINTLOG_BRIDGE_CONFIG"
STATE_DIR_ENV = "PRINTLOG_BRIDGE_STATE_DIR"
"""The state directory when the config doesn't set `state_dir` (the Docker image sets it)."""
TRACE_ENV = "PRINTLOG_BRIDGE_TRACE"
"""`trace` without editing the config (for Docker); wins over `trace` in `[bridge]`."""
BOOLEANS = {
    "true": True, "yes": True, "on": True, "1": True,
    "false": False, "no": False, "off": False, "0": False,
}  # fmt: skip

PRINTER_PREFIX = "printer"
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*(s|m|h)?$")
_UNITS = {None: 1, "s": 1, "m": 60, "h": 3600}


class ConfigError(Exception):
    """The config file is missing or invalid. The message is shown to the user as is."""


@dataclass(frozen=True)
class PrinterConfig:
    name: str
    adapter: Adapter
    printer_id: int | None = None
    """The printer's id in 3D Print Log, which its prints are logged to. Pairing will set it."""


class ApiKeySource(Enum):
    ENVIRONMENT = "environment"
    KEY_FILE = "key file"
    CONFIG = "config"


@dataclass(frozen=True)
class BridgeConfig:
    path: Path
    api_url: str
    api_key: str | None
    state_dir: Path
    printers: Sequence[PrinterConfig]
    api_key_source: ApiKeySource | None = None
    """Where `api_key` came from, or None without one."""
    config_api_key: str | None = None
    """`api_key` in `[bridge]`, even when another source wins: doctor warns about it."""
    trace: bool = False
    """Record every message to and from each printer in `state_dir/traces` (core/trace.py)."""

    @property
    def api_key_file(self) -> Path:
        return self.state_dir / API_KEY_FILE

    def describe_api_key(self) -> str:
        """Where the key in use came from, for `doctor` and `status`."""
        if self.api_key_source is ApiKeySource.ENVIRONMENT:
            return f"the {API_KEY_ENV} environment variable"
        if self.api_key_source is ApiKeySource.KEY_FILE:
            return str(self.api_key_file)
        return f"api_key in [bridge] of {self.path}"


def find_config(explicit: Path | None, env: Mapping[str, str], home: Path) -> Path:
    """The config file to use: `--config`, then the environment, then the usual places.

    Returns the Klipper config folder's path when nothing exists, so the error names where the
    file is expected.
    """
    if explicit is not None:
        return explicit
    if env.get(CONFIG_PATH_ENV):
        return Path(env[CONFIG_PATH_ENV])

    candidates = [
        # Local mode: next to printer.cfg, where Mainsail and Fluidd can edit it.
        home / "printer_data" / "config" / CONFIG_FILE_NAME,
        home / ".config" / "printlog-bridge" / CONFIG_FILE_NAME,
    ]
    return next((c for c in candidates if c.is_file()), candidates[0])


def load_config(
    path: Path,
    adapters: Mapping[str, type[Adapter]],
    env: Mapping[str, str],
    home: Path,
) -> BridgeConfig:
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        with path.open(encoding="utf-8-sig") as f:
            parser.read_file(f)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except configparser.Error as e:
        raise ConfigError(f"{path}: {e}") from None

    bridge = parser["bridge"] if parser.has_section("bridge") else {}

    allow_http_on_lan = _flag(bridge.get("allow_http_on_lan", ""), "allow_http_on_lan") or _flag(
        env.get(ALLOW_HTTP_ON_LAN_ENV, ""), ALLOW_HTTP_ON_LAN_ENV
    )
    api_url = _api_url(bridge.get("api_url", DEFAULT_API_URL), allow_http_on_lan)

    state_dir_option = bridge.get("state_dir", "").strip() or (env.get(STATE_DIR_ENV) or "").strip()
    state_dir = (
        _expand(state_dir_option, home)
        if state_dir_option
        else home / ".local" / "share" / "printlog-bridge"
    )

    config_api_key = (bridge.get("api_key") or "").strip() or None
    api_key, source = _api_key(env, state_dir / API_KEY_FILE, config_api_key)

    printers = [
        _load_printer(section, parser[section], adapters)
        for section in parser.sections()
        if section != "bridge"
    ]
    if not printers:
        raise ConfigError(f"{path}: no [printer <name>] section, so there is nothing to watch")

    return BridgeConfig(
        path,
        api_url,
        api_key,
        state_dir,
        printers,
        source,
        config_api_key,
        trace=_trace(bridge.get("trace"), env.get(TRACE_ENV)),
    )


def _trace(option: str | None, variable: str | None) -> bool:
    if variable is not None and variable.strip():
        value = BOOLEANS.get(variable.strip().lower())
        if value is None:
            raise ConfigError(f"{TRACE_ENV} must be 1 or 0 (true or false), not {variable!r}")
        return value
    if option is None or not option.strip():
        return False
    value = BOOLEANS.get(option.strip().lower())
    if value is None:
        raise ConfigError(f"[bridge] trace must be true or false, not {option.strip()!r}")
    return value


def parse_duration(text: str) -> float | None:
    """Seconds from `90`, `90s`, `3m` or `1h`, or None when `text` isn't one."""
    match = _DURATION.match(text.strip().lower())
    if match is None:
        return None
    return float(match.group(1)) * _UNITS[match.group(2)]


def _api_url(value: str, allow_http_on_lan: bool = False) -> str:
    """3D Print Log's address. Plain http is refused except to this computer itself (a test
    server, or a proxy on the same host), so the key and the prints only ever travel over TLS.
    `allow_http_on_lan` also allows it to a private-network address, such as a development API
    on another computer; a public address is still refused."""
    api_url = value.strip().rstrip("/")
    parsed = urlparse(api_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(f"[bridge] api_url must be an https URL, not {api_url!r}")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        if _is_private(parsed.hostname):
            if not allow_http_on_lan:
                raise ConfigError(
                    f"[bridge] api_url must use https, not {api_url!r}. For a development API "
                    "on your own network, set allow_http_on_lan = true in [bridge] (or "
                    f"{ALLOW_HTTP_ON_LAN_ENV}=1): the API key then travels unencrypted on it."
                )
        else:
            raise ConfigError(
                f"[bridge] api_url must use https, not {api_url!r}: the API key and your prints "
                "are never sent unencrypted. (Plain http is allowed only to this computer, or "
                "to a private-network address with allow_http_on_lan.)"
            )
    return api_url


def _is_private(host: str) -> bool:
    """An IP address on a private network (RFC 1918, link-local, IPv6 unique local). Names are
    never private here: what a name resolves to can change."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private and not address.is_loopback and not address.is_unspecified


def _flag(value: str, name: str) -> bool:
    text = value.strip().lower()
    if text in ("", "0", "false", "no", "off"):
        return False
    if text in ("1", "true", "yes", "on"):
        return True
    raise ConfigError(f"{name} must be true or false, not {value!r}")


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _api_key(
    env: Mapping[str, str], key_file: Path, config_api_key: str | None
) -> tuple[str | None, ApiKeySource | None]:
    from_env = (env.get(API_KEY_ENV) or "").strip()
    if from_env:
        return from_env, ApiKeySource.ENVIRONMENT
    from_file = read_key_file(key_file)
    if from_file:
        return from_file, ApiKeySource.KEY_FILE
    if config_api_key:
        return config_api_key, ApiKeySource.CONFIG
    return None, None


def read_key_file(path: Path) -> str | None:
    """The key in a key file, or None when there is none."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None  # no state directory yet, or a broken one: doctor's own check says so
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"can't read the API key file {path} ({API_KEY_FILE}): {e}") from None
    return clean_key(text) or None


def clean_key(text: str) -> str:
    """A key as typed, piped or saved, without surrounding whitespace or a leading UTF-8 byte
    order mark (PowerShell can put one at the start of what it pipes; editors may save one)."""
    return text.strip().removeprefix("﻿").strip()


def write_key_file(path: Path, key: str) -> None:
    """Writes the key file so only this user can read it (mode 600), replacing any earlier key in
    one step. A state directory it has to make is made mode 700."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(clean_key(key) + "\n")
    os.replace(str(temporary), str(path))


def _load_printer(
    section: str,
    options: configparser.SectionProxy,
    adapters: Mapping[str, type[Adapter]],
) -> PrinterConfig:
    kind, _, name = section.partition(" ")
    if kind != PRINTER_PREFIX:
        raise ConfigError(f"unknown section [{section}]; expected [bridge] or [printer <name>]")
    name = name.strip()
    if not name:
        raise ConfigError("[printer] needs a name, such as [printer voron]")

    values = dict(options)
    printer_id = _printer_id(name, values.pop("printer_id", None))
    adapter_name = values.pop("adapter", DEFAULT_ADAPTER).strip()
    adapter_type = adapters.get(adapter_name)
    if adapter_type is None:
        known = ", ".join(sorted(adapters)) or "none"
        raise ConfigError(
            f"[printer {name}]: unknown adapter {adapter_name!r} (available: {known})"
        )

    return PrinterConfig(name, adapter_type.from_config(name, values), printer_id)


def _printer_id(printer: str, value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    if not text.isdigit() or int(text) == 0:
        raise ConfigError(
            f"[printer {printer}] printer_id must be the number at the end of the printer's page "
            f"in 3D Print Log (https://www.3dprintlog.com/printers/<number>), not {text!r}"
        )
    return int(text)


def _expand(value: str, home: Path) -> Path:
    if value == "~" or value.startswith("~/"):
        return home / value[2:]
    return Path(os.path.expandvars(value))


_SECTION = re.compile(r"^\s*\[([^\]]*)\]")
_API_KEY_LINE = re.compile(r"^(\s*)api_key\s*[=:]", re.IGNORECASE)  # configparser ignores case


def clear_config_api_key(path: Path) -> bool:
    """Empties `api_key` in the config's `[bridge]` section, leaving every other line (and its
    line ending) as it was. True when there was a line to empty."""
    with path.open(encoding="utf-8", newline="") as f:
        lines = f.readlines()
    section = None
    changed = False
    for i, line in enumerate(lines):
        header = _SECTION.match(line)
        if header:
            section = header.group(1).strip()
            continue
        match = _API_KEY_LINE.match(line) if section == "bridge" else None
        if match:
            ending = line[len(line.rstrip("\r\n")) :]
            lines[i] = f"{match.group(1)}api_key ={ending}"
            changed = True
    if changed:
        fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.writelines(lines)
            shutil.copymode(str(path), temporary)
            os.replace(temporary, str(path))
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    return changed
