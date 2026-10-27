"""`trace = true`: every Moonraker message, per printer, in `state_dir/traces` (#17)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from fake_moonraker import FakeMoonraker, eventually
from session_helpers import Recorder, running

from printlog_bridge.adapters.moonraker.session import MoonrakerSession
from printlog_bridge.core.trace import TRACE_DIR, Trace, trace_files

T0 = datetime(2026, 10, 12, 9, 30).timestamp()  # local time, as the file names are


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def recorded(tmp_path: Path, *messages: tuple[str, str], printer: str = "u1", **options) -> Trace:
    trace = Trace(tmp_path, printer, clock=lambda: T0, **options)
    for direction, raw in messages:
        trace.record(direction, raw)
    trace.close()
    return trace


class TestRecording:
    def test_each_message_is_a_line_with_its_time_and_direction(self, tmp_path):
        recorded(
            tmp_path,
            ("send", '{"jsonrpc": "2.0", "method": "server.info", "id": 1}'),
            ("recv", '{"jsonrpc": "2.0", "result": {"klippy_state": "ready"}, "id": 1}'),
        )

        [path] = trace_files(tmp_path, "u1")
        assert path == tmp_path / TRACE_DIR / "u1-20261012.jsonl"
        assert lines(path) == [
            {"t": T0, "dir": "send", "msg": {"jsonrpc": "2.0", "method": "server.info", "id": 1}},
            {
                "t": T0,
                "dir": "recv",
                "msg": {"jsonrpc": "2.0", "result": {"klippy_state": "ready"}, "id": 1},
            },
        ]

    def test_connections_opening_and_closing_are_marked(self, tmp_path):
        trace = Trace(tmp_path, "u1", clock=lambda: T0)
        trace.mark("open")
        trace.mark("close")
        trace.close()

        assert [line["dir"] for line in lines(trace_files(tmp_path, "u1")[0])] == ["open", "close"]

    def test_a_message_split_over_lines_stays_one_line(self, tmp_path):
        recorded(tmp_path, ("recv", '{"jsonrpc": "2.0",\n "result": "ok", "id": 3}'))

        assert lines(trace_files(tmp_path, "u1")[0])[0]["msg"]["result"] == "ok"

    def test_a_message_that_is_not_json_is_kept_as_text(self, tmp_path):
        recorded(tmp_path, ("recv", "not json"))

        assert lines(trace_files(tmp_path, "u1")[0])[0]["text"] == "not json"

    def test_each_printer_has_its_own_files_with_a_safe_name(self, tmp_path):
        recorded(tmp_path, ("recv", "{}"), printer="u1")
        recorded(tmp_path, ("recv", "{}"), printer="../my printer")

        names = sorted(p.name for p in (tmp_path / TRACE_DIR).iterdir())
        assert names == [".._my_printer-20261012.jsonl", "u1-20261012.jsonl"]

    def test_a_restart_appends_to_the_days_file(self, tmp_path):
        recorded(tmp_path, ("recv", '{"id": 1}'))
        recorded(tmp_path, ("recv", '{"id": 2}'))

        [path] = trace_files(tmp_path, "u1")
        assert [line["msg"]["id"] for line in lines(path)] == [1, 2]

    def test_a_new_day_starts_a_new_file(self, tmp_path):
        now = [T0]
        trace = Trace(tmp_path, "u1", clock=lambda: now[0])
        trace.record("recv", '{"id": 1}')
        trace.flush()
        now[0] += 24 * 3600
        trace.record("recv", '{"id": 2}')
        trace.close()

        assert [p.name for p in trace_files(tmp_path, "u1")] == [
            "u1-20261012.jsonl",
            "u1-20261013.jsonl",
        ]


class TestSecrets:
    def test_known_secrets_are_removed(self, tmp_path):
        recorded(
            tmp_path,
            ("send", '{"method": "x", "params": {"note": "key plk-123456 here"}}'),
            secrets=["plk-123456"],
        )

        text = trace_files(tmp_path, "u1")[0].read_text(encoding="utf-8")
        assert "plk-123456" not in text
        assert "<redacted>" in text

    def test_any_field_named_like_a_key_token_or_password_is_removed(self, tmp_path):
        recorded(
            tmp_path,
            (
                "send",
                '{"method": "server.connection.identify", "params": {"client_name": "b", '
                '"api_key": "mrk-unknown", "access_token": "tok-1", "password": "pw-2"}}',
            ),
        )

        params = lines(trace_files(tmp_path, "u1")[0])[0]["msg"]["params"]
        assert params == {
            "client_name": "b",
            "api_key": "<redacted>",
            "access_token": "<redacted>",
            "password": "<redacted>",
        }

    def test_passwords_and_tokens_in_urls_are_removed(self, tmp_path):
        recorded(
            tmp_path,
            (
                "recv",
                '{"result": {"webcams": [{"snapshot_url": '
                '"http://cam:hunter2@cam.local/snap?action=snapshot&token=camtok"}]}, "id": 4}',
            ),
        )

        [webcam] = lines(trace_files(tmp_path, "u1")[0])[0]["msg"]["result"]["webcams"]
        assert webcam["snapshot_url"] == (
            "http://<redacted>@cam.local/snap?action=snapshot&token=<redacted>"
        )

    def test_a_oneshot_token_or_api_key_answer_is_removed(self, tmp_path):
        recorded(
            tmp_path,
            ("send", '{"jsonrpc": "2.0", "method": "access.oneshot_token", "id": 7}'),
            ("send", '{"jsonrpc": "2.0", "method": "access.get_api_key", "id": 8}'),
            ("recv", '{"jsonrpc": "2.0", "result": "ONESHOT1234", "id": 7}'),
            ("recv", '{"jsonrpc": "2.0", "result": "APIKEY5678", "id": 8}'),
        )

        text = trace_files(tmp_path, "u1")[0].read_text(encoding="utf-8")
        assert "ONESHOT1234" not in text and "APIKEY5678" not in text
        assert [line["msg"].get("result") for line in lines(trace_files(tmp_path, "u1")[0])][
            2:
        ] == ["<redacted>", "<redacted>"]

    def test_moonrakers_config_answer_is_removed(self, tmp_path):
        # An Apprise URL holds its token in the host or path, where no scrubber looks for one.
        recorded(
            tmp_path,
            ("send", '{"jsonrpc": "2.0", "method": "server.config", "id": 9}'),
            (
                "recv",
                '{"jsonrpc": "2.0", "result": {"config": {"notifier telegram": '
                '{"url": "tgram://123:SECRET/456", "events": "*"}}}, "id": 9}',
            ),
        )

        text = trace_files(tmp_path, "u1")[0].read_text(encoding="utf-8")
        assert "SECRET" not in text
        assert lines(trace_files(tmp_path, "u1")[0])[1]["msg"]["result"] == "<redacted>"


class TestLimits:
    def test_files_are_split_and_the_oldest_removed_to_stay_under_the_cap(self, tmp_path):
        message = '{"pad": "' + "x" * 100 + '"}'
        trace = Trace(tmp_path, "u1", clock=lambda: T0, file_bytes=1000, max_bytes=3000)
        for _ in range(100):  # about 14 KB
            trace.record("recv", message)
        trace.close()

        files = trace_files(tmp_path, "u1")
        assert sum(p.stat().st_size for p in files) <= 3000
        assert all(p.stat().st_size <= 1000 for p in files)
        assert files[-1].name.startswith("u1-20261012.")  # the newest part is kept
        assert len(files) >= 2

    def test_other_printers_files_are_left_alone(self, tmp_path):
        recorded(tmp_path, ("recv", "{}"), printer="voron")
        message = '{"pad": "' + "x" * 100 + '"}'
        recorded(tmp_path, *[("recv", message)] * 50, file_bytes=500, max_bytes=1000)

        assert len(trace_files(tmp_path, "voron")) == 1

    def test_recording_never_waits_for_the_disk(self, tmp_path):
        stalled = threading.Event()

        class SlowDisk(Trace):
            def _write(self, line: str, t: float) -> None:
                stalled.wait(5)
                super()._write(line, t)

        trace = SlowDisk(tmp_path, "u1", clock=lambda: T0, queue_limit=3)
        started = time.monotonic()
        for n in range(100):
            trace.record("recv", f'{{"id": {n}}}')
        took = time.monotonic() - started
        stalled.set()
        trace.close()

        assert took < 0.5
        assert trace.dropped > 0  # what didn't fit while the disk was stuck is dropped

    def test_a_disk_error_is_logged_once_and_never_raised(self, tmp_path, caplog):
        (tmp_path / TRACE_DIR).write_text("a file where the folder should be", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
            recorded(tmp_path, *[("recv", "{}")] * 5)

        warnings = [r for r in caplog.records if "trace" in r.getMessage()]
        assert len(warnings) == 1

    def test_close_twice_is_harmless(self, tmp_path):
        trace = recorded(tmp_path, ("recv", "{}"))
        trace.close()
        trace.record("recv", "{}")  # after close: ignored


def test_the_session_records_what_it_sends_and_receives(tmp_path):
    moonraker = FakeMoonraker(api_key="mrk-SECRET-0001")
    trace = Trace(tmp_path, "voron", clock=lambda: T0, secrets=["mrk-SECRET-0001"])

    async def scenario():
        url = await moonraker.start()
        session = MoonrakerSession(
            "voron", listener=Recorder(), url=url, api_key="mrk-SECRET-0001", trace=trace
        )
        try:
            async with running(session):
                await eventually(lambda: moonraker.subscriptions)
                await moonraker.respond("hello")
                await asyncio.sleep(0.1)
        finally:
            await moonraker.close()

    asyncio.run(scenario())
    trace.close()

    [path] = trace_files(tmp_path, "voron")
    assert "mrk-SECRET-0001" not in path.read_text(encoding="utf-8")
    found = lines(path)
    assert found[0]["dir"] == "open"
    sent = [line["msg"]["method"] for line in found if line["dir"] == "send"]
    assert sent[:2] == ["server.connection.identify", "server.history.list"]
    assert "printer.objects.subscribe" in sent
    received = [line["msg"] for line in found if line["dir"] == "recv"]
    assert {"jsonrpc": "2.0", "method": "notify_gcode_response", "params": ["hello"]} in received
    assert found[-1]["dir"] == "close"


@pytest.mark.skipif(not hasattr(asyncio, "open_unix_connection"), reason="no Unix sockets")
def test_the_session_records_over_the_unix_socket_too(tmp_path):
    import tempfile

    moonraker = FakeMoonraker()
    trace = Trace(tmp_path, "voron", clock=lambda: T0)
    sock = Path(tempfile.mkdtemp()) / "m.sock"  # short: Unix socket paths are limited

    async def scenario():
        await moonraker.start_unix(str(sock))
        session = MoonrakerSession("voron", listener=Recorder(), socket=sock, trace=trace)
        try:
            async with running(session):
                await eventually(lambda: moonraker.subscriptions)
        finally:
            await moonraker.close()

    asyncio.run(scenario())
    trace.close()

    found = lines(trace_files(tmp_path, "voron")[0])
    assert any(line["dir"] == "recv" for line in found)
    assert any(line["dir"] == "send" for line in found)


def test_a_field_named_access_code_is_removed(tmp_path):
    recorded(tmp_path, ("recv", '{"system": {"command": "get_access_code", "access_code": "x1"}}'))

    assert lines(trace_files(tmp_path, "u1")[0])[0]["msg"]["system"]["access_code"] == "<redacted>"
