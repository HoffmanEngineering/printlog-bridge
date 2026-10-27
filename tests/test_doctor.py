from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import zipfile
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fake_moonraker import FakeMoonraker

from printlog_bridge import __version__
from printlog_bridge.adapters.moonraker import MoonrakerAdapter
from printlog_bridge.adapters.moonraker.locator import config_key
from printlog_bridge.cli.main import main
from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.checks import CheckResult, Status
from printlog_bridge.core.doctor import diagnose, format_report
from printlog_bridge.core.outbox import Outbox
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.status import StatusBoard
from printlog_bridge.core.support import write_support_bundle


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

    async def run(self, sink, report) -> None:  # pragma: no cover
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

    async def printer(request):
        if request.match_info["id"] != "7":
            return web.Response(status=404)
        return web.json_response({"id": 7, "name": "Voron 2.4"})

    app = web.Application()
    app.router.add_get("/api/Users/me", me)
    app.router.add_get("/api/Printers/{id}", printer)
    return TestServer(app)


def write_config(
    tmp_path: Path, api_url: str, api_key: str = "k", printers: str = "", bridge: str = ""
) -> Path:
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(
        f"[bridge]\napi_url = {api_url}\napi_key = {api_key}\nstate_dir = {tmp_path / 'state'}\n"
        + bridge
        + (printers or "[printer voron]\nadapter = fake\nprinter_id = 7\n"),
        encoding="utf-8",
    )
    return path


def run_diagnose(
    tmp_path: Path,
    status: int = 200,
    api_key: str = "k",
    printers: str = "",
    clock=None,
    bridge: str = "",
):
    async def scenario():
        server = serve_me(status)
        await server.start_server()
        try:
            url = str(server.make_url("")).rstrip("/")
            path = write_config(tmp_path, url, api_key, printers, bridge)
            kwargs = {"clock": clock} if clock else {}
            return await diagnose(path, adapters=ADAPTERS, env={}, home=tmp_path, **kwargs)
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
        "Version",
        "State directory",
        "SQLite",
        "API key",
        "3D Print Log API",
        "Clock",
        "Printer voron",
        "Printer voron in 3D Print Log",
    ]
    assert all(r.status is Status.OK for r in results), results
    checks = by_name(results)
    assert "1 printer" in checks["Config"].detail
    assert "signed in as Maker" in checks["3D Print Log API"].detail
    assert checks["Printer voron in 3D Print Log"].detail == "printer 7, Voron 2.4"
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
    printers = "[printer a]\nadapter = fake\nprinter_id = 7\n[printer b]\nadapter = fake\n"

    names = [r.name for r in run_diagnose(tmp_path, printers=printers)]

    assert names[-4:] == [
        "Printer a", "Printer a in 3D Print Log", "Printer b", "Printer b in 3D Print Log"
    ]  # fmt: skip


def test_a_printer_without_a_printer_id_says_where_to_find_it(tmp_path):
    results = run_diagnose(tmp_path, printers="[printer voron]\nadapter = fake\n")

    linked = by_name(results)["Printer voron in 3D Print Log"]
    assert linked.status is Status.FAIL
    assert "printer_id" in linked.detail
    assert "https://www.3dprintlog.com/printers/" in linked.detail


def test_a_printer_id_that_is_not_the_accounts_fails(tmp_path):
    results = run_diagnose(tmp_path, printers="[printer voron]\nadapter = fake\nprinter_id = 8\n")

    linked = by_name(results)["Printer voron in 3D Print Log"]
    assert linked.status is Status.FAIL
    assert "no printer 8" in linked.detail


def test_the_printer_id_is_not_looked_up_when_the_api_check_failed(tmp_path):
    names = [r.name for r in run_diagnose(tmp_path, status=401)]

    assert "Printer voron in 3D Print Log" not in names


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


class ServedLikeMoonraker(FakeAdapter):
    def serves(self, path: Path) -> str | None:
        return MoonrakerAdapter.serves(self, path)  # type: ignore[arg-type]


class ServedLikeOctoPrint(FakeAdapter):
    """An adapter whose host serves its uploads folder, as OctoPrint does."""

    def serves(self, path: Path) -> str | None:
        if "uploads" in path.parts:
            return "OctoPrint serves to anyone who can open it"
        return None


class TestPrinterSecretStorage:
    """A printer's own secret (a Bambu access code) in a config an adapter's host serves."""

    def check(self, config_dir: Path, printer: str, adapter: type[Adapter] = ServedLikeMoonraker):
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / "printlog-bridge.cfg"
        path.write_text(
            f"[bridge]\napi_url = http://127.0.0.1:9\nstate_dir = {config_dir.parent / 'state'}\n"
            f"[printer x1c]\nadapter = fake\nprinter_id = 7\n{printer}",
            encoding="utf-8",
        )
        results = asyncio.run(
            diagnose(path, adapters={"fake": adapter}, env={}, home=config_dir.parent)
        )
        return by_name(results).get("Printer x1c secrets")

    def test_an_access_code_in_a_served_config_is_a_warning(self, tmp_path):
        check = self.check(tmp_path / "printer_data" / "config", "access_code = 12345678\n")

        assert check.status is Status.WARN
        assert "access_code" in check.detail
        assert "Moonraker serves" in check.detail
        assert "12345678" not in check.detail

    def test_a_prusalink_password_in_a_served_config_is_a_warning(self, tmp_path):
        check = self.check(tmp_path / "printer_data" / "config", "password = Xk3pT9vQ2mWz\n")

        assert check.status is Status.WARN
        assert "password" in check.detail
        assert "Xk3pT9vQ2mWz" not in check.detail

    def test_an_access_code_elsewhere_says_nothing(self, tmp_path):
        assert self.check(tmp_path / "nas", "access_code = 12345678\n") is None

    def test_no_secret_says_nothing(self, tmp_path):
        assert self.check(tmp_path / "printer_data" / "config", "") is None


class BrokenServes(FakeAdapter):
    def serves(self, path: Path) -> str | None:
        raise RuntimeError("bug")


class TestApiKeyStorage:
    """Moonraker serves its config folder to anyone who can open Mainsail or Fluidd, and other
    adapters' hosts serve folders of their own (`Adapter.serves`)."""

    def key_check(
        self,
        config_dir: Path,
        bridge: str,
        env=None,
        key_file: str | None = None,
        adapter: type[Adapter] = ServedLikeMoonraker,
    ):
        config_dir.mkdir(parents=True, exist_ok=True)
        state = config_dir.parent / "state"
        if key_file is not None:
            state.mkdir(parents=True, exist_ok=True)
            (state / "api_key").write_text(key_file, encoding="utf-8")
            (state / "api_key").chmod(0o600)  # as store-key writes it
        path = config_dir / "printlog-bridge.cfg"
        path.write_text(
            f"[bridge]\napi_url = http://127.0.0.1:9\nstate_dir = {state}\n{bridge}"
            "[printer voron]\nadapter = fake\nprinter_id = 7\n",
            encoding="utf-8",
        )
        results = asyncio.run(
            diagnose(path, adapters={"fake": adapter}, env=env or {}, home=config_dir.parent)
        )
        return by_name(results).get("API key")

    def test_a_key_in_a_folder_another_adapters_host_serves_names_it(self, tmp_path):
        check = self.key_check(
            tmp_path / "uploads" / "config", "api_key = k\n", adapter=ServedLikeOctoPrint
        )

        assert check.status is Status.WARN
        assert "OctoPrint serves to anyone who can open it" in check.detail
        assert "Moonraker" not in check.detail

    def test_a_key_file_in_a_served_folder_says_to_move_the_state_dir(self, tmp_path):
        check = self.key_check(
            tmp_path / "uploads" / "config", "", key_file="k\n", adapter=ServedLikeOctoPrint
        )

        assert check.status is Status.WARN
        assert "OctoPrint" in check.detail
        assert "state_dir" in check.detail

    def test_a_folder_named_like_klippers_is_fine_for_an_adapter_without_moonraker(self, tmp_path):
        check = self.key_check(
            tmp_path / "printer_data" / "config", "api_key = k\n", adapter=FakeAdapter
        )

        assert check.status is Status.OK

    def test_an_adapter_that_raises_doesnt_hide_the_key_check(self, tmp_path):
        check = self.key_check(tmp_path / "config", "api_key = k\n", adapter=BrokenServes)

        assert check.status is Status.OK

    def test_a_key_in_a_config_moonraker_serves_is_a_warning_saying_how_to_move_it(self, tmp_path):
        config_dir = tmp_path / "printer_data" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "moonraker.conf").write_text("[server]\n", encoding="utf-8")

        check = self.key_check(config_dir, "api_key = k\n")

        assert check.status is Status.WARN
        assert "Moonraker" in check.detail
        assert "printlog-bridge store-key" in check.detail

    def test_a_key_in_a_config_folder_named_like_klippers_is_a_warning(self, tmp_path):
        check = self.key_check(tmp_path / "printer_data" / "config", "api_key = k\n")

        assert check.status is Status.WARN

    def test_a_key_in_a_config_elsewhere_is_fine(self, tmp_path):
        check = self.key_check(tmp_path / ".config" / "printlog-bridge", "api_key = k\n")

        assert check.status is Status.OK
        assert "printlog-bridge.cfg" in check.detail

    def test_a_key_in_the_key_file_is_fine(self, tmp_path):
        check = self.key_check(tmp_path / "printer_data" / "config", "", key_file="k\n")

        assert check.status is Status.OK
        assert str(tmp_path / "printer_data" / "state" / "api_key") in check.detail

    def test_a_key_from_the_environment_is_fine(self, tmp_path):
        check = self.key_check(
            tmp_path / "printer_data" / "config", "", env={"PRINTLOG_API_KEY": "k"}
        )

        assert check.status is Status.OK
        assert "PRINTLOG_API_KEY" in check.detail

    def test_a_key_left_in_a_served_config_is_a_warning_even_when_unused(self, tmp_path):
        check = self.key_check(
            tmp_path / "printer_data" / "config", "api_key = old\n", key_file="new\n"
        )

        assert check.status is Status.WARN
        assert "isn't used" in check.detail

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_a_key_file_others_can_read_is_a_warning(self, tmp_path):
        config_dir = tmp_path / ".config" / "printlog-bridge"
        state = tmp_path / ".config" / "state"
        state.mkdir(parents=True)
        (state / "api_key").write_text("k\n", encoding="utf-8")
        (state / "api_key").chmod(0o644)

        check = self.key_check(config_dir, "")

        assert check.status is Status.WARN
        assert "chmod 600" in check.detail

    def test_no_key_leaves_it_to_the_api_check(self, tmp_path):
        assert self.key_check(tmp_path / "config", "") is None


def now_plus(offset: timedelta):
    return lambda: datetime.now(timezone.utc) + offset


def test_a_clock_in_step_with_the_api_passes(tmp_path):
    clock = by_name(run_diagnose(tmp_path))["Clock"]

    assert clock.status is Status.OK
    assert "3D Print Log" in clock.detail


def test_a_clock_off_by_minutes_is_a_warning_saying_how_to_fix_it(tmp_path):
    clock = by_name(run_diagnose(tmp_path, clock=now_plus(timedelta(minutes=5))))["Clock"]

    assert clock.status is Status.WARN
    assert "ahead" in clock.detail
    assert "NTP" in clock.detail


def test_a_clock_behind_says_so(tmp_path):
    clock = by_name(run_diagnose(tmp_path, clock=now_plus(-timedelta(minutes=5))))["Clock"]

    assert "behind" in clock.detail


def test_the_clock_is_not_checked_without_the_api(tmp_path):
    assert "Clock" not in by_name(run_diagnose(tmp_path, status=503))


class TestSupportBundle:
    SECRETS = ("api-key-secret-1", "moonraker-secret-2", "env-key-secret-3")

    def config(self, tmp_path, api_url):
        path = tmp_path / "printlog-bridge.cfg"
        path.write_text(
            f"[bridge]\napi_url = {api_url}\napi_key = {self.SECRETS[0]}\n"
            f"state_dir = {tmp_path / 'state'}\n"
            "[printer voron]\nadapter = fake\nprinter_id = 7\nurl = http://voron:7125\n"
            f"moonraker_api_key = {self.SECRETS[1]}\n"
            "[printer u1]\nadapter = fake\nurl = http://maker:hunter2@u1:7125\n",
            encoding="utf-8",
        )
        return path

    def doctor(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("printlog_bridge.cli.main.ADAPTERS", ADAPTERS)
        state = tmp_path / "state"
        # Secrets where a bug could leak them: the status file and the log.
        board = StatusBoard(state)
        board.start(["voron"])
        board.printer("voron").warn("api", f"refused key {self.SECRETS[2]}")
        (state / "bridge.log").write_text(
            f"INFO started\nERROR X-Api-Key: {self.SECRETS[0]} and {self.SECRETS[1]}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("PRINTLOG_API_KEY", self.SECRETS[2])
        outbox = Outbox.open(state)
        outbox.add("voron", "00000A", "start", {"file_name": "benchy.gcode"})
        outbox.failed(1, f"the API answered 503 (key {self.SECRETS[0]})")
        outbox.close()
        # A trace, should one ever hold a secret its own scrubbing missed.
        (state / "traces").mkdir()
        (state / "traces" / "voron-20261012.jsonl").write_text(
            f'{{"t": 1.0, "dir": "send", "msg": {{"note": "{self.SECRETS[1]}"}}}}\n',
            encoding="utf-8",
        )

        async def scenario():
            server = serve_me(401)  # the env key is not the API's, which doesn't matter here
            await server.start_server()
            try:
                path = self.config(tmp_path, str(server.make_url("")).rstrip("/"))
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, main, ["doctor", "--config", str(path)])
            finally:
                await server.close()

        asyncio.run(scenario())
        return state / "printlog-bridge-support.zip", capsys.readouterr().out

    def members(self, bundle: Path) -> dict[str, str]:
        with zipfile.ZipFile(bundle) as z:
            return {name: z.read(name).decode("utf-8") for name in z.namelist()}

    def test_doctor_writes_a_bundle_and_says_where(self, tmp_path, monkeypatch, capsys):
        bundle, out = self.doctor(tmp_path, monkeypatch, capsys)

        assert bundle.is_file()
        assert f"Support bundle: {bundle}" in out

    def test_the_bundle_holds_what_support_needs(self, tmp_path, monkeypatch, capsys):
        bundle, _ = self.doctor(tmp_path, monkeypatch, capsys)

        members = self.members(bundle)
        assert set(members) >= {
            "doctor.txt",
            "environment.txt",
            "printlog-bridge.cfg",
            "status.json",
            "bridge.log",
            "outbox.json",
        }
        assert "3D Print Log API" in members["doctor.txt"]
        assert "printlog-bridge" in members["environment.txt"]
        assert "[printer voron]" in members["printlog-bridge.cfg"]
        assert "printer_id = 7" in members["printlog-bridge.cfg"]
        [entry] = json.loads(members["outbox.json"])
        assert entry["job_id"] == "00000A"
        assert entry["payload"] == {"file_name": "benchy.gcode"}
        assert entry["attempts"] == 1
        assert '"dir": "send"' in members["traces/voron-20261012.jsonl"]

    def test_the_bundle_contains_no_key_or_token(self, tmp_path, monkeypatch, capsys):
        bundle, _ = self.doctor(tmp_path, monkeypatch, capsys)

        raw = bundle.read_bytes()
        members = self.members(bundle)
        for secret in (*self.SECRETS, "hunter2"):
            assert secret.encode() not in raw
            for name, text in members.items():
                assert secret not in text, name
        assert "api_key = <redacted>" in members["printlog-bridge.cfg"]
        assert "moonraker_api_key = <redacted>" in members["printlog-bridge.cfg"]
        assert "http://<redacted>@u1:7125" in members["printlog-bridge.cfg"]

    def test_only_the_latest_traces_are_bundled(self, tmp_path):
        state = tmp_path / "state"
        traces = state / "traces"
        traces.mkdir(parents=True)
        line = '{"t": 1.0, "dir": "recv", "msg": {"pad": "' + "x" * 80 + '"}}\n'
        for n, name in enumerate(["u1-20261010.jsonl", "u1-20261011.jsonl", "u1-20261012.jsonl"]):
            path = traces / name
            path.write_bytes((line * 10).encode())  # about 1 KB each
            os.utime(path, (1_000_000 + n, 1_000_000 + n))
        config = tmp_path / "printlog-bridge.cfg"
        config.write_text("[printer u1]\n", encoding="utf-8")

        bundle = write_support_bundle(
            state / "bundle.zip",
            report="",
            config_path=config,
            state_dir=state,
            env={},
            trace_bytes=1500,
        )

        members = self.members(bundle)
        traced = sorted(name for name in members if name.startswith("traces/"))
        assert traced == ["traces/u1-20261011.jsonl", "traces/u1-20261012.jsonl"]
        older = members["traces/u1-20261011.jsonl"]
        assert len(older) < len(line * 10)  # its newest lines only, whole
        assert older.endswith("\n")
        assert all(json.loads(each) for each in older.splitlines())
        assert len(members["traces/u1-20261012.jsonl"]) == len(line * 10)

    def test_no_bundle_without_a_config(self, tmp_path, capsys):
        main(["doctor", "--config", str(tmp_path / "missing.cfg")])

        assert "Support bundle" not in capsys.readouterr().out


# -- a remote printer found at another address (#32) ------------------------------------------

OLD = "127.0.0.87"
NEW = "127.0.0.91"
"""The fake Moonraker serves on NEW, as a printer DHCP moved; the config still names OLD."""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestMovedPrinter:
    """`run` connects where discovery last found the printer, so doctor checks there too."""

    @pytest.fixture
    def port(self):
        return free_port()

    @staticmethod
    def url(port: int, host: str = OLD) -> str:
        return f"http://{host}:{port}"

    @staticmethod
    def state_file(tmp_path: Path) -> Path:
        return tmp_path / "state" / "printers" / "u1.json"

    def remember(self, tmp_path: Path, configured_url: str, address: str) -> Path:
        """Writes the state file as the Locator leaves it after finding the printer at `address`."""
        path = self.state_file(tmp_path)
        path.parent.mkdir(parents=True)
        discovery = {
            "config": config_key(configured_url),
            "address": address,
            "moved": {"from": OLD, "to": address, "at": "2026-10-13T12:00:00+00:00"},
        }
        path.write_text(
            json.dumps(
                {"instance_id": "abc", "jobs": {"j1": {"started_at": "x"}}, "discovery": discovery}
            ),
            encoding="utf-8",
        )
        return path

    def check(self, tmp_path: Path, port: int, serve_on: str | None, options: str = ""):
        """Runs doctor against a moonraker printer configured at OLD, with the fake Moonraker on
        `serve_on` (or nowhere), and returns the printer's check."""

        async def scenario():
            fake = FakeMoonraker()
            if serve_on is not None:
                await fake.start(host=serve_on, port=port)
            server = serve_me()
            await server.start_server()
            try:
                printers = f"[printer u1]\nadapter = moonraker\nurl = {self.url(port)}\n{options}"
                path = write_config(
                    tmp_path, str(server.make_url("")).rstrip("/"), printers=printers
                )
                return await diagnose(
                    path, adapters={"moonraker": MoonrakerAdapter}, env={}, home=tmp_path
                )
            finally:
                await server.close()
                await fake.close()

        return by_name(asyncio.run(scenario()))["Printer u1"]

    def test_says_where_the_printer_was_found_and_how_to_keep_it_there(self, tmp_path, port):
        self.remember(tmp_path, self.url(port), NEW)

        result = self.check(tmp_path, port, serve_on=NEW)

        assert result.status is Status.WARN
        assert f"found at {NEW} (the config says {OLD})" in result.detail
        assert "a DHCP reservation keeps it in one place" in result.detail
        assert "Klipper ready" in result.detail

    def test_without_a_state_file_only_the_configured_url_is_checked(self, tmp_path, port):
        result = self.check(tmp_path, port, serve_on=NEW)

        assert result.status is Status.FAIL
        assert f"can't reach Moonraker at {self.url(port)}" in result.detail
        assert "last found" not in result.detail

    def test_never_creates_the_state_file(self, tmp_path, port):
        self.check(tmp_path, port, serve_on=NEW)

        assert not self.state_file(tmp_path).exists()

    def test_never_changes_the_state_file(self, tmp_path, port):
        path = self.remember(tmp_path, self.url(port), NEW)
        before = path.read_bytes()

        self.check(tmp_path, port, serve_on=NEW)

        assert path.read_bytes() == before
        assert sorted(p.name for p in path.parent.iterdir()) == ["u1.json"]

    def test_a_broken_state_file_is_left_where_it_is(self, tmp_path, port):
        path = self.state_file(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")

        result = self.check(tmp_path, port, serve_on=NEW)

        assert result.status is Status.FAIL
        assert path.read_text(encoding="utf-8") == "{not json"
        assert not path.with_suffix(".broken").exists()

    def test_back_at_the_configured_address_is_fine(self, tmp_path, port):
        self.remember(tmp_path, self.url(port), NEW)

        result = self.check(tmp_path, port, serve_on=OLD)

        assert result.status is Status.OK
        assert "found at" not in result.detail

    def test_at_neither_address_names_both(self, tmp_path, port):
        self.remember(tmp_path, self.url(port), NEW)

        result = self.check(tmp_path, port, serve_on=None)

        assert result.status is Status.FAIL
        assert f"can't reach Moonraker at {self.url(port)}" in result.detail
        assert f"nor at {NEW} where it was last found" in result.detail

    def test_discovery_off_checks_only_the_configured_url(self, tmp_path, port):
        self.remember(tmp_path, self.url(port), NEW)

        result = self.check(tmp_path, port, serve_on=NEW, options="discovery = false\n")

        assert result.status is Status.FAIL
        assert "found at" not in result.detail

    def test_an_address_found_for_an_earlier_url_is_ignored(self, tmp_path, port):
        # The url in the config changed since, so `run` starts afresh from it too.
        self.remember(tmp_path, self.url(port, "127.0.0.50"), NEW)

        result = self.check(tmp_path, port, serve_on=NEW)

        assert result.status is Status.FAIL
        assert "found at" not in result.detail


class TestPeek:
    def test_reads_the_discovery_section(self, tmp_path):
        state = PrinterState.load(tmp_path, "u1")
        state.discovery = {"address": NEW}
        state.save()

        peeked = PrinterState.peek(tmp_path, "u1")

        assert peeked is not None
        assert peeked.instance_id == state.instance_id
        assert peeked.discovery == {"address": NEW}

    def test_a_missing_file_is_none_and_stays_missing(self, tmp_path):
        assert PrinterState.peek(tmp_path, "u1") is None
        assert not (tmp_path / "printers").exists()

    def test_cannot_be_saved_so_jobs_it_didnt_read_are_never_lost(self, tmp_path):
        PrinterState.load(tmp_path, "u1").save()
        peeked = PrinterState.peek(tmp_path, "u1")
        assert peeked is not None

        with pytest.raises(RuntimeError, match="peek"):
            peeked.save()


class TestVersion:
    """`doctor` says when a newer bridge is out, as the running bridge last heard it from 3D
    Print Log (#39). It asks nobody itself."""

    def heard(self, tmp_path, latest):
        board = StatusBoard(tmp_path / "state")
        board.start(["voron"])
        board.latest_version(latest)

    def test_names_this_version(self, tmp_path):
        check = by_name(run_diagnose(tmp_path))["Version"]

        assert check.status is Status.OK
        assert check.detail == __version__

    def test_an_older_bridge_is_a_warning_saying_how_to_update(self, tmp_path):
        self.heard(tmp_path, "999.0.0")

        check = by_name(run_diagnose(tmp_path))["Version"]

        assert check.status is Status.WARN
        assert check.detail.startswith(
            f"printlog-bridge 999.0.0 is available (this is {__version__})."
        )
        assert "docker compose pull" in check.detail

    def test_the_latest_bridge_says_so(self, tmp_path):
        self.heard(tmp_path, __version__)

        check = by_name(run_diagnose(tmp_path))["Version"]

        assert check.status is Status.OK
        assert check.detail == f"{__version__}, the latest"

    def test_update_check_off_says_nothing_about_updates(self, tmp_path):
        self.heard(tmp_path, "999.0.0")

        check = by_name(run_diagnose(tmp_path, bridge="update_check = false\n"))["Version"]

        assert check.status is Status.OK
        assert check.detail == __version__
