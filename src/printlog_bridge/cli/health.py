"""`printlog-bridge-health`: the Docker image's health check.

It exits 0 only while the bridge runs, by the same rule as `printlog-bridge status`, but it reads
only the config's state directory and the status file. Docker runs it every minute, and the full
`status` imports the adapters and aiohttp to describe each printer: about half a second of CPU
on a PC and about a second on a NAS, every minute. This never imports either
(`tests/test_health.py` checks).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from printlog_bridge.core.config import ConfigError, find_config, state_dir_of
from printlog_bridge.core.status import is_running, read_status, utc_now


def main() -> int:
    env = dict(os.environ)
    home = Path.home()
    try:
        state_dir = state_dir_of(find_config(None, env=env, home=home), env, home)
    except ConfigError as e:
        print(f"printlog-bridge: {e}", file=sys.stderr)
        return 2

    status = read_status(state_dir)
    if status is None:
        print(f"not running: the bridge hasn't run yet with the state directory {state_dir}")
        return 1
    if not is_running(status, utc_now()):
        print("not running: see `printlog-bridge status`")
        return 1
    print("running")
    return 0


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
