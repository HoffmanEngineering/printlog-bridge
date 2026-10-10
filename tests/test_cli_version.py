import subprocess
import sys

import pytest

from printlog_bridge import __version__
from printlog_bridge.cli.main import main


def test_version_flag_prints_the_package_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"printlog-bridge {__version__}"


def test_module_entry_point_runs_the_cli():
    result = subprocess.run(
        [sys.executable, "-m", "printlog_bridge", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"printlog-bridge {__version__}"


def test_no_command_prints_usage_and_fails(capsys):
    assert main([]) == 2
    assert "usage: printlog-bridge" in capsys.readouterr().err
