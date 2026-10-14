from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from fake_moonraker import eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge import __version__
from printlog_bridge.core.api_client import ApiClient, ApiUnavailable
from printlog_bridge.core.connections import Connection, Heartbeat
from printlog_bridge.core.status import StatusBoard, read_status

VORON = Connection(printer="voron", instance_id="inst-voron", kind="moonraker", printer_id=42)
U1 = Connection(printer="u1", instance_id="inst-u1", kind="moonraker", printer_id=43)


@contextlib.asynccontextmanager
async def heartbeat(api: FakePrintLogApi, tmp_path, *connections: Connection, **kwargs):
    base = await api.start()
    board = StatusBoard(tmp_path)
    board.start([c.printer for c in connections])
    try:
        async with ApiClient(base, api.api_key) as client:
            yield Heartbeat(client, connections, board, **kwargs)
    finally:
        await api.close()


def run(scenario):
    return asyncio.run(scenario())


def test_registers_each_printer_as_its_own_connection(tmp_path):
    api = FakePrintLogApi(printers=(42, 43))

    async def scenario():
        async with heartbeat(api, tmp_path, VORON, U1) as beat:
            await beat.beat_all()

    run(scenario)

    assert api.heartbeats == [
        (
            "inst-voron",
            {
                "kind": "moonraker",
                "displayName": "printlog-bridge (voron)",
                "agentVersion": __version__,
                "printerId": 42,
            },
        ),
        (
            "inst-u1",
            {
                "kind": "moonraker",
                "displayName": "printlog-bridge (u1)",
                "agentVersion": __version__,
                "printerId": 43,
            },
        ),
    ]
    assert set(api.connections) == {"inst-voron", "inst-u1"}


def test_a_long_printer_name_is_cut_to_the_apis_limit(tmp_path):
    api = FakePrintLogApi()
    long = Connection(printer="v" * 150, instance_id="i", kind="moonraker", printer_id=42)

    async def scenario():
        async with heartbeat(api, tmp_path, long) as beat:
            await beat.beat_all()

    run(scenario)

    [(_, body)] = api.heartbeats
    assert len(body["displayName"]) == 100


def test_beats_again_every_interval(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON, interval=0.05) as beat:
            task = asyncio.ensure_future(beat.run())
            try:
                await eventually(lambda: len(api.heartbeats) >= 3)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    run(scenario)


def test_records_each_heartbeat_for_status(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            await beat.beat_all()

    run(scenario)

    voron = read_status(tmp_path)["printers"]["voron"]
    assert voron["last_heartbeat_at"] is not None
    assert voron["warnings"] == {}


def test_an_outage_is_a_warning_that_clears_when_the_api_is_back(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            api.fail_next(1)
            await beat.beat_all()
            during = read_status(tmp_path)["printers"]["voron"]
            await beat.beat_all()
            return during, read_status(tmp_path)["printers"]["voron"]

    during, after = run(scenario)

    assert during["last_heartbeat_at"] is None
    assert "503" in during["warnings"]["heartbeat"]
    assert after["warnings"] == {}


def test_a_rejected_key_says_what_to_do(tmp_path, caplog):
    api = FakePrintLogApi(api_key="the-real-key")

    async def scenario():
        base = await api.start()
        board = StatusBoard(tmp_path)
        board.start(["voron"])
        try:
            async with ApiClient(base, "a-wrong-key") as client:
                await Heartbeat(client, [VORON], board).beat_all()
        finally:
            await api.close()

    with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
        run(scenario)

    warning = read_status(tmp_path)["printers"]["voron"]["warnings"]["heartbeat"]
    assert "printlog-bridge doctor" in warning
    assert any("printlog-bridge doctor" in r.getMessage() for r in caplog.records)


def test_a_printer_that_is_not_the_accounts_is_a_warning(tmp_path):
    api = FakePrintLogApi(printers=(7,))

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            await beat.beat_all()

    run(scenario)

    warning = read_status(tmp_path)["printers"]["voron"]["warnings"]["heartbeat"]
    assert "not yours" in warning
    assert "printer_id" in warning


def test_ensure_registered_registers_only_once(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            await beat.ensure_registered("voron")
            await beat.ensure_registered("voron")

    run(scenario)

    assert len(api.heartbeats) == 1


def test_ensure_registered_raises_when_3d_print_log_is_unavailable(tmp_path):
    """So the print it runs before waits for the API rather than being created unlinked."""
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            api.fail_next(1)
            with pytest.raises(ApiUnavailable):
                await beat.ensure_registered("voron")

    run(scenario)

    assert api.connections == {}


def test_ensure_registered_does_not_raise_when_the_connection_is_refused(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            api.fail_next(1, status=400)
            await beat.ensure_registered("voron")

    run(scenario)

    assert api.connections == {}


def test_ensure_registered_tries_again_after_a_failure(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            api.fail_next(1)
            with pytest.raises(ApiUnavailable):
                await beat.ensure_registered("voron")
            await beat.ensure_registered("voron")

    run(scenario)

    assert [m for m, _ in api.requests] == ["PUT", "PUT"]
    assert "inst-voron" in api.connections


def test_ensure_registered_ignores_a_printer_it_does_not_know(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        async with heartbeat(api, tmp_path, VORON) as beat:
            await beat.ensure_registered("nope")

    run(scenario)

    assert api.heartbeats == []
