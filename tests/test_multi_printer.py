"""#12's acceptance: one bridge process, as the Docker image runs it, watches three printers.

The process gets its config, state directory and API key the way the container does (from
PRINTLOG_BRIDGE_CONFIG, PRINTLOG_BRIDGE_STATE_DIR and PRINTLOG_API_KEY), and each printer is a
separate Moonraker reached over its WebSocket (remote mode).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from fake_moonraker import FakeMoonraker, eventually
from fake_printlog_api import FakePrintLogApi

PRINTERS = {"voron": 41, "u1": 42, "k2": 43}


def container_env(tmp_path: Path, config: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRINTLOG")}
    env.update(
        PRINTLOG_BRIDGE_CONFIG=str(config),
        PRINTLOG_BRIDGE_STATE_DIR=str(tmp_path / "data" / "state"),
        PRINTLOG_API_KEY="key-123",
        HOME=str(tmp_path / "data"),
    )
    return env


async def printlog_bridge(*args: str, env: dict[str, str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "printlog_bridge", *args, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip


def test_one_bridge_process_logs_prints_from_three_remote_printers(tmp_path):
    async def scenario():
        api = FakePrintLogApi(printers=tuple(PRINTERS.values()))
        moonrakers = {name: FakeMoonraker() for name in PRINTERS}
        api_url = await api.start()
        urls = {name: await m.start() for name, m in moonrakers.items()}
        config = tmp_path / "data" / "printlog-bridge.cfg"
        config.parent.mkdir()
        config.write_text(
            f"[bridge]\napi_url = {api_url}\n"
            + "".join(
                f"[printer {name}]\nprinter_id = {PRINTERS[name]}\nurl = {urls[name]}\n"
                for name in PRINTERS
            ),
            encoding="utf-8",
        )
        env = container_env(tmp_path, config)
        bridge = await printlog_bridge("run", env=env)
        try:
            await eventually(lambda: all(m.subscriptions for m in moonrakers.values()), timeout=20)
            jobs = {name: await m.start_job(f"{name}_part.gcode") for name, m in moonrakers.items()}
            await eventually(lambda: len(api.prints) == 3, timeout=10)
            for name, m in moonrakers.items():
                await m.finish_job(jobs[name])
            await eventually(lambda: all(p["status"] == 3 for p in api.prints.values()), timeout=10)

            status = await printlog_bridge("status", env=env)
            status_out, _ = await status.communicate()
            return list(api.prints.values()), status.returncode, status_out.decode()
        finally:
            bridge.kill()
            await bridge.wait()
            for m in moonrakers.values():
                await m.close()
            await api.close()

    prints, status_code, status_out = asyncio.run(scenario())

    assert sorted((p["printerId"], p["fileName"]) for p in prints) == [
        (41, "voron_part.gcode"),
        (42, "u1_part.gcode"),
        (43, "k2_part.gcode"),
    ]
    assert status_code == 0, status_out
    for name, printer_id in PRINTERS.items():
        assert f"Printer {name} (printer {printer_id} in 3D Print Log)" in status_out
    assert status_out.count("Connection: connected") == 3, status_out
    assert (tmp_path / "data" / "state" / "outbox.sqlite3").exists()
