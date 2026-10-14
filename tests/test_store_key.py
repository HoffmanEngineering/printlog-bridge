"""`printlog-bridge store-key`: keeps the API key in the state directory, where Moonraker can't
serve it, and takes it out of printlog-bridge.cfg."""

from __future__ import annotations

import io
import os
import stat
import sys
from pathlib import Path

import pytest

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.cli.main import main
from printlog_bridge.core.config import ApiKeySource, clear_config_api_key, load_config

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


NAMES = {0: "root", 1000: "pi", 1001: "bob"}


class Owners:
    """Who store-key runs as, and who owns the config and its folder, as it sees them: the
    tests can't chown files, and CI isn't root (a container may be)."""

    def __init__(self, monkeypatch):
        self.euid = 1000
        self.file = 1000
        self.folder = 1000
        self.folder_mode = 0o755
        self.on_file_check = None
        monkeypatch.setattr("printlog_bridge.cli.main.os.geteuid", lambda: self.euid, raising=False)
        monkeypatch.setattr("printlog_bridge.cli.main._owner", self.owner)
        monkeypatch.setattr("printlog_bridge.cli.main._user_name", NAMES.get)
        monkeypatch.delenv("SUDO_UID", raising=False)

    def owner(self, fd):
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            return self.folder, stat.S_IFDIR | self.folder_mode
        if self.on_file_check is not None:
            self.on_file_check()
        return self.file, mode


@pytest.fixture(autouse=True)
def owners(monkeypatch):
    return Owners(monkeypatch)


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
    monkeypatch.setattr("printlog_bridge.cli.main.clear_config_api_key", lambda *a, **k: False)

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


POSIX = pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX owners")


@POSIX
class TestNotAsRoot:
    """`sudo printlog-bridge store-key` would write the key under root's home and leave the
    user's config owned by root, so the service, which runs as that user, couldn't read either."""

    @pytest.fixture(autouse=True)
    def as_root(self, owners, monkeypatch):
        owners.euid = 0
        monkeypatch.setenv("SUDO_UID", "1000")

    def refused(self, path, capsys, before=None):
        assert main(["store-key", "--config", str(path)]) == 1
        if before is not None:
            assert path.read_bytes() == before, "the config is left as it was"
        assert not (path.parent / "state").exists(), "no key file, nor a state directory"
        return capsys.readouterr().err

    def test_refuses_a_config_another_user_owns(self, tmp_path, capsys):
        path = write_config(tmp_path, "api_key = k-inline\n")

        err = self.refused(path, capsys, path.read_bytes())

        assert "root" in err
        assert "Run it as pi, without sudo" in err
        assert f"store-key --config {path}" in err

    def test_refuses_a_config_an_earlier_sudo_left_to_root(self, tmp_path, owners, capsys):
        """The config is root's, but its folder and the bridge are pi's: storing the key under
        root's home would leave pi's bridge without one."""
        path = write_config(tmp_path, "api_key = k-inline\n")
        owners.file = 0

        err = self.refused(path, capsys, path.read_bytes())

        assert f"sudo chown pi: {path}" in err
        assert "without sudo" in err

    def test_refuses_without_asking_for_a_key(self, tmp_path, owners, monkeypatch):
        path = write_config(tmp_path, "")
        owners.file = 0
        monkeypatch.setattr(sys, "stdin", io.StringIO("k-new\n"))

        assert main(["store-key", "--config", str(path)]) == 1

        assert not (tmp_path / "state").exists()
        assert sys.stdin.read() == "k-new\n", "the key wasn't read"

    def test_names_the_owner_by_uid_when_it_has_no_name(self, tmp_path, owners, capsys):
        path = write_config(tmp_path, "")
        owners.file = owners.folder = 4242

        assert "uid 4242" in self.refused(path, capsys)

    def test_refuses_when_there_is_no_config_to_find(self, tmp_path, capsys):
        """`sudo printlog-bridge store-key` looks in root's home and finds nothing: saying so
        is less useful than saying not to use sudo."""
        err = self.refused(tmp_path / "missing.cfg", capsys)

        assert "root" in err
        assert "the user the bridge runs as" in err

    def test_refuses_roots_own_config_through_sudo(self, tmp_path, owners, capsys):
        path = write_config(tmp_path, "api_key = k-inline\n")
        owners.file = owners.folder = 0

        err = self.refused(path, capsys, path.read_bytes())

        assert "sudo" in err
        assert "pi" in err

    def test_refuses_a_root_config_in_a_folder_others_can_write(
        self, tmp_path, owners, monkeypatch, capsys
    ):
        path = write_config(tmp_path, "api_key = k-inline\n")
        owners.file = owners.folder = 0
        owners.folder_mode = 0o777
        monkeypatch.delenv("SUDO_UID")

        assert "other users can write" in self.refused(path, capsys, path.read_bytes())

    @pytest.mark.parametrize("sudo_uid", [None, "0"])
    def test_a_config_root_owns_is_roots_to_change(self, tmp_path, owners, monkeypatch, sudo_uid):
        """A bridge set up as root (its config, folder and state are root's) still works,
        from a root shell rather than through sudo."""
        path = write_config(tmp_path, "api_key = k-inline\n")
        owners.file = owners.folder = 0
        if sudo_uid is None:
            monkeypatch.delenv("SUDO_UID")
        else:
            monkeypatch.setenv("SUDO_UID", sudo_uid)

        assert main(["store-key", "--config", str(path)]) == 0

        assert load(path).api_key_source is ApiKeySource.KEY_FILE

    def test_the_config_checked_is_the_config_read(self, tmp_path, owners, monkeypatch):
        """Swapping the file between the check and the read can't point root elsewhere."""
        path = write_config(tmp_path, "api_key = k-inline\n")
        owners.file = owners.folder = 0
        monkeypatch.delenv("SUDO_UID")
        evil = tmp_path / "evil"

        def swap():
            swapped = tmp_path / "swapped.cfg"
            swapped.write_text(
                f"[bridge]\nstate_dir = {evil}\napi_key = k-evil\n{PRINTERS}", encoding="utf-8"
            )
            os.replace(str(swapped), str(path))

        owners.on_file_check = swap

        assert main(["store-key", "--config", str(path)]) == 1

        assert not evil.exists()
        assert (tmp_path / "state" / "api_key").read_text(encoding="utf-8") == "k-inline\n"
        assert "k-evil" in path.read_text(encoding="utf-8"), "a file it didn't check is untouched"

    def test_the_config_rewritten_is_the_config_checked(self, tmp_path, owners, monkeypatch):
        """Moving the checked folder away and putting another in its place after the check
        can't make root rewrite that other config, and leave the key in the real one."""
        folder = tmp_path / "etc"
        folder.mkdir()
        path = folder / "printlog-bridge.cfg"
        config = f"[bridge]\nstate_dir = {tmp_path / 'state'}\napi_key = {{}}\n{PRINTERS}"
        path.write_text(config.format("k-inline"), encoding="utf-8")
        owners.file = owners.folder = 0
        monkeypatch.delenv("SUDO_UID")

        def swap():
            folder.rename(tmp_path / "moved")
            folder.mkdir()
            path.write_text(config.format("k-other"), encoding="utf-8")

        owners.on_file_check = swap

        assert main(["store-key", "--config", str(path)]) == 0

        assert "k-inline" not in (tmp_path / "moved" / "printlog-bridge.cfg").read_text("utf-8")
        assert "k-other" in path.read_text(encoding="utf-8")
        assert (tmp_path / "state" / "api_key").read_text(encoding="utf-8") == "k-inline\n"

    def test_still_refuses_through_sudo_when_the_config_holds_no_key(
        self, tmp_path, owners, monkeypatch, capsys
    ):
        """Root writes the key file where root's config says, so the root rules stay."""
        path = write_config(tmp_path, "")
        owners.file = owners.folder = 0
        monkeypatch.setattr(sys, "stdin", io.StringIO("k-new\n"))

        assert "sudo" in self.refused(path, capsys)


@POSIX
class TestAsAnotherUser:
    """`sudo -u bob`, or bob himself, on pi's config: the key file would be bob's, which pi's
    bridge can't read, and the config pi's, which bob can't rewrite."""

    def test_refuses_another_users_config(self, tmp_path, owners, capsys):
        path = write_config(tmp_path, "api_key = k-inline\n")
        before = path.read_bytes()
        owners.euid = 1001

        assert main(["store-key", "--config", str(path)]) == 1

        assert path.read_bytes() == before
        assert not (tmp_path / "state").exists(), "nothing is written"
        err = capsys.readouterr().err
        assert "belongs to pi" in err
        assert "Run it as pi" in err

    def test_refuses_a_config_an_earlier_sudo_left_to_root(self, tmp_path, owners, capsys):
        path = write_config(tmp_path, "api_key = k-inline\n")
        owners.file = 0

        assert main(["store-key", "--config", str(path)]) == 1

        assert not (tmp_path / "state").exists()
        assert f"sudo chown pi: {path}" in capsys.readouterr().err

    def test_a_root_config_without_a_key_is_fine(self, tmp_path, owners, monkeypatch):
        """A service user (`printlog`) with root's config in /etc: store-key won't rewrite a
        config that holds no key, so whose it is doesn't matter."""
        path = write_config(tmp_path, "")
        owners.euid = 1001
        owners.file = owners.folder = 0
        monkeypatch.setattr(sys, "stdin", io.StringIO("k-new\n"))

        assert main(["store-key", "--config", str(path)]) == 0

        assert (tmp_path / "state" / "api_key").read_text(encoding="utf-8") == "k-new\n"

    def test_a_root_config_with_a_key_says_how_to_remove_it(self, tmp_path, owners, capsys):
        path = write_config(tmp_path, "api_key = k-inline\n")
        owners.euid = 1001
        owners.file = owners.folder = 0

        assert main(["store-key", "--config", str(path)]) == 1

        assert not (tmp_path / "state").exists()
        err = capsys.readouterr().err
        assert "can't remove api_key" in err
        assert "remove the api_key line as root" in err


def test_says_the_key_is_stored_when_the_config_cant_be_rewritten(tmp_path, monkeypatch, capsys):
    """The key file is written first, so a config it can't rewrite (a symlink into a folder
    this user can't write) leaves the key stored: saying it wasn't would be wrong."""
    path = write_config(tmp_path, "api_key = k-inline\n")

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("printlog_bridge.cli.main.clear_config_api_key", refuse)

    assert main(["store-key", "--config", str(path)]) == 1

    assert (tmp_path / "state" / "api_key").read_text(encoding="utf-8") == "k-inline\n"
    err = capsys.readouterr().err
    assert f"stored the key in {tmp_path / 'state' / 'api_key'}" in err
    assert "Permission denied" in err
    assert "Remove that line by hand" in err
    assert "couldn't store the key" not in err


@POSIX
class TestSymlinkedConfig:
    """A config kept with someone's dotfiles is often a link: it stays one."""

    def linked(self, tmp_path):
        (tmp_path / "dotfiles").mkdir()
        target = write_config(tmp_path / "dotfiles", "api_key = k-inline\n")
        link = tmp_path / "printlog-bridge.cfg.link"
        link.symlink_to(target)
        return link, target

    def test_store_key_keeps_the_link(self, tmp_path):
        link, target = self.linked(tmp_path)

        assert main(["store-key", "--config", str(link)]) == 0

        assert link.is_symlink()
        assert "k-inline" not in target.read_text(encoding="utf-8")
        assert load(link).api_key == "k-inline"

    def test_clearing_the_key_keeps_the_link(self, tmp_path):
        link, target = self.linked(tmp_path)

        assert clear_config_api_key(link)

        assert link.is_symlink()
        assert "k-inline" not in target.read_text(encoding="utf-8")
