"""The `printlog-bridge` command line."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import getpass
import logging
import logging.handlers
import os
import signal
import stat
import sys
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from printlog_bridge import __version__
from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.adapters.anycubic import discovery as anycubic_discovery
from printlog_bridge.adapters.bambu import discovery as bambu_discovery
from printlog_bridge.adapters.flashforge import discovery as flashforge_discovery
from printlog_bridge.adapters.moonraker.discovery import DEFAULT_PORT, parse_subnet
from printlog_bridge.adapters.sdcp import discovery as sdcp_discovery
from printlog_bridge.cli.data_owner import drop_root
from printlog_bridge.cli.discover import (
    ask_duets,
    discover,
    format_anycubic,
    format_bambu,
    format_duet,
    format_flashforge,
    format_found,
    format_sdcp,
    subnets_to_scan,
)
from printlog_bridge.cli.pair import pair_command, pair_on_first_run
from printlog_bridge.core import credentials, mdns
from printlog_bridge.core.bridge import check_runnable, run_bridge
from printlog_bridge.core.checks import Status
from printlog_bridge.core.config import (
    API_KEY_ENV,
    BridgeConfig,
    ConfigError,
    clean_key,
    clear_config_api_key,
    find_config,
    load_config,
    read_key_file,
    write_key_file,
)
from printlog_bridge.core.device import clear_pending_pairing
from printlog_bridge.core.doctor import API_KEYS_PAGE, diagnose, format_report
from printlog_bridge.core.first_run import (
    setup_problem,
    wait_for_printers,
    write_starter_config,
)
from printlog_bridge.core.local_page import LocalPage
from printlog_bridge.core.status import format_status, pairing_state
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
    status.add_argument(
        "--pairing",
        action="store_true",
        help="print one line for scripts: pending <code> <address> [<link>] while a pairing "
        "waits for approval, else paired, api-key or waiting",
    )
    store_key = commands.add_parser(
        "store-key",
        help="keep the API key in the state directory, where Moonraker can't serve it: moves "
        "api_key out of the config, or asks for a key",
    )
    pair = commands.add_parser(
        "pair",
        help="pair the bridge with your 3D Print Log account: approve it on your phone (QR code) "
        "or in a browser, no API key to copy. Running it again pairs the bridge again.",
    )
    pair.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="ask nothing: pair, then say which printer_id to set (also without a terminal)",
    )
    pair.add_argument(
        "--name",
        help="the name the bridge goes by in 3D Print Log (default: $PRINTLOG_BRIDGE_NAME, then "
        "'printlog-bridge on <host name>')",
    )
    pair.add_argument(
        "--switch-printers",
        action="store_true",
        help="also choose again for printers that already have a printer_id",
    )
    rotate_key = commands.add_parser(
        "rotate-key",
        help="replace the bridge's encryption key: confirm the new one in 3D Print Log, then "
        "enter the printers' access codes and passwords again",
    )
    rotate_key.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    discover = commands.add_parser(
        "discover",
        help="list the Moonraker, Bambu Lab, Elegoo (SDCP), Duet, FlashForge and Anycubic printers "
        "on the local network, with a [printer] section for each",
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
    discover.add_argument(
        "--no-bambu",
        action="store_true",
        help="don't listen for Bambu Lab printers' announcements (UDP port 2021)",
    )
    discover.add_argument(
        "--no-sdcp",
        action="store_true",
        help="don't ask for Elegoo printers (SDCP: M99999 broadcast to UDP port 3000)",
    )
    discover.add_argument(
        "--no-duet",
        action="store_true",
        help="don't ask for Duet boards (an mDNS question for _http._tcp; also off with "
        "--no-zeroconf)",
    )
    discover.add_argument(
        "--no-anycubic",
        action="store_true",
        help="don't ask for Anycubic printers in LAN mode (an SSDP search, UDP port 1900)",
    )
    discover.add_argument(
        "--no-flashforge",
        action="store_true",
        help="don't ask for FlashForge printers (an empty datagram to 225.0.0.9 and the "
        "broadcast address, UDP ports 19000, 8899 and 48899)",
    )
    _add_probe(commands)
    for command in (doctor, run, status, store_key, pair, rotate_key, discover):
        command.add_argument(
            "--config",
            type=Path,
            help="config file (default: $PRINTLOG_BRIDGE_CONFIG, then "
            "~/printer_data/config/printlog-bridge.cfg, then "
            "~/.config/printlog-bridge/printlog-bridge.cfg)",
        )
    return parser


def _add_probe(commands) -> None:
    """The `probe` command. Its description and the allow-list are filled in only when its help
    is shown, so building the parser (for `run` too) imports nothing of `printlog_bridge.probe`.
    """
    probe = commands.add_parser(
        "probe",
        help="record, read-only, what an unsupported printer offers on the network, for a "
        "future adapter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    format_help = probe.format_help

    def lazy_help() -> str:
        from printlog_bridge.probe import allowlist, ports

        port_list = ", ".join(str(p.port) for p in ports.PORTS)
        probe.description = (
            "Records what a printer the bridge doesn't support yet announces and answers on "
            "the local network, into one JSON trace, and prints a summary. Strictly read-only: "
            "it never sends anything that changes the printer's state.\n\n"
            f"Ports checked: {port_list}."
        )
        probe.epilog = "Everything the probe sends (the allow-list):\n" + allowlist.describe()
        return format_help()

    probe.format_help = lazy_help  # type: ignore[method-assign]
    probe.add_argument("host", help="the printer's IP address or host name")
    probe.add_argument(
        "--seconds",
        type=float,
        default=30,
        help="how long to listen (default 30; 5 to 600). Longer during a print records more",
    )
    probe.add_argument(
        "--out", type=Path, help="the trace file (default probe-<host>-<time>.json here)"
    )
    probe.add_argument(
        "--redact",
        action="store_true",
        help="replace serial numbers, MAC and IP addresses, Wi-Fi, host and file names in the "
        "trace with placeholders and remove credentials, for attaching it to a public issue",
    )
    probe.add_argument(
        "--port",
        type=int,
        action="append",
        default=[],
        metavar="PORT",
        help="another TCP port to check (connect, read a banner, GET /); repeat for more",
    )
    probe.add_argument(
        "--photon",
        action="store_true",
        help="the printer is an Anycubic Photon (Mono X family): ask its TCP port 6000 for "
        "sysinfo and getstatus. Without it, nothing is sent to port 6000",
    )


def _probe(args: argparse.Namespace) -> int:
    from printlog_bridge.probe import allowlist
    from printlog_bridge.probe import run as probe_run

    if not probe_run.MIN_SECONDS <= args.seconds <= probe_run.MAX_SECONDS:
        print(
            f"printlog-bridge probe: --seconds must be {probe_run.MIN_SECONDS} to "
            f"{probe_run.MAX_SECONDS}",
            file=sys.stderr,
        )
        return 2
    bad = [p for p in args.port if not 0 < p < 65536]
    if bad:
        print(f"printlog-bridge probe: not a port: {bad[0]}", file=sys.stderr)
        return 2

    print("The probe sends only this (the allow-list):")
    print(allowlist.describe())
    print(
        f"\nProbing {args.host} for {args.seconds:g} s, read-only (Ctrl+C to stop)...",
        flush=True,
    )
    probe = probe_run.Probe(
        args.host, args.seconds, extra_ports=tuple(args.port), photon=args.photon
    )
    try:
        asyncio.run(probe.run())
    except probe_run.ProbeError as e:
        print(f"printlog-bridge probe: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        probe.interrupted = True
        if probe.address is None:
            print("\nprintlog-bridge probe: stopped before it started", file=sys.stderr)
            return 130
        print("\nStopped: writing what was recorded so far.", flush=True)
    trace = probe.trace()
    written = probe_run.finish(trace, args.redact)  # `trace` itself keeps its addresses
    out = args.out or Path(probe_run.default_name(args.host, args.redact))
    try:
        probe_run.write(written, out)
    except OSError as e:
        print(f"printlog-bridge probe: couldn't write {out}: {e}", file=sys.stderr)
        out = None
    print()
    print(probe_run.summarize(trace, out))
    if out is None:
        return 1
    return 130 if probe.interrupted else 0


def main(argv: Sequence[str] | None = None) -> int:
    drop_root()  # in the Docker image as root: become the data folder's user first
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor(args.config)
    if args.command == "run":
        return _run(args.config)
    if args.command == "status":
        return _status(args.config, pairing=args.pairing)
    if args.command == "store-key":
        return _store_key(args.config)
    if args.command == "pair":
        return _pair(args)
    if args.command == "rotate-key":
        return _rotate_key(args)
    if args.command == "discover":
        return _discover(args)
    if args.command == "probe":
        return _probe(args)

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


def _status(explicit_config: Path | None, *, pairing: bool = False) -> int:
    env = dict(os.environ)
    home = Path.home()
    config_path = find_config(explicit_config, env=env, home=home)
    try:
        config = _load_for_setup(config_path, explicit_config, env, home)
    except ConfigError as e:
        print(f"printlog-bridge: {e}", file=sys.stderr)
        print("Run `printlog-bridge doctor` to check the setup.", file=sys.stderr)
        return 2

    if pairing:
        print(pairing_state(config))
        return 0
    text, code = format_status(config)
    print(f"printlog-bridge {__version__} status")
    print(text)
    return code


def _pair(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    home = Path.home()
    config_path = find_config(args.config, env=env, home=home)
    try:
        config = _load_for_setup(config_path, args.config, env, home)
        check_writable(config.state_dir)
    except ConfigError as e:
        print(f"printlog-bridge: {e}", file=sys.stderr)
        print("Run `printlog-bridge doctor` to check the setup.", file=sys.stderr)
        return 2
    return pair_command(config, env, name=args.name, yes=args.yes, switch=args.switch_printers)


def _rotate_key(args: argparse.Namespace) -> int:
    """Replaces the encryption key. A running bridge reports the new one at its next check-in."""
    env = dict(os.environ)
    home = Path.home()
    config_path = find_config(args.config, env=env, home=home)
    try:
        config = _load_for_setup(config_path, args.config, env, home)
        check_writable(config.state_dir)
    except ConfigError as e:
        print(f"printlog-bridge: {e}", file=sys.stderr)
        return 2
    if not credentials.available():
        print(f"printlog-bridge: {credentials.MISSING_PACKAGE}", file=sys.stderr)
        return 1
    try:
        old = credentials.read_key(config.state_dir)
    except credentials.KeyUnavailable:
        old = None
    if old is not None:
        print(f"The bridge's encryption key is {credentials.short_fingerprint(old.fingerprint)}.")
    print(
        "A new key can't open anything sealed to the old one: you confirm it in 3D Print Log "
        "(Bridges), then enter each printer's access code or password there again."
    )
    if not args.yes:
        if not sys.stdin.isatty():
            print("printlog-bridge: run it with --yes to replace the key.", file=sys.stderr)
            return 1
        if input("Replace it? Type yes: ").strip().lower() != "yes":
            print("The key is unchanged.")
            return 1
    try:
        key = credentials.write_new_key(config.state_dir)
    except OSError as e:
        print(f"printlog-bridge: couldn't write the new key: {e}", file=sys.stderr)
        return 1
    print(
        f"New encryption key: {credentials.short_fingerprint(key.fingerprint)}, in "
        f"{credentials.key_path(config.state_dir)}. The running bridge reports it at its next "
        "check-in (within 5 minutes); then confirm it in 3D Print Log, checking it shows these "
        "characters."
    )
    return 0


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
    bambu = not args.no_bambu
    sdcp = not args.no_sdcp
    duet = zeroconf and not args.no_duet
    anycubic = not args.no_anycubic
    flashforge = not args.no_flashforge

    async def outcome(task: asyncio.Future | None) -> tuple[list | None, str | None]:
        if task is None:
            return None, None
        try:
            return await task, None
        except OSError as e:
            return None, e.strerror or str(e)

    async def every_kind():
        listening = asyncio.ensure_future(bambu_discovery.listen()) if bambu else None
        asking = (
            asyncio.ensure_future(sdcp_discovery.ask(sdcp_discovery.targets_for(networks)))
            if sdcp
            else None
        )
        duets = asyncio.ensure_future(ask_duets()) if duet else None
        anycubics = (
            asyncio.ensure_future(anycubic_discovery.ask(anycubic_discovery.targets_for(networks)))
            if anycubic
            else None
        )
        flashforges = (
            asyncio.ensure_future(
                flashforge_discovery.ask(flashforge_discovery.targets_for(networks))
            )
            if flashforge
            else None
        )
        found = await discover(networks, port=args.port, zeroconf=zeroconf)
        return (
            found,
            await outcome(listening),
            await outcome(asking),
            await outcome(duets),
            await outcome(anycubics),
            await outcome(flashforges),
        )

    (
        found,
        (bambus, error),
        (elegoos, sdcp_error),
        (boards, duet_error),
        (acs, ac_error),
        (ffs, ff_error),
    ) = asyncio.run(every_kind())
    print(format_found(found, networks, config, zeroconf=zeroconf))
    if bambu:
        print()
        print(format_bambu(bambus, config, bambu_discovery.LISTEN_S, error))
    if sdcp:
        print()
        print(format_sdcp(elegoos, config, sdcp_discovery.ASK_S, sdcp_error))
    if duet:
        print()
        print(format_duet(boards, config, mdns.WAIT_S, duet_error))
    if anycubic:
        print()
        print(format_anycubic(acs, config, anycubic_discovery.ASK_S, ac_error))
    if flashforge:
        print()
        print(format_flashforge(ffs, config, flashforge_discovery.ASK_S, ff_error))
    return 0 if found or bambus or elegoos or boards or acs or ffs else 1


def _store_key(explicit_config: Path | None) -> int:
    """Writes the key file (mode 600). A key in the config's [bridge] is moved there, and emptied
    in the config; otherwise the key is read from the terminal, or standard input."""
    env = dict(os.environ)
    home = Path.home()
    config_path = find_config(explicit_config, env=env, home=home)
    try:
        opened = _open_config(config_path)
    except OSError as e:
        print(f"printlog-bridge: can't read {config_path}: {e.strerror or e}", file=sys.stderr)
        return 2
    try:
        return _store_key_in(opened, explicit_config, env, home)
    finally:
        opened.close()


def _store_key_in(
    opened: _OpenConfig, explicit_config: Path | None, env: Mapping[str, str], home: Path
) -> int:
    euid = os.geteuid() if hasattr(os, "geteuid") else None  # None on Windows
    command = "printlog-bridge store-key"
    if explicit_config is not None:
        command += f" --config {explicit_config}"
    if euid == 0 and opened.text is None:
        print(f"printlog-bridge: {_not_as_root(None, command)}", file=sys.stderr)
        return 1
    try:
        config = load_config(opened.path, adapters=ADAPTERS, env=env, home=home, text=opened.text)
    except ConfigError as e:
        print(f"printlog-bridge: {e}", file=sys.stderr)
        return 2

    key = config.config_api_key
    moving = key is not None
    if euid is not None:
        refusal = _ownership_refusal(
            opened,
            command,
            euid=euid,
            rewrites=moving,
            sudo_uid=(env.get("SUDO_UID") or "").strip(),
        )
        if refusal:
            print(f"printlog-bridge: {refusal}", file=sys.stderr)
            return 1

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
    except OSError as e:
        print(f"printlog-bridge: couldn't store the key: {e}", file=sys.stderr)
        return 1
    if moving:
        try:
            cleared = clear_config_api_key(
                config.path, folder_fd=opened.folder_fd, identity=opened.identity
            )
        except OSError as e:
            print(
                f"printlog-bridge: stored the key in {config.api_key_file}, but couldn't remove "
                f"api_key from {config.path}: {e.strerror or e}. Remove that line by hand.",
                file=sys.stderr,
            )
            return 1
        if not cleared:
            print(
                f"printlog-bridge: stored the key in {config.api_key_file}, but couldn't find "
                f"the api_key line to empty in {config.path}. Delete it there yourself.",
                file=sys.stderr,
            )
            return 1

    if moving:
        print(f"Moved the API key from {config.path} to {config.api_key_file}.")
    else:
        print(f"Stored the API key in {config.api_key_file}.")
    print("Only you can read it. Restart printlog-bridge to use it.")
    if env.get(API_KEY_ENV):
        print(f"Note: {API_KEY_ENV} is set here, and wins over the stored key while it is.")
    return 0


@dataclass
class _OpenConfig:
    """The config store-key reads, checks and rewrites: its real path (through any symlink, so
    rewriting it keeps the link), its text (None when there is no such file), and on POSIX its
    folder, held open, so the file read, the owners checked and the file rewritten are the same
    whatever is renamed in between."""

    path: Path
    text: str | None = None
    folder_fd: int | None = None
    identity: tuple[int, int] | None = None
    """The file's device and inode, which the rewrite checks it still has."""
    file_owner: int | None = None
    folder_owner: int | None = None
    folder_mode: int = 0

    def close(self) -> None:
        if self.folder_fd is not None:
            os.close(self.folder_fd)
            self.folder_fd = None


def _open_config(config_path: Path) -> _OpenConfig:
    path = Path(os.path.realpath(config_path))
    if not hasattr(os, "geteuid"):  # Windows: no owners to check; load_config reads the path
        return _OpenConfig(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    opened = _OpenConfig(path)
    try:
        opened.folder_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | nofollow)
    except FileNotFoundError:
        return opened
    try:
        opened.folder_owner, opened.folder_mode = _owner(opened.folder_fd)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | nofollow, dir_fd=opened.folder_fd)
        except FileNotFoundError:
            return opened
        with os.fdopen(fd, "rb") as f:
            opened.file_owner, mode = _owner(f.fileno())
            if not stat.S_ISREG(mode):
                raise OSError(errno.EINVAL, "not a file")
            found = os.fstat(f.fileno())
            opened.identity = (found.st_dev, found.st_ino)
            data = f.read()
        try:
            opened.text = data.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise OSError(errno.EINVAL, f"not UTF-8 text ({e})") from None
    except BaseException:
        opened.close()
        raise
    return opened


def _ownership_refusal(
    opened: _OpenConfig, command: str, *, euid: int, rewrites: bool, sudo_uid: str
) -> str | None:
    """Why store-key won't run as `euid` on this config, or None when it may.

    Root writes the key file where the config says, so the config must be root's alone (its
    folder too), and not reached through sudo: a sudo user means their own bridge. Any other
    user writes their own key file, and must own the config only when store-key rewrites it
    (to move api_key out), since nobody else's they could, and the bridge then reads it."""
    path, file_owner = opened.path, opened.file_owner
    if file_owner is None:
        return None  # load_config said so
    if euid == 0:
        if file_owner != 0:
            return _not_as_root(file_owner, command)
        if opened.folder_owner != 0:
            return _give_back(path, opened.folder_owner, command)
        if opened.folder_mode & 0o022:
            return (
                f"other users can write {path.parent}, so they could change {path} under root. "
                f"Run store-key as the user the bridge runs as, or make the folder root's alone."
            )
        if sudo_uid not in ("", "0"):
            user = _who(int(sudo_uid)) if sudo_uid.isdigit() else sudo_uid
            return (
                f"store-key was started through sudo by {user}. Run it as the user the bridge "
                f"runs as, without sudo: {command}. Only if the bridge itself runs as root, run "
                "it from a root login (`su -`), not through sudo."
            )
        return None
    if not rewrites or file_owner == euid:
        return None
    who = _who(euid)
    if file_owner == 0:
        return (
            f"{path} belongs to root, so {who} can't remove api_key from it. If an earlier "
            "`sudo printlog-bridge store-key` left it root's, give it back and run store-key "
            f"again: sudo chown {_user_name(euid) or euid}: {path} && {command}. Otherwise "
            "remove the api_key line as root, then run store-key again to store the key."
        )
    owner = _who(file_owner)
    return (
        f"{path} belongs to {owner}, but store-key is running as {who}: the key file would be "
        f"{who}'s, and the bridge, which runs as {owner}, couldn't read it. "
        f"Run it as {owner}: {command}"
    )


def _not_as_root(owner: int | None, command: str) -> str:
    who = _who(owner) if owner is not None else "the user the bridge runs as (such as pi)"
    return (
        "don't run store-key as root (or with sudo): the key file and the config would then "
        f"belong to root, and the bridge couldn't read them. Run it as {who}, without sudo: "
        f"{command}"
    )


def _give_back(path: Path, uid: int | None, command: str) -> str:
    """For root's config in a folder that isn't root's: most likely left by an earlier
    `sudo store-key`, in the folder of the user the bridge runs as."""
    who = _who(uid) if uid is not None else "the user the bridge runs as"
    chown = (_user_name(uid) or str(uid)) if uid is not None else "<user>"
    return (
        f"{path} belongs to root, probably from an earlier `sudo printlog-bridge store-key`, "
        f"so the bridge, which runs as {who}, can't rewrite it. Give it back, then run store-key "
        f"as {who}, without sudo: sudo chown {chown}: {path} && {command}"
    )


def _owner(fd: int) -> tuple[int, int]:
    """The owner and mode of the file open as `fd`."""
    result = os.fstat(fd)
    return result.st_uid, result.st_mode


def _who(uid: int) -> str:
    return _user_name(uid) or f"uid {uid}"


def _user_name(uid: int) -> str | None:
    try:
        import pwd
    except ImportError:  # Windows
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def _ask_for_key() -> str:
    if sys.stdin.isatty():
        print(f"Paste an API key from {API_KEYS_PAGE} (it isn't shown).")
        return clean_key(getpass.getpass("API key: "))
    return clean_key(sys.stdin.readline())


def _load_for_setup(
    config_path: Path, explicit_config: Path | None, env: Mapping[str, str], home: Path
) -> BridgeConfig:
    """The config for `run`, `status` and `pair`, which work before there are printers: a new
    container has no config yet. A file named with --config must exist (a typo shouldn't pair
    a bridge or leave it waiting)."""
    return load_config(
        config_path,
        adapters=ADAPTERS,
        env=env,
        home=home,
        missing_ok=explicit_config is None,
        printers_required=False,
    )


def _run(explicit_config: Path | None) -> int:
    env = dict(os.environ)
    home = Path.home()
    config_path = find_config(explicit_config, env=env, home=home)

    def load() -> BridgeConfig:
        return _load_for_setup(config_path, explicit_config, env, home)

    try:
        config = load()
        check_writable(config.state_dir)
    except ConfigError as e:
        print(f"printlog-bridge: {e}", file=sys.stderr)
        print("Run `printlog-bridge doctor` to check the setup.", file=sys.stderr)
        return 2
    configure_logging(config.state_dir)
    # A pairing code left by a run that was killed while it paired is dead.
    clear_pending_pairing(config.state_dir)
    # From the start, so it shows the code to approve while the bridge pairs.
    page = start_local_page(config, load, env, home)
    try:
        return _run_loaded(config, load, env)
    finally:
        if page is not None:
            page.stop()


def start_local_page(
    config: BridgeConfig, load: Callable[[], BridgeConfig], env: Mapping[str, str], home: Path
) -> LocalPage | None:
    """The local page (status, pairing, doctor) in a thread of its own, or None when it's off
    or can't listen. It keeps the address it started with until the bridge restarts."""
    if config.status_page is None:
        return None
    page = LocalPage(
        config.status_page,
        config.status_page_clients,
        load,
        env=env,
        adapters=ADAPTERS,
        home=home,
    )
    return page if page.start() else None


def _run_loaded(
    config: BridgeConfig, load: Callable[[], BridgeConfig], env: Mapping[str, str]
) -> int:
    try:
        if not config.has_credentials:
            # First run in Docker or an add-on: pair in the log, then carry on.
            problem = _first_run_pairing(config, env)
            if problem is not None:
                print(f"printlog-bridge: {problem}", file=sys.stderr)
                return 2
            config = load()
        if config.has_credentials and setup_problem(config) is not None:
            # Nothing to watch yet (no printers, or one without printer_id): wait for the
            # config instead of exiting into the container's restart loop.
            waited = _wait_for_printers(config, load)
            if waited is None:
                return 0
            config = waited
        check_runnable(config)
    except ConfigError as e:
        print(f"printlog-bridge: {e}", file=sys.stderr)
        print("Run `printlog-bridge doctor` to check the setup.", file=sys.stderr)
        return 2

    logger = logging.getLogger(__name__)
    logger.info("printlog-bridge %s, config %s", __version__, config.path)
    if not config.printers:
        wrote = write_starter_config(config.path)
        logger.info(
            "No [printer] sections in %s%s: connect printers to this bridge in 3D Print Log "
            "(Bridges), or add them to the file; the bridge picks them up with no restart.",
            config.path,
            " (the bridge wrote a starter one, with an example of each)" if wrote else "",
        )
    while True:
        outcome: list[BridgeConfig | None] = []

        async def running(
            config: BridgeConfig = config, outcome: list[BridgeConfig | None] = outcome
        ) -> None:
            outcome.append(await run_bridge(config, adapters=ADAPTERS, reload=load))

        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(until_stopped(running()))
        if not outcome or outcome[0] is None:
            return 0
        config = outcome[0]


def _wait_for_printers(
    config: BridgeConfig, load: Callable[[], BridgeConfig]
) -> BridgeConfig | None:
    """The config once it has printers to watch, or None when stopped while waiting."""
    found: list[BridgeConfig] = []

    async def waiting() -> None:
        found.append(await wait_for_printers(config, load))

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(until_stopped(waiting()))
    return found[0] if found else None


def _first_run_pairing(config: BridgeConfig, env: Mapping[str, str]) -> str | None:
    """Pairs, or says why it can't (pairing off on the server: then what to do instead)."""
    outcome: list[str | None] = []

    async def pairing() -> None:
        outcome.append(await pair_on_first_run(config, env))

    try:
        asyncio.run(until_stopped(pairing()))
    except KeyboardInterrupt:
        return "pairing canceled"
    if not outcome:
        return "pairing canceled"
    problem = outcome[0]
    if problem is None:
        return None
    try:
        check_runnable(config)
    except ConfigError as e:
        return f"{e} (Pairing: {problem})"
    return problem


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
            f"runs as a user{uid} that must be able to write it. In Docker, leave out `user:` "
            "(`--user`): the container then gives itself the folder mounted at /data, as "
            "PUID/PGID or the folder's owner. Or run it as the user and group that own the "
            "folder."
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
