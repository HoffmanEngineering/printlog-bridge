"""A revoked device token: jobs wait in the outbox, nothing is sent, and a re-pair resumes."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import pytest
from fake_moonraker import eventually
from fake_printlog_api import FakePrintLogApi
from test_job_logger import FAST_BACKOFF, drain, started
from test_run import WatchingAdapter

from printlog_bridge.core import lifecycle
from printlog_bridge.core.api_client import ApiClient
from printlog_bridge.core.bridge import run_bridge
from printlog_bridge.core.config import BridgeConfig, PrinterConfig
from printlog_bridge.core.device import write_device_token
from printlog_bridge.core.lifecycle import JobLogger, LoggedPrinter
from printlog_bridge.core.outbox import read_waiting
from printlog_bridge.core.status import format_status, read_status


def store(state_dir: Path, token: str, api_url: str):
    return write_device_token(
        state_dir, token=token, api_url=api_url, grant_id=1, name="nas", scopes=["prints:write"]
    )


def test_jobs_wait_for_a_re_pair_and_go_out_after_it(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(lifecycle, "REPAIR_PAUSE_S", 0.05)
    api = FakePrintLogApi()
    heard: list[str | None] = []

    async def scenario():
        base = await api.start()
        try:
            token = api.issue_device_token()
            device = store(tmp_path, token, base)
            async with ApiClient(
                base, device=device, state_dir=tmp_path, on_credentials=heard.append
            ) as client:
                logger = JobLogger(
                    client,
                    {"voron": LoggedPrinter(42, "moonraker")},
                    tmp_path,
                    backoff=FAST_BACKOFF,
                )
                try:
                    api.revoke(api.grant_for(token)["id"])
                    await logger.emit(started())
                    await drain(logger, 0.3)
                    asked = len(api.requests)
                    waiting = read_waiting(tmp_path)
                    await drain(logger, 0.2)
                    assert len(api.requests) == asked  # nothing more reached the API

                    store(tmp_path, api.issue_device_token(), base)  # `printlog-bridge pair`
                    await drain(logger, 1.0)
                finally:
                    logger.close()
            return asked, waiting
        finally:
            await api.close()

    with caplog.at_level(logging.DEBUG, logger="printlog_bridge"):
        asked, waiting = asyncio.run(scenario())

    assert asked == 1
    assert waiting  # the job stayed in the outbox while the bridge waited
    assert len(api.prints) == 1
    assert read_waiting(tmp_path) == {}
    assert heard[0] is not None and "Re-pair needed" in heard[0]
    assert heard[-1] is None


def test_throttling_is_retried_without_a_re_pair(tmp_path):
    api = FakePrintLogApi()

    async def scenario():
        base = await api.start()
        try:
            device = store(tmp_path, api.issue_device_token(), base)
            async with ApiClient(base, device=device, state_dir=tmp_path) as client:
                logger = JobLogger(
                    client, {"voron": LoggedPrinter(42, "moonraker")}, tmp_path, backoff=(0, 0)
                )
                try:
                    api.throttled = True
                    await logger.emit(started())
                    wait = await logger.send_due()
                    assert not client.repair_needed
                    api.throttled = False
                    logger._paused_until = 0.0
                    logger._due.clear()
                    await drain(logger, 1.0)
                finally:
                    logger.close()
            return wait
        finally:
            await api.close()

    wait = asyncio.run(scenario())
    assert wait == pytest.approx(60, abs=1)  # the API's Retry-After
    assert len(api.prints) == 1


def test_status_says_a_re_pair_is_needed(tmp_path):
    api = FakePrintLogApi()
    adapter = WatchingAdapter("voron")

    async def scenario():
        base = await api.start()
        try:
            token = api.issue_device_token()
            api.revoke(api.grant_for(token)["id"])
            config = BridgeConfig(
                path=Path("printlog-bridge.cfg"),
                api_url=base,
                api_key=None,
                state_dir=tmp_path,
                printers=[PrinterConfig("voron", adapter, printer_id=42)],
                stored_device=store(tmp_path, token, base),
            )
            task = asyncio.ensure_future(run_bridge(config))
            await asyncio.wait_for(adapter.started.wait(), 5)
            await eventually(lambda: (read_status(tmp_path) or {}).get("credentials_problem"))
            text, _ = format_status(config)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return text
        finally:
            await api.close()

    text = asyncio.run(scenario())
    assert "Pairing: 3D Print Log no longer accepts this bridge's device token" in text
    assert "printlog-bridge pair" in text
    # The heartbeat and the spool refresh start together; after the refusal, nothing is sent.
    assert set(kind for kind, _ in api.credentials) == {"device"}
    assert len(api.credentials) <= 2
