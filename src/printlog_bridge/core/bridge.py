"""`printlog-bridge run`: watches every configured printer at once and logs its jobs."""

from __future__ import annotations

import asyncio
import configparser
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from printlog_bridge.core import pairing
from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.app_printers import AppPrinters, resolve_private
from printlog_bridge.core.bridge_sync import BridgeSync
from printlog_bridge.core.config import (
    API_KEY_ENV,
    APP_PRINTER_PREFIX,
    BridgeConfig,
    ConfigError,
)
from printlog_bridge.core.connections import HEARTBEAT_INTERVAL_S, Heartbeat
from printlog_bridge.core.credentials import KeyStore, Opener
from printlog_bridge.core.device import DeviceToken
from printlog_bridge.core.discovery import (
    DiscoveryService,
    Found,
    default_route_address,
    networks_of,
)
from printlog_bridge.core.doctor import API_KEYS_PAGE, PRINTERS_PAGE
from printlog_bridge.core.health import ProblemLog
from printlog_bridge.core.lifecycle import BACKOFF_S, JobLogger
from printlog_bridge.core.spools import REFRESH_INTERVAL_S, Spools
from printlog_bridge.core.status import TOUCH_INTERVAL_S, StatusBoard
from printlog_bridge.core.support import known_secrets
from printlog_bridge.core.trace import Trace, trace_dir
from printlog_bridge.core.watchers import Watchers

log = logging.getLogger(__name__)


NO_PRINTERS_YET = (
    "no printers yet: connect one to this bridge in 3D Print Log (Bridges), or add a "
    "[printer <name>] section to the config file"
)
RELOAD_POLL_S = 2.0


def check_runnable(config: BridgeConfig) -> None:
    """What `run` needs beyond a valid config. Raises ConfigError saying what to set."""
    if not config.has_credentials:
        raise ConfigError(
            "not paired with 3D Print Log yet. Run `printlog-bridge pair`, or create an API key "
            f"at {API_KEYS_PAGE} and store it with `printlog-bridge store-key` (or set the "
            f"{API_KEY_ENV} environment variable)."
        )
    for printer in config.printers:
        if printer.printer_id is None:
            raise ConfigError(
                f"[printer {printer.name}] has no printer_id, so its prints have nowhere to go. "
                "Set it to the number at the end of the printer's page in 3D Print Log "
                f"({PRINTERS_PAGE}<number>)."
            )


async def run_bridge(
    config: BridgeConfig,
    *,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
    touch_interval: float = TOUCH_INTERVAL_S,
    backoff: tuple[float, float] = BACKOFF_S,
    spool_interval: float = REFRESH_INTERVAL_S,
    adapters: Mapping[str, type[Adapter]] | None = None,
    reload: Callable[[], BridgeConfig] | None = None,
    reload_poll: float = RELOAD_POLL_S,
    sync_options: Mapping[str, Any] | None = None,
) -> BridgeConfig | None:
    """Runs every printer's adapter until cancelled, logging their jobs to the API.

    Each job event goes into the outbox in the state directory first, and the job logger sends
    it from there. Registers each printer's connection and keeps it alive, and keeps
    `status.json` in the state directory up to date for `printlog-bridge status`.

    With a device token and `adapters` (the registry), it also checks in with 3D Print Log,
    finds printers on the network (`discovery`), and watches the printers connected in the app
    (#100). With `reload`, it reads the config again when its file changes: a changed
    `[app_printer]` section is applied at once, and any other change returns the new config,
    for the caller to run the bridge again with it.
    """
    check_runnable(config)
    # Every warning and error from here on is counted for the heartbeat (#37).
    problems = ProblemLog([p.name for p in config.printers], secrets=_secrets(config))
    bridge_log = logging.getLogger("printlog_bridge")
    bridge_log.addHandler(problems)
    try:
        return await _run(
            config,
            problems,
            heartbeat_interval,
            touch_interval,
            backoff,
            spool_interval,
            adapters,
            reload,
            reload_poll,
            dict(sync_options or {}),
        )
    finally:
        bridge_log.removeHandler(problems)


async def _run(
    config: BridgeConfig,
    problems: ProblemLog,
    heartbeat_interval: float,
    touch_interval: float,
    backoff: tuple[float, float],
    spool_interval: float,
    adapters: Mapping[str, type[Adapter]] | None,
    reload: Callable[[], BridgeConfig] | None,
    reload_poll: float,
    sync_options: dict[str, Any],
) -> BridgeConfig | None:
    api_url_warning = config.api_url_warning()
    if api_url_warning is not None:
        log.warning("%s", api_url_warning)

    board = StatusBoard(config.state_dir)
    board.start(
        [p.name for p in config.printers],
        trace=config.trace,
        credentials=credentials_label(config),
    )
    if config.trace:
        log.info(
            "tracing is on: every message to and from each printer is recorded in %s",
            trace_dir(config.state_dir),
        )

    def credentials_problem(problem: str | None) -> None:
        if problem is None:
            log.info("the bridge was paired again: sending to 3D Print Log again")
        else:
            log.error("%s. Jobs wait in the outbox until then.", problem)
        board.credentials_problem(problem)

    try:
        async with ApiClient.for_config(config, on_credentials=credentials_problem) as api:
            spools = Spools(api, {}, board, interval=spool_interval)
            sink = JobLogger(
                api,
                {},
                config.state_dir,
                backoff=backoff,
                board=board,
                spools=spools,
                max_attachment_bytes=config.max_attachment_mb * 1024 * 1024,
            )
            heartbeat = Heartbeat(
                api,
                [],
                board,
                interval=heartbeat_interval,
                update_check=config.update_check,
                problems=problems,
            )
            sink.before_create = heartbeat.ensure_registered
            spools.on_swap = sink.spool_swapped
            secrets = _secrets(config)
            opened: list[str] = []
            """The credentials opened from the app, which the scrubbers learn as they're opened."""
            watchers = Watchers(
                sink,
                heartbeat,
                spools,
                board,
                problems,
                api_url=config.api_url,
                trace=(
                    (lambda name: Trace(config.state_dir, name, secrets=[*secrets, *opened]))
                    if config.trace
                    else None
                ),
            )
            for printer in config.printers:  # each has a printer_id: check_runnable
                watchers.start(printer)
            work = [
                sink.run(),
                heartbeat.run(),
                spools.run(),
                _keep_touching(board, touch_interval),
                watchers.run(),
            ]
            app: AppPrinters | None = None
            sync: BridgeSync | None = None
            if config.device is not None and adapters is not None:
                resolve = sync_options.pop("resolve", resolve_private)
                keys = KeyStore(config.state_dir)
                opener = Opener(keys)

                def learn(values: list[str]) -> None:
                    opened[:] = values
                    problems.secrets = [*_secrets(app.config if app else config), *values]

                app = AppPrinters(
                    watchers,
                    config,
                    adapters,
                    board,
                    resolve=resolve,
                    opener=opener,
                    on_secrets=learn,
                )
                sync = _bridge_sync(config, api, board, app, adapters, sync_options, keys, opener)
                work.append(_sync_quietly(sync))
            if not config.printers:
                board.setup_problem(NO_PRINTERS_YET if app is not None else None)
            restart: list[BridgeConfig] = []
            if reload is not None:
                work.append(_watch_config(config, reload, reload_poll, app, restart, problems))
            try:
                await _first_done(work)
            finally:
                sink.close()
            return restart[0] if restart else None
    finally:
        board.stopped()


async def _first_done(work: Sequence[Awaitable[Any]]) -> None:
    """Runs `work` together until one ends (or raises), then cancels the rest."""
    tasks = [asyncio.ensure_future(w) for w in work]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # raises what it raised
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _sync_quietly(sync: BridgeSync) -> None:
    """The check-in loop, which must never end the bridge: when it stops for good (a missing
    scope), the printers carry on."""
    try:
        await sync.run()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("the check-in with 3D Print Log stopped")
    await asyncio.Event().wait()


def _bridge_sync(
    config: BridgeConfig,
    api: ApiClient,
    board: StatusBoard,
    app: AppPrinters,
    adapters: Mapping[str, type[Adapter]],
    options: dict[str, Any],
    keys: KeyStore | None = None,
    opener: Opener | None = None,
) -> BridgeSync:
    discovery = None
    if config.discovery:
        hooks = [h for h in (a.discovery() for a in adapters.values()) if h is not None]
        discovery = DiscoveryService(hooks, **options.pop("discovery_options", {}))

    def configured() -> list[Found]:
        return [e for e in (p.adapter.found_entry() for p in config.printers) if e is not None]

    def is_configured(found: Found) -> bool:
        return any(p.adapter.watches(found) for p in app.config.printers)

    def networks() -> list[str]:
        addresses = [default_route_address()]
        addresses += [e.address for e in configured()]
        addresses += [str(s.get("address")) for s in app.states.values() if s.get("address")]
        return [str(n) for n in networks_of(a for a in addresses if a)]

    async def repair() -> DeviceToken | None:
        stored = await repair_for_sync(config)
        if stored is not None:
            api.use_device(stored)
        return stored

    local = any(p.adapter.mode() == "local" for p in config.printers)
    return BridgeSync(
        api,
        config.state_dir,
        board,
        app.apply,
        device=config.device,
        discovery=discovery,
        networks=networks,
        configured=configured,
        is_configured=is_configured,
        mode="local" if local else "remote",
        repair=options.pop("repair", repair),
        keys=keys,
        opener=opener,
        issues=app.credential_issues,
        refresh=app.refresh,
        **options,
    )


async def repair_for_sync(config: BridgeConfig) -> DeviceToken | None:
    """Pairs the bridge again, in the log, to get `bridge:sync`. None (and no pairing) when 3D
    Print Log doesn't offer it."""
    offered = await pairing.offered_scopes(config.api_url)
    if pairing.SYNC_SCOPE not in offered:
        log.warning("3D Print Log doesn't offer the bridge:sync permission yet: not pairing again")
        return None
    name = (config.device.name if config.device is not None else None) or pairing.default_name(
        os.environ
    )
    stored, _ = await pairing.pair(
        config.api_url,
        config.state_dir,
        name=name,
        create=False,
        show=lambda a: log.warning("%s", "\n" + pairing.instructions(a, sys.stderr)),
        say=lambda message: log.warning("%s", message),
    )
    log.info("Paired again (permissions: %s).", ", ".join(stored.scopes))
    return stored


async def _watch_config(
    config: BridgeConfig,
    reload: Callable[[], BridgeConfig],
    poll: float,
    app: AppPrinters | None,
    restart: list[BridgeConfig],
    problems: ProblemLog | None = None,
) -> None:
    """Reads the config again whenever its file changes. Returns (ending the bridge, for the
    caller to start it again) when anything but `[app_printer]` sections changed."""
    seen = _signature(config.path)
    shape = _shape(config.path)
    while True:
        await asyncio.sleep(poll)
        now = _signature(config.path)
        if now == seen:
            continue
        seen = now
        try:
            latest = reload()
            check_runnable(latest)
        except ConfigError as e:
            log.warning("%s changed, but can't be used (the bridge carries on as it was): %s",
                        config.path, e)  # fmt: skip
            continue
        if _shape(config.path) != shape:
            log.info("%s changed: starting again with it", config.path)
            restart.append(latest)
            return
        if problems is not None:
            problems.secrets = _secrets(latest)
        if app is not None:
            log.info("%s changed: applying [app_printer] sections", config.path)
            await app.use_config(latest)


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _shape(path: Path) -> Any:
    """The config without its `[app_printer]` sections, to tell what changed."""
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        parser.read(path, encoding="utf-8-sig")
    except (configparser.Error, OSError):
        return None
    return {
        s: dict(parser[s]) for s in parser.sections() if s.partition(" ")[0] != APP_PRINTER_PREFIX
    }


def _secrets(config: BridgeConfig) -> list[str]:
    """Every key and password the bridge knows of, for what it writes down or sends."""
    secrets = known_secrets(config.path, config.state_dir, os.environ)
    if config.api_key:
        secrets.append(config.api_key)
    if config.device is not None:
        secrets.append(config.device.token)
    return secrets


def credentials_label(config: BridgeConfig) -> str:
    """What the bridge signs in with, for the status file."""
    if config.device is not None:
        name = f", paired as {config.device.name}" if config.device.name else ""
        return f"device token{name}"
    return "API key"


async def _keep_touching(board: StatusBoard, interval: float) -> None:
    """Rewrites the status file now and then, so `status` can tell the bridge is still up."""
    while True:
        await asyncio.sleep(interval)
        board.touch()
