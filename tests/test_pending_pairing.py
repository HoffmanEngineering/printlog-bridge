"""The code a pairing waits on, in the state directory, for `printlog-bridge status --pairing`:
what the one-line Docker install (scripts/docker-install.sh) shows, read from the bridge's own
state rather than from its log, which a device on the network could put text into."""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import replace

import pytest
from test_pairing import pair, run
from test_run import root_logging, write_config  # noqa: F401 (a fixture)
from test_status import NOW, Clock, board, config

from printlog_bridge.cli.main import main
from printlog_bridge.core import pairing
from printlog_bridge.core.device import (
    PENDING_PAIRING_FILE,
    DeviceToken,
    clear_pending_pairing,
    read_pending_pairing,
    write_pending_pairing,
)
from printlog_bridge.core.pairing import PairingDenied, PairingUnavailable
from printlog_bridge.core.status import format_status, pairing_state

URI = "https://auth.3dprintlog.com/activate"
COMPLETE = "https://auth.3dprintlog.com/activate?user_code=QWER-TYUI"


def write(state_dir, *, code="QWER-TYUI", uri=URI, complete=COMPLETE, expires_in=900.0, now=1000.0):
    write_pending_pairing(state_dir, code, uri, complete, expires_in, now=lambda: now)


class TestTheFile:
    def test_pairing_keeps_its_code_in_the_state_directory_while_it_waits(self, tmp_path):
        seen: list = []

        async def scenario(api, auth0, api_url):
            auth0.script = ["pending"]
            auth0.approve()
            # The file is read when `show` runs, that is, while pairing waits.
            await pairing.pair(
                api_url, tmp_path, name="n", create=False,
                show=lambda a: seen.append((a, read_pending_pairing(tmp_path))),
                say=lambda m: None, sleep=_no_sleep, clock=lambda: 0.0,
            )  # fmt: skip

        run(scenario)

        [(authorization, pending)] = seen
        assert pending is not None
        assert pending.user_code == authorization.user_code
        assert pending.verification_uri == authorization.verification_uri
        assert pending.verification_uri_complete == authorization.verification_uri_complete
        assert not (tmp_path / PENDING_PAIRING_FILE).exists()  # paired: nothing waits any more

    def test_a_pairing_that_fails_removes_it(self, tmp_path):
        async def scenario(api, auth0, api_url):
            auth0.script = ["pending", "deny"]
            call, _, _ = pair(api_url, tmp_path)
            with pytest.raises(PairingDenied):
                await call

        run(scenario)
        assert not (tmp_path / PENDING_PAIRING_FILE).exists()

    def test_it_never_holds_the_device_code(self, tmp_path):
        write(tmp_path)
        data = json.loads((tmp_path / PENDING_PAIRING_FILE).read_text(encoding="utf-8"))
        assert set(data) == {"userCode", "verificationUri", "verificationUriComplete", "expiresAt"}

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_only_the_user_can_read_it(self, tmp_path):
        write(tmp_path)
        mode = stat.S_IMODE(os.stat(tmp_path / PENDING_PAIRING_FILE).st_mode)
        assert mode == 0o600

    def test_it_reads_back_until_it_expires(self, tmp_path):
        write(tmp_path, expires_in=60, now=1000.0)
        assert read_pending_pairing(tmp_path, now=lambda: 1059.0).user_code == "QWER-TYUI"
        assert read_pending_pairing(tmp_path, now=lambda: 1060.0) is None

    def test_clearing_it_twice_is_fine(self, tmp_path):
        write(tmp_path)
        clear_pending_pairing(tmp_path)
        clear_pending_pairing(tmp_path)
        assert read_pending_pairing(tmp_path) is None

    @pytest.mark.parametrize(
        "change",
        [
            {"code": "QWER TYUI"},
            {"code": "\x1b[2J"},
            {"code": ""},
            {"uri": "http://evil.example/activate"},
            {"uri": "https://auth.example/ac tivate"},
            {"uri": "https://auth.example/\x1b[2J"},
            {"uri": "javascript:alert(1)"},
        ],
    )
    def test_a_code_or_address_that_isnt_one_reads_as_nothing(self, tmp_path, change):
        write(tmp_path, **change, now=1000.0)
        assert read_pending_pairing(tmp_path, now=lambda: 1000.0) is None

    def test_a_link_that_isnt_one_is_dropped(self, tmp_path):
        write(tmp_path, complete="http://evil.example/x", now=1000.0)
        pending = read_pending_pairing(tmp_path, now=lambda: 1000.0)
        assert pending.user_code == "QWER-TYUI"
        assert pending.verification_uri_complete is None

    def test_a_damaged_file_reads_as_nothing(self, tmp_path):
        (tmp_path / PENDING_PAIRING_FILE).write_text("{not json", encoding="utf-8")
        assert read_pending_pairing(tmp_path) is None
        (tmp_path / PENDING_PAIRING_FILE).write_text('["a"]', encoding="utf-8")
        assert read_pending_pairing(tmp_path) is None


async def _no_sleep(seconds: float) -> None:
    return None


class TestPairingState:
    """`status --pairing`: one line a script can read."""

    def test_a_code_waiting_wins_even_over_a_device_token(self, tmp_path):
        write(tmp_path, now=1000.0)
        cfg = replace(
            config(tmp_path), stored_device=DeviceToken("pld_x", "https://api.example.test")
        )
        assert pairing_state(cfg, now=lambda: 1000.0) == f"pending QWER-TYUI {URI} {COMPLETE}"

    def test_without_a_link(self, tmp_path):
        write(tmp_path, complete=None, now=1000.0)
        assert pairing_state(config(tmp_path), now=lambda: 1000.0) == f"pending QWER-TYUI {URI}"

    def test_paired(self, tmp_path):
        cfg = replace(
            config(tmp_path), stored_device=DeviceToken("pld_x", "https://api.example.test")
        )
        assert pairing_state(cfg) == "paired"

    def test_an_api_key(self, tmp_path):
        assert pairing_state(config(tmp_path)) == "api-key"

    def test_nothing_yet(self, tmp_path):
        assert pairing_state(replace(config(tmp_path), api_key=None)) == "waiting"

    def test_the_command_line(self, tmp_path, capsys, monkeypatch):
        state = tmp_path / "state"
        cfg = tmp_path / "printlog-bridge.cfg"
        cfg.write_text(f"[bridge]\nstate_dir = {state.as_posix()}\n", encoding="utf-8")
        monkeypatch.delenv("PRINTLOG_API_KEY", raising=False)
        monkeypatch.delenv("PRINTLOG_BRIDGE_STATE_DIR", raising=False)
        write_pending_pairing(state, "QWER-TYUI", URI, None, 900)

        code = main(["status", "--pairing", "--config", str(cfg)])

        assert code == 0
        assert capsys.readouterr().out == f"pending QWER-TYUI {URI}\n"


def test_status_shows_the_code_while_the_bridge_waits_for_it(tmp_path):
    clock = Clock()
    board(tmp_path, clock).start(["voron"])
    write_pending_pairing(tmp_path, "QWER-TYUI", URI, COMPLETE, 900)

    text, _ = format_status(config(tmp_path), now=NOW)

    assert f"Pairing code: QWER-TYUI, to enter at {URI} (or open {COMPLETE})" in text


class TestStaleCodes:
    """A bridge killed while it paired (SIGKILL, out of memory, a power cut) leaves its file."""

    def test_pairing_clears_a_code_left_by_an_earlier_run(self, tmp_path):
        write_pending_pairing(tmp_path, "DEAD-CODE", URI, None, 900)

        async def scenario(api, auth0, api_url):
            call, _, _ = pair(api_url, tmp_path)
            with pytest.raises(PairingUnavailable):
                await call

        run(scenario, pairing_on=False)
        assert not (tmp_path / PENDING_PAIRING_FILE).exists()

    @pytest.mark.usefixtures("root_logging")
    def test_run_clears_it_when_it_starts(self, tmp_path, capsys):
        state = tmp_path / "state"
        write_pending_pairing(state, "DEAD-CODE", URI, None, 900)
        config = write_config(tmp_path, "api_url = http://127.0.0.1:9\n")

        main(["run", "--config", str(config)])

        assert not (state / PENDING_PAIRING_FILE).exists()


@pytest.mark.parametrize("expires_in", [float("nan"), float("inf"), float("-inf")])
def test_a_code_that_never_expires_isnt_kept(tmp_path, expires_in):
    with pytest.raises(ValueError):
        write_pending_pairing(tmp_path, "QWER-TYUI", URI, None, expires_in)
    assert not (tmp_path / PENDING_PAIRING_FILE).exists()


@pytest.mark.parametrize("expires_at", ["NaN", "Infinity", "-Infinity"])
def test_a_file_that_never_expires_reads_as_nothing(tmp_path, expires_at):
    (tmp_path / PENDING_PAIRING_FILE).write_text(
        '{"userCode": "QWER-TYUI", "verificationUri": "'
        + URI
        + '", "expiresAt": '
        + expires_at
        + "}",
        encoding="utf-8",
    )
    assert read_pending_pairing(tmp_path) is None
