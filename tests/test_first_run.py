"""A container with no printers yet (core/first_run.py): `run` waits for them instead of exiting
into a restart loop, and starts as soon as the config has one."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
import sys
from pathlib import Path

import pytest
from fake_moonraker import eventually

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.core.config import BridgeConfig, ConfigError, load_config
from printlog_bridge.core.first_run import (
    STARTER_CONFIG,
    setup_problem,
    wait_for_printers,
    write_starter_config,
)
from printlog_bridge.core.status import format_status, is_running, read_status, utc_now

PRINTER = "[printer voron]\nprinter_id = 42\nurl = http://192.168.1.50:7125\n"


def load(path: Path, state: Path) -> BridgeConfig:
    return load_config(
        path,
        adapters=ADAPTERS,
        env={"PRINTLOG_API_KEY": "k", "PRINTLOG_BRIDGE_STATE_DIR": str(state)},
        home=path.parent,
        missing_ok=True,
        printers_required=False,
    )


def test_no_printers_is_a_setup_problem_naming_the_file(tmp_path):
    problem = setup_problem(load(tmp_path / "printlog-bridge.cfg", tmp_path / "state"))

    assert problem is not None
    assert "printlog-bridge.cfg" in problem
    assert "[printer" in problem


def test_a_printer_without_printer_id_is_a_setup_problem(tmp_path):
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text("[printer u1]\nurl = http://u1:7125\n", encoding="utf-8")

    problem = setup_problem(load(path, tmp_path / "state"))

    assert problem is not None
    assert "[printer u1] has no printer_id" in problem


def test_a_runnable_config_has_no_setup_problem(tmp_path):
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(PRINTER, encoding="utf-8")

    assert setup_problem(load(path, tmp_path / "state")) is None


def test_the_starter_config_has_no_printer_and_shows_each_adapter(tmp_path):
    path = tmp_path / "printlog-bridge.cfg"

    assert write_starter_config(path)

    assert path.read_text(encoding="utf-8") == STARTER_CONFIG
    config = load(path, tmp_path / "state")
    assert config.printers == []
    for adapter in ("adapter = bambu", "adapter = prusalink", "url = http://"):
        assert adapter in STARTER_CONFIG
    assert "discover" in STARTER_CONFIG
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_starter_config_never_replaces_a_file(tmp_path):
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text("# mine\n", encoding="utf-8")

    assert not write_starter_config(path)
    assert path.read_text(encoding="utf-8") == "# mine\n"


def test_the_starter_config_needs_its_folder(tmp_path):
    assert not write_starter_config(tmp_path / "missing" / "printlog-bridge.cfg")


async def _wait(path: Path, state: Path, **kwargs) -> BridgeConfig:
    return await wait_for_printers(
        load(path, state), lambda: load(path, state), poll_s=0.01, touch_s=0.05, **kwargs
    )


def test_waits_with_the_bridge_running_then_starts_when_a_printer_is_added(tmp_path, caplog):
    path, state = tmp_path / "printlog-bridge.cfg", tmp_path / "state"
    caplog.set_level(logging.INFO)

    async def scenario():
        waiting = asyncio.ensure_future(_wait(path, state))
        await eventually(lambda: path.exists())
        status = read_status(state)
        assert status is not None and is_running(status, utc_now())
        assert "[printer" in status["setup_problem"]
        assert not waiting.done()
        report, code = format_status(load(path, state))
        assert code == 0
        assert "Setup: " in report

        path.write_text(PRINTER, encoding="utf-8")
        return await asyncio.wait_for(waiting, 5)

    config = asyncio.run(scenario())

    assert [p.name for p in config.printers] == ["voron"]
    assert "No printers to watch yet" in caplog.text
    assert "discover" in caplog.text
    assert "starter config" in caplog.text


def test_keeps_waiting_through_a_config_error_and_says_what_it_is(tmp_path, caplog):
    path, state = tmp_path / "printlog-bridge.cfg", tmp_path / "state"
    path.write_text("# nothing yet\n", encoding="utf-8")

    async def scenario():
        waiting = asyncio.ensure_future(_wait(path, state))
        await asyncio.sleep(0.05)
        path.write_text("[printer voron]\nprinter_id = forty-two\n", encoding="utf-8")
        await eventually(lambda: "forty-two" in caplog.text)
        assert not waiting.done()
        path.write_text("[printer voron]\nurl = http://v:7125\n", encoding="utf-8")
        await eventually(lambda: "[printer voron] has no printer_id" in caplog.text)
        assert not waiting.done()
        path.write_text(PRINTER, encoding="utf-8")
        return await asyncio.wait_for(waiting, 5)

    config = asyncio.run(scenario())

    assert config.printers[0].printer_id == 42


def test_touches_the_status_file_while_it_waits(tmp_path):
    path, state = tmp_path / "printlog-bridge.cfg", tmp_path / "state"

    async def scenario():
        waiting = asyncio.ensure_future(_wait(path, state))
        await eventually(lambda: read_status(state) is not None)
        first = read_status(state)["updated_at"]
        await eventually(lambda: read_status(state)["updated_at"] != first)
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting

    asyncio.run(scenario())

    assert read_status(state)["stopped_at"] is not None


def test_a_bad_config_found_by_reload_keeps_the_first_error_out_of_the_way(tmp_path):
    # reload() raising ConfigError must never end the wait.
    path, state = tmp_path / "printlog-bridge.cfg", tmp_path / "state"
    calls = []

    def reload() -> BridgeConfig:
        calls.append(1)
        if len(calls) == 1:
            raise ConfigError("broken")
        return load(path, state)

    async def scenario():
        waiting = asyncio.ensure_future(
            wait_for_printers(load(path, state), reload, poll_s=0.01, touch_s=1)
        )
        await eventually(lambda: path.exists())
        os.utime(path, (1, 1))
        await eventually(lambda: calls)
        path.write_text(PRINTER, encoding="utf-8")
        return await asyncio.wait_for(waiting, 5)

    assert asyncio.run(scenario()).printers


@pytest.mark.parametrize("missing_folder", [True, False])
def test_says_where_to_add_printers_whether_or_not_it_could_write_a_starter(
    tmp_path, caplog, missing_folder
):
    folder = tmp_path / ("nope" if missing_folder else "")
    path, state = folder / "printlog-bridge.cfg", tmp_path / "state"
    caplog.set_level(logging.INFO)

    async def scenario():
        waiting = asyncio.ensure_future(_wait(path, state))
        await eventually(lambda: "No printers to watch yet" in caplog.text)
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting

    asyncio.run(scenario())

    assert str(path) in caplog.text
    assert ("starter config" in caplog.text) is not missing_folder
