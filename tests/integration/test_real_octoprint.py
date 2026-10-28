"""The fake OctoPrint against a real one, and the adapter against the real one (#83).

Needs an OctoPrint whose printer is the bundled virtual printer (`serial.port: VIRTUAL`,
`plugins.virtual_printer.enabled: true`, auto-connect on, the first-run wizard done) and a key of
an admin user, given as `OCTOPRINT_URL` and `OCTOPRINT_API_KEY`. Docker's `octoprint/octoprint`
image works, as does `pip install octoprint` in a virtual environment:

    octoprint --basedir <dir> user add admin --password <p> --admin
    octoprint --basedir <dir> config set --bool server.firstRun false
    octoprint --basedir <dir> config set --bool plugins.virtual_printer.enabled true
    octoprint --basedir <dir> config set --bool serial.autoconnect true
    octoprint --basedir <dir> config set serial.port VIRTUAL
    (set the user's `apikey` in <dir>/users.yaml)
    octoprint --basedir <dir> serve --port 15180

Run with `pytest -m integration tests/integration/test_real_octoprint.py`. Skipped without
those variables. It uploads a file and starts prints: never point it at a real printer.
`record_octoprint.py` records `tests/fixtures/octoprint/` from the same setup.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from printlog_bridge.adapters.octoprint import OctoPrintAdapter
from printlog_bridge.core.checks import Status
from printlog_bridge.core.events import JobFinished, JobOutcome, JobStarted

pytestmark = pytest.mark.integration

URL = os.environ.get("OCTOPRINT_URL", "").rstrip("/")
KEY = os.environ.get("OCTOPRINT_API_KEY", "")
CUBE = Path(__file__).parents[1] / "fixtures" / "gcode" / "prusaslicer-2.9.2-calibration-cube.gcode"

if not URL or not KEY:
    pytest.skip("OCTOPRINT_URL and OCTOPRINT_API_KEY name no OctoPrint", allow_module_level=True)


def _fake():
    import sys

    sys.path.insert(0, str(Path(__file__).parents[1]))
    from fake_octoprint import API_KEY, FakeOctoPrint

    return FakeOctoPrint, API_KEY


async def exchange(base: str, key: str) -> dict[str, Any]:
    """The same requests to `base`: what came back, by shape."""
    seen: dict[str, Any] = {}
    async with aiohttp.ClientSession(headers={"X-Api-Key": key}) as http:
        async with http.get(base + "/api/version", headers={"X-Api-Key": "wrong"}) as r:
            seen["wrong key"] = (r.status, sorted((await r.json(content_type=None)).keys()))
        async with http.get(base + "/api/version") as r:
            seen["version"] = sorted((await r.json()).keys())
        async with http.get(base + "/api/job") as r:
            body = await r.json()
            # The file's keys depend on whether one was ever selected: just the ones read.
            file = {"origin", "path", "size"} <= set(body["job"]["file"])
            seen["job"] = (sorted(body), file, sorted(body["progress"]))
        async with http.post(base + "/api/login", json={"passive": True}) as r:
            login = await r.json()
            seen["login"] = {"name", "session"} <= set(login)
        async with http.ws_connect(base.replace("http", "ws", 1) + "/sockjs/websocket") as ws:
            first = json.loads((await ws.receive(timeout=5)).data)
            seen["first"] = sorted(first)
            await ws.send_str(json.dumps({"auth": f"{login['name']}:{login['session']}"}))
            history = json.loads((await ws.receive(timeout=5)).data)
            status = history.get("history", {})
            seen["after auth"] = sorted(history)
            seen["status"] = sorted(
                k for k in status if k in ("job", "progress", "state", "serverTime", "markings")
            )
            seen["flags"] = sorted(status["state"]["flags"])
    return seen


def test_the_fake_answers_as_octoprint_does():
    async def scenario():
        fake_class, fake_key = _fake()
        fake = fake_class()
        await fake.start()
        try:
            return await exchange(fake.url, fake_key), await exchange(URL, KEY)
        finally:
            await fake.close()

    fake, real = asyncio.run(scenario())
    assert fake == real


class Sink:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)

    def of(self, kind):
        return [e for e in self.events if isinstance(e, kind)]


class Report:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


async def until(predicate, timeout: float = 120.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition not met within the timeout"
        await asyncio.sleep(0.2)


async def ready(http: aiohttp.ClientSession) -> None:
    """Waits for the printer to be idle, cancelling whatever an earlier run left printing."""
    for _ in range(300):
        async with http.get(URL + "/api/printer") as r:
            flags = (await r.json())["state"]["flags"]
        if flags["ready"] and not (flags["printing"] or flags["paused"] or flags["finishing"]):
            return
        if flags["printing"] or flags["paused"]:
            async with http.post(URL + "/api/job", json={"command": "cancel"}):
                pass
        await asyncio.sleep(0.2)
    raise AssertionError("the printer never became ready")


def test_the_adapter_logs_a_print_and_a_cancel():
    async def scenario():
        adapter = OctoPrintAdapter.from_config("real", {"url": URL, "api_key": KEY})
        [check] = await adapter.check()
        sink = Sink()
        async with aiohttp.ClientSession(headers={"X-Api-Key": KEY}) as http:
            await ready(http)
        task = asyncio.ensure_future(adapter.run(sink, Report()))
        try:
            async with aiohttp.ClientSession(headers={"X-Api-Key": KEY}) as http:
                form = aiohttp.FormData()
                form.add_field("file", CUBE.read_bytes(), filename="plb-cube.gcode")
                async with http.post(URL + "/api/files/local", data=form) as r:
                    assert r.status == 201
                await asyncio.sleep(2)
                command = {"command": "select", "print": True}
                async with http.post(URL + "/api/files/local/plb-cube.gcode", json=command) as r:
                    assert r.status == 204
                await until(lambda: sink.of(JobFinished))
                form = aiohttp.FormData()  # a dwell, so the cancel lands mid-print
                slow = CUBE.read_bytes().replace(b"M83 ", b"G4 S20\nM83 ", 1)
                form.add_field("file", slow, filename="plb-slow.gcode")
                async with http.post(URL + "/api/files/local", data=form) as r:
                    assert r.status == 201
                await ready(http)
                async with http.post(URL + "/api/files/local/plb-slow.gcode", json=command) as r:
                    assert r.status == 204
                await until(lambda: len(sink.of(JobStarted)) == 2)
                async with http.post(URL + "/api/job", json={"command": "cancel"}) as r:
                    assert r.status == 204
                await until(lambda: len(sink.of(JobFinished)) == 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return check, sink

    check, sink = asyncio.run(scenario())
    assert check.status is Status.OK
    done, cancelled = sink.of(JobFinished)
    assert done.outcome is JobOutcome.COMPLETED
    assert done.filament_mm == {0: 1332.4}  # the slicer's
    assert cancelled.outcome is JobOutcome.CANCELLED
    starts = sink.of(JobStarted)
    assert starts[0].job_id != starts[1].job_id
