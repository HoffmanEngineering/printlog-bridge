"""Sealed LAN credentials, end to end (#100, story 14b): the whole bridge against the fake API,
which seals as the web app does, and fake printers that check what the bridge sends."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

import pytest
from bridge_helpers import until
from fake_bambu import FakeBambu, certificate
from fake_moonraker import FakeMoonraker
from fake_printlog_api import FakePrintLogApi
from fake_prusalink import FakePrusaLink
from sealing import seal_bytes

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.core import bridge_sync
from printlog_bridge.core.bridge import run_bridge
from printlog_bridge.core.config import load_config
from printlog_bridge.core.credentials import (
    KEY_FILE,
    CredentialKey,
    associated_data,
    read_key,
    short_fingerprint,
)
from printlog_bridge.core.device import write_device_token
from printlog_bridge.core.status import read_status


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(bridge_sync, "MIN_POLL_S", 0.05)
    monkeypatch.setattr(bridge_sync, "MIN_CHECK_IN_S", 0.2)
    monkeypatch.setattr(bridge_sync, "ISSUES_CHECK_IN_S", 0.1)


async def as_is(host: str) -> str:
    return host


async def nothing(services, wait):
    return []


async def no_port(port, on_datagram):
    return None


@contextlib.asynccontextmanager
async def running(tmp_path: Path, printers: str = "", before=None):
    api = FakePrintLogApi(printers=(42, 43))
    api.desired_poll_seconds = 0.05
    api.check_in_seconds = 0.2
    url = await api.start()
    token = api.issue_device_token()
    write_device_token(
        tmp_path / "state", token=token, api_url=url, grant_id=1, name="b",
        scopes=sorted(api.grant_for(token)["scopes"]),
    )  # fmt: skip
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(
        f"[bridge]\napi_url = {url}\nstate_dir = {tmp_path / 'state'}\ndiscovery = false\n"
        + printers,
        encoding="utf-8",
    )

    def load():
        return load_config(path, ADAPTERS, env={}, home=tmp_path, printers_required=False)

    if before is not None:
        await before(api)
    task = asyncio.ensure_future(
        run_bridge(
            load(),
            adapters=ADAPTERS,
            reload=load,
            reload_poll=0.05,
            heartbeat_interval=0.1,
            sync_options={"resolve": as_is},
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


def bridge_status(tmp_path: Path) -> dict:
    return (read_status(tmp_path / "state") or {}).get("bridge") or {}


def app_state(tmp_path: Path, entry: int) -> dict:
    return (bridge_status(tmp_path).get("app_printers") or {}).get(str(entry)) or {}


def connection(tmp_path: Path, entry: int) -> str | None:
    printer = ((read_status(tmp_path / "state") or {}).get("printers") or {}).get(f"app-{entry}")
    return printer["connection"] if printer else None


def last_issues(api: FakePrintLogApi):
    reported = [c for c in api.check_ins if "credentialIssues" in c]
    return reported[-1] if reported else None


def issues_now(api: FakePrintLogApi, version_of=None):
    """The issues of the latest check-in that described the current desired version."""
    last = last_issues(api)
    if last is None or last["credentialIssuesVersion"] != api.bridge()["desired"]["version"]:
        return None
    return last["credentialIssues"]


async def prusalink(password: str) -> tuple[FakePrusaLink, int]:
    printer = FakePrusaLink(password=password)
    url = await printer.start()
    return printer, int(url.rsplit(":", 1)[1].split("/")[0])


def test_the_first_check_in_registers_the_key_it_keeps(tmp_path):
    async def scenario():
        async with running(tmp_path) as (api, _, _):
            await until(lambda: bridge_status(tmp_path).get("key", {}).get("state"), 5)
            return api.check_ins[0], api.bridge(), bridge_status(tmp_path)["key"]

    first, stored, key_status = asyncio.run(scenario())
    key = read_key(tmp_path / "state")
    assert first["publicKey"] == key.public_key
    assert first["publicKeyFingerprint"] == key.fingerprint
    assert stored["publicKeyFingerprint"] == key.fingerprint  # taken on the registering check-in
    assert key_status["state"] == "confirmed"
    assert key_status["fingerprint"] == key.fingerprint


def test_a_sealed_password_is_opened_and_used_exactly_as_entered(tmp_path, caplog):
    password = " pässword with spaces "  # never trimmed: the printer compares it as is
    caplog.set_level(logging.DEBUG)

    async def scenario():
        printer, port = await prusalink(password)
        try:
            async with running(tmp_path) as (api, _, _):
                await until(lambda: api.bridge().get("publicKey"), 5)
                entry = api.connect("prusalink", "127.0.0.1", 42, port=port)
                await until(lambda: app_state(tmp_path, entry).get("state"), 5)
                sealed = api.set_secret(entry, "password", password)
                await until(lambda: connection(tmp_path, entry) == "connected", 10)
                await until(lambda: issues_now(api) == [], 5)
                return entry, sealed, app_state(tmp_path, entry)
        finally:
            await printer.close()

    entry, sealed, state = asyncio.run(scenario())
    assert state["state"] == "watching"
    assert state["credentials"] == {"password": "from the app"}
    written = b"".join(p.read_bytes() for p in (tmp_path / "state").rglob("*") if p.is_file())
    assert password.encode() not in written  # not in bridge.json, status.json, the outbox...
    assert password not in caplog.text
    assert sealed not in caplog.text
    assert read_key(tmp_path / "state").to_pem().splitlines()[1] not in caplog.text


def test_a_refused_password_is_reported_then_cleared_when_entered_again(tmp_path):
    async def scenario():
        printer, port = await prusalink("right")
        try:
            async with running(tmp_path) as (api, _, _):
                await until(lambda: api.bridge().get("publicKey"), 5)
                entry = api.connect("prusalink", "127.0.0.1", 42, port=port)
                await until(lambda: app_state(tmp_path, entry).get("state"), 5)
                api.set_secret(entry, "password", "wrong")
                await until(lambda: issues_now(api), 10)
                refused = issues_now(api)
                api.set_secret(entry, "password", "right")
                await until(lambda: connection(tmp_path, entry) == "connected", 10)
                await until(lambda: issues_now(api) == [], 5)
                return entry, refused
        finally:
            await printer.close()

    entry, refused = asyncio.run(scenario())
    assert refused == [{"entryId": entry, "field": "password", "code": "rejected_by_printer"}]


def test_a_printer_without_its_credential_is_missing_and_says_how_to_add_it(tmp_path):
    async def scenario():
        async with running(tmp_path) as (api, _, _):
            entry = api.connect("bambu", "127.0.0.1", 42, serial="01S00C123456789")
            await until(lambda: issues_now(api), 5)
            return entry, issues_now(api), app_state(tmp_path, entry)

    entry, issues, state = asyncio.run(scenario())
    assert issues == [{"entryId": entry, "field": "access_code", "code": "missing"}]
    assert state["state"] == "needs credentials"
    assert "3D Print Log" in state["detail"]
    assert f"[app_printer {entry}]" in state["detail"]


def test_values_that_dont_open_or_arent_credentials_are_reported(tmp_path):
    async def scenario():
        async with running(tmp_path) as (api, _, _):
            await until(lambda: api.bridge().get("publicKey"), 5)
            key = read_key(tmp_path / "state")
            bambu = api.connect("bambu", "127.0.0.1", 42, serial="01S00C123456789")
            mk4 = api.connect("prusalink", "127.0.0.1", 43, port=9)
            other_bridge = api.bridge()["id"] + 1
            api.set_secret(
                bambu, "access_code", sealed=_seal(key, other_bridge, bambu, "access_code", b"1")
            )
            api.set_secret(
                mk4, "password", sealed=_seal(key, api.bridge()["id"], mk4, "password", b"a\nb")
            )
            await until(lambda: issues_now(api) and len(issues_now(api)) == 2, 5)
            return bambu, mk4, issues_now(api), app_state(tmp_path, bambu)

    bambu, mk4, issues, state = asyncio.run(scenario())
    assert {"entryId": bambu, "field": "access_code", "code": "undecryptable"} in issues
    assert {"entryId": mk4, "field": "password", "code": "invalid_plaintext"} in issues
    assert "doesn't open with this bridge's key" in state["detail"]


def _seal(key: CredentialKey, bridge_id: int, entry_id: int, field: str, data: bytes) -> str:
    from cryptography.hazmat.primitives import serialization

    public = serialization.load_der_public_key(key.spki)
    return seal_bytes(public, associated_data(bridge_id, entry_id, field), data)


def test_the_config_file_wins_over_the_app(tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def scenario():
        printer, port = await prusalink("from-the-file")
        try:
            async with running(tmp_path) as (api, path, _):
                await until(lambda: api.bridge().get("publicKey"), 5)
                entry = api.connect("prusalink", "127.0.0.1", 42, port=port)
                with path.open("a", encoding="utf-8") as f:
                    f.write(f"\n[app_printer {entry}]\npassword = from-the-file\n")
                api.set_secret(entry, "password", "from-the-app")
                await until(lambda: connection(tmp_path, entry) == "connected", 10)
                await until(lambda: issues_now(api) == [], 5)
                return entry, app_state(tmp_path, entry)
        finally:
            await printer.close()

    entry, state = asyncio.run(scenario())
    assert "overrides the app's" in state["credentials"]["password"]
    assert "the config file's is used" in caplog.text


def test_a_moonraker_that_wants_a_key_reports_it_missing(tmp_path):
    async def scenario():
        moonraker = FakeMoonraker(api_key="k" * 32)
        url = await moonraker.start()
        port = int(url.rsplit(":", 1)[1].split("/")[0])
        try:
            async with running(tmp_path) as (api, _, _):
                entry = api.connect("moonraker", "127.0.0.1", 42, port=port)
                await until(lambda: issues_now(api), 10)
                missing = issues_now(api)
                await until(lambda: api.bridge().get("publicKey"), 5)
                api.set_secret(entry, "moonraker_api_key", "k" * 32)
                await until(lambda: connection(tmp_path, entry) == "connected", 10)
                await until(lambda: issues_now(api) == [], 5)
                return entry, missing
        finally:
            await moonraker.close()

    entry, missing = asyncio.run(scenario())
    assert missing == [{"entryId": entry, "field": "moonraker_api_key", "code": "missing"}]


def test_a_bambu_access_code_from_the_app_reaches_the_printer(tmp_path):
    async def scenario():
        printer = FakeBambu(*certificate(tmp_path / "cert"), access_code="87654321")
        port = await printer.start()
        try:
            async with running(tmp_path) as (api, _, _):
                await until(lambda: api.bridge().get("publicKey"), 5)
                entry = api.connect("bambu", "127.0.0.1", 42, port=port, serial=printer.serial)
                api.set_secret(entry, "access_code", "12345678")
                await until(lambda: issues_now(api), 10)
                refused = issues_now(api)
                api.set_secret(entry, "access_code", "87654321")
                await until(lambda: issues_now(api) == [], 10)
                return entry, refused
        finally:
            await printer.close()

    entry, refused = asyncio.run(scenario())
    assert refused == [{"entryId": entry, "field": "access_code", "code": "rejected_by_printer"}]


def test_a_new_key_waits_for_confirmation_in_the_app(tmp_path, caplog):
    """A bridge registered before it had a key (paired under story 12) reports one: it's held
    until the owner confirms it."""

    async def registered_without_a_key(api):
        api.bridges[1] = {
            "id": 1,
            "desired": {"version": "v0", "scanRequestedAt": None, "printers": []},
            "networks": [],
            "discovered": [],
        }

    async def scenario():
        async with running(tmp_path, before=registered_without_a_key) as (api, _, _):
            await until(lambda: bridge_status(tmp_path).get("key", {}).get("state"), 5)
            pending = dict(bridge_status(tmp_path)["key"])
            api.confirm_key()
            await until(lambda: bridge_status(tmp_path)["key"]["state"] == "confirmed", 5)
            return pending

    pending = asyncio.run(scenario())
    key = read_key(tmp_path / "state")
    assert pending["state"] == "pending"
    assert short_fingerprint(key.fingerprint) in pending["detail"]
    assert "waiting for confirmation in the app" in pending["detail"]
    assert "waiting for confirmation in the app" in caplog.text


def test_an_api_without_sealing_still_runs_app_printers_from_the_config_file(tmp_path):
    async def old_api(api):
        api.keys_supported = False

    async def scenario():
        printer, port = await prusalink("s3cret")
        try:
            async with running(tmp_path, before=old_api) as (api, path, _):
                entry = api.connect("prusalink", "127.0.0.1", 42, port=port)
                with path.open("a", encoding="utf-8") as f:
                    f.write(f"\n[app_printer {entry}]\npassword = s3cret\n")
                await until(lambda: connection(tmp_path, entry) == "connected", 10)
                await until(lambda: bridge_status(tmp_path).get("key", {}).get("state"), 5)
                return bridge_status(tmp_path)["key"]
        finally:
            await printer.close()

    assert asyncio.run(scenario())["state"] == "unsupported"


def test_rotating_the_key_while_running_reports_the_new_one(tmp_path):
    async def scenario():
        async with running(tmp_path) as (api, _, _):
            await until(lambda: api.bridge().get("publicKey"), 5)
            old = api.bridge()["publicKeyFingerprint"]
            new = CredentialKey.generate()
            (tmp_path / "state" / KEY_FILE).write_text(new.to_pem(), encoding="utf-8")
            await until(lambda: api.bridge().get("pendingPublicKeyFingerprint"), 5)
            await until(lambda: bridge_status(tmp_path)["key"]["state"] == "pending", 5)
            return old, new.fingerprint, api.bridge()

    old, new, stored = asyncio.run(scenario())
    assert stored["publicKeyFingerprint"] == old
    assert stored["pendingPublicKeyFingerprint"] == new


def test_bridge_json_keeps_the_bridge_id_and_only_sealed_values(tmp_path):
    async def scenario():
        async with running(tmp_path) as (api, _, _):
            await until(lambda: api.bridge().get("publicKey"), 5)
            entry = api.connect("prusalink", "127.0.0.1", 42, port=9)
            sealed = api.set_secret(entry, "password", "hunter2-password")
            path = tmp_path / "state" / "bridge.json"
            await until(lambda: path.exists() and sealed in path.read_text(encoding="utf-8"), 5)
            return api.bridge()["id"], json.loads(path.read_text(encoding="utf-8"))

    bridge_id, stored = asyncio.run(scenario())
    assert stored["bridge_id"] == bridge_id
    assert "hunter2-password" not in json.dumps(stored)
