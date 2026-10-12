"""The file edits behind install.sh and uninstall.sh (scripts/install_helper.py).

The scripts themselves run in CI's install-smoke job, against a fake systemctl. These tests hold
the promise that matters most to users: uninstalling leaves moonraker.conf and moonraker.asvc
exactly as they were, byte for byte.
"""

from __future__ import annotations

import configparser
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.core.config import load_config

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "install_helper", ROOT / "scripts" / "install_helper.py"
)
helper = importlib.util.module_from_spec(_spec)
sys.modules["install_helper"] = helper
_spec.loader.exec_module(helper)

REPO = "/home/pi/printlog-bridge"
VENV = "/home/pi/printlog-bridge-env"
ORIGIN = "https://github.com/HoffmanEngineering/printlog-bridge.git"

MOONRAKER_CONFS = [
    "",
    "[server]\nhost: 0.0.0.0\n",
    "[server]\nhost: 0.0.0.0",  # no newline at the end
    "[server]\nhost: 0.0.0.0\n\n",  # a blank line at the end
    "[server]\r\nhost: 0.0.0.0\r\n",  # Windows line ends, from an editor
    "[update_manager mainsail]\ntype: web\nrepo: mainsail-crew/mainsail\npath: ~/mainsail\n",
]
ASVCS = [
    "",
    "klipper_mcu\nwebcamd\nMoonCord\nKlipperScreen\nmoonraker-obico\ncrowsnest\n",
    "klipper_mcu\nwebcamd",  # no newline at the end
    "klipper_mcu\n\n",
]


def section() -> str:
    return helper.update_manager_section(repo=REPO, venv=VENV, origin=ORIGIN)


@pytest.mark.parametrize("original", MOONRAKER_CONFS)
def test_removing_the_section_restores_moonraker_conf_exactly(original):
    added, change = helper.add_section(original, section())

    assert added != original
    assert helper.remove_section(added, change) == original


@pytest.mark.parametrize("original", ASVCS)
def test_removing_the_service_restores_moonraker_asvc_exactly(original):
    added, change = helper.add_service(original, "printlog-bridge")

    assert "printlog-bridge" in added.splitlines()
    assert helper.remove_service(added, "printlog-bridge", change) == original


def test_the_section_is_what_moonrakers_update_manager_reads():
    added, _ = helper.add_section("[server]\nhost: 0.0.0.0\n", section())

    parser = configparser.ConfigParser()
    parser.read_string(added)
    options = parser["update_manager printlog-bridge"]
    assert options["type"] == "git_repo"
    assert options["path"] == REPO
    assert options["origin"] == ORIGIN
    assert options["primary_branch"] == "main"
    assert options["virtualenv"] == VENV
    assert options["requirements"] == "requirements.txt"
    assert options["managed_services"] == "printlog-bridge"


def test_installing_twice_adds_the_section_once():
    once, change = helper.add_section("[server]\n", section())

    twice, again = helper.add_section(once, section())

    assert twice == once
    assert not again["added"]


def test_a_section_the_user_wrote_is_left_alone():
    theirs = "[update_manager printlog-bridge]\ntype: git_repo\npath: ~/elsewhere\n"

    text, change = helper.add_section(theirs, section())

    assert text == theirs
    assert not change["added"]
    assert helper.remove_section(text, change) == theirs


def test_a_service_already_allowed_is_left_alone():
    theirs = "webcamd\nprintlog-bridge\n"

    text, change = helper.add_service(theirs, "printlog-bridge")

    assert text == theirs
    assert helper.remove_service(text, "printlog-bridge", change) == theirs


def test_what_the_user_added_after_installing_is_kept():
    added, change = helper.add_section("[server]\nhost: 0.0.0.0", section())
    edited = added + "\n[timelapse]\n"

    removed = helper.remove_section(edited, change)

    assert removed == "[server]\nhost: 0.0.0.0\n\n[timelapse]\n"
    assert "printlog-bridge" not in removed


def test_without_a_record_uninstall_still_removes_the_section_and_service():
    conf, _ = helper.add_section("[server]\n", section())
    asvc, _ = helper.add_service("webcamd\n", "printlog-bridge")

    assert helper.remove_section(conf, None) == "[server]\n"
    assert helper.remove_service(asvc, "printlog-bridge", None) == "webcamd\n"


class TestCommands:
    def files(self, tmp_path, conf: str = "[server]\nhost: 0.0.0.0", asvc: str = "webcamd"):
        (tmp_path / "moonraker.conf").write_bytes(conf.encode())
        (tmp_path / "moonraker.asvc").write_bytes(asvc.encode())
        return [
            "--conf", str(tmp_path / "moonraker.conf"),
            "--asvc", str(tmp_path / "moonraker.asvc"),
            "--record", str(tmp_path / "record.json"),
        ]  # fmt: skip

    def add(self, tmp_path, files):
        return helper.main(
            ["moonraker-add", *files, "--repo", REPO, "--venv", VENV, "--origin", ORIGIN]
        )

    def test_add_then_remove_leaves_both_files_as_they_were(self, tmp_path, capsys):
        files = self.files(tmp_path)

        assert self.add(tmp_path, files) == 0
        assert capsys.readouterr().out.strip() == "changed"
        assert b"[update_manager printlog-bridge]" in (tmp_path / "moonraker.conf").read_bytes()
        assert (tmp_path / "moonraker.asvc").read_bytes() == b"webcamd\nprintlog-bridge\n"

        assert helper.main(["moonraker-remove", *files]) == 0
        assert (tmp_path / "moonraker.conf").read_bytes() == b"[server]\nhost: 0.0.0.0"
        assert (tmp_path / "moonraker.asvc").read_bytes() == b"webcamd"
        assert not (tmp_path / "record.json").exists()

    def test_a_second_install_changes_nothing_and_keeps_what_the_first_recorded(
        self, tmp_path, capsys
    ):
        files = self.files(tmp_path)
        self.add(tmp_path, files)
        record = json.loads((tmp_path / "record.json").read_text())
        capsys.readouterr()

        self.add(tmp_path, files)

        assert capsys.readouterr().out.strip() == "unchanged"
        assert json.loads((tmp_path / "record.json").read_text()) == record
        helper.main(["moonraker-remove", *files])
        assert (tmp_path / "moonraker.conf").read_bytes() == b"[server]\nhost: 0.0.0.0"

    def test_a_missing_asvc_is_not_created(self, tmp_path, capsys):
        # Without the file Moonraker allows its default services; creating one with only ours
        # would take those away.
        files = self.files(tmp_path)
        (tmp_path / "moonraker.asvc").unlink()

        assert self.add(tmp_path, files) == 0

        assert not (tmp_path / "moonraker.asvc").exists()
        assert "moonraker.asvc" in capsys.readouterr().err
        assert helper.main(["moonraker-remove", *files]) == 0

    def test_a_missing_moonraker_conf_fails(self, tmp_path):
        files = self.files(tmp_path)
        (tmp_path / "moonraker.conf").unlink()

        assert self.add(tmp_path, files) == 1

    def test_a_symlinked_moonraker_conf_stays_a_symlink(self, tmp_path, capsys):
        backup = tmp_path / "backup"
        backup.mkdir()
        (backup / "moonraker.conf").write_bytes(b"[server]\nhost: 0.0.0.0\n")
        files = self.files(tmp_path)
        (tmp_path / "moonraker.conf").unlink()
        try:
            (tmp_path / "moonraker.conf").symlink_to(backup / "moonraker.conf")
        except OSError:
            pytest.skip("this system can't make symlinks")

        assert self.add(tmp_path, files) == 0
        assert (tmp_path / "moonraker.conf").is_symlink()
        assert b"printlog-bridge" in (backup / "moonraker.conf").read_bytes()

        assert helper.main(["moonraker-remove", *files]) == 0
        assert (tmp_path / "moonraker.conf").is_symlink()
        assert (backup / "moonraker.conf").read_bytes() == b"[server]\nhost: 0.0.0.0\n"


class TestConfig:
    def write(self, tmp_path, *extra, env=None):
        return helper.main(
            [
                "write-config",
                "--path",
                str(tmp_path / "config" / "printlog-bridge.cfg"),
                "--printer-data",
                "/home/pi/printer_data",
                "--name",
                "voron",
                "--key-file",
                str(self.key_file(tmp_path)),
                *extra,
            ],  # fmt: skip
            env=env or {},
        )

    @staticmethod
    def key_file(tmp_path):
        return tmp_path / ".local" / "share" / "printlog-bridge" / "api_key"

    def load(self, tmp_path):
        return load_config(
            tmp_path / "config" / "printlog-bridge.cfg", ADAPTERS, env={}, home=tmp_path
        )

    def test_writes_a_local_mode_config_the_bridge_loads(self, tmp_path):
        (tmp_path / "config").mkdir()

        assert self.write(tmp_path, "--printer-id", "42", env={"PRINTLOG_API_KEY": "k-1"}) == 0

        config = self.load(tmp_path)
        assert config.api_key == "k-1"
        assert config.api_key_file == self.key_file(tmp_path)
        [printer] = config.printers
        assert printer.name == "voron"
        assert printer.printer_id == 42
        assert printer.adapter.socket == Path("/home/pi/printer_data/comms/moonraker.sock")

    def test_without_a_key_or_printer_leaves_them_for_the_user_to_fill_in(self, tmp_path):
        (tmp_path / "config").mkdir()

        self.write(tmp_path)

        config = self.load(tmp_path)
        assert config.api_key is None
        assert config.printers[0].printer_id is None

    def test_an_existing_config_is_never_overwritten(self, tmp_path, capsys):
        (tmp_path / "config").mkdir()
        path = tmp_path / "config" / "printlog-bridge.cfg"
        path.write_text("[bridge]\napi_key = mine\n", encoding="utf-8")

        self.write(tmp_path, "--printer-id", "42", env={"PRINTLOG_API_KEY": "other"})

        assert path.read_text(encoding="utf-8") == "[bridge]\napi_key = mine\n"
        assert capsys.readouterr().out.strip() == "exists"

    def test_the_key_is_kept_out_of_the_folder_moonraker_serves(self, tmp_path):
        (tmp_path / "config").mkdir()

        self.write(tmp_path, env={"PRINTLOG_API_KEY": "k-secret-1"})

        assert "k-secret-1" not in (tmp_path / "config" / "printlog-bridge.cfg").read_text(
            encoding="utf-8"
        )
        assert self.key_file(tmp_path).read_text(encoding="utf-8") == "k-secret-1\n"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_only_this_user_can_read_the_key(self, tmp_path):
        (tmp_path / "config").mkdir()

        self.write(tmp_path, env={"PRINTLOG_API_KEY": "k-secret-1"})

        assert self.key_file(tmp_path).stat().st_mode & 0o777 == 0o600
        assert self.key_file(tmp_path).parent.stat().st_mode & 0o077 == 0

    def test_no_key_writes_no_key_file(self, tmp_path):
        (tmp_path / "config").mkdir()

        self.write(tmp_path)

        assert not self.key_file(tmp_path).exists()

    def test_installing_again_leaves_the_stored_key_alone(self, tmp_path):
        (tmp_path / "config").mkdir()
        self.write(tmp_path, env={"PRINTLOG_API_KEY": "first"})

        self.write(tmp_path, env={"PRINTLOG_API_KEY": "second"})

        assert self.load(tmp_path).api_key == "first"

    def test_a_printer_name_from_any_host_name_is_a_valid_section(self):
        assert helper.printer_name("Voron 2.4 (garage)") == "voron-2-4-garage"
        assert helper.printer_name("") == "printer"


def test_requirements_txt_matches_the_packages_dependencies():
    """Moonraker's update manager installs requirements.txt into the virtualenv on updates."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    [block] = re.findall(r"^dependencies = \[(.*?)\]", pyproject, flags=re.M | re.S)
    declared = sorted(re.findall(r'"([^"]+)"', block))
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    required = sorted(line for line in lines if line.strip() and not line.startswith("#"))

    assert required == declared


SAVE_CONFIG_BLOCK = (
    "#*# <---------------------- SAVE_CONFIG ---------------------->\n"
    "#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.\n"
    "#*#\n#*# [extruder]\n#*# pid_kp = 21.5\n"
)
PRINTER_CFGS = [
    "",
    "[printer]\nkinematics: corexy\n",
    "[printer]\nkinematics: corexy",  # no newline at the end
    "[printer]\nkinematics: corexy\n\n",
    "[printer]\r\nkinematics: corexy\r\n",
    "[printer]\nkinematics: corexy\n\n" + SAVE_CONFIG_BLOCK,
    "[printer]\nkinematics: corexy\n" + SAVE_CONFIG_BLOCK,
]


@pytest.mark.parametrize("original", PRINTER_CFGS)
def test_removing_the_include_restores_printer_cfg_exactly(original):
    added, change = helper.add_include(original)

    assert "[include printlog.cfg]" in added
    assert helper.remove_include(added, change) == original


def test_the_include_goes_before_klippers_save_config_block():
    added, _ = helper.add_include("[printer]\nkinematics: corexy\n\n" + SAVE_CONFIG_BLOCK)

    assert added.index("[include printlog.cfg]") < added.index("SAVE_CONFIG")
    assert added.endswith(SAVE_CONFIG_BLOCK)  # Klipper needs it last


def test_what_save_config_writes_later_is_kept_on_uninstall():
    added, change = helper.add_include("[printer]\nkinematics: corexy\n")
    saved = added + SAVE_CONFIG_BLOCK  # SAVE_CONFIG appends its block below ours

    assert helper.remove_include(saved, change) == "[printer]\nkinematics: corexy\n" + (
        SAVE_CONFIG_BLOCK
    )


@pytest.mark.parametrize(
    "existing",
    [
        "[include printlog.cfg]\n",
        "[include  printlog.cfg ]\n",
        "[gcode_macro PRINTLOG_SET_SPOOL]\ngcode:\n  M117 mine\n",
    ],
)
def test_an_include_or_macros_the_user_has_are_left_alone(existing):
    added, change = helper.add_include(existing)

    assert added == existing
    assert change["added"] is False


class TestMacroCommands:
    SOURCE = ROOT / "klipper" / "printlog.cfg"

    def args(self, tmp_path, printer_cfg: str = "[printer]\nkinematics: corexy\n"):
        (tmp_path / "printer.cfg").write_bytes(printer_cfg.encode())
        return [
            "--printer-cfg", str(tmp_path / "printer.cfg"),
            "--record", str(tmp_path / "macros.json"),
        ]  # fmt: skip

    def add(self, args):
        return helper.main(["macros-add", *args, "--source", str(self.SOURCE)])

    def test_add_then_remove_leaves_the_config_folder_as_it_was(self, tmp_path, capsys):
        args = self.args(tmp_path)

        assert self.add(args) == 0
        assert capsys.readouterr().out.strip() == "changed"
        assert (tmp_path / "printlog.cfg").read_bytes() == self.SOURCE.read_bytes()
        assert b"[include printlog.cfg]" in (tmp_path / "printer.cfg").read_bytes()

        assert helper.main(["macros-remove", *args]) == 0
        assert (tmp_path / "printer.cfg").read_bytes() == b"[printer]\nkinematics: corexy\n"
        assert not (tmp_path / "printlog.cfg").exists()
        assert not (tmp_path / "macros.json").exists()

    def test_installing_again_changes_nothing(self, tmp_path, capsys):
        args = self.args(tmp_path)
        self.add(args)
        before = (tmp_path / "printer.cfg").read_bytes()
        capsys.readouterr()

        self.add(args)

        assert capsys.readouterr().out.strip() == "unchanged"
        assert (tmp_path / "printer.cfg").read_bytes() == before

    def test_an_update_replaces_the_macros_the_user_didnt_change(self, tmp_path, capsys):
        args = self.args(tmp_path)
        self.add(args)
        old = b"[gcode_macro PRINTLOG_SET_SPOOL]\ngcode:\n  M117 old\n"
        (tmp_path / "printlog.cfg").write_bytes(old)
        record = json.loads((tmp_path / "macros.json").read_text())
        record["macros"] = helper.sha256(old)  # as an older install would have recorded
        (tmp_path / "macros.json").write_text(json.dumps(record))

        self.add(args)

        assert (tmp_path / "printlog.cfg").read_bytes() == self.SOURCE.read_bytes()

    def test_macros_the_user_changed_are_kept_and_kept_on_uninstall(self, tmp_path, capsys):
        args = self.args(tmp_path)
        self.add(args)
        mine = (tmp_path / "printlog.cfg").read_bytes() + b"\n# my change\n"
        (tmp_path / "printlog.cfg").write_bytes(mine)
        capsys.readouterr()

        self.add(args)
        assert "kept" in capsys.readouterr().err
        assert (tmp_path / "printlog.cfg").read_bytes() == mine

        helper.main(["macros-remove", *args])
        assert (tmp_path / "printlog.cfg").read_bytes() == mine
        assert b"[include printlog.cfg]" not in (tmp_path / "printer.cfg").read_bytes()

    def test_a_printlog_cfg_of_the_users_own_is_not_included(self, tmp_path, capsys):
        args = self.args(tmp_path)
        (tmp_path / "printlog.cfg").write_bytes(b"[gcode_macro SOMETHING_ELSE]\ngcode:\n")

        assert self.add(args) == 0

        assert "[include printlog.cfg]" not in (tmp_path / "printer.cfg").read_text()
        assert (tmp_path / "printlog.cfg").read_bytes() == b"[gcode_macro SOMETHING_ELSE]\ngcode:\n"
        helper.main(["macros-remove", *args])
        assert (tmp_path / "printlog.cfg").exists()

    def test_without_printer_cfg_nothing_is_done(self, tmp_path, capsys):
        args = self.args(tmp_path)
        (tmp_path / "printer.cfg").unlink()

        assert self.add(args) == 0

        assert capsys.readouterr().out.strip() == "skipped"
        assert not (tmp_path / "printlog.cfg").exists()

    def test_a_symlinked_printer_cfg_stays_a_symlink(self, tmp_path, capsys):
        backup = tmp_path / "backup"
        backup.mkdir()
        (backup / "printer.cfg").write_bytes(b"[printer]\nkinematics: corexy\n")
        args = self.args(tmp_path)
        (tmp_path / "printer.cfg").unlink()
        try:
            (tmp_path / "printer.cfg").symlink_to(backup / "printer.cfg")
        except OSError:
            pytest.skip("this system can't make symlinks")

        self.add(args)
        assert (tmp_path / "printer.cfg").is_symlink()
        assert b"[include printlog.cfg]" in (backup / "printer.cfg").read_bytes()

        helper.main(["macros-remove", *args])
        assert (tmp_path / "printer.cfg").is_symlink()
        assert (backup / "printer.cfg").read_bytes() == b"[printer]\nkinematics: corexy\n"

    def test_a_copy_the_user_included_themselves_survives_uninstall(self, tmp_path, capsys):
        # They followed printlog.cfg's own instructions, then ran install.sh and uninstall.sh.
        printer_cfg = "[include printlog.cfg]\n[printer]\nkinematics: corexy\n"
        args = self.args(tmp_path, printer_cfg)
        (tmp_path / "printlog.cfg").write_bytes(self.SOURCE.read_bytes())

        self.add(args)
        helper.main(["macros-remove", *args])

        assert (tmp_path / "printer.cfg").read_text() == printer_cfg
        assert (tmp_path / "printlog.cfg").exists()  # else Klipper won't start

    def test_an_include_left_in_printer_cfg_keeps_its_file(self, tmp_path, capsys):
        args = self.args(tmp_path)
        self.add(args)
        # The markers were lost (the file was saved with CRLF line ends), the include kept.
        text = (tmp_path / "printer.cfg").read_text().replace("\n", "\r\n")
        (tmp_path / "printer.cfg").write_bytes(text.encode())

        helper.main(["macros-remove", *args])

        assert b"[include printlog.cfg]" in (tmp_path / "printer.cfg").read_bytes()
        assert (tmp_path / "printlog.cfg").exists()
