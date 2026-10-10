"""The `printlog-bridge` command line."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from printlog_bridge import __version__
from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.core.checks import Status
from printlog_bridge.core.config import find_config
from printlog_bridge.core.doctor import diagnose, format_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="printlog-bridge",
        description="Logs every Klipper/Moonraker print to 3D Print Log.",
    )
    parser.add_argument("--version", action="version", version=f"printlog-bridge {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")

    doctor = commands.add_parser(
        "doctor", help="check the config, the API and each printer, and say what to fix"
    )
    doctor.add_argument(
        "--config",
        type=Path,
        help="config file (default: $PRINTLOG_BRIDGE_CONFIG, then "
        "~/printer_data/config/printlog-bridge.cfg, then "
        "~/.config/printlog-bridge/printlog-bridge.cfg)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor(args.config)

    parser.print_usage(sys.stderr)
    return 2


def _doctor(explicit_config: Path | None) -> int:
    env = dict(os.environ)
    home = Path.home()
    config_path = find_config(explicit_config, env=env, home=home)

    print(f"printlog-bridge {__version__} doctor")
    results = asyncio.run(diagnose(config_path, adapters=ADAPTERS, env=env, home=home))
    print(format_report(results))

    return 1 if any(r.status is Status.FAIL for r in results) else 0


def run() -> None:
    sys.exit(main())
