"""Loads `printlog-bridge.cfg`.

```ini
[bridge]
api_key = ...            ; or set PRINTLOG_API_KEY
api_url = https://api.3dprintlog.com
state_dir = ~/.local/share/printlog-bridge

[printer voron]          ; one section per printer
printer_id = 42          ; the printer in 3D Print Log (from its page's URL)
adapter = moonraker      ; the default
url = http://voron.local:7125
```
"""

from __future__ import annotations

import configparser
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from printlog_bridge.core.adapter import Adapter

DEFAULT_API_URL = "https://api.3dprintlog.com"
DEFAULT_ADAPTER = "moonraker"
CONFIG_FILE_NAME = "printlog-bridge.cfg"

API_KEY_ENV = "PRINTLOG_API_KEY"
CONFIG_PATH_ENV = "PRINTLOG_BRIDGE_CONFIG"

PRINTER_PREFIX = "printer"


class ConfigError(Exception):
    """The config file is missing or invalid. The message is shown to the user as is."""


@dataclass(frozen=True)
class PrinterConfig:
    name: str
    adapter: Adapter
    printer_id: int | None = None
    """The printer's id in 3D Print Log, which its prints are logged to. Pairing will set it."""


@dataclass(frozen=True)
class BridgeConfig:
    path: Path
    api_url: str
    api_key: str | None
    state_dir: Path
    printers: Sequence[PrinterConfig]


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
        with path.open(encoding="utf-8") as f:
            parser.read_file(f)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except configparser.Error as e:
        raise ConfigError(f"{path}: {e}") from None

    bridge = parser["bridge"] if parser.has_section("bridge") else {}

    api_url = bridge.get("api_url", DEFAULT_API_URL).strip().rstrip("/")
    if urlparse(api_url).scheme not in ("http", "https"):
        raise ConfigError(f"[bridge] api_url must be an http(s) URL, not {api_url!r}")

    api_key = (env.get(API_KEY_ENV) or bridge.get("api_key") or "").strip() or None

    state_dir_option = bridge.get("state_dir", "").strip()
    state_dir = (
        _expand(state_dir_option, home)
        if state_dir_option
        else home / ".local" / "share" / "printlog-bridge"
    )

    printers = [
        _load_printer(section, parser[section], adapters)
        for section in parser.sections()
        if section != "bridge"
    ]
    if not printers:
        raise ConfigError(f"{path}: no [printer <name>] section, so there is nothing to watch")

    return BridgeConfig(path, api_url, api_key, state_dir, printers)


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
