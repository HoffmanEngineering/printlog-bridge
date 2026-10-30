"""The Home Assistant app's start-up (cli/addon.py): the app's options become the bridge's config
and environment, `/data` is made the bridge user's, and the bridge runs as that user."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from printlog_bridge import __version__
from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.cli import addon
from printlog_bridge.cli.addon import OptionsError, bridge_env, prepare, render_config
from printlog_bridge.core.api_client import REPAIR_NEEDED
from printlog_bridge.core.config import (
    API_KEY_ENV,
    CONFIG_PATH_ENV,
    STATE_DIR_ENV,
    STATUS_PAGE_CLIENTS_ENV,
    STATUS_PAGE_ENV,
    TRACE_ENV,
    load_config,
)
from printlog_bridge.core.device import DEVICE_ID_FILE, DEVICE_TOKEN_FILE
from printlog_bridge.core.pairing import NAME_ENV

MOONRAKER = {"name": "voron", "adapter": "moonraker", "printer_id": 42, "url": "http://voron:7125"}
BAMBU = {
    "name": "x1c",
    "adapter": "bambu",
    "printer_id": 44,
    "host": "192.168.1.50",
    "serial": "01S00C123456789",
    "access_code": "12345678",
}
PRUSALINK = {
    "name": "mk4s",
    "adapter": "prusalink",
    "printer_id": 45,
    "host": "192.168.1.60",
    "password": "s3cret-pass",
}
OCTOPRINT = {
    "name": "ender",
    "adapter": "octoprint",
    "printer_id": 46,
    "url": "http://octopi.local",
    "api_key": "octo-key",
}
SDCP = {"name": "cc", "adapter": "sdcp", "printer_id": 47, "host": "192.168.1.77"}


def load(tmp_path: Path, text: str):
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(text, encoding="utf-8")
    env = {STATE_DIR_ENV: str(tmp_path / "state")}
    return load_config(path, adapters=ADAPTERS, env=env, home=tmp_path)


# -- options -> config ---------------------------------------------------------------------------


def test_every_adapter_becomes_a_printer_section_the_bridge_loads(tmp_path):
    printers = [MOONRAKER, BAMBU, PRUSALINK, OCTOPRINT, SDCP]
    config = load(tmp_path, render_config({"printers": printers}))

    assert [(p.name, p.adapter.name, p.printer_id) for p in config.printers] == [
        ("voron", "moonraker", 42),
        ("x1c", "bambu", 44),
        ("mk4s", "prusalink", 45),
        ("ender", "octoprint", 46),
        ("cc", "sdcp", 47),
    ]
    assert config.printers[3].adapter.api_key == "octo-key"
    assert config.update_check is False  # Home Assistant shows the app's updates itself


def test_the_printer_api_key_goes_where_each_adapter_reads_it():
    text = render_config(
        {
            "printers": [
                dict(MOONRAKER, api_key="moon-key"),
                dict(PRUSALINK, password=None, api_key="prusa-key", username="maker2"),
            ]
        }
    )
    voron, mk4s = text.split("[printer mk4s]")
    assert "moonraker_api_key = moon-key" in voron
    assert "api_key = prusa-key" in mk4s
    assert "username = maker2" in mk4s
    assert "password" not in mk4s


def test_a_printer_without_printer_id_is_left_for_the_bridge_to_explain(tmp_path):
    printer = dict(MOONRAKER)
    del printer["printer_id"]
    config = load(tmp_path, render_config({"printers": [printer]}))
    assert config.printers[0].printer_id is None


def test_unset_optional_fields_are_left_out():
    text = render_config({"printers": [dict(MOONRAKER, host=None, serial="", api_key=None)]})
    assert "host" not in text
    assert "serial" not in text
    assert "api_key" not in text


def test_api_url_goes_into_the_bridge_section(tmp_path):
    text = render_config({"api_url": "https://dev.example.com/", "printers": [MOONRAKER]})
    assert load(tmp_path, text).api_url == "https://dev.example.com"


def test_values_are_stripped():
    text = render_config({"printers": [dict(MOONRAKER, name="  voron  ", url=" http://v:7125 ")]})
    assert "[printer voron]" in text
    assert "url = http://v:7125\n" in text


@pytest.mark.parametrize(
    ("printers", "says"),
    [
        ([], "Add your printers"),
        ([dict(MOONRAKER, url=None)], "printer 'voron' (moonraker) needs url"),
        ([dict(MOONRAKER, host="x")], "printer 'voron' (moonraker) doesn't use host"),
        ([dict(BAMBU, password="x")], "printer 'x1c' (bambu) doesn't use password"),
        ([dict(MOONRAKER, adapter="creality")], "unknown adapter 'creality'"),
        ([dict(MOONRAKER, name="")], "every printer needs a name"),
        ([dict(MOONRAKER, name="a]b")], "can't contain"),
        ([MOONRAKER, dict(BAMBU, name="voron")], "two printers are called 'voron'"),
    ],
)
def test_options_that_cant_work_are_refused_with_what_to_fix(printers, says):
    with pytest.raises(OptionsError, match=re.escape(says)):
        render_config({"printers": printers})


@pytest.mark.parametrize(
    "value",
    [
        "12345678\n[printer evil]",  # would add a section
        "1234\r5678",
        "abc\x00",
        "qx12 #zq9x",  # an inline comment: the bridge would read "qx12"
        "qx12 ;zq9x",
        "qx12\t#zq9x",
        "#zq9x",  # after `key = `, a comment too: the bridge would read nothing
        ";zq9x",
    ],
)
def test_a_value_the_config_file_cant_hold_is_refused_without_showing_it(value):
    with pytest.raises(OptionsError) as raised:
        render_config({"printers": [dict(BAMBU, access_code=value)]})
    message = str(raised.value)
    assert "access_code" in message
    assert value not in message
    for part in ("5678", "zq9x", "qx12", "evil"):
        assert part not in message


def test_a_hash_without_a_space_before_it_is_kept():
    text = render_config({"printers": [dict(PRUSALINK, password="pa#ss;word")]})
    assert "password = pa#ss;word\n" in text


def test_an_error_never_shows_a_secret():
    with pytest.raises(OptionsError) as raised:
        render_config({"printers": [dict(BAMBU, url="http://x", access_code="topsecret1")]})
    assert "topsecret1" not in str(raised.value)


# -- options -> environment ----------------------------------------------------------------------


def test_the_api_key_and_name_reach_the_bridge_through_its_environment():
    env = bridge_env(
        {"device_name": " Garage HA ", "api_key": " key-123 ", "trace": True, "printers": []},
        {"PATH": "/usr/bin", "TZ": "Europe/Berlin"},
    )
    assert env[NAME_ENV] == "Garage HA"
    assert env[API_KEY_ENV] == "key-123"
    assert env[TRACE_ENV] == "1"
    assert env["TZ"] == "Europe/Berlin"
    assert env["PATH"] == "/usr/bin"


def test_unset_options_leave_the_environment_alone():
    env = bridge_env({"trace": False, "printers": []}, {API_KEY_ENV: "", NAME_ENV: "x"})
    assert API_KEY_ENV not in env  # an empty variable would read as no key anyway
    assert env[NAME_ENV] == "x"
    assert TRACE_ENV not in env


def test_the_local_page_is_the_ingress_page_for_the_supervisor_only():
    # Even a printlog-bridge.cfg in the app's config folder can't move it: the environment wins.
    env = bridge_env({"printers": []}, {STATUS_PAGE_ENV: "off", STATUS_PAGE_CLIENTS_ENV: ""})
    assert env[STATUS_PAGE_ENV] == f"0.0.0.0:{addon.INGRESS_PORT}"
    assert env[STATUS_PAGE_CLIENTS_ENV] == addon.INGRESS_PROXY == "172.30.32.2"


# -- preparing /data -----------------------------------------------------------------------------


class Owner:
    """Records chown calls instead of making them (tests don't run as root)."""

    def __init__(self) -> None:
        self.calls: dict[str, tuple[int, int]] = {}

    def __call__(self, path, uid: int, gid: int) -> None:
        self.calls[str(path)] = (uid, gid)


def write_options(data: Path, options: dict) -> None:
    data.mkdir(exist_ok=True)
    (data / "options.json").write_text(json.dumps(options), encoding="utf-8")


def test_prepare_writes_the_config_and_hands_data_to_the_bridge_user(tmp_path):
    data = tmp_path / "data"
    write_options(data, {"api_key": "key-123", "printers": [BAMBU]})
    (data / "state").mkdir()
    (data / "state" / "outbox.json").write_text("[]", encoding="utf-8")
    owner = Owner()

    config_path, env = prepare(
        data, tmp_path / "config" / "printlog-bridge.cfg", {}, uid=1000, gid=1000, chown=owner
    )

    assert config_path == data / "printlog-bridge.cfg"
    assert env[CONFIG_PATH_ENV] == str(config_path)
    assert env[STATE_DIR_ENV] == str(data / "state")
    assert env[API_KEY_ENV] == "key-123"
    text = config_path.read_text(encoding="utf-8")
    assert "access_code = 12345678" in text
    assert "key-123" not in text  # the API key stays out of every file
    for path in (data, data / "state", data / "state" / "outbox.json", config_path):
        assert owner.calls[str(path)] == (1000, 1000), path
    assert str(data / "options.json") not in owner.calls


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_config_holding_printer_secrets_is_readable_by_its_owner_only(tmp_path):
    data = tmp_path / "data"
    write_options(data, {"printers": [BAMBU]})
    config_path, _ = prepare(data, tmp_path / "none.cfg", {}, uid=0, gid=0, chown=Owner())
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((data / "state").stat().st_mode) == 0o700


def test_a_config_file_in_the_app_config_folder_wins_over_the_printers_option(tmp_path, capsys):
    data = tmp_path / "data"
    write_options(data, {"printers": [BAMBU]})
    user_config = tmp_path / "config" / "printlog-bridge.cfg"
    user_config.parent.mkdir()
    # Settings the options don't have, such as where to look for a printer that moved.
    own = "[printer mine]\nprinter_id = 7\nurl = http://mine:7125\n"
    own += "discovery_networks = 10.0.0.0/24\n"
    user_config.write_text(own, encoding="utf-8")

    config_path, _ = prepare(data, user_config, {}, uid=1000, gid=1000, chown=Owner())

    assert config_path.read_text(encoding="utf-8") == own
    out = capsys.readouterr().out
    assert str(user_config) in out
    assert "ignores the printers" in out


def test_a_config_file_is_enough_without_printers_in_the_options(tmp_path):
    data = tmp_path / "data"
    write_options(data, {"printers": []})
    user_config = tmp_path / "printlog-bridge.cfg"
    user_config.write_text("[printer mine]\nurl = http://mine:7125\n", encoding="utf-8")
    config_path, _ = prepare(data, user_config, {}, uid=1000, gid=1000, chown=Owner())
    assert "[printer mine]" in config_path.read_text(encoding="utf-8")


def test_prepare_says_what_it_uses_without_any_secret(tmp_path, capsys):
    data = tmp_path / "data"
    write_options(data, {"api_key": "key-123", "printers": [BAMBU, PRUSALINK]})
    prepare(data, tmp_path / "none.cfg", {}, uid=1000, gid=1000, chown=Owner())
    out = capsys.readouterr().out
    assert "2 printers from the app's options (x1c, mk4s)" in out
    assert "the API key from the app's options" in out
    for secret in ("key-123", "12345678", "s3cret-pass"):
        assert secret not in out


def stopped_needing_a_repair(data: Path, problem: str = REPAIR_NEEDED) -> Path:
    """A state directory as a bridge leaves it after 3D Print Log refused its device token."""
    state = data / "state"
    state.mkdir(parents=True)
    token = state / DEVICE_TOKEN_FILE
    token.write_text("{}", encoding="utf-8")
    status = {"credentials_problem": problem, "updated_at": "2026-10-27T10:00:00+00:00"}
    (state / "status.json").write_text(json.dumps(status), encoding="utf-8")
    os.utime(token, (1_000_000, 1_000_000))  # older than the status file
    return token


def test_a_restart_after_the_token_was_revoked_pairs_again(tmp_path, capsys):
    # There is no shell to run `printlog-bridge pair` in an app: restarting it is the re-pair.
    data = tmp_path / "data"
    write_options(data, {"printers": [MOONRAKER]})
    token = stopped_needing_a_repair(data)
    (data / "state" / DEVICE_ID_FILE).write_text("id-1", encoding="utf-8")

    prepare(data, tmp_path / "none.cfg", {}, uid=1000, gid=1000, chown=Owner())

    assert not token.exists()
    # The same device in 3D Print Log is paired again, not a second one.
    assert (data / "state" / DEVICE_ID_FILE).read_text(encoding="utf-8") == "id-1"
    assert "pairs again" in capsys.readouterr().out


def test_a_token_newer_than_the_refusal_is_kept(tmp_path):
    data = tmp_path / "data"
    write_options(data, {"printers": [MOONRAKER]})
    token = stopped_needing_a_repair(data)
    os.utime(token, None)  # written after the status file: a new pairing
    os.utime(data / "state" / "status.json", (1_000_000, 1_000_000))
    prepare(data, tmp_path / "none.cfg", {}, uid=1000, gid=1000, chown=Owner())
    assert token.exists()


def test_a_token_is_kept_for_any_other_problem(tmp_path):
    data = tmp_path / "data"
    write_options(data, {"printers": [MOONRAKER]})
    token = stopped_needing_a_repair(data, problem="something else")
    prepare(data, tmp_path / "none.cfg", {}, uid=1000, gid=1000, chown=Owner())
    assert token.exists()


def test_without_options_json_it_isnt_running_as_an_app(tmp_path):
    with pytest.raises(OptionsError, match="options.json"):
        prepare(tmp_path, tmp_path / "none.cfg", {}, uid=1000, gid=1000, chown=Owner())


def test_a_damaged_options_json_is_refused(tmp_path):
    (tmp_path / "options.json").write_text("{", encoding="utf-8")
    with pytest.raises(OptionsError, match="options.json"):
        prepare(tmp_path, tmp_path / "none.cfg", {}, uid=1000, gid=1000, chown=Owner())


# -- main ----------------------------------------------------------------------------------------


def test_main_drops_to_the_bridge_user_then_runs_the_bridge(tmp_path, monkeypatch):
    data = tmp_path / "data"
    write_options(data, {"device_name": "HA", "printers": [MOONRAKER]})
    calls: list = []
    monkeypatch.setattr(addon, "DATA_DIR", data)
    monkeypatch.setattr(addon, "USER_CONFIG", tmp_path / "none.cfg")
    monkeypatch.setattr(addon, "_is_root", lambda: True)
    monkeypatch.setattr(addon, "_chown", Owner())
    monkeypatch.setattr(addon, "_drop_to", lambda uid, gid: calls.append(("drop", uid, gid)))
    monkeypatch.setattr(addon, "_exec", lambda argv, env: calls.append(("exec", argv, env)))

    assert addon.main([]) == 0

    assert calls[0] == ("drop", 1000, 1000)
    kind, argv, env = calls[1]
    assert kind == "exec"
    assert argv == ["printlog-bridge", "run"]
    assert env[NAME_ENV] == "HA"


def test_main_reports_bad_options_and_exits_2(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    write_options(data, {"printers": [dict(MOONRAKER, url=None)]})
    monkeypatch.setattr(addon, "DATA_DIR", data)
    monkeypatch.setattr(addon, "USER_CONFIG", tmp_path / "none.cfg")
    monkeypatch.setattr(addon, "_is_root", lambda: False)
    monkeypatch.setattr(addon, "_exec", lambda argv, env: pytest.fail("ran the bridge"))

    assert addon.main([]) == 2
    assert "needs url" in capsys.readouterr().err


def test_main_not_as_root_runs_the_bridge_as_it_is(tmp_path, monkeypatch):
    data = tmp_path / "data"
    write_options(data, {"printers": [MOONRAKER]})
    calls: list = []
    monkeypatch.setattr(addon, "DATA_DIR", data)
    monkeypatch.setattr(addon, "USER_CONFIG", tmp_path / "none.cfg")
    monkeypatch.setattr(addon, "_is_root", lambda: False)
    monkeypatch.setattr(addon, "_chown", lambda *a: pytest.fail("chown without root"))
    monkeypatch.setattr(addon, "_drop_to", lambda uid, gid: pytest.fail("dropped without root"))
    monkeypatch.setattr(addon, "_exec", lambda argv, env: calls.append(argv))

    assert addon.main([]) == 0
    assert calls == [["printlog-bridge", "run"]]


def test_version_answers_without_options(tmp_path, monkeypatch, capsys):
    # tests/docker/check_published.sh starts every published image with --version.
    monkeypatch.setattr(addon, "DATA_DIR", tmp_path)
    monkeypatch.setattr(addon, "_exec", lambda argv, env: pytest.fail("ran the bridge"))
    assert addon.main(["--version"]) == 0
    assert capsys.readouterr().out == f"printlog-bridge {__version__} (Home Assistant app)\n"


def test_the_bridge_user_is_the_images():
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text("utf-8")
    assert f"--uid {addon.BRIDGE_UID} --gid {addon.BRIDGE_GID}" in dockerfile
    assert addon.DATA_DIR.as_posix() == "/data"
    assert addon.USER_CONFIG.as_posix() == "/config/printlog-bridge.cfg"
