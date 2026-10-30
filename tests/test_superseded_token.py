"""A device token another pairing replaced (api#299): 403 `device_token_superseded`.

During the re-pair's grace the old token still logs prints and sends heartbeats, and nothing
else. A newer token in the state folder (another `printlog-bridge pair` run) is picked up and
the request sent again, once. The same token held means another install has the new one: the
check-in and the desired poll stop, prints go on, and the 401 after the grace is a re-pair.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import pytest
from fake_moonraker import eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.core.api_client import (
    ApiClient,
    ApiUnavailable,
    RepairNeeded,
    TokenSuperseded,
)
from printlog_bridge.core.bridge_sync import BridgeSync
from printlog_bridge.core.device import write_device_token
from printlog_bridge.core.status import StatusBoard, read_status

PRINT = {"printerId": 42, "title": "benchy", "externalSource": "moonraker", "externalId": "j-1"}


def store(state_dir: Path, token: str, api_url: str):
    return write_device_token(
        state_dir, token=token, api_url=api_url, grant_id=1, name="nas", scopes=["prints:write"]
    )


async def noop(entries) -> None:
    return None


@contextlib.asynccontextmanager
async def setup(tmp_path: Path):
    api = FakePrintLogApi()
    url = await api.start()
    token = api.issue_device_token()
    device = store(tmp_path, token, url)
    board = StatusBoard(tmp_path)
    board.start([])
    try:
        async with ApiClient(url, device=device, state_dir=tmp_path) as client:
            sync = BridgeSync(client, tmp_path, board, noop, device=device)
            yield api, url, token, client, sync
    finally:
        await api.close()


def check_in_problem(tmp_path: Path) -> str | None:
    bridge = (read_status(tmp_path) or {}).get("bridge") or {}
    return (bridge.get("check_in") or {}).get("problem")


def test_a_newer_stored_token_is_picked_up_and_the_check_in_sent_again(tmp_path, caplog):
    async def scenario():
        async with setup(tmp_path) as (api, url, token, client, sync):
            store(tmp_path, api.supersede(token), url)  # `printlog-bridge pair` while running
            await sync.check_in()
            return api, sync, client

    with caplog.at_level(logging.INFO, logger="printlog_bridge"):
        api, sync, client = asyncio.run(scenario())

    assert [p for _, p in api.requests].count("/api/bridges/me") == 2  # refused, then sent again
    assert len(api.check_ins) == 1
    assert sync._registered and sync.held is None
    assert not client.superseded
    assert check_in_problem(tmp_path) is None
    [grant] = api.grants.values()
    assert grant["previousTokenHash"] is None  # the new token was used: the grace is over


def test_a_newer_stored_token_is_picked_up_for_a_read_too(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (api, url, token, client, _):
            new = api.supersede(token)
            store(tmp_path, new, url)
            found = await client.get_printer(42)
            return api, found, new

    api, found, new = asyncio.run(scenario())
    assert found is not None and found["id"] == 42
    assert [p for _, p in api.requests].count("/api/Printers/42") == 2


def test_the_same_token_holding_stops_the_sync_once_and_prints_go_on(tmp_path, caplog):
    async def scenario():
        async with setup(tmp_path) as (api, url, token, client, sync):
            sync.poll_s = 0.05
            api.supersede(token)  # another install of this device paired again
            task = asyncio.ensure_future(sync.run())
            try:
                await eventually(lambda: sync.held is not None)
                await asyncio.sleep(0.3)  # many poll intervals: nothing more is asked
                print_, created = await client.create_print(PRINT)
                with pytest.raises(TokenSuperseded):
                    await client.get_printer(42)
                bridge_calls = [p for _, p in api.requests if p.startswith("/api/bridges")]
                return bridge_calls, created, print_, check_in_problem(tmp_path), client
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    with caplog.at_level(logging.DEBUG, logger="printlog_bridge"):
        bridge_calls, created, print_, problem, client = asyncio.run(scenario())

    assert bridge_calls == ["/api/bridges/me"]  # one refused check-in, no retry, no polling
    assert created and print_["title"] == "benchy"  # the old token still logs prints
    assert client.superseded and not client.repair_needed
    assert problem is not None and "paired again somewhere else" in problem
    assert caplog.text.count("paired again somewhere else") == 1


def test_a_superseded_refusal_is_retried_later_never_dropped(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (api, _, token, client, _sync):
            api.supersede(token)
            with pytest.raises(ApiUnavailable) as error:
                await client.attachment_upload_url(1, "a.3mf", "model/3mf", 10)
            return error.value

    error = asyncio.run(scenario())
    assert isinstance(error, TokenSuperseded)
    assert error.status == 403


def test_after_the_grace_the_re_pair_path_takes_over(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (api, _, token, client, sync):
            sync.poll_s = 0.05
            api.supersede(token)
            task = asyncio.ensure_future(sync.run())
            try:
                await eventually(lambda: sync.held is not None)
                api.end_grace()  # the new token was used, or 10 minutes passed
                with pytest.raises(RepairNeeded):
                    await client.create_print(PRINT)
                await eventually(lambda: "Re-pair needed" in (check_in_problem(tmp_path) or ""))
                bridge_calls = [p for _, p in api.requests if p.startswith("/api/bridges")]
                return bridge_calls, sync, client
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    bridge_calls, sync, client = asyncio.run(scenario())
    assert client.repair_needed and not client.superseded
    assert sync.held is None
    assert bridge_calls == ["/api/bridges/me"]  # a refused token isn't sent again


def test_a_pairing_here_after_the_hold_resumes_the_sync(tmp_path):
    async def scenario():
        async with setup(tmp_path) as (api, url, token, client, sync):
            sync.poll_s = 0.05
            api.supersede(token)
            task = asyncio.ensure_future(sync.run())
            try:
                await eventually(lambda: sync.held is not None)
                store(tmp_path, api.issue_device_token(), url)  # `printlog-bridge pair` here
                await eventually(lambda: sync._registered)
                return sync
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    sync = asyncio.run(scenario())
    assert sync.held is None
    assert check_in_problem(tmp_path) is None
