from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.config import (
    DEFAULT_API_URL,
    ConfigError,
    find_config,
    load_config,
)


class FakeAdapter(Adapter):
    name = "fake"

    def __init__(self, printer: str, options: Mapping[str, str]) -> None:
        self.printer = printer
        self.options = dict(options)

    @classmethod
    def from_config(cls, printer: str, options: Mapping[str, str]) -> FakeAdapter:
        if options.get("broken"):
            raise ConfigError(f"[printer {printer}]: broken is set")
        return cls(printer, options)

    def describe(self) -> str:
        return "fake"

    async def run(self, sink) -> None:  # pragma: no cover - not exercised here
        raise NotImplementedError

    async def check(self):  # pragma: no cover - not exercised here
        return []


ADAPTERS = {"fake": FakeAdapter}


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(text, encoding="utf-8")
    return path


def load(path: Path, env: Mapping[str, str] | None = None):
    return load_config(path, adapters=ADAPTERS, env=env or {}, home=path.parent)


def test_reads_the_bridge_section_and_each_printer(tmp_path):
    path = write(
        tmp_path,
        """
[bridge]
api_url = https://api.example.test/
api_key = key-123
state_dir = /var/lib/printlog-bridge

[printer voron]
adapter = fake
color = red

[printer u1]
adapter = fake
""",
    )

    config = load(path)

    assert config.path == path
    assert config.api_url == "https://api.example.test"
    assert config.api_key == "key-123"
    assert config.state_dir == Path("/var/lib/printlog-bridge")
    assert [p.name for p in config.printers] == ["voron", "u1"]
    voron = config.printers[0].adapter
    assert isinstance(voron, FakeAdapter)
    assert voron.printer == "voron"
    assert voron.options == {"color": "red"}


def test_bridge_section_is_optional_and_has_defaults(tmp_path):
    config = load(write(tmp_path, "[printer voron]\nadapter = fake\n"))

    assert config.api_url == DEFAULT_API_URL
    assert config.api_key is None
    assert config.state_dir == tmp_path / ".local" / "share" / "printlog-bridge"


def test_adapter_defaults_to_moonraker(tmp_path):
    path = write(tmp_path, "[printer voron]\n")

    with pytest.raises(ConfigError, match="unknown adapter 'moonraker'"):
        load(path)


def test_api_key_from_the_environment_wins(tmp_path):
    path = write(tmp_path, "[bridge]\napi_key = from-file\n[printer voron]\nadapter = fake\n")

    config = load(path, env={"PRINTLOG_API_KEY": "from-env"})

    assert config.api_key == "from-env"


def test_blank_api_key_counts_as_missing(tmp_path):
    config = load(write(tmp_path, "[bridge]\napi_key =\n[printer voron]\nadapter = fake\n"))

    assert config.api_key is None


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.cfg")


def test_needs_at_least_one_printer(tmp_path):
    with pytest.raises(ConfigError, match=r"no \[printer <name>\] section"):
        load(write(tmp_path, "[bridge]\napi_key = k\n"))


def test_printer_section_needs_a_name(tmp_path):
    with pytest.raises(ConfigError, match=r"\[printer\] needs a name"):
        load(write(tmp_path, "[printer]\nadapter = fake\n"))


def test_unknown_section_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match=r"unknown section \[printers voron\]"):
        load(write(tmp_path, "[printers voron]\n[printer a]\nadapter = fake\n"))


def test_duplicate_printer_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="voron"):
        load(write(tmp_path, "[printer voron]\nadapter = fake\n[printer voron]\nadapter = fake\n"))


def test_unknown_adapter_names_the_known_ones(tmp_path):
    with pytest.raises(ConfigError, match=r"unknown adapter 'prusalink'.*fake"):
        load(write(tmp_path, "[printer mk4]\nadapter = prusalink\n"))


def test_adapter_option_errors_surface(tmp_path):
    with pytest.raises(ConfigError, match="broken is set"):
        load(write(tmp_path, "[printer voron]\nadapter = fake\nbroken = yes\n"))


def test_api_url_must_be_http(tmp_path):
    path = write(tmp_path, "[bridge]\napi_url = ftp://x\n[printer a]\nadapter = fake\n")

    with pytest.raises(ConfigError, match="api_url"):
        load(path)


def test_unparseable_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="line"):
        load(write(tmp_path, "this is not ini\n"))


class TestFindConfig:
    def test_explicit_path_wins_even_if_missing(self, tmp_path):
        explicit = tmp_path / "mine.cfg"

        assert find_config(explicit, env={}, home=tmp_path) == explicit

    def test_environment_variable_is_next(self, tmp_path):
        env = {"PRINTLOG_BRIDGE_CONFIG": str(tmp_path / "env.cfg")}

        assert find_config(None, env=env, home=tmp_path) == tmp_path / "env.cfg"

    def test_prefers_the_klipper_config_folder(self, tmp_path):
        klipper = tmp_path / "printer_data" / "config" / "printlog-bridge.cfg"
        xdg = tmp_path / ".config" / "printlog-bridge" / "printlog-bridge.cfg"
        for p in (klipper, xdg):
            p.parent.mkdir(parents=True)
            p.write_text("")

        assert find_config(None, env={}, home=tmp_path) == klipper

    def test_falls_back_to_the_user_config_folder(self, tmp_path):
        xdg = tmp_path / ".config" / "printlog-bridge" / "printlog-bridge.cfg"
        xdg.parent.mkdir(parents=True)
        xdg.write_text("")

        assert find_config(None, env={}, home=tmp_path) == xdg

    def test_names_the_klipper_path_when_nothing_exists(self, tmp_path):
        expected = tmp_path / "printer_data" / "config" / "printlog-bridge.cfg"

        assert find_config(None, env={}, home=tmp_path) == expected


def test_the_example_config_loads(tmp_path):
    from printlog_bridge.adapters import ADAPTERS as REAL_ADAPTERS

    example = Path(__file__).parent.parent / "printlog-bridge.example.cfg"

    config = load_config(example, adapters=REAL_ADAPTERS, env={}, home=tmp_path)

    assert [p.name for p in config.printers] == ["voron"]
    assert config.api_key is None
    assert config.api_url == DEFAULT_API_URL
