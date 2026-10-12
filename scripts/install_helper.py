"""The file edits behind install.sh and uninstall.sh. Standard library only: uninstall.sh runs it
with the system's python3, after the bridge's virtualenv may be gone.

    install_helper.py moonraker-add --conf C --asvc A --record R --repo DIR --venv DIR --origin URL
    install_helper.py moonraker-remove --conf C --asvc A --record R
    install_helper.py write-config --path P --printer-data DIR --name NAME [--printer-id N]

`moonraker-add` appends an `[update_manager printlog-bridge]` section to moonraker.conf, between
two marker comments, and `printlog-bridge` to moonraker.asvc, so Mainsail and Fluidd can update
and restart the bridge. It records what it changed (a JSON file), and `moonraker-remove` uses
that record to put both files back exactly as they were, byte for byte. Without the record it
still removes the marked section and the service line.

`write-config` writes a local-mode printlog-bridge.cfg, unless one exists. The API key comes from
the PRINTLOG_API_KEY environment variable, never from the command line, where `ps` shows it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

SERVICE = "printlog-bridge"
SECTION_HEADER = f"[update_manager {SERVICE}]"
BEGIN = f"# BEGIN {SERVICE}: added by its install.sh; its uninstall.sh removes this block"
END = f"# END {SERVICE}"
API_KEY_ENV = "PRINTLOG_API_KEY"

Change = Optional[dict]
"""What an add changed: `added`, and `newline` when the file had no newline at its end."""


def update_manager_section(*, repo: str, venv: str, origin: str) -> str:
    return "\n".join(
        [
            SECTION_HEADER,
            "type: git_repo",
            f"path: {repo}",
            f"origin: {origin}",
            "primary_branch: main",
            f"virtualenv: {venv}",
            "requirements: requirements.txt",
            f"managed_services: {SERVICE}",
        ]
    )


def add_section(text: str, section: str) -> tuple[str, dict]:
    """moonraker.conf with the section appended, unless it has one (ours or the user's)."""
    if SECTION_HEADER in text:
        return text, {"added": False, "newline": False}
    newline = bool(text) and not text.endswith("\n")
    prefix = text + ("\n" if newline else "") + ("\n" if text else "")
    return prefix + f"{BEGIN}\n{section}\n{END}\n", {"added": True, "newline": newline}


def remove_section(text: str, change: Change) -> str:
    """moonraker.conf without the marked section: as it was, when nothing was added after it."""
    if change is not None and not change.get("added"):
        return text
    start = text.find(BEGIN + "\n")
    end = text.find(END + "\n", start)
    if start < 0 or end < 0:
        return text
    before, after = text[:start], text[end + len(END) + 1 :]
    if before.endswith("\n\n"):
        before = before[:-1]  # the blank line put before the section
    if change is not None and change.get("newline") and not after:
        before = before[:-1]  # the newline put at the end of the user's last line
    return before + after


def add_service(text: str, service: str) -> tuple[str, dict]:
    """moonraker.asvc with the service on a line of its own, unless it's there."""
    if service in (line.strip() for line in text.splitlines()):
        return text, {"added": False, "newline": False}
    newline = bool(text) and not text.endswith("\n")
    return text + ("\n" if newline else "") + service + "\n", {"added": True, "newline": newline}


def remove_service(text: str, service: str, change: Change) -> str:
    if change is not None and not change.get("added"):
        return text
    lines = text.splitlines(keepends=True)
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == service:
            last = index == len(lines) - 1
            del lines[index]
            if last and change is not None and change.get("newline") and lines:
                lines[-1] = lines[-1][:-1]
            return "".join(lines)
    return text


def printer_name(host: str) -> str:
    """A `[printer <name>]` name from the host's name."""
    name = re.sub(r"[^a-z0-9_]+", "-", host.lower()).strip("-")
    return name or "printer"


def config_text(*, printer_data: str, name: str, api_key: str, printer_id: str) -> str:
    return f"""\
# printlog-bridge config, written by install.sh. Every option is in
# https://github.com/HoffmanEngineering/printlog-bridge/blob/main/printlog-bridge.example.cfg
# Check it with: printlog-bridge doctor. After a change, restart printlog-bridge (in Mainsail
# or Fluidd, or: sudo systemctl restart printlog-bridge).

[bridge]
# A personal API key from https://www.3dprintlog.com/api-keys. Pairing will replace this.
api_key = {api_key}

# Local mode: the bridge runs on the printer's own host, next to Moonraker.
[printer {name}]
# The printer's number in 3D Print Log: the end of its page's address, such as
# https://www.3dprintlog.com/printers/42. Prints from this printer are logged there.
printer_id = {printer_id}
socket = {printer_data}/comms/moonraker.sock
"""


# -- files -----------------------------------------------------------------------------------


def _read(path: Path) -> str:
    # newline="" keeps the file's own line ends, so writing it back changes nothing else.
    with path.open(encoding="utf-8", newline="") as f:
        return f.read()


def _write(path: Path, text: str) -> None:
    """Replaces the file in one step, keeping its permissions."""
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if path.exists():
            shutil.copymode(str(path), temporary)
        os.replace(temporary, str(path))
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _load_record(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def moonraker_add(args: argparse.Namespace) -> int:
    conf, asvc, record_path = Path(args.conf), Path(args.asvc), Path(args.record)
    if not conf.is_file():
        print(f"{conf} not found. Is Moonraker installed here?", file=sys.stderr)
        return 1
    record = _load_record(record_path)
    changed = False

    section = update_manager_section(repo=args.repo, venv=args.venv, origin=args.origin)
    text, change = add_section(_read(conf), section)
    if change["added"]:
        _write(conf, text)
        changed = True
    if "conf" not in record or change["added"]:
        record["conf"] = change

    if asvc.is_file():
        text, change = add_service(_read(asvc), SERVICE)
        if change["added"]:
            _write(asvc, text)
            changed = True
        if "asvc" not in record or change["added"]:
            record["asvc"] = change
    else:
        print(
            f"{asvc} not found, so Moonraker allows only its default services: Mainsail and "
            f"Fluidd won't offer to restart {SERVICE}. Add a line `{SERVICE}` to it if your "
            "Moonraker has one elsewhere.",
            file=sys.stderr,
        )

    record_path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print("changed" if changed else "unchanged")
    return 0


def moonraker_remove(args: argparse.Namespace) -> int:
    conf, asvc, record_path = Path(args.conf), Path(args.asvc), Path(args.record)
    record = _load_record(record_path) if record_path.is_file() else None
    changed = False
    for path, edit in (
        (conf, lambda text: remove_section(text, record.get("conf") if record else None)),
        (asvc, lambda text: remove_service(text, SERVICE, record.get("asvc") if record else None)),
    ):
        if not path.is_file():
            continue
        before = _read(path)
        after = edit(before)
        if after != before:
            _write(path, after)
            changed = True
    if record_path.exists():
        record_path.unlink()
    print("changed" if changed else "unchanged")
    return 0


def write_config(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    path = Path(args.path)
    if path.exists():
        print("exists")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    text = config_text(
        printer_data=args.printer_data,
        name=printer_name(args.name),
        api_key=env.get(API_KEY_ENV, "").strip(),
        printer_id=(args.printer_id or "").strip(),
    )
    _write(path, text)
    print("created")
    return 0


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install_helper.py")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("moonraker-add", "moonraker-remove"):
        command = commands.add_parser(name)
        command.add_argument("--conf", required=True)
        command.add_argument("--asvc", required=True)
        command.add_argument("--record", required=True)
        if name == "moonraker-add":
            command.add_argument("--repo", required=True)
            command.add_argument("--venv", required=True)
            command.add_argument("--origin", required=True)
    config = commands.add_parser("write-config")
    config.add_argument("--path", required=True)
    config.add_argument("--printer-data", required=True)
    config.add_argument("--name", required=True)
    config.add_argument("--printer-id")

    args = parser.parse_args(argv)
    if args.command == "moonraker-add":
        return moonraker_add(args)
    if args.command == "moonraker-remove":
        return moonraker_remove(args)
    return write_config(args, os.environ if env is None else env)


if __name__ == "__main__":
    sys.exit(main())
