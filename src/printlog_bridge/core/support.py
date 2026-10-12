"""The support bundle `printlog-bridge doctor` writes: everything support needs, minus every secret.

Secrets are removed twice. The config file is redacted option by option (any option named like a
key, token, secret or password, and passwords in URLs), and then every known secret value, from
the config and the environment, is replaced wherever it appears in any file of the bundle, in
case one was ever written to the log or the status file.
"""

from __future__ import annotations

import configparser
import contextlib
import json
import platform
import re
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from pathlib import Path

from printlog_bridge import __version__
from printlog_bridge.core.config import API_KEY_ENV
from printlog_bridge.core.outbox import read_entries
from printlog_bridge.core.status import STATUS_FILE

BUNDLE_NAME = "printlog-bridge-support.zip"
LOG_FILES = ("bridge.log", "bridge.log.1")
REDACTED = "<redacted>"

_SECRET_OPTION = re.compile(r"(key|token|secret|password)", re.IGNORECASE)
_OPTION_LINE = re.compile(r"^(\s*)([^=:\s\[;#][^=:]*?)(\s*[=:]\s*)(.*)$")
_URL_USERINFO = re.compile(r"(\w+://)[^/@\s]+@")
_SECRET_ENV = (API_KEY_ENV,)
MIN_SECRET_LENGTH = 4
"""Shorter values aren't scrubbed everywhere: replacing every "abc" would garble the bundle."""


def write_support_bundle(
    path: Path,
    *,
    report: str,
    config_path: Path,
    state_dir: Path,
    env: Mapping[str, str],
) -> Path:
    config_text = _read(config_path)
    secrets = sorted(
        _secrets(config_text or "", env), key=len, reverse=True
    )  # longest first, so a secret containing another is replaced whole

    files: dict[str, str] = {
        "doctor.txt": report,
        "environment.txt": _environment(config_path, state_dir),
    }
    if config_text is not None:
        files["printlog-bridge.cfg"] = redact_config(config_text)
    for name in (STATUS_FILE, *LOG_FILES):
        text = _read(state_dir / name)
        if text is not None:
            files[name] = text
    for state in sorted((state_dir / "printers").glob("*.json")):
        text = _read(state)
        if text is not None:
            files[f"printers/{state.name}"] = text
    entries = read_entries(state_dir)
    if entries is not None:
        files["outbox.json"] = json.dumps(
            [{**asdict(e), "queued_at": e.queued_at.isoformat()} for e in entries], indent=1
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, text in files.items():
            bundle.writestr(name, _scrub(text, secrets))
    temporary.replace(path)
    return path


def redact_config(text: str) -> str:
    lines = []
    for line in text.splitlines():
        match = _OPTION_LINE.match(line)
        if match and _SECRET_OPTION.search(match.group(2)) and match.group(4).strip():
            line = f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}"
        lines.append(_URL_USERINFO.sub(rf"\1{REDACTED}@", line))
    return "\n".join(lines) + "\n"


def _secrets(config_text: str, env: Mapping[str, str]) -> set[str]:
    found = {env[name].strip() for name in _SECRET_ENV if env.get(name)}
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    with contextlib.suppress(configparser.Error):  # redact_config still catches it by line
        parser.read_string(config_text)
    for section in parser.sections():
        for option, value in parser[section].items():
            if _SECRET_OPTION.search(option):
                found.add(value.strip())
            found.update(m.group(1) for m in re.finditer(r"\w+://([^/@\s]+)@", value))
    return {s for s in found if len(s) >= MIN_SECRET_LENGTH}


def _scrub(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    return _URL_USERINFO.sub(rf"\1{REDACTED}@", text)


def _environment(config_path: Path, state_dir: Path) -> str:
    return "\n".join(
        [
            f"printlog-bridge {__version__}",
            f"Python {platform.python_version()} ({platform.python_implementation()})",
            f"Platform {platform.platform()}",
            f"Config {config_path}",
            f"State directory {state_dir}",
            "",
        ]
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
