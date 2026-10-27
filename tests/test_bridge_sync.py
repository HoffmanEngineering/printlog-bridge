"""The bridge's check-in and desired list against the fake API (#100, api#288's contract)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import pytest
from fake_printlog_api import ALL_DEVICE_SCOPES, FakePrintLogApi

from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.bridge_sync import STATE_FILE, BridgeSync
from printlog_bridge.core.device import DeviceToken
from printlog_bridge.core.discovery import Discovery, DiscoveryService, Found
from printlog_bridge.core.status import StatusBoard, read_status


class Recorder:
    def __init__(self) -> None:
        self.applied: list[list] = []

    async def __call__(self, entries) -> None:
        self.applied.append(list(entries))


class Sweeper(Discovery):
    kind = "moonraker"

    def __init__(self) -> None:
        self.swept = 0

    async def sweep(self, networks):
        self.swept += 1
        return [Found("moonraker", "192.168.1.90", 7125, name="swept")]


@contextlib.asynccontextmanager
async def setup(tmp_path, scopes=None, discovery=True, **options):
    api = FakePrintLogApi()
    url = await api.start()
    token = api.issue_device_token(scopes=scopes)
    device = DeviceToken(token, url, grant_id=1)
    board = StatusBoard(tmp_path)
    board.start([])
    apply = Recorder()

    async def ask(services, wait):
        return []

    async def bind(port, on_datagram):
        return None

    hook = Sweeper()
    sync = None
    found = DiscoveryService([hook], ask=ask, bind=bind) if discovery else None
    try:
        async with ApiClient(url, device=device, state_dir=tmp_path) as client:
            sync = BridgeSync(
                client,
                tmp_path,
                board,
                apply,
                device=device,
                discovery=found,
                **{"networks": lambda: ["192.168.1.0/24"], **options},
            )
            sync.hook = hook
            yield api, sync, apply
    finally:
        if sync is not None:
            await sync._stop_discovery()
        await api.close()


def run(coroutine):
    return asyncio.run(coroutine)


def test_the_check_in_carries_what_the_contract_lists(tmp_path):
    async def scenario():
        configured = [Found("moonraker", "192.168.1.50", 7125, macs=("2c:cf:67:00:00:01",))]
        async with setup(tmp_path, configured=lambda: configured) as (api, sync, _):
            sync.discovery.heard(Found("bambu", "192.168.1.60", 8883, serial="01P00A1"), "udp")
            sync.discovery.heard(
                Found("moonraker", "192.168.1.50", 7125, name="voron",
                      macs=("2c:cf:67:00:00:01",)), "mdns",
            )  # fmt: skip
            await sync.check_in()
            return api.check_ins[-1], api.bridge()

    body, bridge = run(scenario())
    assert set(body) == {"agentVersion", "mode", "hostName", "networks", "discovered"}
    assert body["networks"] == ["192.168.1.0/24"]
    assert body["mode"] == "remote"
    assert body["discovered"] == [
        {"kind": "moonraker", "address": "192.168.1.50", "port": 7125, "model": None,
         "name": "voron", "serial": None, "mainboardId": None, "macs": ["2c:cf:67:00:00:01"],
         "configured": True},
        {"kind": "bambu", "address": "192.168.1.60", "port": 8883, "model": None, "name": None,
         "serial": "01P00A1", "mainboardId": None, "macs": [], "configured": False},
    ]  # fmt: skip
    assert len(bridge["discovered"]) == 2


def test_a_config_printer_not_found_on_the_network_is_still_reported_as_configured(tmp_path):
    async def scenario():
        configured = [Found("prusalink", "192.168.1.70", 80)]
        async with setup(tmp_path, configured=lambda: configured) as (api, sync, _):
            await sync.check_in()
            return api.check_ins[-1]["discovered"]

    [only] = run(scenario())
    assert only["kind"] == "prusalink" and only["configured"] is True


def test_with_discovery_off_nothing_found_is_sent(tmp_path):
    async def scenario():
        async with setup(tmp_path, discovery=False) as (api, sync, _):
            await sync.check_in()
            return api.check_ins[-1]

    assert "discovered" not in run(scenario())


def test_rejected_entries_are_logged_once(tmp_path, caplog):
    async def scenario():
        async with setup(tmp_path, networks=lambda: ["8.8.8.0/24"]) as (api, sync, _):
            await sync.check_in()
            await sync.check_in()

    with caplog.at_level(logging.WARNING):
        run(scenario())
    assert caplog.text.count("networks[0]") == 1
    assert "network_not_private" in caplog.text


def test_a_new_desired_version_is_applied_once(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (api, sync, apply):
            await sync.check_in()
            first = len(apply.applied)
            entry = api.connect("bambu", "192.168.1.60", 42, serial="01P00A1")
            await sync.poll()
            await sync.poll()  # 304: unchanged
            await sync.check_in()  # the same version again
            return first, apply.applied, entry, api.desired_polls

    first, applied, entry, polls = run(scenario())
    assert first == 1 and applied[0] == []  # the first check-in's (empty) list
    assert len(applied) == 2
    [desired] = applied[1]
    assert (desired.id, desired.printer_id, desired.kind, desired.serial) == (
        entry, 42, "bambu", "01P00A1",
    )  # fmt: skip
    assert polls[0] is None and polls[1] is not None and polls[1].startswith('W/"')


def test_the_desired_list_is_remembered_across_restarts(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (api, sync, _):
            await sync.check_in()
            api.connect("moonraker", "192.168.1.50", 42)
            await sync.poll()
        async with setup(tmp_path) as (api2, sync2, apply2):
            # Offline: the stored list is applied before 3D Print Log answers.
            api2.offline = True
            task = asyncio.ensure_future(sync2.run())
            await asyncio.sleep(0.2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return apply2.applied

    applied = run(scenario())
    assert [e.host for e in applied[0]] == ["192.168.1.50"]


def test_not_registered_on_the_poll_checks_in_again(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (api, sync, _):
            await sync.check_in()
            api.bridges.clear()  # 3D Print Log forgot it
            await sync.poll()
            assert sync._next_check_in == 0.0
            await sync.check_in()
            return api.bridges

    assert run(scenario())


def test_a_scan_request_sweeps_once_and_checks_in_with_its_time(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (api, sync, _):
            await sync.check_in()
            api.request_scan()
            await sync.poll()
            await sync._sweep()
            await sync.poll()
            await sync.check_in()
            return sync.hook.swept, api.check_ins

    swept, check_ins = run(scenario())
    assert swept == 1
    assert "lastSweepAt" in check_ins[1]
    assert any(d["name"] == "swept" for d in check_ins[1]["discovered"])


def test_the_scan_time_is_never_compared_with_the_bridges_clock(tmp_path):
    """A time in the past (a bridge clock ahead of the server's) still sweeps."""

    async def scenario():
        async with setup(tmp_path) as (api, sync, _):
            await sync.check_in()
            api.bridge()["desired"]["scanRequestedAt"] = "2001-01-01T00:00:00+00:00"
            api.bridge()["desired"]["version"] = "v2"
            await sync.poll()
            return sync._scan

    assert run(scenario()) == "2001-01-01T00:00:00+00:00"


def test_429_backs_the_check_in_off_and_polling_goes_on(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (api, sync, apply):
            await sync.check_in()
            api.check_in_limited = 120
            before = sync.clock()
            await sync.check_in()
            wait = sync._next_check_in - before
            api.connect("moonraker", "192.168.1.50", 42)
            await sync.poll()
            return wait, apply.applied, read_status(tmp_path)

    wait, applied, status = run(scenario())
    assert wait >= 120
    assert [e.host for e in applied[-1]] == ["192.168.1.50"]
    assert "limiting" in status["bridge"]["check_in"]["problem"]


class TestScope:
    SCOPES = sorted(ALL_DEVICE_SCOPES - {"bridge:sync"})

    def test_without_the_scope_it_pairs_again_once_then_stops(self, tmp_path):
        repairs = []

        async def repair():
            repairs.append(1)
            return None  # the user didn't approve

        async def scenario():
            async with setup(tmp_path, scopes=self.SCOPES, repair=repair) as (api, sync, _):
                await sync.check_in()
                return sync.stopped, read_status(tmp_path)

        stopped, status = run(scenario())
        assert repairs == [1]
        assert "bridge:sync" in stopped
        assert "printlog-bridge pair" in status["bridge"]["check_in"]["problem"]

    def test_it_never_pairs_again_for_the_same_token_after_a_restart(self, tmp_path):
        repairs = []

        async def repair():
            repairs.append(1)
            return None

        async def scenario():
            async with setup(tmp_path, scopes=self.SCOPES, repair=repair) as (api, sync, _):
                await sync.check_in()
                stored = json.loads((tmp_path / STATE_FILE).read_text())
                # The same token, a new run.
                sync2 = BridgeSync(sync.api, tmp_path, sync.board, sync.apply,
                                   device=sync.device, repair=repair)  # fmt: skip
                await sync2.check_in()
                return stored, sync2.stopped

        stored, stopped = run(scenario())
        assert repairs == [1] and stopped and stored["scope_repair"]["refused"]

    def test_a_token_from_the_re_pair_that_still_lacks_it_isnt_paired_again(self, tmp_path):
        async def scenario():
            async with setup(tmp_path, scopes=self.SCOPES) as (api, sync, _):
                repairs = []

                async def repair():
                    repairs.append(1)
                    token = api.issue_device_token(scopes=self.SCOPES)
                    stored = DeviceToken(token, sync.api.base_url)
                    sync.api.use_device(stored)
                    return stored

                sync.repair = repair
                await sync.check_in()  # 403: pairs again
                await sync.check_in()  # 403 with the new token: stops
                return repairs, sync.stopped

        repairs, stopped = run(scenario())
        assert repairs == [1] and stopped

    def test_a_re_pair_that_gets_the_scope_checks_in(self, tmp_path):
        async def scenario():
            async with setup(tmp_path, scopes=self.SCOPES) as (api, sync, _):

                async def repair():
                    stored = DeviceToken(api.issue_device_token(), sync.api.base_url)
                    sync.api.use_device(stored)
                    return stored

                sync.repair = repair
                await sync.check_in()
                await sync.check_in()
                return sync.stopped, api.bridges

        stopped, bridges = run(scenario())
        assert stopped is None and bridges

    def test_an_api_without_bridges_stops_syncing_and_keeps_quiet(self, tmp_path):
        async def scenario():
            async with setup(tmp_path) as (api, sync, _):
                from fake_printlog_api import DEVICE_SCOPES

                saved = DEVICE_SCOPES.pop(("PUT", "/api/bridges/me"))
                try:
                    await sync.check_in()
                finally:
                    DEVICE_SCOPES[("PUT", "/api/bridges/me")] = saved
                return sync.stopped

        assert "doesn't take bridge check-ins" in run(scenario())


def test_the_loop_runs_beside_everything_and_never_raises(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (api, sync, apply):
            api.fail_next(3, 500)
            task = asyncio.ensure_future(sync.run())
            await asyncio.sleep(0.3)
            assert not task.done()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return read_status(tmp_path)["bridge"]

    bridge = run(scenario())
    assert "couldn't check in" in bridge["check_in"]["problem"]


@pytest.mark.parametrize("field", ["desired", "printers", "desiredPrinters"])
def test_nothing_the_bridge_sends_can_set_its_own_desired_list(tmp_path, field):
    async def scenario():
        async with setup(tmp_path) as (api, sync, _):
            body = sync.body()
            assert field not in body
            await sync.check_in()
            return api.bridge()["desired"]["printers"]

    assert run(scenario()) == []
