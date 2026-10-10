from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer

from printlog_bridge.cli.main import main
from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.checks import CheckResult, Status
from printlog_bridge.core.doctor import diagnose, format_report


class FakeAdapter(Adapter):
    name = "fake"
    result = CheckResult.ok("Printer", "fake printer is fine")

    def __init__(self, printer: str) -> None:
        self.printer = printer

    @classmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> FakeAdapter:
        return cls(printer)

    def describe(self) -> str:
        return "fake"

    async def run(self, sink) -> None:  # pragma: no cover
        raise NotImplementedError

    async def check(self):
        return [CheckResult(f"Printer {self.printer}", self.result.status, self.result.detail)]


ADAPTERS = {"fake": FakeAdapter}


def serve_me(status: int = 200):
    """A fake API answering GET /api/Users/me with `status`."""

    async def me(_request):
        if status != 200:
            return web.Response(status=status)
        return web.json_response({"id": 7, "displayName": "Maker"})

    app = web.Application()
    app.router.add_get("/api/Users/me", me)
    return TestServer(app)


def write_config(tmp_path: Path, api_url: str, api_key: str = "k", printers: str = "") -> Path:
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(
        f"[bridge]\napi_url = {api_url}\napi_key = {api_key}\nstate_dir = {tmp_path / 'state'}\n"
        + (printers or "[printer voron]\nadapter = fake\n"),
        encoding="utf-8",
    )
    return path


def run_diagnose(tmp_path: Path, status: int = 200, api_key: str = "k", printers: str = ""):
    async def scenario():
        server = serve_me(status)
        await server.start_server()
        try:
            path = write_config(tmp_path, str(server.make_url("")).rstrip("/"), api_key, printers)
            return await diagnose(path, adapters=ADAPTERS, env={}, home=tmp_path)
        finally:
            await server.close()

    return asyncio.run(scenario())


def by_name(results):
    return {r.name: r for r in results}


def test_a_healthy_setup_passes_every_check(tmp_path):
    results = run_diagnose(tmp_path)

    assert [r.name for r in results] == [
        "Python",
        "Config",
        "State directory",
        "SQLite",
        "3D Print Log API",
        "Printer voron",
    ]
    assert all(r.status is Status.OK for r in results), results
    checks = by_name(results)
    assert "1 printer" in checks["Config"].detail
    assert "signed in as Maker" in checks["3D Print Log API"].detail
    assert (tmp_path / "state").is_dir()


def test_a_missing_config_stops_before_the_other_checks(tmp_path):
    results = asyncio.run(
        diagnose(tmp_path / "missing.cfg", adapters=ADAPTERS, env={}, home=tmp_path)
    )

    assert [r.name for r in results] == ["Python", "Config"]
    assert results[1].status is Status.FAIL
    assert "not found" in results[1].detail


def test_a_missing_api_key_says_where_to_get_one(tmp_path):
    results = run_diagnose(tmp_path, api_key="")

    api = by_name(results)["3D Print Log API"]
    assert api.status is Status.FAIL
    assert "https://www.3dprintlog.com/api-keys" in api.detail
    assert "PRINTLOG_API_KEY" in api.detail


def test_a_rejected_key_fails_the_api_check(tmp_path):
    api = by_name(run_diagnose(tmp_path, status=401))["3D Print Log API"]

    assert api.status is Status.FAIL
    assert "rejected" in api.detail


def test_an_unavailable_api_fails_the_api_check(tmp_path):
    api = by_name(run_diagnose(tmp_path, status=503))["3D Print Log API"]

    assert api.status is Status.FAIL
    assert "503" in api.detail


def test_an_unwritable_state_directory_fails(tmp_path):
    # A file where the directory should be: mkdir fails on every platform.
    (tmp_path / "state").write_text("not a directory")

    state = by_name(run_diagnose(tmp_path))["State directory"]

    assert state.status is Status.FAIL


def test_each_printer_reports_its_own_check(tmp_path):
    printers = "[printer a]\nadapter = fake\n[printer b]\nadapter = fake\n"

    names = [r.name for r in run_diagnose(tmp_path, printers=printers)]

    assert names[-2:] == ["Printer a", "Printer b"]


def test_an_adapter_that_raises_is_reported_not_fatal(tmp_path, monkeypatch):
    async def boom(self):
        raise RuntimeError("adapter bug")

    monkeypatch.setattr(FakeAdapter, "check", boom)

    printer = by_name(run_diagnose(tmp_path))["Printer voron"]

    assert printer.status is Status.FAIL
    assert "adapter bug" in printer.detail


def test_format_report_lists_each_check_and_a_verdict():
    report = format_report(
        [CheckResult.ok("Python", "3.9.2"), CheckResult.fail("Config", "not found: /x")]
    )

    lines = report.splitlines()
    assert lines[0] == "  ok    Python: 3.9.2"
    assert lines[1] == "  FAIL  Config: not found: /x"
    assert lines[-1] == "1 problem found."


def test_format_report_counts_warnings_separately():
    report = format_report(
        [CheckResult.ok("Python", "3.9.2"), CheckResult.warn("Printer u1", "Klipper shutdown")]
    )

    assert report.splitlines()[-1] == "No problems found, 1 warning."


class TestDoctorCommand:
    def test_exits_zero_when_everything_passes(self, tmp_path, capsys, monkeypatch):
        exit_code = self._run(tmp_path, monkeypatch, status=200)

        assert exit_code == 0
        assert "No problems found." in capsys.readouterr().out

    def test_exits_one_when_a_check_fails(self, tmp_path, capsys, monkeypatch):
        exit_code = self._run(tmp_path, monkeypatch, status=401)

        assert exit_code == 1
        assert "FAIL  3D Print Log API" in capsys.readouterr().out

    def test_exits_one_without_a_config_file(self, tmp_path, capsys):
        exit_code = main(["doctor", "--config", str(tmp_path / "missing.cfg")])

        assert exit_code == 1
        assert "FAIL  Config" in capsys.readouterr().out

    @staticmethod
    def _run(tmp_path, monkeypatch, status):
        # The command uses the real adapter registry; swap in the fake so no printer is needed.
        monkeypatch.setattr("printlog_bridge.cli.main.ADAPTERS", ADAPTERS)

        exit_code = {}

        async def scenario():
            server = serve_me(status)
            await server.start_server()
            try:
                path = write_config(tmp_path, str(server.make_url("")).rstrip("/"))
                loop = asyncio.get_running_loop()
                # main() runs its own event loop, so run it off this one.
                exit_code["value"] = await loop.run_in_executor(
                    None, main, ["doctor", "--config", str(path)]
                )
            finally:
                await server.close()

        asyncio.run(scenario())
        return exit_code["value"]
