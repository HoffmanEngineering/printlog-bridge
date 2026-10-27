"""The Docker image starts as root and the bridge gives itself the data folder (cli/data_owner.py),
so no NAS needs a permission step: `Permission denied: '/data/state'` can't happen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from printlog_bridge.cli import data_owner
from printlog_bridge.cli.data_owner import (
    DATA_DIR_ENV,
    OwnerError,
    choose_owner,
    drop_root,
    hand_over,
)


class Owners:
    """A fake file system's owners: lstat and chown over a dict, for any path under a tmp dir."""

    def __init__(self, default: tuple[int, int] = (0, 0)) -> None:
        self.owners: dict[Path, tuple[int, int]] = {}
        self.default = default
        self.chowned: list[Path] = []

    def owner(self, path: Path) -> tuple[int, int]:
        return self.owners.get(path, self.default)

    def chown(self, path: Path, uid: int, gid: int) -> None:
        self.owners[path] = (uid, gid)
        self.chowned.append(path)


def test_puid_and_pgid_win():
    assert choose_owner({"PUID": "99", "PGID": "100"}, (1026, 100)) == (99, 100)


def test_pgid_defaults_to_puid():
    assert choose_owner({"PUID": "1001"}, (0, 0)) == (1001, 1001)


def test_without_puid_the_folders_own_owner_runs_it():
    # A Synology folder made in File Station belongs to the DSM user (1026:100): run as them,
    # and nothing needs changing.
    assert choose_owner({}, (1026, 100)) == (1026, 100)


def test_a_root_owned_folder_goes_to_the_images_own_user():
    # Docker makes a missing bind-mount folder as root; Unraid's appdata can be root's too.
    assert choose_owner({}, (0, 0)) == (1000, 1000)


@pytest.mark.parametrize("value", ["nobody", "-1", "1.5", ""])
def test_a_bad_puid_is_refused(value):
    if value == "":
        assert choose_owner({"PUID": ""}, (0, 0)) == (1000, 1000)  # empty means unset
        return
    with pytest.raises(OwnerError, match="PUID"):
        choose_owner({"PUID": value}, (0, 0))


def test_a_bad_pgid_is_refused():
    with pytest.raises(OwnerError, match="PGID"):
        choose_owner({"PUID": "99", "PGID": "users"}, (0, 0))


def test_hands_over_only_what_someone_else_owns(tmp_path):
    (tmp_path / "state" / "printers").mkdir(parents=True)
    (tmp_path / "printlog-bridge.cfg").write_text("", encoding="utf-8")
    (tmp_path / "state" / "device_token").write_text("", encoding="utf-8")
    (tmp_path / "state" / "printers" / "u1.json").write_text("", encoding="utf-8")
    fs = Owners(default=(1000, 1000))
    fs.owners[tmp_path] = (0, 0)
    fs.owners[tmp_path / "state" / "device_token"] = (0, 0)

    changed = hand_over(tmp_path, 1000, 1000, owner=fs.owner, chown=fs.chown)

    assert changed == 2
    assert sorted(fs.chowned) == sorted([tmp_path, tmp_path / "state" / "device_token"])


def test_hands_over_everything_for_a_new_puid(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "api_key").write_text("", encoding="utf-8")
    fs = Owners(default=(1000, 1000))

    assert hand_over(tmp_path, 99, 100, owner=fs.owner, chown=fs.chown) == 3
    assert all(fs.owner(p) == (99, 100) for p in fs.chowned)


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name != "posix", reason="symlinks")
def test_never_follows_a_symlink_out_of_the_folder(tmp_path):
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    (data / "link").symlink_to(outside, target_is_directory=True)
    fs = Owners(default=(0, 0))

    hand_over(data, 1000, 1000, owner=fs.owner, chown=fs.chown)

    assert outside not in fs.chowned and outside / "deep" not in fs.chowned


def test_does_nothing_outside_the_image(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(data_owner, "_is_root", lambda: True)
    monkeypatch.setattr(data_owner, "_switch_to", lambda uid, gid: calls.append((uid, gid)))

    drop_root({})  # no PRINTLOG_BRIDGE_DATA_DIR: not the image (a Pi, a dev machine)

    assert calls == []


def test_does_nothing_when_not_root(monkeypatch, tmp_path):
    # An existing install that runs the container with `user: 1026:100`.
    calls = []
    monkeypatch.setattr(data_owner, "_is_root", lambda: False)
    monkeypatch.setattr(data_owner, "_switch_to", lambda uid, gid: calls.append((uid, gid)))

    drop_root({DATA_DIR_ENV: str(tmp_path)})

    assert calls == []


def test_as_root_in_the_image_gives_the_folder_away_then_switches(monkeypatch, tmp_path, capsys):
    fs = Owners(default=(0, 0))
    calls = []
    monkeypatch.setattr(data_owner, "_is_root", lambda: True)
    monkeypatch.setattr(data_owner, "_owner", fs.owner)
    monkeypatch.setattr(data_owner, "_chown", fs.chown)
    monkeypatch.setattr(data_owner, "_switch_to", lambda uid, gid: calls.append((uid, gid)))

    drop_root({DATA_DIR_ENV: str(tmp_path), "PUID": "99", "PGID": "100"})

    assert calls == [(99, 100)]
    assert fs.owner(tmp_path) == (99, 100)
    assert "99:100" in capsys.readouterr().err


def test_puid_0_stays_root(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(data_owner, "_is_root", lambda: True)
    monkeypatch.setattr(data_owner, "_switch_to", lambda uid, gid: calls.append((uid, gid)))

    drop_root({DATA_DIR_ENV: str(tmp_path), "PUID": "0"})

    assert calls == []


def test_a_folder_it_cant_change_is_reported_and_it_still_switches(monkeypatch, tmp_path, capsys):
    calls = []

    def refuse(path, uid, gid):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(data_owner, "_is_root", lambda: True)
    monkeypatch.setattr(data_owner, "_owner", lambda path: (0, 0))
    monkeypatch.setattr(data_owner, "_chown", refuse)
    monkeypatch.setattr(data_owner, "_switch_to", lambda uid, gid: calls.append((uid, gid)))

    drop_root({DATA_DIR_ENV: str(tmp_path)})

    assert calls == [(1000, 1000)]
    err = capsys.readouterr().err
    assert "couldn't give" in err
    assert "PUID" in err


def test_the_command_line_drops_root_before_anything_else(monkeypatch):
    from printlog_bridge.cli import main as cli

    order = []
    monkeypatch.setattr(cli, "drop_root", lambda env=None: order.append("drop"))
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert order == ["drop"]
