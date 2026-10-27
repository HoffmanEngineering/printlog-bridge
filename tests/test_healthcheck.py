"""`printlog-bridge-health`, the Docker health check: the same verdict as `status`, without
importing aiohttp (Docker runs it every minute)."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.cli import health
from printlog_bridge.core.config import CONFIG_PATH_ENV, STATE_DIR_ENV, load_config
from printlog_bridge.core.status import STATUS_FILE, format_status

PRINTER = "[printer voron]\nprinter_id = 42\nurl = http://192.168.1.50:7125\n"


def write_config(tmp_path: Path, bridge: str = "") -> Path:
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(f"[bridge]\n{bridge}\n{PRINTER}", encoding="utf-8")
    return path


def write_status(state_dir: Path, updated: timedelta, stopped: bool = False) -> None:
    now = datetime.now(timezone.utc)
    state_dir.mkdir(parents=True, exist_ok=True)
    status = {
        "version": "0.1.0",
        "pid": 1,
        "started_at": (now - timedelta(hours=1)).isoformat(),
        "updated_at": (now - updated).isoformat(),
        "stopped_at": now.isoformat() if stopped else None,
        "printers": {},
    }
    (state_dir / STATUS_FILE).write_text(json.dumps(status), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_PATH_ENV, str(write_config(tmp_path)))
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
    return tmp_path


def test_never_imports_aiohttp_or_the_adapters():
    code = (
        "import sys, printlog_bridge.cli.health; "
        "print(sorted(m for m in sys.modules "
        "if m == 'aiohttp' or m.startswith('printlog_bridge.adapters')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


@pytest.mark.parametrize(
    ("updated", "stopped", "expected"),
    [
        (timedelta(seconds=10), False, 0),  # running
        (timedelta(minutes=10), False, 1),  # stale: crashed, or hung
        (timedelta(seconds=10), True, 1),  # stopped
    ],
)
def test_agrees_with_status(env, capsys, updated, stopped, expected):
    write_status(env / "state", updated, stopped)

    assert health.main() == expected
    config = load_config(
        env / "printlog-bridge.cfg",
        adapters=ADAPTERS,
        env={STATE_DIR_ENV: str(env / "state")},
        home=env,
    )
    assert format_status(config)[1] == expected


def test_not_running_before_the_first_run(env, capsys):
    assert health.main() == 1
    assert "hasn't run yet" in capsys.readouterr().out


def test_the_config_state_dir_wins_over_the_environment(tmp_path, monkeypatch):
    # As in load_config: the image sets PRINTLOG_BRIDGE_STATE_DIR, and a state_dir in the
    # config wins over it.
    mine = tmp_path / "mine"
    monkeypatch.setenv(CONFIG_PATH_ENV, str(write_config(tmp_path, f"state_dir = {mine}\n")))
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "image-default"))
    write_status(mine, timedelta(seconds=10))

    assert health.main() == 0


def test_a_missing_config_still_finds_the_state_directory(tmp_path, monkeypatch):
    # A container with no config yet pairs and waits for printers, and is healthy meanwhile.
    monkeypatch.setenv(CONFIG_PATH_ENV, str(tmp_path / "nope.cfg"))
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))
    write_status(tmp_path / "state", timedelta(seconds=10))

    assert health.main() == 0


def test_an_unreadable_config_is_unhealthy(tmp_path, monkeypatch, capsys):
    path = tmp_path / "broken.cfg"
    path.write_text("not a section\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(path))

    assert health.main() == 2
    assert "broken.cfg" in capsys.readouterr().err
