"""`printlog-bridge doctor`: checks the bridge can run, and says what to fix when it can't."""

from __future__ import annotations

import configparser
import os
import platform
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from printlog_bridge import __version__
from printlog_bridge.core import credentials
from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.api_client import ApiClient, ApiUnavailable, AuthRejected, RepairNeeded
from printlog_bridge.core.checks import CheckResult, Status
from printlog_bridge.core.config import (
    API_KEY_ENV,
    ApiKeySource,
    BridgeConfig,
    ConfigError,
    PrinterConfig,
    load_config,
)
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.status import is_running, read_status
from printlog_bridge.core.support import SECRET_OPTION
from printlog_bridge.core.updates import is_newer, update_notice

API_KEYS_PAGE = "https://www.3dprintlog.com/api-keys"
PRINTERS_PAGE = "https://www.3dprintlog.com/printers/"
MAX_CLOCK_SKEW = timedelta(minutes=2)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def diagnose(
    config_path: Path,
    adapters: Mapping[str, type[Adapter]],
    env: Mapping[str, str],
    home: Path,
    clock: Callable[[], datetime] = _utc_now,
) -> list[CheckResult]:
    results = [CheckResult.ok("Python", platform.python_version())]

    try:
        config = load_config(
            config_path, adapters=adapters, env=env, home=home, printers_required=False
        )
        if not config.printers and config.device is None:
            # Unpaired, the config's printers are all it can watch.
            load_config(config_path, adapters=adapters, env=env, home=home)
    except ConfigError as e:
        results.append(CheckResult.fail("Config", str(e)))
        return results

    count = len(config.printers)
    results.append(
        CheckResult.ok("Config", f"{config.path} ({count} printer{'' if count == 1 else 's'})")
    )
    results.append(_check_version(config))
    api_url_warning = config.api_url_warning()
    if api_url_warning is not None:
        results.append(CheckResult.warn("API URL", api_url_warning))
    results.append(_check_state_dir(config.state_dir))
    results.append(_check_sqlite())
    key_storage = _check_key_storage(config)
    if key_storage is not None:
        results.append(key_storage)
    device = _check_device_token(config)
    if device is not None:
        results.append(device)
    results.extend(_check_printer_secrets(config))
    key = _check_credential_key(config, clock())
    if key is not None:
        results.append(key)
    results.extend(_check_bridge(config, clock()))
    api, server_time = await _check_api(config)
    results.append(api)
    if server_time is not None:
        results.append(_check_clock(server_time, clock()))

    for printer in config.printers:
        # Read-only, for an adapter that remembers where it last found the printer.
        state = PrinterState.peek(config.state_dir, printer.name)
        if state is not None:
            printer.adapter.use_state(state)
        try:
            results.extend(await printer.adapter.check())
        except Exception as e:  # an adapter bug must not hide every other check
            results.append(
                CheckResult.fail(f"Printer {printer.name}", f"the check itself failed: {e!r}")
            )
        if api.status is Status.OK:  # otherwise the API check already says what to fix
            results.append(await _check_printer_link(config, printer))

    return results


def format_report(results: Sequence[CheckResult]) -> str:
    lines = [f"  {r.status.value:<5} {r.name}: {r.detail}" for r in results]

    failures = sum(r.status is Status.FAIL for r in results)
    warnings = sum(r.status is Status.WARN for r in results)
    warning_note = f", {warnings} warning{'' if warnings == 1 else 's'}" if warnings else ""
    if failures:
        lines.append(f"{failures} problem{'' if failures == 1 else 's'} found{warning_note}.")
    else:
        lines.append(f"No problems found{warning_note}.")
    return "\n".join(lines)


def _check_version(config: BridgeConfig) -> CheckResult:
    """This bridge's version, and whether 3D Print Log has told the running bridge of a newer
    one. Read from the status file, so doctor itself asks nobody."""
    name = "Version"
    if not config.update_check:
        return CheckResult.ok(name, __version__)
    latest = (read_status(config.state_dir) or {}).get("latest_version")
    if is_newer(latest):
        return CheckResult.warn(name, update_notice(latest))
    if isinstance(latest, str) and not is_newer(__version__, latest):
        return CheckResult.ok(name, f"{__version__}, the latest")
    return CheckResult.ok(name, __version__)


def _check_state_dir(state_dir: Path) -> CheckResult:
    name = "State directory"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return CheckResult.fail(name, f"can't write to {state_dir}: {e.strerror or e}")
    return CheckResult.ok(name, str(state_dir))


def _check_sqlite() -> CheckResult:
    try:
        with sqlite3.connect(":memory:") as db:
            db.execute("select 1")
    except sqlite3.Error as e:
        return CheckResult.fail("SQLite", str(e))
    return CheckResult.ok("SQLite", sqlite3.sqlite_version)


async def _check_printer_link(config: BridgeConfig, printer: PrinterConfig) -> CheckResult:
    name = f"Printer {printer.name} in 3D Print Log"
    if printer.printer_id is None:
        return CheckResult.fail(
            name,
            "printer_id isn't set, so its prints have nowhere to go. Set it in "
            f"[printer {printer.name}] to the number at the end of the printer's page "
            f"({PRINTERS_PAGE}<number>).",
        )
    try:
        async with ApiClient.for_config(config) as client:
            found = await client.get_printer(printer.printer_id)
    except (ApiUnavailable, AuthRejected) as e:
        return CheckResult.fail(name, str(e))
    if found is None:
        return CheckResult.fail(
            name,
            f"this account has no printer {printer.printer_id}. Check printer_id in "
            f"[printer {printer.name}] against the printer's page ({PRINTERS_PAGE}<number>).",
        )
    return CheckResult.ok(name, f"printer {printer.printer_id}, {found.get('name')}")


def _served(config: BridgeConfig, path: Path) -> str | None:
    """Who can download `path` because a printer's host serves the folder it's in, as the first
    configured adapter that serves it says (`Adapter.serves`), or None."""
    for printer in config.printers:
        try:
            served = printer.adapter.serves(path)
        except Exception:  # an adapter bug must not hide the key check
            continue
        if served:
            return served
    return None


def _check_key_storage(config: BridgeConfig) -> CheckResult | None:
    """Where the API key is kept, and a warning when someone else could read it."""
    name = "API key"
    if config.device is not None:
        if config.api_key_source is None:
            return None
        return CheckResult.ok(
            name,
            f"not used: the device token from `printlog-bridge pair` is. The key in "
            f"{config.describe_api_key()} can go (delete it at {API_KEYS_PAGE}).",
        )
    move = (
        f"Move it to {config.api_key_file}, which only you can read: run "
        "`printlog-bridge store-key`, then restart printlog-bridge."
    )
    served = _served(config, config.path) if config.config_api_key is not None else None
    if served is not None:
        unused = (
            f" It isn't used (the key in {config.describe_api_key()} is), so delete it from "
            "the file."
            if config.api_key_source is not ApiKeySource.CONFIG
            else f" {move}"
        )
        return CheckResult.warn(
            name,
            f"api_key is in {config.path}, which {served}.{unused}",
        )
    if config.api_key_source is None:
        return None  # the API check says what to do
    if config.api_key_source is ApiKeySource.KEY_FILE:
        key_file = config.api_key_file
        served = _served(config, key_file)
        if served is not None:
            return CheckResult.warn(
                name,
                f"{key_file} is in a folder {served}. Set state_dir in [bridge] to a folder "
                "outside it.",
            )
        if os.name == "posix":
            mode = key_file.stat().st_mode & 0o777
            if mode & 0o077:
                return CheckResult.warn(
                    name,
                    f"other users on this computer can read {key_file} (mode {mode:o}). "
                    f"Run: chmod 600 {key_file}",
                )
    return CheckResult.ok(name, f"in {config.describe_api_key()}")


def _check_device_token(config: BridgeConfig) -> CheckResult | None:
    """Where the device token is kept, and a warning when someone else could read it, or when
    it was issued by another API than the configured one (so it isn't used)."""
    name = "Device token"
    stored = config.stored_device
    if stored is None:
        return None
    path = config.device_token_file
    if config.device is None:
        return CheckResult.warn(
            name,
            f"{path} is from pairing with {stored.api_url}, not {config.api_url}, so it isn't "
            "used. Run `printlog-bridge pair` to pair with this one.",
        )
    served = _served(config, path)
    if served is not None:
        return CheckResult.warn(
            name,
            f"{path} is in a folder {served}. Set state_dir in [bridge] to a folder outside it, "
            "then run `printlog-bridge pair` again.",
        )
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            return CheckResult.warn(
                name,
                f"other users on this computer can read {path} (mode {mode:o}). Run: chmod 600 "
                f"{path}",
            )
    paired = f", paired as {stored.name!r}" if stored.name else ""
    return CheckResult.ok(name, f"in {path}{paired}")


def _check_credential_key(config: BridgeConfig, now: datetime) -> CheckResult | None:
    """The key printer credentials from the app are sealed to (story 14b): the package, the
    file, who can read it, and whether 3D Print Log uses it. Only for a paired bridge."""
    if config.device is None:
        return None
    name = "Credential key"
    if not credentials.available():
        return CheckResult.warn(name, credentials.MISSING_PACKAGE)
    path = credentials.key_path(config.state_dir)
    try:
        key = credentials.read_key(config.state_dir)
    except credentials.KeyUnavailable as e:
        return CheckResult.fail(
            name,
            f"{e}. Printers connected in the app can't use credentials entered there until the "
            "bridge can read its key (or, if the file is damaged, run "
            "`printlog-bridge rotate-key` and confirm the new key in 3D Print Log)",
        )
    if key is None:
        return CheckResult.ok(name, "none yet: the bridge makes one when it runs")
    served = _served(config, path)
    if served is not None:
        return CheckResult.warn(
            name,
            f"{path} is in a folder {served}. Set state_dir in [bridge] to a folder outside it, "
            "then run `printlog-bridge rotate-key`.",
        )
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            return CheckResult.warn(
                name,
                f"other users on this computer can read {path} (mode {mode:o}). Run: chmod 600 "
                f"{path}",
            )
    detail = f"fingerprint {credentials.short_fingerprint(key.fingerprint)}, in {path}"
    status = read_status(config.state_dir) or {}
    reported = (status.get("bridge") or {}).get("key") or {}
    if not (status and is_running(status, now)) or reported.get("fingerprint") != key.fingerprint:
        return CheckResult.ok(name, detail)
    if reported.get("state") in ("pending", "other", "rejected"):
        return CheckResult.warn(name, f"{detail}: {reported.get('detail')}")
    return CheckResult.ok(name, f"{detail}: {reported.get('detail')}")


def _check_bridge(config: BridgeConfig, now: datetime) -> list[CheckResult]:
    """Discovery, the check-in and the printers connected in the app (#100), as the running
    bridge last wrote them."""
    if config.device is None:
        return []
    status = read_status(config.state_dir) or {}
    running = bool(status) and is_running(status, now)
    bridge = status.get("bridge") or {}
    results = []
    discovery = "on" if config.discovery else "off ([bridge] discovery = false)"
    check_in = bridge.get("check_in") or {}
    if not running:
        results.append(CheckResult.ok("Bridge", f"discovery {discovery}; the bridge isn't running"))
        return results
    if check_in.get("problem"):
        results.append(CheckResult.warn("Bridge check-in", check_in["problem"]))
    elif check_in.get("at"):
        results.append(
            CheckResult.ok(
                "Bridge check-in",
                f"last at {check_in['at']}; discovery {discovery}; desired printers version "
                f"{bridge.get('desired_version') or 'none yet'}",
            )
        )
    for entry, app in sorted((bridge.get("app_printers") or {}).items()):
        name = f"App printer {entry}"
        detail = f"{app.get('kind')} at {app.get('host')}: {app.get('state')}" + (
            f": {app['detail']}" if app.get("detail") else ""
        )
        problems = [
            f"{field}: {words}"
            for field, words in sorted((app.get("credentials") or {}).items())
            if not words.startswith("from ")
        ]
        if problems:
            detail += "; " + "; ".join(problems)
        if app.get("state") in ("watching", "in the config file") and not problems:
            results.append(CheckResult.ok(name, detail))
        else:
            results.append(CheckResult.warn(name, detail))
    return results


def _check_printer_secrets(config: BridgeConfig) -> list[CheckResult]:
    """A warning for each printer whose own secret (a Bambu access code) is in a config file a
    printer's host serves to its users. Nothing otherwise."""
    served = _served(config, config.path)
    if served is None:
        return []
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        parser.read(config.path, encoding="utf-8-sig")
    except (configparser.Error, OSError):
        return []
    results = []
    for printer in config.printers:
        section = f"printer {printer.name}"
        if not parser.has_section(section):
            continue
        options = sorted(
            option
            for option, value in parser[section].items()
            if SECRET_OPTION.search(option) and value.strip()
        )
        if options:
            results.append(
                CheckResult.warn(
                    f"Printer {printer.name} secrets",
                    f"{', '.join(options)} is in {config.path}, which {served}. Keep the config "
                    "in a folder nothing serves, and point printlog-bridge at it with --config "
                    "or PRINTLOG_BRIDGE_CONFIG.",
                )
            )
    return results


async def _check_api(config: BridgeConfig) -> tuple[CheckResult, datetime | None]:
    """The API check, and the API's clock when it answered."""
    name = "3D Print Log API"
    if not config.has_credentials:
        return CheckResult.fail(
            name,
            "not paired yet. Run `printlog-bridge pair`, or create an API key at "
            f"{API_KEYS_PAGE} and store it with `printlog-bridge store-key` (or set the "
            f"{API_KEY_ENV} environment variable).",
        ), None

    try:
        async with ApiClient.for_config(config) as client:
            user = await client.current_user()
    except RepairNeeded as e:
        return CheckResult.fail(name, f"{config.api_url}: {e}."), None
    except AuthRejected:
        if config.device is not None:
            return CheckResult.fail(
                name,
                f"{config.api_url} rejected the device token. Run `printlog-bridge pair` to pair "
                "the bridge again.",
            ), None
        return CheckResult.fail(
            name,
            f"{config.api_url} rejected the API key. Check it, or create a new one at "
            f"{API_KEYS_PAGE}.",
        ), None
    except ApiUnavailable as e:
        return CheckResult.fail(name, str(e)), None

    who = user.display_name or f"user {user.id}"
    how = "the device token" if config.device is not None else "the API key"
    return CheckResult.ok(
        name, f"{config.api_url}, signed in as {who} with {how}"
    ), client.server_time


def _check_clock(server_time: datetime, now: datetime) -> CheckResult:
    name = "Clock"
    skew = now - server_time
    seconds = abs(round(skew.total_seconds()))
    if abs(skew) <= MAX_CLOCK_SKEW:
        return CheckResult.ok(name, f"within {seconds}s of 3D Print Log's")
    direction = "ahead of" if skew > timedelta(0) else "behind"
    return CheckResult.warn(
        name,
        f"this computer's clock is {seconds}s {direction} 3D Print Log's, so the app may show the "
        "printer as offline. Turn on NTP time sync (on Linux: sudo timedatectl set-ntp true).",
    )
