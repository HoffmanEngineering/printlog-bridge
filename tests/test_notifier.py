"""#10: a Moonraker notifier still logging prints to 3D Print Log is found and named."""

from __future__ import annotations

import asyncio
import logging

import pytest
from bridge_helpers import bridge, write_config
from captures import capture
from fake_moonraker import FakeMoonraker, eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.adapters.moonraker import MoonrakerAdapter
from printlog_bridge.adapters.moonraker.notifier import (
    TOPIC,
    NotifierCheck,
    OldNotifier,
    message,
    old_notifiers,
)
from printlog_bridge.adapters.moonraker.session import MoonrakerRpcError
from printlog_bridge.core.checks import Status
from printlog_bridge.core.config import load_config
from printlog_bridge.core.status import format_status, read_status

KEY = "NOTIFIER-KEY-0123456789"
URL = f"jsons://api.3dprintlog.com/api/Moonraker/notifier?+X-Api-Key={KEY}"
BODY = '{% set payload = {"printerId": 42} %}\n{ payload|tojson }'
"""The section 3D Print Log's guide had users add, as Moonraker parses it."""
DOCUMENTED = {"url": URL, "events": ["complete", "error", "cancelled"], "body": BODY}


def answer(config, files=None):
    return {"config": config, "orig": {}, "files": files if files is not None else []}


class TestFinding:
    def test_the_real_infimech_capture_has_one(self):
        # The maintainer's own printer, set up with the guide's section before the bridge.
        found = old_notifiers(capture("infimech", "server_config"))

        assert found == [OldNotifier("notifier 3d_print_log", "moonraker.conf")]

    def test_the_real_u1_capture_has_none(self):
        assert old_notifiers(capture("u1", "server_config")) == []

    def test_the_section_name_is_whatever_the_user_called_it(self):
        found = old_notifiers(answer({"notifier printlog": DOCUMENTED}))

        assert [n.section for n in found] == ["notifier printlog"]

    @pytest.mark.parametrize(
        "url",
        [
            "json://api.3dprintlog.com/api/Moonraker/notifier",
            "jsons://API.3DPrintLog.com/api/Moonraker/notifier",
            "jsons://3dprintlog.com/api/Moonraker/notifier",
            "jsons://user:pass@api.3dprintlog.com:443/x",
            f"tgram://123:abc/456\n{URL}",
            f"tgram://123:abc/456, {URL}",
        ],
    )
    def test_any_url_to_3d_print_log_counts(self, url):
        assert old_notifiers(answer({"notifier p": {"url": url}})) != []

    def test_a_list_of_urls_counts(self):
        assert old_notifiers(answer({"notifier p": {"url": ["tgram://1:a/2", URL]}})) != []

    @pytest.mark.parametrize(
        "url",
        [
            "tgram://123:abc/456",
            "jsons://not3dprintlog.com/x",
            "jsons://3dprintlog.com.example.net/x",
            "jsons://example.net/3dprintlog.com",
            "jsons://example.net/?to=api.3dprintlog.com",
            "jsons://[::1/x",  # urlsplit refuses it
            "",
        ],
    )
    def test_other_notifiers_are_left_alone(self, url):
        assert old_notifiers(answer({"notifier other": {"url": url}})) == []

    def test_only_notifier_sections_count(self):
        # Spoolman pointed at 3D Print Log is a different thing, which the bridge works with.
        config = {
            "spoolman": {"server": "https://api.3dprintlog.com/spoolman"},
            "notifierx": {"url": URL},
            "notifier": {},
        }

        assert old_notifiers(answer(config)) == []

    def test_the_file_comes_from_moonrakers_list(self):
        files = [
            {"filename": "moonraker.conf", "sections": ["server"]},
            {"filename": "includes/notify.conf", "sections": ["notifier p"]},
        ]

        [found] = old_notifiers(answer({"notifier p": DOCUMENTED}, files))

        assert found.file == "includes/notify.conf"

    @pytest.mark.parametrize("files", [None, "x", [None, {"filename": 3}, {"sections": None}]])
    def test_without_a_usable_list_it_is_moonraker_conf(self, files):
        reply = {"config": {"notifier p": DOCUMENTED}, "files": files}

        [found] = old_notifiers(reply)

        assert found.file == "moonraker.conf"

    @pytest.mark.parametrize("reply", [{}, {"config": None}, {"config": {"notifier p": "x"}}])
    def test_an_odd_answer_finds_nothing(self, reply):
        assert old_notifiers(reply) == []


class TestMessage:
    def test_names_the_section_and_its_file_and_what_to_do(self):
        text = message([OldNotifier("notifier 3d_print_log", "moonraker.conf")])

        assert "[notifier 3d_print_log] in moonraker.conf also logs prints" in text
        assert "remove that section" in text
        assert "restart Moonraker" in text

    def test_names_every_section(self):
        text = message(
            [
                OldNotifier("notifier a", "moonraker.conf"),
                OldNotifier("notifier b", "moonraker.conf"),
                OldNotifier("notifier c", "notify.conf"),
            ]
        )

        assert (
            "[notifier a] in moonraker.conf, [notifier b] in moonraker.conf and "
            "[notifier c] in notify.conf also log prints"
        ) in text
        assert "remove each of those sections" in text


class Report:
    def __init__(self) -> None:
        self.warnings: dict[str, str] = {}

    def warn(self, topic: str, message: str | None) -> None:
        if message is None:
            self.warnings.pop(topic, None)
        else:
            self.warnings[topic] = message


class TestCheck:
    def run(self, check: NotifierCheck, result) -> None:
        async def call(method, params=None):
            assert method == "server.config"
            if isinstance(result, BaseException):
                raise result
            return result

        asyncio.run(check.on_connected(call))

    def test_warns_and_logs_once_then_clears_when_it_is_gone(self, caplog):
        report = Report()
        check = NotifierCheck("voron", report)
        caplog.set_level(logging.INFO)

        self.run(check, answer({"notifier p": DOCUMENTED}))
        self.run(check, answer({"notifier p": DOCUMENTED}))  # a reconnect
        warned = dict(report.warnings)
        self.run(check, answer({}))  # removed, and Moonraker restarted

        assert "[notifier p] in moonraker.conf" in warned[TOPIC]
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
        assert report.warnings == {}
        assert "no longer has a notifier" in caplog.text
        assert KEY not in caplog.text

    @pytest.mark.parametrize(
        "error", [MoonrakerRpcError(-32601, "Method not found"), asyncio.TimeoutError()]
    )
    def test_a_config_that_cant_be_read_changes_nothing(self, error):
        report = Report()
        check = NotifierCheck("voron", report)
        self.run(check, answer({"notifier p": DOCUMENTED}))

        self.run(check, error)

        assert TOPIC in report.warnings


def test_the_running_bridge_shows_it_in_status_and_clears_it_after_a_restart(tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def scenario():
        api = FakePrintLogApi()
        moonraker = FakeMoonraker()
        moonraker.config = {"server": {}, "notifier 3d_print_log": DOCUMENTED}
        moonraker.config_files = [
            {"filename": "moonraker.conf", "sections": ["server", "notifier 3d_print_log"]}
        ]
        path = write_config(tmp_path, await moonraker.start(), await api.start())

        def warnings():
            status = read_status(tmp_path / "state")
            return status["printers"]["voron"]["warnings"] if status else {}

        try:
            async with bridge(
                path,
                connected=lambda: moonraker.client_count == 1,
                disconnected=lambda: moonraker.client_count == 0,
            ):
                await eventually(lambda: TOPIC in warnings())
                config = load_config(path, adapters=ADAPTERS, env={}, home=tmp_path)
                shown, _ = format_status(config)
                status_file = (tmp_path / "state" / "status.json").read_text(encoding="utf-8")

                del moonraker.config["notifier 3d_print_log"]  # removed by the user
                await moonraker.drop()  # and Moonraker restarted
                await eventually(lambda: TOPIC not in warnings())
            return api, shown, status_file
        finally:
            await moonraker.close()
            await api.close()

    api, shown, status_file = asyncio.run(scenario())

    assert "[notifier 3d_print_log] in moonraker.conf also logs prints" in shown
    for where in (shown, status_file, caplog.text):
        assert KEY not in where
        assert "api/Moonraker/notifier" not in where
    assert "[printer voron] Moonraker's [notifier 3d_print_log]" in caplog.text
    assert api.prints == {}  # nothing about it reaches 3D Print Log


def check_against(fake: FakeMoonraker):
    async def scenario():
        base = await fake.start()
        try:
            return await MoonrakerAdapter.from_config("u1", {"url": base}).check()
        finally:
            await fake.close()

    return asyncio.run(scenario())


def test_doctor_warns_about_it():
    fake = FakeMoonraker()
    fake.config = {"notifier 3d_print_log": DOCUMENTED}

    reachable, notifier = check_against(fake)

    assert reachable.status is Status.OK
    assert notifier.status is Status.WARN
    assert notifier.name == "Printer u1 notifier"
    assert "[notifier 3d_print_log] in moonraker.conf" in notifier.detail
    assert KEY not in notifier.detail


def test_doctor_says_nothing_without_one():
    fake = FakeMoonraker()
    fake.config = {"notifier telegram": {"url": "tgram://1:a/2"}}

    [result] = check_against(fake)

    assert result.status is Status.OK
