from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from printlog_bridge.core.adapter import Adapter
from printlog_bridge.core.config import (
    DEFAULT_API_URL,
    Adoption,
    ApiKeySource,
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

    async def run(self, sink, report) -> None:  # pragma: no cover - not exercised here
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


def test_a_config_saved_with_a_byte_order_mark_loads(tmp_path):
    # Windows editors (e.g. Notepad over an SMB share to a NAS) can save UTF-8 with a BOM.
    config = load(write(tmp_path, "﻿[printer voron]\nadapter = fake\n"))

    assert [p.name for p in config.printers] == ["voron"]


def test_bridge_section_is_optional_and_has_defaults(tmp_path):
    config = load(write(tmp_path, "[printer voron]\nadapter = fake\n"))

    assert config.api_url == DEFAULT_API_URL
    assert config.api_key is None
    assert config.state_dir == tmp_path / ".local" / "share" / "printlog-bridge"


class TestStateDirFromTheEnvironment:
    """The Docker image sets PRINTLOG_BRIDGE_STATE_DIR, so state lands in the mounted volume
    even when the config doesn't name a state_dir."""

    def test_is_the_default_without_state_dir_in_the_config(self, tmp_path):
        config = load(
            write(tmp_path, "[printer voron]\nadapter = fake\n"),
            env={"PRINTLOG_BRIDGE_STATE_DIR": "/data/state"},
        )

        assert config.state_dir == Path("/data/state")
        assert config.api_key_file == Path("/data/state/api_key")

    def test_state_dir_in_the_config_wins(self, tmp_path):
        config = load(
            write(tmp_path, "[bridge]\nstate_dir = /srv/bridge\n[printer voron]\nadapter = fake\n"),
            env={"PRINTLOG_BRIDGE_STATE_DIR": "/data/state"},
        )

        assert config.state_dir == Path("/srv/bridge")

    def test_a_blank_variable_is_ignored(self, tmp_path):
        config = load(
            write(tmp_path, "[printer voron]\nadapter = fake\n"),
            env={"PRINTLOG_BRIDGE_STATE_DIR": "  "},
        )

        assert config.state_dir == tmp_path / ".local" / "share" / "printlog-bridge"

    def test_the_key_file_is_read_from_it(self, tmp_path):
        state = tmp_path / "volume" / "state"
        state.mkdir(parents=True)
        (state / "api_key").write_text("from-the-volume\n", encoding="utf-8")

        config = load(
            write(tmp_path, "[printer voron]\nadapter = fake\n"),
            env={"PRINTLOG_BRIDGE_STATE_DIR": str(state)},
        )

        assert config.api_key == "from-the-volume"
        assert config.api_key_source is ApiKeySource.KEY_FILE


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


def test_a_missing_file_can_load_as_an_empty_one_for_run(tmp_path):
    # `run` in Docker starts with no config at all: it pairs, then waits for printers.
    config = load_config(
        tmp_path / "nope.cfg",
        adapters=ADAPTERS,
        env={"PRINTLOG_BRIDGE_STATE_DIR": str(tmp_path / "state")},
        home=tmp_path,
        missing_ok=True,
        printers_required=False,
    )

    assert config.printers == []
    assert config.api_url == DEFAULT_API_URL
    assert config.state_dir == tmp_path / "state"
    assert config.path == tmp_path / "nope.cfg"


def test_missing_ok_still_needs_printers_unless_told_otherwise(tmp_path):
    with pytest.raises(ConfigError, match=r"no \[printer <name>\] section"):
        load_config(
            tmp_path / "nope.cfg", adapters=ADAPTERS, env={}, home=tmp_path, missing_ok=True
        )


def test_a_config_without_printers_loads_when_printers_arent_required(tmp_path):
    path = write(tmp_path, "[bridge]\napi_key = k\n")

    config = load_config(path, adapters=ADAPTERS, env={}, home=tmp_path, printers_required=False)

    assert config.printers == []
    assert config.api_key == "k"


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


def test_printer_id_links_a_printer_to_3d_print_log_and_is_not_an_adapter_option(tmp_path):
    config = load(write(tmp_path, "[printer voron]\nadapter = fake\nprinter_id = 42\n"))

    [voron] = config.printers
    assert voron.printer_id == 42
    assert voron.adapter.options == {}


def test_printer_id_is_optional_until_run(tmp_path):
    config = load(write(tmp_path, "[printer voron]\nadapter = fake\n"))

    assert config.printers[0].printer_id is None


@pytest.mark.parametrize("value", ["voron", "0", "-3", "1.5"])
def test_printer_id_must_be_a_positive_whole_number(tmp_path, value):
    path = write(tmp_path, f"[printer voron]\nadapter = fake\nprinter_id = {value}\n")

    with pytest.raises(ConfigError, match=r"\[printer voron\] printer_id must be the number"):
        load(path)


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


class TestApiKeyFile:
    """The key can be kept in `<state_dir>/api_key`, out of the folder Moonraker serves."""

    def write(self, tmp_path: Path, key_file: str | None, bridge: str = "") -> Path:
        state = tmp_path / "state"
        state.mkdir()
        if key_file is not None:
            (state / "api_key").write_text(key_file, encoding="utf-8")
        return write(
            tmp_path, f"[bridge]\nstate_dir = {state}\n{bridge}[printer voron]\nadapter = fake\n"
        )

    def test_is_read_from_the_state_directory(self, tmp_path):
        config = load(self.write(tmp_path, "from-key-file\n"))

        assert config.api_key == "from-key-file"
        assert config.api_key_source is ApiKeySource.KEY_FILE
        assert config.api_key_file == tmp_path / "state" / "api_key"

    def test_wins_over_a_key_in_the_config(self, tmp_path):
        config = load(self.write(tmp_path, "from-key-file", "api_key = inline\n"))

        assert config.api_key == "from-key-file"

    def test_the_environment_wins_over_it(self, tmp_path):
        config = load(self.write(tmp_path, "from-key-file"), env={"PRINTLOG_API_KEY": "env"})

        assert config.api_key == "env"
        assert config.api_key_source is ApiKeySource.ENVIRONMENT

    def test_a_key_in_the_config_still_works(self, tmp_path):
        config = load(self.write(tmp_path, None, "api_key = inline\n"))

        assert config.api_key == "inline"
        assert config.api_key_source is ApiKeySource.CONFIG

    def test_an_empty_key_file_counts_as_missing(self, tmp_path):
        config = load(self.write(tmp_path, "\n", "api_key = inline\n"))

        assert config.api_key == "inline"

    def test_no_key_anywhere(self, tmp_path):
        config = load(self.write(tmp_path, None))

        assert config.api_key is None
        assert config.api_key_source is None

    def test_an_unreadable_key_file_is_a_config_error(self, tmp_path):
        path = self.write(tmp_path, None)
        (tmp_path / "state" / "api_key").mkdir()

        with pytest.raises(ConfigError, match="api_key"):
            load(path)


class TestTrace:
    def config(self, tmp_path, option: str = "", env=None):
        path = write(tmp_path, f"[bridge]\n{option}\n[printer voron]\nadapter = fake\n")
        return load(path, env)

    def test_is_off_by_default(self, tmp_path):
        assert self.config(tmp_path).trace is False

    @pytest.mark.parametrize("value", ["true", "yes", "on", "1", "True"])
    def test_trace_in_the_bridge_section_turns_it_on(self, tmp_path, value):
        assert self.config(tmp_path, f"trace = {value}").trace is True

    def test_trace_false_leaves_it_off(self, tmp_path):
        assert self.config(tmp_path, "trace = false").trace is False

    def test_the_environment_turns_it_on(self, tmp_path):
        assert self.config(tmp_path, env={"PRINTLOG_BRIDGE_TRACE": "1"}).trace is True

    def test_the_environment_wins_over_the_config(self, tmp_path):
        config = self.config(tmp_path, "trace = true", env={"PRINTLOG_BRIDGE_TRACE": "0"})

        assert config.trace is False

    def test_a_blank_variable_is_ignored(self, tmp_path):
        config = self.config(tmp_path, "trace = true", env={"PRINTLOG_BRIDGE_TRACE": " "})

        assert config.trace is True

    def test_anything_else_is_a_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match=r"\[bridge\] trace must be true or false"):
            self.config(tmp_path, "trace = sometimes")

    def test_a_bad_variable_is_a_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="PRINTLOG_BRIDGE_TRACE must be"):
            self.config(tmp_path, env={"PRINTLOG_BRIDGE_TRACE": "maybe"})


class TestAdoption:
    """`adopt_prints`, `create_prints` and `adopt_window` (#43)."""

    def test_defaults_adopt_a_matching_print_else_create_one(self, tmp_path):
        [printer] = load(write(tmp_path, "[printer voron]\nadapter = fake\n")).printers

        assert printer.adoption == Adoption(adopt=True, create=True, window_s=12 * 60 * 60)
        assert printer.adapter.options == {}  # not adapter options

    def test_bridge_sets_every_printer_and_a_printer_overrides_it(self, tmp_path):
        path = write(
            tmp_path,
            "[bridge]\ncreate_prints = false\nadopt_window = 2d\n"
            "[printer a]\nadapter = fake\n"
            "[printer b]\nadapter = fake\ncreate_prints = true\nadopt_window = 90m\n",
        )

        a, b = load(path).printers

        assert a.adoption == Adoption(adopt=True, create=False, window_s=2 * 24 * 60 * 60)
        assert b.adoption == Adoption(adopt=True, create=True, window_s=90 * 60)
        assert b.adapter.options == {}

    @pytest.mark.parametrize(("value", "seconds"), [("6", 6 * 3600), ("0.5h", 1800), ("30s", 30)])
    def test_adopt_window_units(self, tmp_path, value, seconds):
        path = write(tmp_path, f"[printer voron]\nadapter = fake\nadopt_window = {value}\n")

        [printer] = load(path).printers

        assert printer.adoption.window_s == seconds

    @pytest.mark.parametrize(
        ("option", "message"),
        [
            ("adopt_window = soon", "adopt_window must be a time"),
            ("adopt_window = 0h", "adopt_window must be a time"),
            ("create_prints = maybe", "create_prints must be true or false"),
            ("adopt_prints = false\ncreate_prints = false", "no print would ever be logged"),
        ],
    )
    def test_bad_values_are_config_errors(self, tmp_path, option, message):
        path = write(tmp_path, f"[printer voron]\nadapter = fake\n{option}\n")

        with pytest.raises(ConfigError, match=message):
            load(path)


class TestUpdateCheck:
    """`update_check`: say when a newer bridge is out (#39)."""

    def config(self, tmp_path, option: str = ""):
        return load(write(tmp_path, f"[bridge]\n{option}\n[printer voron]\nadapter = fake\n"))

    def test_is_on_by_default(self, tmp_path):
        assert self.config(tmp_path).update_check is True

    def test_a_blank_value_leaves_it_on(self, tmp_path):
        assert self.config(tmp_path, "update_check =").update_check is True

    @pytest.mark.parametrize("value", ["false", "no", "off", "0", "False"])
    def test_false_turns_it_off(self, tmp_path, value):
        assert self.config(tmp_path, f"update_check = {value}").update_check is False

    def test_anything_else_is_a_config_error(self, tmp_path):
        with pytest.raises(ConfigError, match="update_check must be true or false"):
            self.config(tmp_path, "update_check = weekly")


class TestSlicerSettings:
    """`slicer_settings` (#36): on unless turned off, in `[bridge]` or for one printer."""

    def test_on_by_default_and_not_an_adapter_option(self, tmp_path):
        [printer] = load(write(tmp_path, "[printer voron]\nadapter = fake\n")).printers

        assert printer.slicer_settings is True
        assert printer.adapter.options == {}

    def test_bridge_sets_every_printer_and_a_printer_overrides_it(self, tmp_path):
        path = write(
            tmp_path,
            "[bridge]\nslicer_settings = false\n"
            "[printer a]\nadapter = fake\n"
            "[printer b]\nadapter = fake\nslicer_settings = true\n"
            "[printer c]\nadapter = fake\nslicer_settings = no\n",
        )

        a, b, c = load(path).printers

        assert (a.slicer_settings, b.slicer_settings, c.slicer_settings) == (False, True, False)
        assert b.adapter.options == {}

    def test_anything_else_is_a_config_error(self, tmp_path):
        path = write(tmp_path, "[printer voron]\nadapter = fake\nslicer_settings = some\n")

        with pytest.raises(ConfigError, match="slicer_settings must be true or false"):
            load(path)
