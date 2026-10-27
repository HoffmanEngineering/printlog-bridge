"""The support bundle `printlog-bridge doctor` writes: everything support needs, minus every secret.

Secrets are removed twice. The config file is redacted option by option (any option named like a
key, token, secret, password or access code, and passwords in URLs), and then every known secret
value, from the config, the key file, the device token file and the environment, is replaced
wherever it appears in any file of the bundle, in case one was ever written to the log or the
status file. The key file, the device token file and the encryption key
(`<state_dir>/api_key`, `<state_dir>/device_token`, `<state_dir>/credential-key.pem`) are never
bundled, nor is `bridge.json`; the encryption key's lines, and every credential the app sealed to
it (opened here only to be found and removed) are scrubbed too. With `trace = true`, the newest
traces are bundled too, up to `TRACE_BYTES`; they were scrubbed when written, and are scrubbed
again here.

The outbox goes in as `outbox.json`, less the text of any slicer settings waiting to be sent
(the file's header and settings block): only their size is kept.
"""

from __future__ import annotations

import configparser
import contextlib
import json
import platform
import re
import zipfile
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from printlog_bridge import __version__
from printlog_bridge.core import credentials
from printlog_bridge.core.config import API_KEY_ENV, API_KEY_FILE
from printlog_bridge.core.device import DeviceFileError, read_device_token
from printlog_bridge.core.outbox import Entry, read_entries
from printlog_bridge.core.secrets import REDACTED, URL_USERINFO, scrub
from printlog_bridge.core.slicer_settings import OUTBOX_KIND, TEXT_FIELDS
from printlog_bridge.core.status import STATUS_FILE
from printlog_bridge.core.trace import TRACE_DIR, trace_files

BUNDLE_NAME = "printlog-bridge-support.zip"
LOG_FILES = ("bridge.log", "bridge.log.1")
TRACE_BYTES = 20 * 1024 * 1024
"""How much of the newest traces (`trace = true`) goes into the bundle, before compression."""

SECRET_OPTION = re.compile(r"(key|token|secret|password|access_code)", re.IGNORECASE)
"""A config option whose value is a secret: an API key, a password, a Bambu printer's LAN
access code."""
_OPTION_LINE = re.compile(r"^(\s*)([^=:\s\[;#][^=:]*?)(\s*[=:]\s*)(.*)$")
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
    trace_bytes: int = TRACE_BYTES,
) -> Path:
    config_text = _read(config_path)
    secrets = known_secrets(config_path, state_dir, env)

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
            [
                {**asdict(e), "queued_at": e.queued_at.isoformat(), "payload": _payload(e)}
                for e in entries
            ],
            indent=1,
        )
    files.update(_latest_traces(state_dir, trace_bytes))

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, text in files.items():
            bundle.writestr(name, scrub(text, secrets))
    temporary.replace(path)
    return path


def redact_config(text: str) -> str:
    lines = []
    for line in text.splitlines():
        match = _OPTION_LINE.match(line)
        if match and SECRET_OPTION.search(match.group(2)) and match.group(4).strip():
            line = f"{match.group(1)}{match.group(2)}{match.group(3)}{REDACTED}"
        lines.append(URL_USERINFO.sub(rf"\1{REDACTED}@", line))
    return "\n".join(lines) + "\n"


def known_secrets(config_path: Path, state_dir: Path, env: Mapping[str, str]) -> list[str]:
    """Every secret the bridge knows of: the config's keys, tokens and passwords (URL passwords
    among them), the API key from the environment and from the key file, and the device token.
    Longest first, so a secret containing another is replaced whole."""
    found = _secrets(_read(config_path) or "", env)
    key_file = (_read(state_dir / API_KEY_FILE) or "").strip()
    if len(key_file) >= MIN_SECRET_LENGTH:
        found.add(key_file)
    try:
        device = read_device_token(state_dir)
    except DeviceFileError:
        device = None
    if device is not None:
        found.add(device.token)
    found.update(_sealed_secrets(state_dir))
    return sorted((s for s in found if len(s) >= MIN_SECRET_LENGTH), key=len, reverse=True)


def _sealed_secrets(state_dir: Path) -> set[str]:
    """The encryption key's lines, and the credentials sealed to it in the stored desired list
    (as sent, and opened), in case any reached a log."""
    found = set()
    pem = _read(credentials.key_path(state_dir)) or ""
    found.update(line.strip() for line in pem.splitlines() if "-----" not in line)
    try:
        stored = json.loads(_read(state_dir / "bridge.json") or "{}")
    except ValueError:
        return found
    if not isinstance(stored, dict):
        return found
    desired = stored.get("desired")
    for printer in (desired.get("printers") or []) if isinstance(desired, dict) else []:
        for secret in (printer.get("secrets") or []) if isinstance(printer, dict) else []:
            if isinstance(secret, dict) and isinstance(secret.get("sealed"), str):
                found.add(secret["sealed"])
    found.update(credentials.stored_plaintexts(state_dir, desired, stored.get("bridge_id")))
    return found


def _secrets(config_text: str, env: Mapping[str, str]) -> set[str]:
    found = {env[name].strip() for name in _SECRET_ENV if env.get(name)}
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    with contextlib.suppress(configparser.Error):  # redact_config still catches it by line
        parser.read_string(config_text)
    for section in parser.sections():
        for option, value in parser[section].items():
            if SECRET_OPTION.search(option):
                found.add(value.strip())
            found.update(m.group(1) for m in re.finditer(r"\w+://([^/@\s]+)@", value))
    return {s for s in found if len(s) >= MIN_SECRET_LENGTH}


def _latest_traces(state_dir: Path, budget: int) -> dict[str, str]:
    """The newest trace files (every printer's), up to `budget` bytes in all. The oldest one
    that fits only in part is cut to its newest whole lines."""
    found: dict[str, str] = {}
    for path in reversed(trace_files(state_dir)):
        if budget <= 0:
            break
        try:
            size = path.stat().st_size
            with path.open("rb") as f:
                if size > budget:
                    f.seek(size - budget)
                    f.readline()  # the rest of a line cut in two
                data = f.read(budget)
        except OSError:
            continue
        budget -= len(data)
        if data:
            found[f"{TRACE_DIR}/{path.name}"] = data.decode("utf-8", errors="replace")
    return dict(reversed(list(found.items())))


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


def _payload(entry: Entry) -> dict:
    if entry.kind != OUTBOX_KIND:
        return entry.payload
    return {
        field: f"<{len(value)} characters, left out>"
        if field in TEXT_FIELDS and isinstance(value, str)
        else value
        for field, value in entry.payload.items()
    }


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
