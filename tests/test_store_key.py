"""`printlog-bridge store-key`: keeps the API key in the state directory, where Moonraker can't
serve it, and takes it out of printlog-bridge.cfg."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.cli.main import main
from printlog_bridge.core.config import ApiKeySource, load_config

PRINTERS = "[printer voron]\nprinter_id = 42\nurl = http://voron:7125\nmoonraker_api_key = mk\n"


def write_config(tmp_path: Path, bridge: str) -> Path:
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(
        f"# my printer\n[bridge]\nstate_dir = {tmp_path / 'state'}\n{bridge}\n{PRINTERS}",
        encoding="utf-8",
    )
    return path


def load(path: Path):
    return load_config(path, adapters=ADAPTERS, env={}, home=path.parent)


@pytest.fixture(autouse=True)
def no_key_in_the_environment(monkeypatch):
    monkeypatch.delenv("PRINTLOG_API_KEY", raising=False)


def test_moves_a_key_out_of_the_config(tmp_path, capsys):
    path = write_config(tmp_path, "api_key = k-inline  ; from the website\n")

    assert main(["store-key", "--config", str(path)]) == 0

    config = load(path)
    assert config.api_key == "k-inline"
    assert config.api_key_source is ApiKeySource.KEY_FILE
    text = path.read_text(encoding="utf-8")
    assert "k-inline" not in text
    assert "api_key =\n" in text
    assert text.startswith("# my printer\n[bridge]\n")
    assert PRINTERS in text, "moonraker_api_key and the rest are left alone"
    assert str(tmp_path / "state" / "api_key") in capsys.readouterr().out


def test_reads_a_new_key_from_standard_input(tmp_path, monkeypatch):
    path = write_config(tmp_path, "")
    monkeypatch.setattr(sys, "stdin", io.StringIO("k-new\n"))

    assert main(["store-key", "--config", str(path)]) == 0

    assert load(path).api_key == "k-new"


def test_a_new_key_replaces_the_stored_one(tmp_path, monkeypatch):
    path = write_config(tmp_path, "")
    monkeypatch.setattr(sys, "stdin", io.StringIO("k-old\n"))
    main(["store-key", "--config", str(path)])
    monkeypatch.setattr(sys, "stdin", io.StringIO("k-new\n"))

    assert main(["store-key", "--config", str(path)]) == 0

    assert load(path).api_key == "k-new"


def test_no_key_given_stores_nothing(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path, "")
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))

    assert main(["store-key", "--config", str(path)]) == 1

    assert not (tmp_path / "state" / "api_key").exists()
    assert "No API key" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_only_this_user_can_read_the_key_file(tmp_path):
    path = write_config(tmp_path, "api_key = k-inline\n")

    main(["store-key", "--config", str(path)])

    assert (tmp_path / "state" / "api_key").stat().st_mode & 0o777 == 0o600


def test_never_replaces_a_stored_key_with_a_different_one_left_in_the_config(tmp_path, capsys):
    path = write_config(tmp_path, "api_key = k-old\n")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "api_key").write_text("k-new\n", encoding="utf-8")

    assert main(["store-key", "--config", str(path)]) == 1

    assert load(path).api_key == "k-new"
    assert "Delete api_key" in capsys.readouterr().err


def test_a_config_that_does_not_load_is_an_error(tmp_path, capsys):
    assert main(["store-key", "--config", str(tmp_path / "missing.cfg")]) == 2
    assert "not found" in capsys.readouterr().err


@pytest.mark.parametrize("line", ["API_KEY = k-inline", "Api_Key: k-inline"])
def test_empties_the_key_whatever_its_case(tmp_path, line):
    path = write_config(tmp_path, line + "\n")  # configparser ignores an option's case
    assert load(path).config_api_key == "k-inline"

    assert main(["store-key", "--config", str(path)]) == 0

    assert "k-inline" not in path.read_text(encoding="utf-8")
    assert load(path).api_key_source is ApiKeySource.KEY_FILE


def test_says_so_when_the_key_line_can_not_be_found(tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path, "api_key = k-inline\n")
    monkeypatch.setattr("printlog_bridge.cli.main.clear_config_api_key", lambda path: False)

    assert main(["store-key", "--config", str(path)]) == 1

    assert "Delete it there yourself" in capsys.readouterr().err
