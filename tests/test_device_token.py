"""The API client with a device token: one credential, re-pair needed, throttling, scopes."""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.core.api_client import (
    ApiClient,
    ApiRejected,
    ApiUnavailable,
    AuthRejected,
    RepairNeeded,
)
from printlog_bridge.core.config import ConfigError, load_config
from printlog_bridge.core.device import DEVICE_TOKEN_FILE, read_device_token, write_device_token
from printlog_bridge.core.support import known_secrets, write_support_bundle


def with_api(scenario, **fake):
    async def main():
        api = FakePrintLogApi(**fake)
        base = await api.start()
        try:
            return await scenario(api, base)
        finally:
            await api.close()

    return asyncio.run(main())


def store(state_dir: Path, token: str, api_url: str):
    return write_device_token(
        state_dir, token=token, api_url=api_url, grant_id=1, name="bridge", scopes=["prints:write"]
    )


def test_a_device_token_is_the_only_credential_sent(tmp_path):
    async def scenario(api, base):
        device = store(tmp_path, api.issue_device_token(), base)
        async with ApiClient(base, api.api_key, device=device) as client:
            user = await client.current_user()
            await client.get_printer(42)
        return api, user

    api, user = with_api(scenario)
    assert user.id == 7
    assert [kind for kind, _ in api.credentials] == ["device", "device"]


def test_without_a_device_token_the_api_key_is_sent(tmp_path):
    async def scenario(api, base):
        async with ApiClient(base, api.api_key) as client:
            await client.current_user()
        return api

    api = with_api(scenario)
    assert api.credentials == [("key", "/api/Users/me")]


def test_a_revoked_token_needs_a_re_pair_and_stops_asking_the_api(tmp_path):
    heard: list[str | None] = []

    async def scenario(api, base):
        token = api.issue_device_token()
        device = store(tmp_path, token, base)
        async with ApiClient(
            base, device=device, state_dir=tmp_path, on_credentials=heard.append
        ) as client:
            api.revoke(api.grant_for(token)["id"])
            with pytest.raises(RepairNeeded, match="printlog-bridge pair"):
                await client.current_user()
            asked = len(api.requests)
            for _ in range(3):
                with pytest.raises(RepairNeeded):
                    await client.create_print({"title": "x"})
            assert len(api.requests) == asked  # refused here, without asking the API
            assert client.repair_needed

            # `printlog-bridge pair` in another shell stores a new token: picked up at once.
            store(tmp_path, api.issue_device_token(), base)
            user = await client.current_user()
            assert not client.repair_needed
        return user

    user = with_api(scenario)
    assert user.id == 7
    assert len(heard) == 2
    assert "Re-pair needed" in heard[0]
    assert heard[1] is None


def test_repair_needed_is_an_auth_rejection():
    assert issubclass(RepairNeeded, AuthRejected)


def test_another_401_is_not_a_re_pair(tmp_path):
    async def scenario(api, base):
        device = store(tmp_path, api.issue_device_token(), base)
        async with ApiClient(base, device=device) as client:
            api.fail_next(1, 401)
            with pytest.raises(AuthRejected) as rejected:
                await client.current_user()
            assert not client.repair_needed
            return rejected.value

    rejected = with_api(scenario)
    assert not isinstance(rejected, RepairNeeded)


def test_throttling_is_waited_out_not_a_re_pair(tmp_path):
    async def scenario(api, base):
        device = store(tmp_path, api.issue_device_token(), base)
        async with ApiClient(base, device=device) as client:
            api.throttled = True
            with pytest.raises(ApiUnavailable) as unavailable:
                await client.current_user()
            assert not client.repair_needed
            api.throttled = False
            await client.current_user()
            return unavailable.value

    unavailable = with_api(scenario)
    assert unavailable.status == 429
    assert unavailable.retry_after == 60
    assert "device_token_throttled" in str(unavailable)


def test_a_missing_scope_says_to_pair_again(tmp_path):
    async def scenario(api, base):
        device = store(tmp_path, api.issue_device_token(scopes=["prints:read"]), base)
        async with ApiClient(base, device=device) as client:
            with pytest.raises(ApiRejected, match="printlog-bridge pair") as rejected:
                await client.create_print({"title": "x"})
            return rejected.value

    assert with_api(scenario).status == 403


def test_the_spoolman_lookup_a_device_cant_use_is_not_found(tmp_path):
    async def scenario(api, base):
        device = store(tmp_path, api.issue_device_token(), base)
        async with ApiClient(base, device=device) as client:
            return await client.filament_by_spoolman_id(3)

    assert with_api(scenario) is None


def test_printers_are_listed_page_by_page_and_created(tmp_path):
    async def scenario(api, base):
        device = store(tmp_path, api.issue_device_token(), base)
        async with ApiClient(base, device=device) as client:
            created = await client.create_printer(
                {"name": "Voron", "make": "Voron", "model": "2.4", "isActive": True, "slotCount": 2}
            )
            listed = await client.printers()
        return api, created, listed

    api, created, listed = with_api(scenario, printers=tuple(range(1, 251)))
    assert created["name"] == "Voron"
    assert created["slotCount"] == 2
    assert len(listed) == 251
    assert {p["id"] for p in listed} == api.printers


def test_creating_a_printer_needs_printers_create(tmp_path):
    async def scenario(api, base):
        scopes = ["printers:read", "prints:write"]
        device = store(tmp_path, api.issue_device_token(scopes=scopes), base)
        async with ApiClient(base, device=device) as client:
            with pytest.raises(ApiRejected, match="insufficient_scope"):
                await client.create_printer({"name": "a", "make": "b", "model": "c"})

    with_api(scenario)


CONFIG = """\
[bridge]
api_url = {api_url}
state_dir = {state}

[printer voron]
printer_id = 42
url = http://voron.local:7125
"""


def config_at(tmp_path: Path, api_url: str = "https://api.3dprintlog.com"):
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(CONFIG.format(api_url=api_url, state=tmp_path / "state"), encoding="utf-8")
    return load_config(path, adapters=ADAPTERS, env={"PRINTLOG_API_KEY": "env-key"}, home=tmp_path)


def test_the_config_prefers_the_device_token_over_any_api_key(tmp_path):
    store(tmp_path / "state", "pld_device", "https://api.3dprintlog.com")
    config = config_at(tmp_path)
    assert config.device is not None
    assert config.device.token == "pld_device"
    assert config.api_key == "env-key"
    assert "device token" in config.describe_credentials()
    assert config.has_credentials


def test_a_token_from_another_api_is_never_used(tmp_path):
    store(tmp_path / "state", "pld_device", "https://api.example.com")
    config = config_at(tmp_path)
    assert config.stored_device is not None
    assert config.device is None
    assert "API key" in config.describe_credentials()


def test_a_damaged_token_file_is_a_config_error(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / DEVICE_TOKEN_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="pair"):
        config_at(tmp_path)


def test_the_device_token_never_reaches_the_support_bundle(tmp_path):
    state = tmp_path / "state"
    token = "pld_" + "s" * 43
    store(state, token, "https://api.3dprintlog.com")
    (state / "bridge.log").write_text(f"oops, logged {token}\n", encoding="utf-8")
    config = config_at(tmp_path)
    assert token in known_secrets(config.path, state, {})
    bundle = write_support_bundle(
        state / "bundle.zip", report="r", config_path=config.path, state_dir=state, env={}
    )
    with zipfile.ZipFile(bundle) as z:
        names = z.namelist()
        text = "".join(z.read(n).decode() for n in names)
    assert DEVICE_TOKEN_FILE not in names
    assert token not in text


def test_the_token_never_shows_in_a_repr(tmp_path):
    stored = store(tmp_path, "pld_secret", "https://api.3dprintlog.com")
    assert "pld_secret" not in repr(stored)
    assert read_device_token(tmp_path) == stored
