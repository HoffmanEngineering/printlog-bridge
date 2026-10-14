"""`printlog-bridge store-key`: keeps the API key in the state directory, where Moonraker can't
serve it, and takes it out of printlog-bridge.cfg."""

from __future__ import annotations

import io
import os
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


@pytest.fixture(autouse=True)
def not_root(monkeypatch):
    """As a normal user, whoever runs the tests (CI isn't root; a container may be)."""
    monkeypatch.setattr("printlog_bridge.cli.main.os.geteuid", lambda: 1000, raising=False)


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


def test_a_byte_order_mark_piped_in_is_not_stored(tmp_path, monkeypatch):
    # PowerShell can start what it pipes with a UTF-8 BOM, which the API would refuse.
    path = write_config(tmp_path, "")
    monkeypatch.setattr(sys, "stdin", io.StringIO("\ufeff k-new \r\n"))

    assert main(["store-key", "--config", str(path)]) == 0

    assert (tmp_path / "state" / "api_key").read_bytes() == b"k-new\n"
    assert load(path).api_key == "k-new"


def test_a_key_file_saved_with_a_byte_order_mark_still_works(tmp_path):
    path = write_config(tmp_path, "")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "api_key").write_bytes(b"\xef\xbb\xbfk-saved\r\n")

    assert load(path).api_key == "k-saved"


def test_run_says_when_it_cant_write_the_state_directory(tmp_path, capsys):
    (tmp_path / "taken").write_text("", encoding="utf-8")
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(
        f"[bridge]\nstate_dir = {tmp_path / 'taken' / 'state'}\napi_key = k\n{PRINTERS}",
        encoding="utf-8",
    )

    assert main(["run", "--config", str(path)]) == 2

    err = capsys.readouterr().err
    assert "can't write the state directory" in err
    assert "/data" in err


class TestNotAsRoot:
    """`sudo printlog-bridge store-key` would write the key under root's home and leave the
    user's config owned by root, so the service, which runs as that user, couldn't read either."""

    @pytest.fixture(autouse=True)
    def as_root(self, monkeypatch):
        monkeypatch.setattr("printlog_bridge.cli.main.os.geteuid", lambda: 0, raising=False)

    def owned_by(self, monkeypatch, uid):
        monkeypatch.setattr("printlog_bridge.cli.main._owner_uid", lambda path: uid)

    def test_refuses_a_config_another_user_owns(self, tmp_path, monkeypatch, capsys):
        path = write_config(tmp_path, "api_key = k-inline\n")
        before = path.read_bytes()
        self.owned_by(monkeypatch, 4242)
        monkeypatch.setattr("printlog_bridge.cli.main._user_name", lambda uid: "pi")

        assert main(["store-key", "--config", str(path)]) == 1

        assert path.read_bytes() == before, "the config is left as it was"
        assert not (tmp_path / "state").exists(), "no key file, nor a state directory"
        err = capsys.readouterr().err
        assert "root" in err
        assert "Run it as pi, without sudo" in err
        assert f"store-key --config {path}" in err

    def test_refuses_without_asking_for_a_key(self, tmp_path, monkeypatch, capsys):
        path = write_config(tmp_path, "")
        self.owned_by(monkeypatch, 4242)
        monkeypatch.setattr(sys, "stdin", io.StringIO("k-new\n"))

        assert main(["store-key", "--config", str(path)]) == 1

        assert not (tmp_path / "state" / "api_key").exists()
        assert sys.stdin.read() == "k-new\n", "the key wasn't read"

    def test_names_the_owner_by_uid_when_it_has_no_name(self, tmp_path, monkeypatch, capsys):
        path = write_config(tmp_path, "")
        self.owned_by(monkeypatch, 4242)
        monkeypatch.setattr("printlog_bridge.cli.main._user_name", lambda uid: None)

        assert main(["store-key", "--config", str(path)]) == 1

        assert "uid 4242" in capsys.readouterr().err

    def test_refuses_when_there_is_no_config_to_find(self, tmp_path, monkeypatch, capsys):
        """`sudo printlog-bridge store-key` looks in root's home and finds nothing: saying so
        is less useful than saying not to use sudo."""
        self.owned_by(monkeypatch, None)

        assert main(["store-key", "--config", str(tmp_path / "missing.cfg")]) == 1

        err = capsys.readouterr().err
        assert "root" in err
        assert "the user the bridge runs as" in err

    def test_a_config_root_owns_is_roots_to_change(self, tmp_path, monkeypatch):
        """A bridge set up as root (its config and state are root's) still works."""
        path = write_config(tmp_path, "api_key = k-inline\n")
        self.owned_by(monkeypatch, 0)

        assert main(["store-key", "--config", str(path)]) == 0

        assert load(path).api_key_source is ApiKeySource.KEY_FILE

    @pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX owners")
    def test_the_owner_is_the_config_files(self, tmp_path):
        from printlog_bridge.cli.main import _owner_uid

        path = write_config(tmp_path, "")
        assert _owner_uid(path) == path.stat().st_uid
        assert _owner_uid(tmp_path / "missing.cfg") is None
