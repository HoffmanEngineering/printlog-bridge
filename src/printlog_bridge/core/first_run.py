"""`run` with nothing to watch yet: a new container, whose config has no printers or doesn't
exist (#99).

Instead of exiting, and being restarted by Docker every few seconds, the bridge (once paired)
writes a starter config when there is none, says in the log what to add, and waits: the status
file says it runs, so the container stays healthy, and `status` shows what's missing. It reads
the config again whenever the file changes, and starts watching as soon as it has a printer it
can log to, with no restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from printlog_bridge.core.bridge import check_runnable, credentials_label
from printlog_bridge.core.config import BridgeConfig, ConfigError
from printlog_bridge.core.device import write_private_file
from printlog_bridge.core.status import TOUCH_INTERVAL_S, StatusBoard

log = logging.getLogger(__name__)

SETUP_POLL_S = 2.0
"""How often the config file is checked for a change while the bridge waits for printers."""

STARTER_CONFIG = """\
# printlog-bridge config, written by the bridge because there was none. Full reference:
# https://github.com/HoffmanEngineering/printlog-bridge/blob/main/printlog-bridge.example.cfg
#
# Add a [printer <name>] section for each printer (remove the # in front of the lines of an
# example below and change them), then save: the bridge starts watching within a few seconds,
# with no restart. `printlog-bridge discover`, run in the container's console, lists the
# printers on your network with a section for each to paste here.
#
# printer_id is the printer's number in 3D Print Log: the end of its page's address, such as
# https://www.3dprintlog.com/printers/42. Use the printers' IP addresses (give each a fixed one
# in your router): names like voron.local usually don't resolve inside a container.

# A Klipper printer (Moonraker):
# [printer voron]
# printer_id = 42
# url = http://192.168.1.50:7125

# A Bambu Lab printer, in LAN Only Mode with Developer Mode on (the access code is on the same
# screen; the serial is on its device information page):
# [printer x1c]
# printer_id = 43
# adapter = bambu
# host = 192.168.1.51
# serial = 01S00C123456789
# access_code = 12345678

# A Prusa printer (PrusaLink: Settings > Network > PrusaLink shows the password):
# [printer mk4s]
# printer_id = 44
# adapter = prusalink
# host = 192.168.1.52
# password = the password the printer shows
"""


def setup_problem(config: BridgeConfig) -> str | None:
    """What keeps `config` from being watched yet, or None when it's ready."""
    if not config.printers:
        if not config.path.exists():
            return f"{config.path} doesn't exist yet: add a [printer <name>] section to it"
        return f"{config.path} has no [printer <name>] section yet"
    try:
        check_runnable(config)
    except ConfigError as e:
        return str(e)
    return None


def write_starter_config(path: Path) -> bool:
    """Writes STARTER_CONFIG at `path` (readable only by its owner, like the bridge's other
    files: it will hold printers' access codes and passwords) when there is no file there and
    its folder exists. Returns whether it did."""
    if path.exists() or not path.parent.is_dir():
        return False
    try:
        write_private_file(path, STARTER_CONFIG)
    except OSError as e:
        log.warning("couldn't write a starter config at %s: %s", path, e.strerror or e)
        return False
    return True


async def wait_for_printers(
    config: BridgeConfig,
    reload: Callable[[], BridgeConfig],
    *,
    poll_s: float = SETUP_POLL_S,
    touch_s: float = TOUCH_INTERVAL_S,
) -> BridgeConfig:
    """Waits until the config (re-read with `reload` each time its file changes) can be watched,
    and returns it. Cancel it to stop waiting."""
    path = config.path
    problem = setup_problem(config)
    board = StatusBoard(config.state_dir)
    board.start([], trace=config.trace, credentials=credentials_label(config))
    board.setup_problem(problem)
    wrote = write_starter_config(path)
    log.warning("%s", _guidance(path, problem, wrote))
    seen = _signature(path)
    touched = time.monotonic()
    try:
        while True:
            await asyncio.sleep(poll_s)
            now = _signature(path)
            if now == seen:
                if time.monotonic() - touched >= touch_s:
                    board.touch()
                    touched = time.monotonic()
                continue
            seen = now
            try:
                latest = reload()
                problem = setup_problem(latest)
            except ConfigError as e:
                problem = str(e)
            if problem is None:
                names = ", ".join(p.name for p in latest.printers)
                log.info("%s changed: starting to watch %s", path, names)
                return latest
            log.warning("%s changed, but the bridge can't start yet: %s", path, problem)
            board.setup_problem(problem)
            touched = time.monotonic()
    finally:
        board.stopped()


def _guidance(path: Path, problem: str | None, wrote: bool) -> str:
    lines = [f"No printers to watch yet: {problem}."]
    if wrote:
        lines.append(
            f"The bridge wrote a starter config at {path}, with an example for each kind of "
            "printer."
        )
    lines += [
        f"Add a [printer <name>] section for each printer to {path} (in Docker, the "
        "printlog-bridge.cfg in the folder mounted at /data). `printlog-bridge discover` lists "
        "the printers on your network with a section for each. The bridge starts watching "
        "within a few seconds of the file being saved; no restart needed.",
    ]
    return "\n".join(lines)


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size
