"""Pairing against a fake Auth0 and the fake API (api#286's contract)."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import aiohttp
import pytest
from fake_auth0 import FakeAuth0
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.core import pairing
from printlog_bridge.core.device import (
    DEVICE_ID_FILE,
    DEVICE_TOKEN_FILE,
    read_device_id,
    read_device_token,
)
from printlog_bridge.core.pairing import (
    BRIDGE_SCOPES,
    DeviceAuthorization,
    ExchangeRefused,
    PairingDenied,
    PairingError,
    PairingExpired,
    PairingUnavailable,
)


class FakeTime:
    """A clock that only moves when the pairing sleeps, so polling takes no real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)

    def clock(self) -> float:
        return self.now


def run(scenario, *, pairing_on: bool = True):
    """Runs `scenario(api, auth0, api_url)` with both fakes up."""

    async def main():
        auth0, api = FakeAuth0(), FakePrintLogApi()
        await auth0.start()
        api_url = await api.start()
        api.auth0 = auth0
        api.pairing = auth0.discovery() if pairing_on else None
        try:
            return await scenario(api, auth0, api_url)
        finally:
            await api.close()
            await auth0.close()

    return asyncio.run(main())


def pair(api_url: str, state_dir: Path, time: FakeTime | None = None, **options):
    time = time or FakeTime()
    shown: list[DeviceAuthorization] = []
    said: list[str] = []
    options.setdefault("create", False)
    options.setdefault("name", "printlog-bridge on voron")
    result = pairing.pair(
        api_url,
        state_dir,
        show=shown.append,
        say=said.append,
        sleep=time.sleep,
        clock=time.clock,
        **options,
    )
    return result, shown, said


def test_pairing_stores_the_device_token_and_a_stable_device_id(tmp_path):
    async def scenario(api, auth0, api_url):
        auth0.script = ["pending", "pending"]
        auth0.approve()
        call, shown, _ = pair(api_url, tmp_path)
        stored, repaired = await call
        return api, auth0, stored, repaired, shown

    api, auth0, stored, repaired, shown = run(scenario)

    assert not repaired
    [grant] = api.grants.values()
    assert grant["deviceId"] == read_device_id(tmp_path)
    assert grant["name"] == "printlog-bridge on voron"
    assert api.grant_for(stored.token) is grant
    assert stored.grant_id == grant["id"]
    assert read_device_token(tmp_path) == stored
    assert [a.user_code for a in shown] == auth0.user_codes
    [request] = auth0.authorizations
    assert request["client_id"] == auth0.client_id
    assert request["audience"] == auth0.audience
    assert request["scope"].split() == list(BRIDGE_SCOPES)
    assert "offline_access" not in request["scope"]


def test_printers_create_is_asked_for_only_when_pair_may_create_one(tmp_path):
    async def scenario(api, auth0, api_url):
        auth0.approve()
        call, _, _ = pair(api_url, tmp_path, create=True)
        await call
        return auth0

    auth0 = run(scenario)
    assert "printers:create" in auth0.authorizations[0]["scope"].split()


def test_only_scopes_the_server_offers_are_asked_for(tmp_path):
    async def scenario(api, auth0, api_url):
        api.pairing = {**auth0.discovery(), "scopes": ["prints:write", "printers:read", "x:y"]}
        auth0.approve()
        call, _, _ = pair(api_url, tmp_path, create=True)
        await call
        return auth0

    auth0 = run(scenario)
    assert auth0.authorizations[0]["scope"] == "printers:read prints:write"


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_the_token_file_is_only_the_users(tmp_path):
    async def scenario(api, auth0, api_url):
        auth0.approve()
        call, _, _ = pair(api_url, tmp_path / "state")
        await call

    run(scenario)
    assert stat.S_IMODE((tmp_path / "state" / DEVICE_TOKEN_FILE).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700


def test_pairing_again_rotates_the_same_grant(tmp_path):
    async def scenario(api, auth0, api_url):
        auth0.approve()
        first, _, _ = pair(api_url, tmp_path)
        old, _ = await first
        second, _, _ = pair(api_url, tmp_path, name="workshop")
        new, repaired = await second
        return api, old, new, repaired

    api, old, new, repaired = run(scenario)

    assert repaired
    [grant] = api.grants.values()
    assert grant["name"] == "workshop"
    assert api.grant_for(old.token) is None
    assert api.grant_for(new.token) is grant
    assert [status for status, _ in api.pairings] == [201, 200]


def test_polling_waits_the_interval_and_slows_down_when_told(tmp_path):
    time = FakeTime()

    async def scenario(api, auth0, api_url):
        auth0.script = ["pending", "slow_down", "pending", "slow_down"]
        auth0.approve()
        call, _, _ = pair(api_url, tmp_path, time)
        await call

    run(scenario)
    assert time.sleeps == [5, 5, 10, 10, 15]


def test_a_denied_request_says_so(tmp_path):
    async def scenario(api, auth0, api_url):
        auth0.script = ["pending", "deny"]
        call, _, _ = pair(api_url, tmp_path)
        with pytest.raises(PairingDenied, match="denied"):
            await call
        return api

    api = run(scenario)
    assert api.grants == {}
    assert not (tmp_path / DEVICE_TOKEN_FILE).exists()


def test_an_expired_code_says_so(tmp_path):
    async def scenario(api, auth0, api_url):
        auth0.script = ["expire"]
        call, _, _ = pair(api_url, tmp_path)
        with pytest.raises(PairingExpired, match="expired"):
            await call

    run(scenario)


def test_the_bridge_stops_polling_when_the_code_runs_out(tmp_path):
    time = FakeTime()

    async def scenario(api, auth0, api_url):
        auth0.expires_in = 12
        call, _, _ = pair(api_url, tmp_path, time)
        with pytest.raises(PairingExpired):
            await call
        return auth0

    auth0 = run(scenario)
    assert len(auth0.polls) == 2  # at 5 s and 10 s; at 15 s the code is gone


def test_keep_trying_shows_a_new_code_after_one_expires_or_is_denied(tmp_path):
    async def scenario(api, auth0, api_url):
        auth0.script = ["expire", "deny", "approve"]
        call, shown, said = pair(api_url, tmp_path, keep_trying=True)
        stored, _ = await call
        return api, shown, said, stored

    api, shown, said, stored = run(scenario)
    assert len(shown) == 3
    assert len({a.user_code for a in shown}) == 3
    assert len(said) == 2
    assert api.grant_for(stored.token) is not None


def test_a_server_with_pairing_off_is_unavailable(tmp_path):
    async def scenario(api, auth0, api_url):
        call, _, _ = pair(api_url, tmp_path)
        with pytest.raises(PairingUnavailable, match="store-key"):
            await call
        return auth0

    auth0 = run(scenario, pairing_on=False)
    assert auth0.authorizations == []


def test_a_sign_in_address_that_isnt_https_is_refused(tmp_path):
    async def scenario(api, auth0, api_url):
        api.pairing = {
            **auth0.discovery(),
            "deviceAuthorizationEndpoint": "http://auth.example.com/oauth/device/code",
        }
        call, _, _ = pair(api_url, tmp_path)
        with pytest.raises(PairingError, match="isn't https"):
            await call
        return auth0

    auth0 = run(scenario)
    assert auth0.authorizations == []


def test_the_approval_is_traded_only_once(tmp_path):
    async def scenario(api, auth0, api_url):
        auth0.approve()
        async with aiohttp.ClientSession() as session:
            config = await pairing.discover(session, api_url)
            authorization = await pairing.start(session, config, ["prints:write"])
            token = await pairing.wait_for_approval(
                session, config, authorization, sleep=FakeTime().sleep
            )
            device = "2f0c6c1e-6a52-4e0f-9b1d-2b8f3f1d7a10"
            await pairing.exchange(session, api_url, token, device, "bridge")
            with pytest.raises(ExchangeRefused) as refused:
                await pairing.exchange(session, api_url, token, device, "bridge")
        return refused.value

    refused = run(scenario)
    assert refused.code == "pairing_token_used"
    assert refused.again


@pytest.mark.parametrize(
    ("setup", "code", "again", "words"),
    [
        (lambda api, auth0: setattr(api, "max_grants", 0), "device_grant_limit", False, "Remove"),
        (lambda api, auth0: setattr(auth0, "offline_access", True), "offline_access_not_allowed",
         False, "offline access"),
        (lambda api, auth0: setattr(auth0, "grant_scope", "openid"), "insufficient_scope", False,
         "permissions"),
    ],
)  # fmt: skip
def test_refused_exchanges_say_why(tmp_path, setup, code, again, words):
    async def scenario(api, auth0, api_url):
        setup(api, auth0)
        auth0.approve()
        call, _, _ = pair(api_url, tmp_path)
        with pytest.raises(ExchangeRefused) as refused:
            await call
        return api, refused.value

    api, refused = run(scenario)
    assert refused.code == code
    assert refused.again is again
    assert words in str(refused)
    assert api.grants == {}
    assert not (tmp_path / DEVICE_TOKEN_FILE).exists()


def test_a_revoke_during_a_re_pair_asks_for_a_new_approval(tmp_path):
    async def scenario(api, auth0, api_url):
        auth0.approve()
        first, _, _ = pair(api_url, tmp_path)
        await first
        api.revoke_during_repair.add(read_device_id(tmp_path))
        second, shown, said = pair(api_url, tmp_path, keep_trying=True)
        stored, repaired = await second
        return api, shown, said, stored, repaired

    api, shown, said, stored, repaired = run(scenario)
    assert len(shown) == 2
    assert "removed in 3D Print Log" in said[0]
    assert not repaired  # the revoke deleted the grant, so this made a new one
    assert api.grant_for(stored.token) is not None


def test_the_device_id_survives_a_lost_token(tmp_path):
    async def scenario(api, auth0, api_url):
        auth0.approve()
        first, _, _ = pair(api_url, tmp_path)
        await first
        (tmp_path / DEVICE_TOKEN_FILE).unlink()
        second, _, _ = pair(api_url, tmp_path)
        return api, await second

    api, (_, repaired) = run(scenario)
    assert repaired
    assert len(api.grants) == 1
    assert (tmp_path / DEVICE_ID_FILE).exists()


def test_the_token_file_holds_the_api_it_came_from(tmp_path):
    async def scenario(api, auth0, api_url):
        auth0.approve()
        call, _, _ = pair(api_url, tmp_path)
        await call
        return api_url

    api_url = run(scenario)
    data = json.loads((tmp_path / DEVICE_TOKEN_FILE).read_text(encoding="utf-8"))
    assert data["apiUrl"] == api_url
    assert data["token"].startswith("pld_")
    assert "access_token" not in json.dumps(data)


def test_instructions_show_the_code_the_address_and_a_qr_code():
    authorization = DeviceAuthorization(
        device_code="secret-device-code",
        user_code="QWER-TYUI",
        verification_uri="https://auth.3dprintlog.com/activate",
        verification_uri_complete="https://auth.3dprintlog.com/activate?user_code=QWER-TYUI",
        expires_in=900,
        interval=5,
    )
    text = pairing.instructions(authorization)
    assert "QWER-TYUI" in text
    assert "https://auth.3dprintlog.com/activate?user_code=QWER-TYUI" in text
    assert "15 minutes" in text
    assert "█" in text
    assert "secret-device-code" not in text
    assert "secret-device-code" not in repr(authorization)


def test_names_are_cleaned_for_the_api():
    assert pairing.clean_name("bridge\x07 on\tvoron") == "bridge onvoron"
    assert len(pairing.clean_name("x" * 300)) == 100
    assert pairing.clean_name("\x01") == "printlog-bridge"


def test_the_code_waiting_for_approval_is_kept_for_the_local_page_until_pairing_ends(tmp_path):
    seen: list = []

    async def scenario(api, auth0, api_url):
        auth0.script = ["pending"]
        auth0.approve()
        time = FakeTime()
        result = pairing.pair(
            api_url, tmp_path, name="n", create=False,
            show=lambda a: seen.append(pairing.pending.get()), say=lambda m: None,
            sleep=time.sleep, clock=time.clock,
        )  # fmt: skip
        await result

    assert pairing.pending.get() is None
    run(scenario)

    [shown] = seen
    assert shown.user_code
    assert shown.verification_uri.startswith("http")
    assert "device_code" not in repr(shown)
    assert not hasattr(shown, "device_code")
    assert shown.expires_at > 0
    assert pairing.pending.get() is None  # paired: nothing waits any more


def test_a_code_that_expired_is_no_longer_shown():
    authorization = DeviceAuthorization("dc", "ABCD-EFGH", "https://a/activate", None, 1, 5)
    now = [100.0]
    codes = pairing.PendingCodes(clock=lambda: now[0])
    codes.show(authorization)
    assert codes.get().user_code == "ABCD-EFGH"
    now[0] += 2
    assert codes.get() is None


def test_pairing_that_fails_forgets_its_code(tmp_path):
    async def scenario(api, auth0, api_url):
        auth0.script = ["pending", "deny"]
        call, _, _ = pair(api_url, tmp_path)
        with pytest.raises(PairingDenied):
            await call

    run(scenario)
    assert pairing.pending.get() is None
