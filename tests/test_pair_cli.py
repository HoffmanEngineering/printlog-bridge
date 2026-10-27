"""`printlog-bridge pair`, the first run's pairing, and writing printer ids into the config."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import sys
from pathlib import Path

import pytest
from fake_auth0 import FakeAuth0
from fake_printlog_api import FakePrintLogApi
from pairing_helpers import ThreadedFakes

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.cli import pair as pair_cli
from printlog_bridge.cli.main import main
from printlog_bridge.cli.pair import pair_command, pair_on_first_run
from printlog_bridge.core.config import load_config, set_printer_ids
from printlog_bridge.core.device import DEVICE_TOKEN_FILE, read_device_id, read_device_token

UNREACHABLE = "http://127.0.0.1:9"
"""A Moonraker that isn't there: the adapter's suggestions come from the config alone."""
TWO_PRINTERS = f"""\
[printer voron]
url = {UNREACHABLE}

[printer u1]
printer_id = 42
url = {UNREACHABLE}
"""


def answers(*replies: str):
    """An `ask` that answers the prompts in order, and records them."""
    remaining = list(replies)
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return remaining.pop(0)

    ask.asked = asked  # type: ignore[attr-defined]
    return ask


def load(path: Path):
    return load_config(path, adapters=ADAPTERS, env={}, home=path.parent)


def run_pair(path: Path, *, ask=None, interactive=True, yes=False, switch=False, name=None):
    return pair_command(
        load(path), {}, name=name, yes=yes, switch=switch, ask=ask, interactive=interactive
    )


def test_pair_links_a_printer_picked_from_the_account(tmp_path, capsys):
    with ThreadedFakes(printers=(42, 7)) as fakes:
        fakes.auth0.script = ["pending"]
        fakes.auth0.approve()
        path = fakes.config(tmp_path, TWO_PRINTERS)

        assert run_pair(path, ask=answers("2"), name="printlog-bridge on nas") == 0

        out = capsys.readouterr().out
        [grant] = fakes.api.grants.values()
        scope = fakes.auth0.authorizations[0]["scope"].split()
    assert "printers:create" in scope
    assert grant["name"] == "printlog-bridge on nas"
    assert grant["deviceId"] == read_device_id(tmp_path / "state")
    assert fakes.auth0.user_codes[0] in out
    assert "█" in out  # the QR code
    assert "Paired as 'printlog-bridge on nas'" in out
    config = load(path)
    assert {p.name: p.printer_id for p in config.printers} == {"voron": 7, "u1": 42}
    assert config.device is not None
    assert "[printer voron] now logs to printer 7" in out


def test_pair_creates_a_printer_filled_in_from_the_adapter(tmp_path, capsys):
    with ThreadedFakes() as fakes:
        fakes.auth0.approve()
        path = fakes.config(tmp_path, TWO_PRINTERS)
        # New printer; keep the name and make; the model has no suggestion; 4 slots.
        ask = answers("n", "", "", "Voron 2.4", "4")

        assert run_pair(path, ask=ask) == 0

        [created] = fakes.api.created_printers
    assert created == {
        "name": "voron",
        "make": "Klipper",
        "model": "Voron 2.4",
        "isActive": True,
        "slotCount": 4,
    }
    voron = next(p for p in load(path).printers if p.name == "voron")
    assert voron.printer_id is not None and voron.printer_id != 42
    assert "Created printer" in capsys.readouterr().out


def test_without_a_terminal_pair_asks_nothing_and_says_what_to_set(tmp_path, capsys):
    with ThreadedFakes(printers=(42, 7)) as fakes:
        fakes.auth0.approve()
        path = fakes.config(tmp_path, TWO_PRINTERS)
        before = path.read_text(encoding="utf-8")

        assert run_pair(path, ask=answers(), interactive=False) == 0

        scope = fakes.auth0.authorizations[0]["scope"].split()
    out = capsys.readouterr().out
    assert "printers:create" not in scope
    assert "[printer voron] has no printer_id" in out
    assert "  7: Printer 7 (Snapmaker U1)" in out
    assert "Set printer_id = <number>" in out
    assert path.read_text(encoding="utf-8") == before


def test_pair_again_rotates_the_token_and_keeps_one_grant(tmp_path, capsys):
    with ThreadedFakes() as fakes:
        fakes.auth0.approve()
        path = fakes.config(tmp_path, "[printer u1]\nprinter_id = 42\nurl = http://u1:7125\n")
        assert run_pair(path, interactive=False) == 0
        first = read_device_token(tmp_path / "state")
        assert run_pair(path, interactive=False) == 0
        second = read_device_token(tmp_path / "state")
        grants = list(fakes.api.grants.values())
    out = capsys.readouterr().out
    assert "already paired" in out
    assert "Paired again" in out
    assert len(grants) == 1
    assert first.token != second.token


def test_switch_printers_asks_for_printers_that_have_an_id_too(tmp_path):
    with ThreadedFakes(printers=(42, 7)) as fakes:
        fakes.auth0.approve()
        path = fakes.config(tmp_path, "[printer u1]\nprinter_id = 42\nurl = http://u1:7125\n")
        ask = answers("")  # the default: the printer it already logs to

        assert run_pair(path, ask=ask, switch=True) == 0

    assert ask.asked == ["Choice [1]: "]
    assert load(path).printers[0].printer_id == 42


def test_pair_on_a_server_without_pairing_says_to_store_a_key(tmp_path, capsys):
    with ThreadedFakes(pairing_on=False) as fakes:
        path = fakes.config(tmp_path, TWO_PRINTERS)
        assert run_pair(path, interactive=False) == 1
    err = capsys.readouterr().err
    assert "store-key" in err
    assert not (tmp_path / "state" / DEVICE_TOKEN_FILE).exists()


def test_the_pair_command_line(tmp_path, capsys, monkeypatch):
    with ThreadedFakes() as fakes:
        fakes.auth0.approve()
        path = fakes.config(tmp_path, "[printer u1]\nprinter_id = 42\nurl = http://u1:7125\n")
        monkeypatch.setattr(sys, "stdin", open(os.devnull))  # noqa: SIM115 - no terminal

        assert main(["pair", "--config", str(path), "--yes", "--name", "garage"]) == 0

        [grant] = fakes.api.grants.values()
    assert grant["name"] == "garage"
    assert "Restart printlog-bridge" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_pair_leaves_the_token_readable_only_by_the_user(tmp_path):
    with ThreadedFakes() as fakes:
        fakes.auth0.approve()
        path = fakes.config(tmp_path, "[printer u1]\nprinter_id = 42\nurl = http://u1:7125\n")
        assert run_pair(path, interactive=False) == 0
    mode = stat.S_IMODE((tmp_path / "state" / DEVICE_TOKEN_FILE).stat().st_mode)
    assert mode == 0o600


# -- the first run -------------------------------------------------------------------------------


def first_run(tmp_path: Path, printers: str, setup=None):
    async def scenario():
        auth0, api = FakeAuth0(interval=0.01), FakePrintLogApi(printers=(42, 7))
        await auth0.start()
        api_url = await api.start()
        api.auth0 = auth0
        api.pairing = auth0.discovery()
        auth0.script = ["pending", "expire", "pending"]
        auth0.approve()
        if setup is not None:
            setup(api)
        path = tmp_path / "printlog-bridge.cfg"
        path.write_text(
            f"[bridge]\napi_url = {api_url}\nstate_dir = {tmp_path / 'state'}\n{printers}",
            encoding="utf-8",
        )
        try:
            return await pair_on_first_run(load(path), {"PRINTLOG_BRIDGE_NAME": "nas"}), api
        finally:
            await api.close()
            await auth0.close()

    return asyncio.run(scenario())


def test_the_first_run_pairs_in_the_log_with_a_qr_code(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    problem, api = first_run(tmp_path, "[printer u1]\nprinter_id = 42\nurl = http://u1:7125\n")

    assert problem is None
    [grant] = api.grants.values()
    assert grant["name"] == "nas"
    assert load(tmp_path / "printlog-bridge.cfg").device is not None
    text = caplog.text
    assert text.count("scan the QR code") == 2  # the first code expired; a second one is shown
    assert "█" in text
    assert "Here is a new code" in text


def test_the_first_run_says_which_printer_id_to_set(tmp_path, caplog):
    problem, _ = first_run(tmp_path, f"[printer voron]\nurl = {UNREACHABLE}\n")

    assert problem is None
    assert "[printer voron] has no printer_id" in caplog.text
    assert "  7: Printer 7" in caplog.text


def test_the_first_run_without_pairing_on_the_server_gives_up(tmp_path):
    problem, api = first_run(
        tmp_path, "[printer u1]\nprinter_id = 42\nurl = http://u1:7125\n", setup=_pairing_off
    )
    assert "store-key" in problem
    assert api.grants == {}


def _pairing_off(api: FakePrintLogApi) -> None:
    api.pairing = None


@pytest.fixture
def root_logging():
    before = list(logging.getLogger().handlers)
    yield
    for handler in logging.getLogger().handlers[:]:
        if handler not in before:
            logging.getLogger().removeHandler(handler)
            handler.close()


def test_run_without_credentials_and_pairing_off_says_what_to_do(tmp_path, capsys, root_logging):
    with ThreadedFakes(pairing_on=False) as fakes:
        path = fakes.config(tmp_path, "[printer u1]\nprinter_id = 42\nurl = http://u1:7125\n")
        assert main(["run", "--config", str(path)]) == 2
    err = capsys.readouterr().err
    assert "doesn't offer pairing" in err
    assert "printlog-bridge store-key" in err


# -- printer ids in the config ---------------------------------------------------------------------


def test_set_printer_ids_replaces_or_adds_the_line_and_keeps_the_rest(tmp_path):
    path = tmp_path / "printlog-bridge.cfg"
    path.write_bytes(
        b"[bridge]\r\napi_key = k ; a comment\r\n\r\n"
        b"[printer voron]\r\nurl = http://voron:7125\r\n\r\n"
        b"[printer u1]\r\n  printer_id =  ; set me\r\nurl = http://u1:7125\r\n"
        b"[printer k2]\r\nprinter_id = 3\r\n"
    )

    missing = set_printer_ids(path, {"voron": 7, "u1": 42, "gone": 1})

    assert missing == ["gone"]
    assert path.read_bytes() == (
        b"[bridge]\r\napi_key = k ; a comment\r\n\r\n"
        b"[printer voron]\r\nprinter_id = 7\r\nurl = http://voron:7125\r\n\r\n"
        b"[printer u1]\r\n  printer_id = 42\r\nurl = http://u1:7125\r\n"
        b"[printer k2]\r\nprinter_id = 3\r\n"
    )


def test_set_printer_ids_on_a_last_line_without_an_ending(tmp_path):
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text("[printer voron]", encoding="utf-8")
    set_printer_ids(path, {"voron": 7})
    assert path.read_text(encoding="utf-8") == "[printer voron]\nprinter_id = 7\n"


def test_the_default_choice_is_the_printer_with_the_same_name():
    printer = load_config_printer("Printer 7")
    account = [{"id": 42, "name": "Printer 42"}, {"id": 7, "name": "printer 7"}]
    assert pair_cli._default_choice(printer, account, can_create=True) == "2"
    assert pair_cli._default_choice(load_config_printer("x"), account, can_create=True) == "n"
    assert pair_cli._default_choice(load_config_printer("x"), account, can_create=False) == "s"


def load_config_printer(name: str):
    from printlog_bridge.core.config import PrinterConfig

    return PrinterConfig(name, ADAPTERS["moonraker"].from_config(name, {"url": UNREACHABLE}))
