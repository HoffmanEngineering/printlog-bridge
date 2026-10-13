"""The `printlog-bridge` command line."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import logging
import logging.handlers
import os
import signal
import sys
import tempfile
from collections.abc import Awaitable, Sequence
from pathlib import Path

from printlog_bridge import __version__
from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.adapters.moonraker.discovery import DEFAULT_PORT, parse_subnet
from printlog_bridge.cli.discover import discover, format_found, subnets_to_scan
from printlog_bridge.core.bridge import check_runnable, run_bridge
from printlog_bridge.core.checks import Status
from printlog_bridge.core.config import (
    API_KEY_ENV,
    ConfigError,
    clean_key,
    clear_config_api_key,
    find_config,
    load_config,
    read_key_file,
    write_key_file,
)
from printlog_bridge.core.doctor import API_KEYS_PAGE, diagnose, format_report
from printlog_bridge.core.status import format_status
from printlog_bridge.core.support import BUNDLE_NAME, write_support_bundle


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
    run = commands.add_parser("run", help="watch every printer and log its prints")
    status = commands.add_parser(
        "status", help="what the running bridge is doing: each printer's connection and outbox"
    )
    store_key = commands.add_parser(
        "store-key",
        help="keep the API key in the state directory, where Moonraker can't serve it: moves "
        "api_key out of the config, or asks for a key",
    )
    discover = commands.add_parser(
        "discover",
        help="list the Moonraker printers on the local network, with a [printer] section for each",
    )
    discover.add_argument(
        "--subnet",
        action="append",
        default=[],
        metavar="NETWORK",
        help="a private /24 (or smaller) to scan, such as 192.168.1.0/24; repeat for more "
        "(default: the networks of the configured printers and of this host)",
    )
    discover.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"Moonraker's port (default {DEFAULT_PORT})"
    )
    discover.add_argument(
        "--no-zeroconf", action="store_true", help="only scan; don't ask over mDNS"
    )
    for command in (doctor, run, status, store_key, discover):
        command.add_argument(
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
    if args.command == "run":
        return _run(args.config)
    if args.command == "status":
        return _status(args.config)
    if args.command == "store-key":
        return _store_key(args.config)
    if args.command == "discover":
        return _discover(args)

    parser.print_usage(sys.stderr)
    return 2


def _doctor(explicit_config: Path | None) -> int:
    env = dict(os.environ)
    home = Path.home()
    config_path = find_config(explicit_config, env=env, home=home)

    print(f"printlog-bridge {__version__} doctor")
    results = asyncio.run(diagnose(config_path, adapters=ADAPTERS, env=env, home=home))
    report = format_report(results)
    print(report)

    try:
        config = load_config(config_path, adapters=ADAPTERS, env=env, home=home)
    except ConfigError:
        pass  # the report says what to fix; there is no state directory to bundle yet
    else:
        try:
            bundle = write_support_bundle(
                config.state_dir / BUNDLE_NAME,
                report=report,
                config_path=config.path,
                state_dir=config.state_dir,
                env=env,
            )
        except OSError as e:
            print(f"Couldn't write the support bundle: {e}")
        else:
            print(f"Support bundle: {bundle} (API keys and passwords removed)")

    return 1 if any(r.status is Status.FAIL for r in results) else 0


def _status(explicit_config: Path | None) -> int:
    env = dict(os.environ)
    home = Path.home()
    config_path = find_config(explicit_config, env=env, home=home)
    try:
        config = load_config(config_path, adapters=ADAPTERS, env=env, home=home)
    except ConfigError as e:
        print(f"printlog-bridge: {e}", file=sys.stderr)
        print("Run `printlog-bridge doctor` to check the setup.", file=sys.stderr)
        return 2

    text, code = format_status(config)
    print(f"printlog-bridge {__version__} status")
    print(text)
    return code


def _discover(args: argparse.Namespace) -> int:
    """Lists every Moonraker that answers on the local network. Works without a config; with
    one, also scans the configured printers' networks and marks those already set up."""
    try:
        subnets = [parse_subnet(s) for s in args.subnet]
    except ValueError as e:
        print(f"printlog-bridge: --subnet: {e}", file=sys.stderr)
        return 2
    env = dict(os.environ)
    home = Path.home()
    config = None
    with contextlib.suppress(ConfigError):
        config = load_config(
            find_config(args.config, env=env, home=home), adapters=ADAPTERS, env=env, home=home
        )
    networks = subnets_to_scan(subnets, config)
    zeroconf = not args.no_zeroconf
    print(f"printlog-bridge {__version__} discover")
    found = asyncio.run(discover(networks, port=args.port, zeroconf=zeroconf))
    print(format_found(found, networks, config, zeroconf=zeroconf))
    return 0 if found else 1


def _store_key(explicit_config: Path | None) -> int:
    """Writes the key file (mode 600). A key in the config's [bridge] is moved there, and emptied
    in the config; otherwise the key is read from the terminal, or standard input."""
    env = dict(os.environ)
    home = Path.home()
    config_path = find_config(explicit_config, env=env, home=home)
    try:
        config = load_config(config_path, adapters=ADAPTERS, env=env, home=home)
    except ConfigError as e:
        print(f"printlog-bridge: {e}", file=sys.stderr)
        return 2

    key = config.config_api_key
    moving = key is not None
    stored = read_key_file(config.api_key_file)
    if moving and stored is not None and stored != key:
        print(
            f"printlog-bridge: {config.api_key_file} already holds a different key, and it is "
            f"the one in use. Delete api_key from {config.path} instead.",
            file=sys.stderr,
        )
        return 1
    if not moving:
        key = _ask_for_key()
    if not key:
        print(f"No API key given, so nothing was stored. Create one at {API_KEYS_PAGE}.",
              file=sys.stderr)  # fmt: skip
        return 1

    try:
        write_key_file(config.api_key_file, key)
        if moving and not clear_config_api_key(config.path):
            print(
                f"printlog-bridge: stored the key in {config.api_key_file}, but couldn't find "
                f"the api_key line to empty in {config.path}. Delete it there yourself.",
                file=sys.stderr,
            )
            return 1
    except OSError as e:
        print(f"printlog-bridge: couldn't store the key: {e}", file=sys.stderr)
        return 1

    if moving:
        print(f"Moved the API key from {config.path} to {config.api_key_file}.")
    else:
        print(f"Stored the API key in {config.api_key_file}.")
    print("Only you can read it. Restart printlog-bridge to use it.")
    if env.get(API_KEY_ENV):
        print(f"Note: {API_KEY_ENV} is set here, and wins over the stored key while it is.")
    return 0


def _ask_for_key() -> str:
    if sys.stdin.isatty():
        print(f"Paste an API key from {API_KEYS_PAGE} (it isn't shown).")
        return clean_key(getpass.getpass("API key: "))
    return clean_key(sys.stdin.readline())


def _run(explicit_config: Path | None) -> int:
    env = dict(os.environ)
    home = Path.home()
    config_path = find_config(explicit_config, env=env, home=home)
    try:
        config = load_config(config_path, adapters=ADAPTERS, env=env, home=home)
        check_runnable(config)
        check_writable(config.state_dir)
    except ConfigError as e:
        print(f"printlog-bridge: {e}", file=sys.stderr)
        print("Run `printlog-bridge doctor` to check the setup.", file=sys.stderr)
        return 2

    configure_logging(config.state_dir)
    logging.getLogger(__name__).info("printlog-bridge %s, config %s", __version__, config.path)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(until_stopped(run_bridge(config)))
    return 0


def check_writable(state_dir: Path) -> None:
    """Raises ConfigError, saying whose folder it is, when this user can't write the state
    directory: in Docker, a folder mounted at /data that another user owns."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=state_dir):
            pass
    except OSError as e:
        uid = f" (uid {os.getuid()})" if hasattr(os, "getuid") else ""
        raise ConfigError(
            f"can't write the state directory {state_dir}: {e.strerror or e}. The bridge "
            f"runs as a user{uid} that must be able to write it. In Docker, run the container "
            "as the user and group that own the folder mounted at /data (`user:` in "
            "docker-compose.yml, `--user` with docker run)."
        ) from None


async def until_stopped(work: Awaitable[None]) -> None:
    """Runs `work` until it ends, or until SIGTERM (how systemd stops a service), which stops it
    the way Ctrl-C does: cancelled, so it can record that it stopped."""
    task = asyncio.ensure_future(work)
    stopping = False

    def stop() -> None:
        nonlocal stopping
        logging.getLogger(__name__).info("stopping (SIGTERM)")
        stopping = True
        task.cancel()

    loop = asyncio.get_running_loop()
    # Windows has no signal handlers in asyncio; there, only Ctrl-C stops the bridge.
    with contextlib.suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGTERM, stop)
    try:
        await task
    except asyncio.CancelledError:
        if not stopping:
            raise
    finally:
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(signal.SIGTERM)


LOG_FILE = "bridge.log"
LOG_FILE_BYTES = 1_000_000


def configure_logging(state_dir: Path) -> list[logging.Handler]:
    """Logs to stderr (journald under systemd) and to `state_dir/bridge.log`, which `doctor`
    puts in the support bundle. The file keeps about 2 MB: itself and one older copy."""
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                state_dir / LOG_FILE, maxBytes=LOG_FILE_BYTES, backupCount=1, encoding="utf-8"
            )
        )
    except OSError as e:
        print(f"printlog-bridge: not logging to {state_dir / LOG_FILE}: {e}", file=sys.stderr)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    return handlers


def run() -> None:
    sys.exit(main())
