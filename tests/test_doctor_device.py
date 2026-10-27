"""`doctor` with a device token: which credential is used, and whether the API takes it."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.core.checks import Status
from printlog_bridge.core.device import DEVICE_TOKEN_FILE, write_device_token
from printlog_bridge.core.doctor import diagnose


def diagnose_with(
    tmp_path: Path, *, api_key: str = "", revoke=False, other_api=False, mode: int | None = None
):
    async def scenario():
        api = FakePrintLogApi()
        base = await api.start()
        try:
            token = api.issue_device_token()
            if revoke:
                api.revoke(api.grant_for(token)["id"])
            write_device_token(
                tmp_path / "state",
                token=token,
                api_url="https://api.example.com" if other_api else base,
                grant_id=1,
                name="nas",
                scopes=[],
            )
            if mode is not None:
                (tmp_path / "state" / DEVICE_TOKEN_FILE).chmod(mode)
            path = tmp_path / "printlog-bridge.cfg"
            path.write_text(
                f"[bridge]\napi_url = {base}\nstate_dir = {tmp_path / 'state'}\n"
                f"api_key = {api_key}\n[printer voron]\nprinter_id = 42\nurl = http://127.0.0.1:9\n",
                encoding="utf-8",
            )
            results = await diagnose(path, adapters=ADAPTERS, env={}, home=tmp_path)
            return {r.name: r for r in results}, api
        finally:
            await api.close()

    return asyncio.run(scenario())


def test_doctor_signs_in_with_the_device_token(tmp_path):
    results, api = diagnose_with(tmp_path, api_key="key-123")

    assert results["3D Print Log API"].status is Status.OK
    assert "with the device token" in results["3D Print Log API"].detail
    assert results["Device token"].status is Status.OK
    assert "'nas'" in results["Device token"].detail
    assert "not used" in results["API key"].detail
    assert results["Printer voron in 3D Print Log"].status is Status.OK
    assert {kind for kind, _ in api.credentials} == {"device"}


def test_doctor_says_a_revoked_token_needs_a_re_pair(tmp_path):
    results, _ = diagnose_with(tmp_path, revoke=True)

    api = results["3D Print Log API"]
    assert api.status is Status.FAIL
    assert "Re-pair needed: run `printlog-bridge pair`" in api.detail


def test_doctor_warns_about_a_token_from_another_api(tmp_path):
    results, _ = diagnose_with(tmp_path, other_api=True)

    assert results["Device token"].status is Status.WARN
    assert "isn't used" in results["Device token"].detail
    assert results["3D Print Log API"].status is Status.FAIL
    assert "printlog-bridge pair" in results["3D Print Log API"].detail


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_doctor_warns_when_others_can_read_the_token(tmp_path):
    results, _ = diagnose_with(tmp_path, mode=0o644)
    assert results["Device token"].status is Status.WARN
    assert "chmod 600" in results["Device token"].detail
