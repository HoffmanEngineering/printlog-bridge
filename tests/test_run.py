from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Mapping
from pathlib import Path

from fake_moonraker import FakeMoonraker, eventually

from printlog_bridge.cli.main import main
from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.bridge import run_bridge
from printlog_bridge.core.config import BridgeConfig, PrinterConfig


class WatchingAdapter(Adapter):
    name = "watching"

    def __init__(self, printer: str) -> None:
        self.printer = printer
        self.started = asyncio.Event()
        self.cancelled = False
        self.sink = None

    @classmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> WatchingAdapter:
        return cls(printer)

    def describe(self) -> str:
        return "watching"

    async def run(self, sink) -> None:
        self.sink = sink
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def check(self):  # pragma: no cover
        return []


def config_with(*adapters: WatchingAdapter) -> BridgeConfig:
    return BridgeConfig(
        path=Path("printlog-bridge.cfg"),
        api_url="https://api.example.test",
        api_key="k",
        state_dir=Path("state"),
        printers=[PrinterConfig(a.printer, a) for a in adapters],
    )


def test_runs_every_printer_until_cancelled():
    async def scenario():
        voron, u1 = WatchingAdapter("voron"), WatchingAdapter("u1")
        task = asyncio.ensure_future(run_bridge(config_with(voron, u1)))
        await asyncio.wait_for(voron.started.wait(), 5)
        await asyncio.wait_for(u1.started.wait(), 5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return voron, u1

    voron, u1 = asyncio.run(scenario())

    assert voron.cancelled and u1.cancelled
    assert voron.sink is not None


def test_run_with_a_bad_config_says_why_and_fails(tmp_path, capsys):
    assert main(["run", "--config", str(tmp_path / "missing.cfg")]) == 2

    err = capsys.readouterr().err
    assert "not found" in err
    assert "printlog-bridge doctor" in err


def test_the_bridge_process_shows_up_in_moonraker(tmp_path):
    """The acceptance check for #3: a real `printlog-bridge run` identifies to Moonraker."""

    async def scenario():
        fake = FakeMoonraker()
        base = await fake.start()
        config = tmp_path / "printlog-bridge.cfg"
        config.write_text(
            f"[bridge]\nstate_dir = {tmp_path / 'state'}\n[printer u1]\nurl = {base}\n",
            encoding="utf-8",
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "printlog_bridge", "run", "--config", str(config),
            stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip
        try:
            await eventually(lambda: fake.subscriptions, timeout=20)
            return fake.identified[0]
        finally:
            process.kill()
            await process.wait()
            await fake.close()

    identity = asyncio.run(scenario())

    assert identity["client_name"] == "printlog-bridge"
    assert identity["type"] == "agent"
