"""A stored API key is sent only to the API it was stored for.

In local mode the config sits in Klipper's config folder, which anyone who can open Mainsail or
Fluidd can edit through Moonraker; the key file (`<state_dir>/api_key`) is kept out of it. Without
a binding, pointing `api_url` at another host would hand that host the key on the bridge's next
start. `store-key` records the API's origin next to the key (`api_key_origin`), as `pair` records
the API that issued a device token.
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import pytest
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.cli.main import main
from printlog_bridge.core.config import (
    DEFAULT_API_URL,
    ApiKeySource,
    ConfigError,
    api_origin,
    load_config,
    read_key_origin,
    write_key_file,
)
from printlog_bridge.core.device import write_device_token
from printlog_bridge.core.doctor import diagnose

KEY = "pl_test_key_never_shown"
PRINTERS = "[printer voron]\nprinter_id = 42\nurl = http://voron:7125\n"


@pytest.fixture(autouse=True)
def no_key_in_the_environment(monkeypatch):
    monkeypatch.delenv("PRINTLOG_API_KEY", raising=False)


def setup(tmp_path: Path, api_url: str | None, *, origin: str | None, key: str | None = KEY):
    """A config naming `api_url` (none: the default) and a key file, with `origin` recorded
    next to it (None: an install from before the bridge recorded one)."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if key is not None:
        (state / "api_key").write_text(key + "\n", encoding="utf-8")
    if origin is not None:
        (state / "api_key_origin").write_text(origin + "\n", encoding="utf-8")
    line = f"api_url = {api_url}\n" if api_url else ""
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(f"[bridge]\nstate_dir = {state}\n{line}\n{PRINTERS}", encoding="utf-8")
    return path


def load(path: Path, env=None):
    return load_config(path, adapters=ADAPTERS, env=env or {}, home=path.parent)


class TestRecordedOrigin:
    def test_a_key_stored_for_one_api_is_not_used_with_another(self, tmp_path):
        path = setup(tmp_path, "https://attacker.example", origin="https://api.3dprintlog.com")

        with pytest.raises(ConfigError) as raised:
            load(path)

        message = str(raised.value)
        assert "https://api.3dprintlog.com" in message
        assert "https://attacker.example" in message
        assert "store-key" in message
        assert KEY not in message

    def test_the_same_origin_is_used(self, tmp_path):
        path = setup(tmp_path, "https://API.3dprintlog.com:443/", origin=DEFAULT_API_URL)

        config = load(path)

        assert config.api_key == KEY
        assert config.api_key_source is ApiKeySource.KEY_FILE

    def test_a_custom_api_the_key_was_stored_for_is_used(self, tmp_path):
        path = setup(
            tmp_path, "https://printlog.home.example", origin="https://printlog.home.example"
        )

        assert load(path).api_key == KEY

    def test_another_port_is_another_api(self, tmp_path):
        path = setup(tmp_path, "http://127.0.0.1:5001", origin="http://127.0.0.1:5000")

        with pytest.raises(ConfigError, match="store-key"):
            load(path)

    def test_the_record_wins_over_the_allowances_for_older_installs(self, tmp_path):
        # A key stored for the real API isn't sent to a loopback address either.
        path = setup(tmp_path, "http://127.0.0.1:5000", origin=DEFAULT_API_URL)

        with pytest.raises(ConfigError):
            load(path)

    def test_a_device_token_for_the_api_is_used_instead_of_refusing(self, tmp_path):
        # The key wouldn't be sent anyway (a device token wins), so a working install that
        # paired with its own API keeps working.
        path = setup(tmp_path, "https://printlog.home.example", origin=DEFAULT_API_URL)
        write_device_token(
            tmp_path / "state",
            token="pld_x",
            api_url="https://printlog.home.example",
            grant_id=1,
            name="b",
            scopes=["prints:write"],
        )

        config = load(path)

        assert config.device is not None
        assert config.api_key is None


class TestInstallsFromBeforeTheRecord:
    """A key file with no recorded origin: the bridge can't know which API it was for."""

    @pytest.mark.parametrize(
        "api_url",
        [
            None,
            "https://api.3dprintlog.com",
            "https://api.3dprintlog.com/",
            "http://127.0.0.1:5000",
            "http://localhost:5000",
            "http://[::1]:5000",
        ],
    )
    def test_the_default_api_and_this_computer_keep_working(self, tmp_path, api_url):
        assert load(setup(tmp_path, api_url, origin=None)).api_key == KEY

    @pytest.mark.parametrize(
        "api_url", ["https://attacker.example", "https://printlog.home.example"]
    )
    def test_any_other_api_asks_for_the_key_to_be_stored_again(self, tmp_path, api_url):
        with pytest.raises(ConfigError) as raised:
            load(setup(tmp_path, api_url, origin=None))

        message = str(raised.value)
        assert "printlog-bridge store-key" in message
        assert api_url in message
        assert KEY not in message


class TestKeysTheUserPutNextToTheUrl:
    """The environment variable and `api_key` in `[bridge]` aren't bound: README "Signing in"."""

    def test_the_environment_variable_goes_to_the_configured_api(self, tmp_path):
        path = setup(tmp_path, "https://printlog.home.example", origin=None, key=None)

        config = load(path, env={"PRINTLOG_API_KEY": "from-env"})

        assert config.api_key == "from-env"

    def test_the_environment_wins_over_a_key_file_stored_for_another_api(self, tmp_path):
        path = setup(tmp_path, "https://printlog.home.example", origin=DEFAULT_API_URL)

        assert load(path, env={"PRINTLOG_API_KEY": "from-env"}).api_key == "from-env"

    def test_a_key_in_the_config_goes_to_the_configured_api(self, tmp_path):
        path = setup(tmp_path, "https://printlog.home.example", origin=None, key=None)
        path.write_text(
            path.read_text(encoding="utf-8").replace("[bridge]\n", "[bridge]\napi_key = inline\n"),
            encoding="utf-8",
        )

        assert load(path).api_key == "inline"


class TestStoring:
    def test_write_key_file_records_the_apis_origin(self, tmp_path):
        key_file = tmp_path / "state" / "api_key"

        write_key_file(key_file, KEY, api_url="https://Printlog.Home.Example:443/x")

        assert read_key_origin(tmp_path / "state") == "https://printlog.home.example"
        assert key_file.read_text(encoding="utf-8").strip() == KEY

    @pytest.mark.parametrize(
        ("url", "origin"),
        [
            ("https://api.3dprintlog.com", "https://api.3dprintlog.com"),
            ("https://api.3dprintlog.com:443/", "https://api.3dprintlog.com"),
            ("http://127.0.0.1:80", "http://127.0.0.1"),
            ("http://192.168.1.20:5000/api", "http://192.168.1.20:5000"),
            ("https://user:pw@host.example:8443", "https://host.example:8443"),
            ("http://[::1]:5000", "http://[::1]:5000"),
        ],
    )
    def test_origin(self, url, origin):
        assert api_origin(url) == origin

    def test_store_key_binds_the_key_to_the_configured_api(self, tmp_path, monkeypatch, capsys):
        path = setup(tmp_path, "https://printlog.home.example", origin=None, key=None)
        monkeypatch.setattr(sys, "stdin", io.StringIO("k-new\n"))

        assert main(["store-key", "--config", str(path)]) == 0

        assert read_key_origin(tmp_path / "state") == "https://printlog.home.example"
        assert load(path).api_key == "k-new"
        assert "https://printlog.home.example" in capsys.readouterr().out

    def test_store_key_fixes_an_older_install_with_a_custom_api(self, tmp_path, monkeypatch):
        # The upgrade path for a self-hosted or development API: load refuses the key until
        # store-key stores it again, which must itself still load the config.
        path = setup(tmp_path, "https://printlog.home.example", origin=None)
        with pytest.raises(ConfigError):
            load(path)
        monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))

        assert main(["store-key", "--config", str(path)]) == 0

        assert load(path).api_key == KEY

    def test_store_key_moving_a_key_out_of_the_config_records_the_api(self, tmp_path):
        path = setup(tmp_path, "https://printlog.home.example", origin=None, key=None)
        path.write_text(
            path.read_text(encoding="utf-8").replace("[bridge]\n", "[bridge]\napi_key = inline\n"),
            encoding="utf-8",
        )

        assert main(["store-key", "--config", str(path)]) == 0

        assert read_key_origin(tmp_path / "state") == "https://printlog.home.example"
        assert load(path).api_key == "inline"


def test_no_request_to_another_api_carries_the_key(tmp_path):
    """The attack end to end: the config's api_url is changed to a server that isn't the API
    the key was stored for. doctor (which signs in) must not send it there."""
    api = FakePrintLogApi(api_key=KEY)

    async def scenario():
        base = await api.start()
        try:
            path = setup(tmp_path, base, origin=DEFAULT_API_URL)
            return await diagnose(path, adapters=ADAPTERS, env={}, home=tmp_path)
        finally:
            await api.close()

    results = asyncio.run(scenario())

    assert api.requests == []
    assert any("store-key" in r.detail for r in results)
