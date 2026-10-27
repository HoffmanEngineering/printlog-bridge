"""The local page (#100 story 14d, #98's ingress page): what it shows, what it never shows, and
who may open it."""

from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from printlog_bridge import __version__
from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.core import credentials, local_page
from printlog_bridge.core.checks import CheckResult
from printlog_bridge.core.config import ConfigError, PageAddress, load_config
from printlog_bridge.core.device import write_device_token
from printlog_bridge.core.local_page import LocalPage
from printlog_bridge.core.pairing import DeviceAuthorization, PendingCodes
from printlog_bridge.core.status import StatusBoard

API_KEY = "plk_SECRET-API-KEY-1234567890"
DEVICE_TOKEN = "pld_SECRET-DEVICE-TOKEN-1234567890"
MOONRAKER_KEY = "MOONRAKER-SECRET-KEY-1234567890"
PRINTER_NAME = '<img src=x onerror="alert(1)">'
LOCAL = "127.0.0.1"


def write_config(tmp_path: Path, extra: str = "") -> Path:
    state = tmp_path / "state"
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(
        f"[bridge]\napi_url = http://127.0.0.1:9\nstate_dir = {state}\n{extra}"
        f"[printer voron]\nprinter_id = 42\nurl = http://voron.local:7125\n"
        f"moonraker_api_key = {MOONRAKER_KEY}\n",
        encoding="utf-8",
    )
    return path


def make_page(tmp_path: Path, *, env=None, pending=None, diagnose=None, clients=(), extra=""):
    path = write_config(tmp_path, extra)
    env = {"PRINTLOG_API_KEY": API_KEY, **(env or {})}

    def load():
        return load_config(path, adapters=ADAPTERS, env=env, home=tmp_path, printers_required=False)

    return LocalPage(
        PageAddress(LOCAL, 0),
        clients,
        load,
        env=env,
        pending=pending or PendingCodes(),
        diagnose=diagnose,
    )


def get(page: LocalPage, path: str = "/", *, host: str = "127.0.0.1:7180", peer=LOCAL, **headers):
    return page.respond("GET", path, {"Host": host, **headers}, peer)


def text_of(response) -> str:
    return response.body.decode("utf-8")


def doctor(page: LocalPage, timeout: float = 5, **headers):
    """/doctor once its checks (run in the background) have finished."""
    deadline = time.monotonic() + timeout
    while True:
        response = get(page, "/doctor", **headers)
        if RUNNING not in text_of(response) or time.monotonic() > deadline:
            return response
        time.sleep(0.02)


RUNNING = "The checks are running"


def start_board(tmp_path: Path, printers=("voron",)) -> StatusBoard:
    board = StatusBoard(tmp_path / "state")
    board.start(list(printers), credentials="API key")
    return board


class TestWhatItShows:
    def test_the_version_and_that_the_bridge_runs(self, tmp_path):
        start_board(tmp_path).printer("voron").connected()
        page = make_page(tmp_path)

        response = get(page)

        assert response.status == 200
        assert response.headers["Content-Type"] == "text/html; charset=utf-8"
        body = text_of(response)
        assert __version__ in body
        assert "running" in body
        assert "voron" in body and "connected" in body

    def test_before_the_bridge_ever_ran(self, tmp_path):
        body = text_of(get(make_page(tmp_path)))
        assert "hasn&#x27;t run yet" in body or "hasn't run yet" in body

    def test_it_reuses_what_status_says(self, tmp_path):
        start_board(tmp_path)
        body = text_of(get(make_page(tmp_path)))
        # format_status's lines, as `printlog-bridge status` prints them
        assert "Printer voron (printer 42 in 3D Print Log)" in body
        assert "Outbox: empty" in body

    def test_the_last_check_in(self, tmp_path):
        board = start_board(tmp_path)
        board.bridge(check_in={"at": board._now(), "problem": None})
        body = text_of(get(make_page(tmp_path)))
        assert "Last check-in" in body

    def test_names_from_the_network_are_escaped(self, tmp_path):
        board = start_board(tmp_path, printers=("voron", PRINTER_NAME))
        board.printer(PRINTER_NAME).disconnected('<script>alert("x")</script>')
        body = text_of(get(make_page(tmp_path)))
        assert "<img" not in body
        assert "<script>" not in body
        assert "&lt;img src=x" in body

    def test_the_code_to_approve_while_pairing(self, tmp_path):
        codes = PendingCodes()
        codes.show(
            DeviceAuthorization(
                "DEVICE-CODE-SECRET", "QWER-TYUI", "https://auth.example/activate",
                "https://auth.example/activate?user_code=QWER-TYUI", 900, 5,
            )
        )  # fmt: skip
        body = text_of(get(make_page(tmp_path, pending=codes)))
        assert "QWER-TYUI" in body
        assert 'href="https://auth.example/activate?user_code=QWER-TYUI"' in body
        assert "<svg " in body
        assert "DEVICE-CODE-SECRET" not in body
        assert "15 minutes" in body

    def test_while_pairing_the_bridge_says_so_once(self, tmp_path):
        board = start_board(tmp_path)
        board.credentials_problem("waiting for approval: scan the QR code in the log")
        codes = PendingCodes()
        codes.show(DeviceAuthorization("d", "ABCD", "https://a.example/activate", None, 900, 5))
        body = text_of(get(make_page(tmp_path, pending=codes)))
        assert "<dd>pairing: waiting for approval (above)</dd>" in body

    def test_the_page_updates_itself_while_pairing(self, tmp_path):
        codes = PendingCodes()
        codes.show(DeviceAuthorization("d", "ABCD", "https://a.example/activate", None, 900, 5))
        body = text_of(get(make_page(tmp_path, pending=codes)))
        assert '<meta http-equiv="refresh" content="5">' in body

    def test_a_verification_address_that_isnt_https_is_not_a_link(self, tmp_path):
        codes = PendingCodes()
        codes.show(DeviceAuthorization("d", "ABCD", "javascript:alert(1)", None, 900, 5))
        body = text_of(get(make_page(tmp_path, pending=codes)))
        assert 'href="javascript' not in body
        assert "ABCD" in body

    def test_paired_with_the_keys_fingerprint(self, tmp_path):
        state = tmp_path / "state"
        write_device_token(
            state, token=DEVICE_TOKEN, api_url="http://127.0.0.1:9", grant_id=1,
            name="bridge on the NAS", scopes=["bridge:sync"],
        )  # fmt: skip
        if not credentials.available():
            pytest.skip("needs cryptography")
        key = credentials.write_new_key(state)
        start_board(tmp_path)

        body = text_of(get(make_page(tmp_path)))

        assert "Paired as" in body and "bridge on the NAS" in body
        assert credentials.short_fingerprint(key.fingerprint) in body

    def test_a_broken_config_says_where_to_look_instead_of_failing(self, tmp_path):
        page = make_page(tmp_path)
        page.load = lambda: (_ for _ in ()).throw(ConfigError("[bridge] trace must be true"))
        response = get(page)
        assert response.status == 200
        assert "trace must be true" not in text_of(response)
        assert "The config file has an error" in text_of(response)

    def test_a_bug_answers_500_without_a_traceback(self, tmp_path):
        page = make_page(tmp_path)
        page.load = lambda: (_ for _ in ()).throw(RuntimeError("boom at /secret/path"))
        response = get(page)
        assert response.status == 500
        assert "boom" not in text_of(response)
        assert "Traceback" not in text_of(response)


class TestSecrets:
    def test_no_key_token_password_or_private_key_reaches_the_page(self, tmp_path):
        state = tmp_path / "state"
        write_device_token(
            state, token=DEVICE_TOKEN, api_url="http://127.0.0.1:9", grant_id=1, name="n",
            scopes=[],
        )  # fmt: skip
        pem_lines = []
        if credentials.available():
            key = credentials.write_new_key(state)
            pem_lines = [line for line in key.to_pem().splitlines() if "-----" not in line]
        board = start_board(tmp_path)
        # Even when a secret made it into the status file, the page scrubs it.
        board.printer("voron").disconnected(f"refused key {MOONRAKER_KEY} and {DEVICE_TOKEN}")

        def diagnose():
            return [CheckResult.ok("API", f"signed in with {API_KEY}")]

        page = make_page(tmp_path, diagnose=diagnose)
        bodies = text_of(get(page)) + text_of(doctor(page))

        for secret in (API_KEY, DEVICE_TOKEN, MOONRAKER_KEY, *pem_lines):
            assert secret not in bodies
        assert "&lt;redacted&gt;" in bodies  # escaped like any value


class TestHeaders:
    def test_every_answer_locks_the_page_down(self, tmp_path):
        page = make_page(tmp_path)
        for response in (get(page), get(page, "/nothing"), get(page, host="evil.example.com")):
            headers = response.headers
            csp = headers["Content-Security-Policy"]
            assert "default-src 'none'" in csp
            assert "frame-ancestors 'self'" in csp
            assert "script-src" not in csp  # no scripts at all: default-src 'none'
            assert "http" not in csp
            assert headers["X-Frame-Options"] == "SAMEORIGIN"
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert headers["Cache-Control"] == "no-store"
            assert headers["Referrer-Policy"] == "no-referrer"

    def test_the_inline_style_carries_the_csp_nonce(self, tmp_path):
        response = get(make_page(tmp_path))
        csp = response.headers["Content-Security-Policy"]
        nonce = csp.split("'nonce-")[1].split("'")[0]
        assert f'<style nonce="{nonce}">' in text_of(response)
        assert "<script" not in text_of(response)

    def test_each_answer_has_its_own_nonce(self, tmp_path):
        page = make_page(tmp_path)
        assert (
            get(page).headers["Content-Security-Policy"]
            != get(page).headers["Content-Security-Policy"]
        )


class TestWhoMayOpenIt:
    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1:7180", "localhost:7180", "192.168.1.20:7180", "[::1]:7180", "nas:7180",
         "nas.local:7180", "printer-pi.lan", "bridge.home.arpa:7180", "192.168.1.20"],
    )  # fmt: skip
    def test_a_local_address_is_answered(self, tmp_path, host):
        assert get(make_page(tmp_path), host=host).status == 200

    @pytest.mark.parametrize("host", ["evil.example.com", "rebind.attacker.net:7180", "", "a b"])
    def test_any_other_host_name_is_refused_against_dns_rebinding(self, tmp_path, host):
        response = get(make_page(tmp_path), host=host)
        assert response.status == 421
        assert "voron" not in text_of(response)

    def test_only_the_listed_clients_may_open_it(self, tmp_path):
        page = make_page(tmp_path, clients=("172.30.32.2",))
        assert get(page, peer="192.168.1.50").status == 403
        assert get(page, peer="::ffff:192.168.1.50").status == 403
        assert get(page, peer="172.30.32.2").status == 200
        assert get(page, peer="::ffff:172.30.32.2").status == 200

    def test_a_listed_client_may_use_any_host_name(self, tmp_path):
        # Home Assistant's ingress proxy: the browser's Host is Home Assistant's own name.
        page = make_page(tmp_path, clients=("172.30.32.2",))
        assert get(page, host="homeassistant.example.com", peer="172.30.32.2").status == 200

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    def test_it_is_read_only(self, tmp_path, method):
        response = make_page(tmp_path).respond(method, "/", {"Host": "127.0.0.1"}, LOCAL)
        assert response.status == 405
        assert response.headers["Allow"] == "GET, HEAD"

    def test_head_has_no_body(self, tmp_path):
        response = make_page(tmp_path).respond("HEAD", "/", {"Host": "127.0.0.1"}, LOCAL)
        assert response.status == 200 and response.body == b""

    def test_unknown_paths_are_not_found(self, tmp_path):
        assert get(make_page(tmp_path), "/../state/device_token").status == 404
        assert get(make_page(tmp_path), "/static/x.js").status == 404


class TestIngress:
    def test_links_follow_the_ingress_path(self, tmp_path):
        page = make_page(tmp_path, clients=("172.30.32.2",))
        body = text_of(
            get(page, peer="172.30.32.2", **{"X-Ingress-Path": "/api/hassio_ingress/AbC_d-9"})
        )
        assert 'href="/api/hassio_ingress/AbC_d-9/doctor"' in body

    @pytest.mark.parametrize(
        "value", ['/api/hassio_ingress/x"><b>', "//evil.example/x", "/elsewhere", "javascript:x"]
    )
    def test_any_other_ingress_path_is_ignored(self, tmp_path, value):
        body = text_of(get(make_page(tmp_path), **{"X-Ingress-Path": value}))
        assert 'href="doctor"' in body
        assert "evil" not in body and "<b>" not in body

    def test_without_ingress_links_are_relative(self, tmp_path):
        body = text_of(get(make_page(tmp_path)))
        assert 'href="doctor"' in body
        checks = text_of(doctor(make_page(tmp_path, diagnose=lambda: [])))
        assert 'href="./"' in checks


class TestDoctor:
    def test_runs_the_checks_and_shows_them(self, tmp_path):
        results = [CheckResult.ok("Python", "3.12"), CheckResult.fail("3D Print Log API", "<down>")]
        body = text_of(doctor(make_page(tmp_path, diagnose=lambda: results)))
        assert "Python" in body and "3.12" in body
        assert "&lt;down&gt;" in body
        assert "1 problem found" in body  # doctor's own summary line

    def test_the_checks_run_at_most_once_in_a_while(self, tmp_path):
        calls = []

        def diagnose():
            calls.append(1)
            return []

        page = make_page(tmp_path, diagnose=diagnose)
        doctor(page)
        doctor(page)
        assert len(calls) == 1

    def test_checks_that_fail_say_so(self, tmp_path):
        def diagnose():
            raise RuntimeError("adapter bug")

        response = doctor(make_page(tmp_path, diagnose=diagnose))
        assert response.status == 200
        assert "couldn" in text_of(response).lower()

    def test_the_main_page_never_runs_the_checks(self, tmp_path):
        def diagnose():
            raise AssertionError("ran")

        assert get(make_page(tmp_path, diagnose=diagnose)).status == 200

    def test_a_request_never_waits_for_the_checks(self, tmp_path):
        release = threading.Event()
        calls = []

        def diagnose():
            calls.append(1)
            release.wait(10)
            return [CheckResult.ok("Python", "3.12")]

        page = make_page(tmp_path, diagnose=diagnose)
        try:
            for _ in range(3):
                started = time.monotonic()
                response = get(page, "/doctor")
                assert time.monotonic() - started < 1
                body = text_of(response)
                assert RUNNING in body
                assert '<meta http-equiv="refresh" content="3">' in body
        finally:
            release.set()
        assert "3.12" in text_of(doctor(page))
        assert len(calls) == 1  # one run, however many requests came while it ran

    def test_a_hung_executor_thread_doesnt_wedge_the_checks(self):
        # asyncio.run waits for the default executor's threads on shutdown (for good on 3.9 to
        # 3.11): the checks' own loop must not.
        import asyncio

        stuck = threading.Event()

        async def checks():
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, stuck.wait, 30)
            await asyncio.sleep(10)

        started = time.monotonic()
        try:
            with pytest.raises(asyncio.TimeoutError):
                local_page.run_with_timeout(checks(), 0.2)
            assert time.monotonic() - started < 3
        finally:
            stuck.set()


class TestCrossSite:
    """No other site can make the bridge run its checks (or load the page) by linking to it."""

    @pytest.mark.parametrize("site", ["cross-site", "same-site"])
    @pytest.mark.parametrize("path", ["/", "/doctor"])
    def test_a_request_from_another_site_is_refused(self, tmp_path, site, path):
        def diagnose():
            raise AssertionError("ran")

        page = make_page(tmp_path, diagnose=diagnose)
        response = get(page, path, **{"Sec-Fetch-Site": site, "Sec-Fetch-Dest": "document"})
        assert response.status == 403
        assert "voron" not in text_of(response)

    @pytest.mark.parametrize("dest", ["image", "script", "style", "empty"])
    def test_anything_but_a_page_or_frame_is_refused(self, tmp_path, dest):
        response = get(
            make_page(tmp_path),
            "/doctor",
            **{"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Dest": dest},
        )
        assert response.status == 403

    @pytest.mark.parametrize(
        "headers",
        [
            {"Sec-Fetch-Site": "none", "Sec-Fetch-Dest": "document"},  # typed or bookmarked
            {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Dest": "document"},  # its own links
            {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Dest": "iframe"},  # Home Assistant
            {},  # curl, an older browser
        ],
    )
    def test_a_visit_or_the_ingress_frame_is_answered(self, tmp_path, headers):
        assert get(make_page(tmp_path, diagnose=lambda: []), "/doctor", **headers).status == 200


class TestConfigErrors:
    SECRET = "ACCESS-CODE-SECRET-12345678"

    def test_a_broken_line_is_never_shown_since_it_can_hold_a_secret(self, tmp_path):
        page = make_page(tmp_path, extra=f"access_code {self.SECRET}\n")
        body = text_of(get(page))
        assert self.SECRET not in body
        assert "The config file has an error" in body
        assert "printlog-bridge doctor" in body

    def test_nor_on_the_checks_page(self, tmp_path):
        def diagnose():
            return [CheckResult.fail("Config", f"line 4: 'access_code {self.SECRET}'")]

        page = make_page(tmp_path, extra=f"access_code {self.SECRET}\n", diagnose=diagnose)
        body = text_of(doctor(page))
        assert self.SECRET not in body
        assert "The config file has an error" in body


class TestServer:
    def test_serves_the_page_from_its_own_thread_and_stops(self, tmp_path):
        page = make_page(tmp_path)
        assert page.start()
        try:
            port = page.port
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                assert response.status == 200
                assert __version__ in response.read().decode()
            with pytest.raises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(
                    urllib.request.Request(f"http://127.0.0.1:{port}/", method="POST"), timeout=5
                )
            assert refused.value.code == 405
        finally:
            page.stop()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_a_slow_client_lets_go_of_its_slot_at_the_deadline(self, tmp_path):
        page = make_page(tmp_path)
        page.request_deadline = 0.5
        assert page.start()
        idle = []
        try:
            port = page.port
            for _ in range(local_page.MAX_REQUESTS + 2):
                s = socket.create_connection(("127.0.0.1", port), timeout=5)
                s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n")  # and never the end
                idle.append(s)
            time.sleep(1.5)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                assert response.status == 200
        finally:
            for s in idle:
                s.close()
            page.stop()

    def test_a_peer_not_on_the_list_never_takes_a_slot(self, tmp_path):
        page = make_page(tmp_path, clients=("127.0.0.1",))
        assert page.start()
        idle = []
        try:
            port = page.port
            for _ in range(local_page.MAX_REQUESTS * 2):
                s = socket.socket()
                try:
                    s.bind(("127.0.0.2", 0))
                except OSError:
                    s.close()
                    pytest.skip("can't send from 127.0.0.2 here")
                s.settimeout(5)
                s.connect(("127.0.0.1", port))
                idle.append(s)
            # Refused before anything is read: closed at once, with no answer.
            assert idle[0].recv(100) == b""
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                assert response.status == 200
        finally:
            for s in idle:
                s.close()
            page.stop()

    def test_a_port_in_use_is_logged_and_the_bridge_carries_on(self, tmp_path, caplog):
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        try:
            page = make_page(tmp_path)
            page.address = PageAddress(LOCAL, taken.getsockname()[1])
            with caplog.at_level(logging.WARNING):
                assert page.start() is False
            assert "local page" in caplog.text
        finally:
            taken.close()


def test_status_json_is_never_served(tmp_path):
    start_board(tmp_path)
    for path in ("/status.json", "/state/status.json", "/?raw=1"):
        response = get(make_page(tmp_path), path)
        assert response.status in (200, 404)
        assert not text_of(response).lstrip().startswith("{")


def test_the_module_never_imports_an_adapter():
    source = Path(local_page.__file__).read_text(encoding="utf-8")
    assert "printlog_bridge.adapters" not in source
