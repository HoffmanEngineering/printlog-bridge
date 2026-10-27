"""Printers connected in the app (#100): the whole bridge applying its desired list live."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from bridge_helpers import until
from fake_moonraker import FakeMoonraker
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.core import bridge_sync
from printlog_bridge.core.app_printers import Refused, resolve_private
from printlog_bridge.core.bridge import run_bridge
from printlog_bridge.core.config import ConfigError, load_config
from printlog_bridge.core.device import write_device_token
from printlog_bridge.core.status import read_status


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(bridge_sync, "MIN_POLL_S", 0.05)


async def as_is(host: str) -> str:
    return host


async def nothing(services, wait):
    return []


async def no_port(port, on_datagram):
    return None


def write(tmp_path: Path, api_url: str, printers: str = "") -> Path:
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(
        f"[bridge]\napi_url = {api_url}\nstate_dir = {tmp_path / 'state'}\n{printers}",
        encoding="utf-8",
    )
    return path


@contextlib.asynccontextmanager
async def running(tmp_path, printers="", resolve=as_is, before=None):
    api = FakePrintLogApi(printers=(42, 43))
    api.desired_poll_seconds = 0.05
    url = await api.start()
    token = api.issue_device_token()
    write_device_token(
        tmp_path / "state", token=token, api_url=url, grant_id=1, name="b",
        scopes=sorted(api.grant_for(token)["scopes"]),
    )  # fmt: skip
    path = write(tmp_path, url, printers)

    def load():
        return load_config(path, ADAPTERS, env={}, home=tmp_path, printers_required=False)

    if before is not None:
        await before(api, url)
    task = asyncio.ensure_future(
        run_bridge(
            load(),
            adapters=ADAPTERS,
            reload=load,
            reload_poll=0.05,
            heartbeat_interval=0.1,
            sync_options={
                "resolve": resolve,
                "discovery_options": {"ask": nothing, "bind": no_port},
            },
        )
    )
    try:
        await until(lambda: api.bridges, 5)
        yield api, path, task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await api.close()


def app_state(tmp_path: Path) -> dict:
    status = read_status(tmp_path / "state") or {}
    return ((status.get("bridge") or {}).get("app_printers")) or {}


def test_a_printer_connected_in_the_app_is_watched_then_let_go(tmp_path):
    async def scenario():
        moonraker = FakeMoonraker()
        murl = await moonraker.start()
        port = int(murl.rsplit(":", 1)[1].split("/")[0])
        try:
            async with running(tmp_path) as (api, _, _):
                entry = api.connect("moonraker", "127.0.0.1", 42, port=port)
                await until(lambda: moonraker.client_count == 1, 5)
                await until(lambda: any(b["printerId"] == 42 for _, b in api.heartbeats), 5)
                state = app_state(tmp_path)[str(entry)]
                assert state["state"] == "watching"
                status = read_status(tmp_path / "state")
                assert f"app-{entry}" in status["printers"]

                api.disconnect(entry)
                await until(lambda: moonraker.client_count == 0, 5)
                await until(
                    lambda: f"app-{entry}" not in read_status(tmp_path / "state")["printers"]
                )
                assert app_state(tmp_path) == {}
                return (tmp_path / "state" / "printers" / f"app-{entry}.json").exists()
        finally:
            await moonraker.close()

    assert asyncio.run(scenario())


def test_the_config_file_wins(tmp_path):
    async def scenario():
        moonraker = FakeMoonraker()
        murl = await moonraker.start()
        port = int(murl.rsplit(":", 1)[1].split("/")[0])
        printers = f"[printer voron]\nprinter_id = 42\nurl = {murl}\n"
        try:
            async with running(tmp_path, printers) as (api, _, _):
                await until(lambda: moonraker.client_count == 1, 5)
                entry = api.connect("moonraker", "127.0.0.1", 43, port=port)
                await until(lambda: str(entry) in app_state(tmp_path), 5)
                await asyncio.sleep(0.3)
                return app_state(tmp_path)[str(entry)], moonraker.client_count
        finally:
            await moonraker.close()

    state, clients = asyncio.run(scenario())
    assert state["state"] == "in the config file"
    assert "[printer voron]" in state["detail"]
    assert clients == 1


def test_the_same_printer_id_in_the_config_wins_too(tmp_path):
    async def scenario():
        printers = "[printer mk4]\nprinter_id = 42\nadapter = prusalink\nhost = 127.0.0.1\nport = 9\npassword = x\n"  # noqa: E501
        async with running(tmp_path, printers) as (api, _, _):
            entry = api.connect("moonraker", "192.168.1.50", 42)
            await until(lambda: str(entry) in app_state(tmp_path), 5)
            return app_state(tmp_path)[str(entry)]["state"]

    assert asyncio.run(scenario()) == "in the config file"


def test_a_printer_that_needs_a_password_waits_for_its_section_then_starts(tmp_path, caplog):
    async def scenario():
        async with running(tmp_path) as (api, path, task):
            entry = api.connect("prusalink", "127.0.0.1", 42, port=9)
            await until(lambda: str(entry) in app_state(tmp_path), 5)
            waiting = dict(app_state(tmp_path)[str(entry)])
            with path.open("a", encoding="utf-8") as f:
                f.write(f"\n[app_printer {entry}]\npassword = s3cret-pass\n")
            await until(lambda: app_state(tmp_path)[str(entry)]["state"] == "watching", 5)
            assert not task.done()  # applied without a restart
            return entry, waiting

    entry, waiting = asyncio.run(scenario())
    assert waiting["state"] == "needs credentials"
    assert f"[app_printer {entry}]" in waiting["detail"]
    assert "password" in waiting["detail"]
    assert "s3cret-pass" not in caplog.text


def test_a_host_that_isnt_private_is_refused(tmp_path):
    async def scenario():
        async with running(tmp_path, resolve=resolve_private) as (api, _, _):
            entry = api.connect("moonraker", "8.8.8.8", 42)
            await until(lambda: str(entry) in app_state(tmp_path), 5)
            return app_state(tmp_path)[str(entry)]

    state = asyncio.run(scenario())
    assert state["state"] == "refused" and "8.8.8.8" in state["detail"]


@pytest.mark.parametrize("host", ["8.8.8.8", "127.0.0.1", "localhost"])
def test_resolving_refuses_anything_but_a_private_address(host):
    with pytest.raises(Refused):
        asyncio.run(resolve_private(host))


def test_a_private_literal_resolves_to_itself():
    assert asyncio.run(resolve_private("192.168.1.50")) == "192.168.1.50"


def test_never_watches_what_it_only_found(tmp_path):
    """Discovery reports; nothing found is watched unless the desired list names it."""
    from printlog_bridge.core.mdns import MdnsAnswer

    async def scenario():
        moonraker = FakeMoonraker()
        murl = await moonraker.start()
        port = int(murl.rsplit(":", 1)[1].split("/")[0])

        async def ask(services, wait):
            return [MdnsAnswer("_moonraker._tcp.local", "v", "v.local", "127.0.0.1", port, {})]

        try:
            api = FakePrintLogApi()
            url = await api.start()
            token = api.issue_device_token()
            write_device_token(tmp_path / "state", token=token, api_url=url, grant_id=1,
                               name="b", scopes=["bridge:sync", "connections:write"])  # fmt: skip
            config = load_config(write(tmp_path, url), ADAPTERS, env={}, home=tmp_path,
                                 printers_required=False)  # fmt: skip
            task = asyncio.ensure_future(
                run_bridge(config, adapters=ADAPTERS,
                           sync_options={"discovery_options": {"ask": ask, "bind": no_port}})
            )  # fmt: skip
            await until(lambda: api.bridges, 5)
            await asyncio.sleep(0.5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await api.close()
            return moonraker.client_count, api.heartbeats
        finally:
            await moonraker.close()

    clients, heartbeats = asyncio.run(scenario())
    assert clients == 0 and heartbeats == []


class TestConfig:
    def test_app_printer_sections_take_only_secrets(self, tmp_path):
        path = tmp_path / "c.cfg"
        path.write_text("[app_printer 7]\naccess_code = 12345678\nhost = 1.2.3.4\n")
        with pytest.raises(ConfigError, match="unknown option 'host'"):
            load_config(path, ADAPTERS, env={}, home=tmp_path, printers_required=False)

    def test_an_app_printer_section_needs_a_number(self, tmp_path):
        path = tmp_path / "c.cfg"
        path.write_text("[app_printer x1c]\naccess_code = 12345678\n")
        with pytest.raises(ConfigError, match=r"\[app_printer 7\]"):
            load_config(path, ADAPTERS, env={}, home=tmp_path, printers_required=False)

    def test_secrets_are_read_by_entry(self, tmp_path):
        path = tmp_path / "c.cfg"
        path.write_text("[bridge]\ndiscovery = false\n[app_printer 7]\naccess_code = 12345678\n")
        config = load_config(path, ADAPTERS, env={}, home=tmp_path, printers_required=False)
        assert config.app_secrets == {7: {"access_code": "12345678"}}
        assert config.discovery is False


def test_adding_a_printer_section_starts_the_bridge_again_with_it(tmp_path):
    async def scenario():
        moonraker = FakeMoonraker()
        murl = await moonraker.start()
        try:
            async with running(tmp_path) as (api, path, task):
                with path.open("a", encoding="utf-8") as f:
                    f.write(f"\n[printer voron]\nprinter_id = 42\nurl = {murl}\n")
                restarted = await asyncio.wait_for(task, 5)
                return [p.name for p in restarted.printers]
        finally:
            await moonraker.close()

    assert asyncio.run(scenario()) == ["voron"]


def test_status_and_doctor_show_the_app_printers(tmp_path):
    from datetime import datetime, timezone

    from printlog_bridge.core.doctor import _check_bridge
    from printlog_bridge.core.status import format_status

    async def scenario():
        async with running(tmp_path) as (api, path, _):
            entry = api.connect("prusalink", "127.0.0.1", 42, port=9)
            await until(lambda: str(entry) in app_state(tmp_path), 5)
            config = load_config(path, ADAPTERS, env={}, home=tmp_path, printers_required=False)
            now = datetime.now(timezone.utc)
            return entry, format_status(config, now)[0], _check_bridge(config, now)

    entry, text, checks = asyncio.run(scenario())
    assert "Discovery: on" in text
    assert "Check-in: last" in text
    assert "Desired printers: version" in text
    assert f"App printer {entry}: printer 42" in text and "needs credentials" in text
    assert any(c.name == f"App printer {entry}" and c.status.value == "warn" for c in checks)
    assert any(c.name == "Bridge check-in" and c.status.value == "ok" for c in checks)


def test_pairing_asks_for_bridge_sync_only_when_offered():
    from printlog_bridge.core.pairing import scopes_to_request

    assert "bridge:sync" in scopes_to_request(["bridge:sync", "prints:write"], create=False)
    assert scopes_to_request(["prints:write"], create=False) == ["prints:write"]
