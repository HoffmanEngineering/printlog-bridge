"""`printlog-bridge doctor`: checks the bridge can run, and says what to fix when it can't."""

from __future__ import annotations

import platform
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.api_client import ApiClient, ApiUnavailable, AuthRejected
from printlog_bridge.core.checks import CheckResult, Status
from printlog_bridge.core.config import (
    API_KEY_ENV,
    BridgeConfig,
    ConfigError,
    PrinterConfig,
    load_config,
)

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
        config = load_config(config_path, adapters=adapters, env=env, home=home)
    except ConfigError as e:
        results.append(CheckResult.fail("Config", str(e)))
        return results

    count = len(config.printers)
    results.append(
        CheckResult.ok("Config", f"{config.path} ({count} printer{'' if count == 1 else 's'})")
    )
    results.append(_check_state_dir(config.state_dir))
    results.append(_check_sqlite())
    api, server_time = await _check_api(config)
    results.append(api)
    if server_time is not None:
        results.append(_check_clock(server_time, clock()))

    for printer in config.printers:
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
    assert config.api_key is not None  # the API check passed
    try:
        async with ApiClient(config.api_url, config.api_key) as client:
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


async def _check_api(config: BridgeConfig) -> tuple[CheckResult, datetime | None]:
    """The API check, and the API's clock when it answered."""
    name = "3D Print Log API"
    if config.api_key is None:
        return CheckResult.fail(
            name,
            f"no API key. Create one at {API_KEYS_PAGE}, then set api_key in [bridge] "
            f"or the {API_KEY_ENV} environment variable.",
        ), None

    try:
        async with ApiClient(config.api_url, config.api_key) as client:
            user = await client.current_user()
    except AuthRejected:
        return CheckResult.fail(
            name,
            f"{config.api_url} rejected the API key. Check it, or create a new one at "
            f"{API_KEYS_PAGE}.",
        ), None
    except ApiUnavailable as e:
        return CheckResult.fail(name, str(e)), None

    who = user.display_name or f"user {user.id}"
    return CheckResult.ok(name, f"{config.api_url}, signed in as {who}"), client.server_time


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
