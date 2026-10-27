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

`adopt_prints`, `create_prints` and `adopt_window` go in `[bridge]` for every printer, or in a
`[printer]` section for that one (see `Adoption`), and so does `slicer_settings`.
`max_attachment_mb` in `[bridge]` caps the files attached to prints (a timelapse video).

The API key is looked up in this order: the `PRINTLOG_API_KEY` environment variable, the key
file `<state_dir>/api_key` (which `printlog-bridge store-key` and install.sh write, mode 600),
then `api_key` in `[bridge]`. In local mode the config sits in Klipper's config folder, which
Moonraker serves to anyone who can open Mainsail or Fluidd, so the key file is the default.

A device token from `printlog-bridge pair` (`<state_dir>/device_token`, core/device.py) wins
over every API key: the bridge sends one credential, never both.
"""

from __future__ import annotations

import configparser
import contextlib
import errno
import io
import ipaddress
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.device import (
    DEVICE_TOKEN_FILE,
    DeviceFileError,
    DeviceToken,
    read_device_token,
    write_private_file,
)
from printlog_bridge.core.urls import mask_url

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
ADOPTION_OPTIONS = ("adopt_prints", "create_prints", "adopt_window")
DEFAULT_ADOPT_WINDOW_S = 12 * 60 * 60
DEFAULT_MAX_ATTACHMENT_MB = 100
MAX_ATTACHMENT_MB = 200
"""The API refuses a file attachment over 200 MB."""
_WINDOW = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd]?)$")
_WINDOW_UNITS = {"s": 1, "m": 60, "h": 60 * 60, "d": 24 * 60 * 60, "": 60 * 60}


class ConfigError(Exception):
    """The config file is missing or invalid. The message is shown to the user as is."""


@dataclass(frozen=True)
class Adoption:
    """What the bridge does when a job starts and the user already made its print (the Slic3r
    uploader, the Cura plugin, or by hand)."""

    adopt: bool = True
    """`adopt_prints`: log the job to a matching print in Printing instead of a new one."""
    create: bool = True
    """`create_prints`: create a print when there's none to adopt. False is finish-only mode,
    where a job with no print to adopt isn't logged."""
    window_s: float = DEFAULT_ADOPT_WINDOW_S
    """`adopt_window`: how recently the print must have been created (`12h`, `90m`, `2d`; a
    bare number is hours)."""


@dataclass(frozen=True)
class PrinterConfig:
    name: str
    adapter: Adapter
    printer_id: int | None = None
    """The printer's id in 3D Print Log, which its prints are logged to. Pairing will set it."""
    adoption: Adoption = field(default_factory=Adoption)
    slicer_settings: bool = True
    """`slicer_settings`: send each print's slicer settings, read from the header and the
    settings block of its file (#36). False: the file is never read."""


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
    max_attachment_mb: int = DEFAULT_MAX_ATTACHMENT_MB
    """A file to attach to a print (a timelapse video) larger than this is skipped."""
    update_check: bool = True
    """Say in the log, `status` and `doctor` when 3D Print Log reports a newer bridge (#39)."""
    stored_device: DeviceToken | None = None
    """The device token `printlog-bridge pair` stored, whichever API issued it."""

    @property
    def device(self) -> DeviceToken | None:
        """The device token to use: the stored one, when this config's API issued it."""
        if self.stored_device is None or self.stored_device.api_url != self.api_url:
            return None
        return self.stored_device

    @property
    def has_credentials(self) -> bool:
        return self.device is not None or self.api_key is not None

    @property
    def device_token_file(self) -> Path:
        return self.state_dir / DEVICE_TOKEN_FILE

    def describe_credentials(self) -> str:
        """The credential the bridge sends, for `doctor` and `status`."""
        if self.device is not None:
            return f"the device token in {self.device_token_file} (from printlog-bridge pair)"
        if self.api_key is not None:
            return f"the API key in {self.describe_api_key()}"
        return "none"

    def api_url_warning(self) -> str | None:
        """What to say, every time, when `api_url` is plain http to another computer, which
        only `allow_http_on_lan` allows."""
        parsed = urlparse(self.api_url)
        if parsed.scheme != "http" or _is_loopback(parsed.hostname or ""):
            return None
        return (
            f"api_url is {mask_url(self.api_url)}, plain http (allow_http_on_lan): the API key "
            "and your prints travel unencrypted on your network. Use it only for a development "
            "API."
        )

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
    text: str | None = None,
) -> BridgeConfig:
    """`text` is the config's contents when the caller has already read them (store-key, which
    checks who owns the file it read); otherwise they are read from `path`."""
    parser = _parse(path, text)
    bridge = parser["bridge"] if parser.has_section("bridge") else {}

    allow_http_on_lan = _flag(bridge.get("allow_http_on_lan", ""), "allow_http_on_lan") or _flag(
        env.get(ALLOW_HTTP_ON_LAN_ENV, ""), ALLOW_HTTP_ON_LAN_ENV
    )
    api_url = _api_url(bridge.get("api_url", DEFAULT_API_URL), allow_http_on_lan)

    state_dir = _state_dir(bridge, env, home)

    config_api_key = (bridge.get("api_key") or "").strip() or None
    api_key, source = _api_key(env, state_dir / API_KEY_FILE, config_api_key)
    try:
        stored_device = read_device_token(state_dir)
    except DeviceFileError as e:
        raise ConfigError(str(e)) from None

    adoption = _adoption("[bridge]", bridge, Adoption())
    slicer_settings = _slicer_settings("[bridge]", bridge, True)
    printers = [
        _load_printer(section, parser[section], adapters, adoption, slicer_settings)
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
        max_attachment_mb=_max_attachment_mb(bridge.get("max_attachment_mb")),
        update_check=_flag(bridge.get("update_check") or "true", "update_check"),
        stored_device=stored_device,
    )


def _max_attachment_mb(option: str | None) -> int:
    text = (option or "").strip()
    if not text:
        return DEFAULT_MAX_ATTACHMENT_MB
    if not text.isdigit() or not 1 <= int(text) <= MAX_ATTACHMENT_MB:
        raise ConfigError(
            f"[bridge] max_attachment_mb must be a number of megabytes from 1 to "
            f"{MAX_ATTACHMENT_MB} (3D Print Log's limit), not {text!r}"
        )
    return int(text)


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


_LAN_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # RFC 1918
        "169.254.0.0/16", "fe80::/10",  # link-local
        "fc00::/7",  # IPv6 unique local
    )
)  # fmt: skip
"""Named, not `is_private`: that also counts Teredo (2001::/32), which tunnels to the
internet, and documentation, benchmarking and reserved ranges, none of which is a LAN."""


def _is_private(host: str) -> bool:
    """An IP address on a private network (RFC 1918, link-local, IPv6 unique local). Names are
    never private here: what a name resolves to can change."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in _LAN_NETWORKS)


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
    write_private_file(path, clean_key(key))


def _load_printer(
    section: str,
    options: configparser.SectionProxy,
    adapters: Mapping[str, type[Adapter]],
    adoption: Adoption | None = None,
    slicer_settings: bool = True,
) -> PrinterConfig:
    kind, _, name = section.partition(" ")
    if kind != PRINTER_PREFIX:
        raise ConfigError(f"unknown section [{section}]; expected [bridge] or [printer <name>]")
    name = name.strip()
    if not name:
        raise ConfigError("[printer] needs a name, such as [printer voron]")

    values = dict(options)
    printer_id = _printer_id(name, values.pop("printer_id", None))
    adoption = _adoption(f"[printer {name}]", values, adoption or Adoption())
    for option in ADOPTION_OPTIONS:
        values.pop(option, None)
    slicer_settings = _slicer_settings(
        f"[printer {name}]", {"slicer_settings": values.pop("slicer_settings", "")}, slicer_settings
    )
    adapter_name = values.pop("adapter", DEFAULT_ADAPTER).strip()
    adapter_type = adapters.get(adapter_name)
    if adapter_type is None:
        known = ", ".join(sorted(adapters)) or "none"
        raise ConfigError(
            f"[printer {name}]: unknown adapter {adapter_name!r} (available: {known})"
        )

    return PrinterConfig(
        name, adapter_type.from_config(name, values), printer_id, adoption, slicer_settings
    )


def _adoption(where: str, options: Mapping[str, str], base: Adoption) -> Adoption:
    """`base` with what `options` sets of `adopt_prints`, `create_prints` and `adopt_window`."""

    def flag(option: str, default: bool) -> bool:
        text = (options.get(option) or "").strip()
        if not text:
            return default
        value = BOOLEANS.get(text.lower())
        if value is None:
            raise ConfigError(f"{where} {option} must be true or false, not {text!r}")
        return value

    window = (options.get("adopt_window") or "").strip()
    window_s = base.window_s
    if window:
        match = _WINDOW.match(window.lower())
        if match is None or float(match.group(1)) <= 0:
            raise ConfigError(
                f"{where} adopt_window must be a time such as 12h, 90m or 2d, not {window!r}"
            )
        window_s = float(match.group(1)) * _WINDOW_UNITS[match.group(2)]
    adoption = Adoption(
        adopt=flag("adopt_prints", base.adopt),
        create=flag("create_prints", base.create),
        window_s=window_s,
    )
    if not adoption.adopt and not adoption.create:
        raise ConfigError(
            f"{where} sets adopt_prints and create_prints both to false, so no print would ever "
            "be logged. Finish-only mode is create_prints = false with adopt_prints = true."
        )
    return adoption


def _slicer_settings(where: str, options: Mapping[str, str], default: bool) -> bool:
    text = (options.get("slicer_settings") or "").strip()
    if not text:
        return default
    value = BOOLEANS.get(text.lower())
    if value is None:
        raise ConfigError(f"{where} slicer_settings must be true or false, not {text!r}")
    return value


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


def state_dir_of(path: Path, env: Mapping[str, str], home: Path) -> Path:
    """The state directory the config at `path` uses, read without loading its printers: for the
    container's health check, which must not import the adapters (and aiohttp with them)."""
    parser = _parse(path, None)
    return _state_dir(parser["bridge"] if parser.has_section("bridge") else {}, env, home)


def _parse(path: Path, text: str | None) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        if text is not None:
            parser.read_string(text, source=str(path))
        else:
            with path.open(encoding="utf-8-sig") as f:
                parser.read_file(f)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except configparser.Error as e:
        raise ConfigError(f"{path}: {e}") from None
    return parser


def _state_dir(bridge: Mapping[str, str], env: Mapping[str, str], home: Path) -> Path:
    option = bridge.get("state_dir", "").strip() or (env.get(STATE_DIR_ENV) or "").strip()
    return _expand(option, home) if option else home / ".local" / "share" / "printlog-bridge"


def _expand(value: str, home: Path) -> Path:
    if value == "~" or value.startswith("~/"):
        return home / value[2:]
    return Path(os.path.expandvars(value))


_SECTION = re.compile(r"^\s*\[([^\]]*)\]")
_API_KEY_LINE = re.compile(r"^(\s*)api_key\s*[=:]", re.IGNORECASE)  # configparser ignores case


def clear_config_api_key(
    path: Path, *, folder_fd: int | None = None, identity: tuple[int, int] | None = None
) -> bool:
    """Empties `api_key` in the config's `[bridge]` section, leaving every other line (and its
    line ending) as it was. True when there was a line to empty. A symlink stays one: the file
    it points to is rewritten.

    With `folder_fd`, the config's folder held open (POSIX), the file is found and replaced in
    that folder, whatever has been renamed since, and only while it is still the file
    `identity` (device, inode) names; otherwise OSError."""
    if folder_fd is None:
        return _clear_by_path(Path(os.path.realpath(path)))
    name = path.name
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=folder_fd)
    with os.fdopen(fd, "rb") as f:
        found = os.fstat(f.fileno())
        if identity is not None and (found.st_dev, found.st_ino) != identity:
            raise OSError(errno.ESTALE, "it was replaced while store-key was running")
        # Split as readlines() would (only \n, \r and \r\n), keeping each line's ending.
        lines = io.StringIO(f.read().decode("utf-8"), newline="").readlines()
    if not _empty_api_key(lines):
        return False
    temporary = f".{name}.{os.urandom(6).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    out = os.open(temporary, flags, 0o600, dir_fd=folder_fd)
    try:
        with os.fdopen(out, "w", encoding="utf-8", newline="") as f:
            os.fchmod(f.fileno(), stat.S_IMODE(found.st_mode))
            f.writelines(lines)
        os.replace(temporary, name, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=folder_fd)
        raise
    return True


def _clear_by_path(path: Path) -> bool:
    with path.open(encoding="utf-8", newline="") as f:
        lines = f.readlines()
    if not _empty_api_key(lines):
        return False
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
    return True


_PRINTER_ID_LINE = re.compile(r"^(\s*)printer_id\s*[=:]", re.IGNORECASE)


def set_printer_ids(path: Path, ids: Mapping[str, int]) -> list[str]:
    """Sets `printer_id` in each named `[printer <name>]` section of the config, replacing the
    line that has one or adding it under the section's header, and leaving every other line (and
    its line ending) as it was. Returns the names it found no section for. A symlink stays one:
    the file it points to is rewritten, keeping its mode."""
    real = Path(os.path.realpath(path))
    with real.open(encoding="utf-8-sig", newline="") as f:
        lines = f.readlines()
    missing = []
    for name, printer_id in ids.items():
        if not _set_printer_id(lines, name, printer_id):
            missing.append(name)
    fd, temporary = tempfile.mkstemp(dir=str(real.parent), prefix=f".{real.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines)
        shutil.copymode(str(real), temporary)
        os.replace(temporary, str(real))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return missing


def _set_printer_id(lines: list[str], name: str, printer_id: int) -> bool:
    header = None
    section = None
    for i, line in enumerate(lines):
        match = _SECTION.match(line)
        if match:
            if header is not None:
                break  # the section ended without a printer_id line
            kind, _, title = match.group(1).strip().partition(" ")
            if kind == PRINTER_PREFIX and title.strip() == name:
                header, section = i, True
            continue
        if section and _PRINTER_ID_LINE.match(line):
            ending = line[len(line.rstrip("\r\n")) :] or "\n"
            indent = _PRINTER_ID_LINE.match(line).group(1)
            lines[i] = f"{indent}printer_id = {printer_id}{ending}"
            return True
    if header is None:
        return False
    ending = lines[header][len(lines[header].rstrip("\r\n")) :]
    if not ending:
        lines[header] += "\n"
        ending = "\n"
    lines.insert(header + 1, f"printer_id = {printer_id}{ending}")
    return True


def _empty_api_key(lines: list[str]) -> bool:
    """Empties, in place, each `api_key` line of `[bridge]`. True when there was one."""
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
    return changed
