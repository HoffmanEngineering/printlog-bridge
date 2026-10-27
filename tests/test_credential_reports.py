"""The bridge's encryption key in `status`, `doctor`, `pair` and `rotate-key` (story 14b)."""

from __future__ import annotations

import builtins
import io
from datetime import datetime, timezone

import pytest

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.cli import main as cli
from printlog_bridge.cli.pair import key_line as pair_key_line
from printlog_bridge.core.config import load_config
from printlog_bridge.core.credentials import KeyStore, read_key, short_fingerprint
from printlog_bridge.core.device import TOKEN_PREFIX, write_device_token
from printlog_bridge.core.doctor import _check_bridge, _check_credential_key
from printlog_bridge.core.status import StatusBoard, format_status


def paired(tmp_path):
    state = tmp_path / "state"
    write_device_token(
        state,
        token=TOKEN_PREFIX + "t" * 40,
        api_url="http://127.0.0.1:9",
        grant_id=1,
        name="b",
        scopes=[],
    )
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(
        f"[bridge]\napi_url = http://127.0.0.1:9\nstate_dir = {state}\n", encoding="utf-8"
    )
    return load_config(path, ADAPTERS, env={}, home=tmp_path, printers_required=False)


def running_with(config, **bridge):
    board = StatusBoard(config.state_dir)
    board.start([])
    board.bridge(**bridge)
    return datetime.now(timezone.utc)


def test_status_shows_the_fingerprint_and_that_the_app_waits_for_confirmation(tmp_path):
    config = paired(tmp_path)
    keys = KeyStore(config.state_dir)
    keys.load()
    detail = "waiting for confirmation in the app: open Bridges in 3D Print Log"
    now = running_with(
        config, key={"fingerprint": keys.key.fingerprint, "state": "pending", "detail": detail}
    )

    text, _ = format_status(config, now)

    assert f"Encryption key: {short_fingerprint(keys.key.fingerprint)}, {detail}" in text
    check = _check_credential_key(config, now)
    assert check.status.value == "warn"
    assert short_fingerprint(keys.key.fingerprint) in check.detail
    assert "waiting for confirmation in the app" in check.detail


def test_status_and_doctor_before_there_is_a_key(tmp_path):
    config = paired(tmp_path)
    now = datetime.now(timezone.utc)

    text, _ = format_status(config, running_with(config))

    assert "Encryption key: none yet" in text
    assert _check_credential_key(config, now).status.value == "ok"
    assert read_key(config.state_dir) is None  # status and doctor never make one


def test_doctor_names_the_missing_package(tmp_path, monkeypatch):
    config = paired(tmp_path)
    real_import = builtins.__import__

    def no_cryptography(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_cryptography)

    check = _check_credential_key(config, datetime.now(timezone.utc))

    assert check.status.value == "warn"
    assert "pip install cryptography" in check.detail


def test_doctor_fails_on_a_key_it_cant_read(tmp_path):
    config = paired(tmp_path)
    (config.state_dir / "credential-key.pem").write_text("garbage", encoding="utf-8")

    check = _check_credential_key(config, datetime.now(timezone.utc))

    assert check.status.value == "FAIL"
    assert "rotate-key" in check.detail


def test_an_unpaired_bridge_has_no_key_check(tmp_path):
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(f"[bridge]\nstate_dir = {tmp_path / 's'}\n", encoding="utf-8")
    config = load_config(path, ADAPTERS, env={}, home=tmp_path, printers_required=False)

    assert _check_credential_key(config, datetime.now(timezone.utc)) is None


def test_doctor_warns_about_a_credential_issue_of_an_app_printer(tmp_path):
    config = paired(tmp_path)
    now = running_with(
        config,
        check_in={"at": "2026-10-27T00:00:00Z", "problem": None},
        app_printers={
            "7": {
                "kind": "bambu",
                "host": "192.168.1.50",
                "state": "watching",
                "detail": "bambu, 192.168.1.50",
                "credentials": {"access_code": "entered in the app, and the printer refused it"},
            }
        },
    )

    [check] = [c for c in _check_bridge(config, now) if c.name == "App printer 7"]

    assert check.status.value == "warn"
    assert "access_code: entered in the app, and the printer refused it" in check.detail


def test_pair_shows_the_key_and_keeps_it(tmp_path):
    config = paired(tmp_path)

    first = pair_key_line(config)
    again = pair_key_line(config)

    key = read_key(config.state_dir)
    assert short_fingerprint(key.fingerprint) in first
    assert first == again


def test_rotate_key_replaces_the_key_with_yes(tmp_path, capsys):
    config = paired(tmp_path)
    KeyStore(config.state_dir).load()
    old = read_key(config.state_dir).fingerprint

    code = cli.main(["rotate-key", "--yes", "--config", str(config.path)])

    new = read_key(config.state_dir).fingerprint
    out = capsys.readouterr().out
    assert code == 0
    assert new != old
    assert short_fingerprint(old) in out and short_fingerprint(new) in out


def test_rotate_key_without_a_terminal_needs_yes(tmp_path, monkeypatch, capsys):
    config = paired(tmp_path)
    KeyStore(config.state_dir).load()
    old = read_key(config.state_dir).fingerprint
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    code = cli.main(["rotate-key", "--config", str(config.path)])

    assert code == 1
    assert read_key(config.state_dir).fingerprint == old
    assert "--yes" in capsys.readouterr().err


@pytest.mark.parametrize("answer, kept", [("yes\n", False), ("no\n", True)])
def test_rotate_key_asks_at_a_terminal(tmp_path, monkeypatch, answer, kept):
    config = paired(tmp_path)
    KeyStore(config.state_dir).load()
    old = read_key(config.state_dir).fingerprint
    stdin = io.StringIO(answer)
    stdin.isatty = lambda: True
    monkeypatch.setattr("sys.stdin", stdin)

    cli.main(["rotate-key", "--config", str(config.path)])

    assert (read_key(config.state_dir).fingerprint == old) is kept
