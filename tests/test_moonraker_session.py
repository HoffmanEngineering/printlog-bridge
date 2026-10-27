from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fake_moonraker import FakeMoonraker, eventually
from session_helpers import Recorder, ReportRecorder, running

from printlog_bridge import __version__
from printlog_bridge.adapters.moonraker.session import (
    Backoff,
    MoonrakerSession,
    _JsonRpc,
    bounded,
    websocket_url,
)

FAST = Backoff(initial=0.01, maximum=0.05)


@contextlib.asynccontextmanager
async def fake_moonraker(**kwargs):
    fake = FakeMoonraker(**kwargs)
    base = await fake.start()
    try:
        yield fake, base
    finally:
        await fake.close()


def session_for(base: str, listener: Recorder, **kwargs) -> MoonrakerSession:
    kwargs.setdefault("backoff", FAST)
    return MoonrakerSession("u1", url=base, listener=listener, **kwargs)


def test_identifies_as_an_agent_so_moonraker_lists_the_bridge():
    async def scenario():
        async with fake_moonraker() as (fake, base):
            async with running(session_for(base, Recorder())):
                await eventually(lambda: fake.identified)
            return fake.identified[0]

    identity = asyncio.run(scenario())

    assert identity["client_name"] == "printlog-bridge"
    assert identity["type"] == "agent"
    assert identity["version"] == __version__
    assert identity["url"] == "https://github.com/HoffmanEngineering/printlog-bridge"


def test_subscribes_to_the_objects_a_job_needs():
    async def scenario():
        async with fake_moonraker() as (fake, base):
            async with running(session_for(base, Recorder())):
                await eventually(lambda: fake.subscriptions)
            return fake.subscriptions[0]

    objects = asyncio.run(scenario())

    assert objects == {
        "print_stats": None,
        "toolhead": ["extruder", "position"],
        "virtual_sdcard": None,
        "webhooks": None,
    }


def test_subscribes_to_every_runout_sensor():
    async def scenario():
        fake_objects = ("webhooks", "print_stats", "toolhead", "virtual_sdcard",
                        "filament_switch_sensor runout", "filament_motion_sensor encoder",
                        "gcode_macro M600")  # fmt: skip
        async with fake_moonraker(objects=fake_objects) as (fake, base):
            async with running(session_for(base, Recorder())):
                await eventually(lambda: fake.subscriptions)
            return fake.subscriptions[0]

    objects = asyncio.run(scenario())

    assert objects["filament_switch_sensor runout"] == ["filament_detected", "enabled"]
    assert objects["filament_motion_sensor encoder"] == ["filament_detected", "enabled"]
    assert "gcode_macro M600" not in objects


def test_subscribes_to_filament_detect_when_the_printer_has_it():
    async def scenario():
        fake_objects = ("webhooks", "print_stats", "toolhead", "virtual_sdcard", "filament_detect")
        async with fake_moonraker(objects=fake_objects) as (fake, base):
            async with running(session_for(base, Recorder())):
                await eventually(lambda: fake.subscriptions)
            return fake.subscriptions[0]

    objects = asyncio.run(scenario())

    assert objects["filament_detect"] is None


def test_reports_the_initial_status_then_merges_each_update():
    async def scenario():
        recorder = Recorder()
        async with fake_moonraker() as (fake, base), running(session_for(base, recorder)):
            await eventually(lambda: recorder.status)
            initial = recorder.status
            await fake.notify(
                "notify_status_update",
                {"print_stats": {"state": "printing", "filename": "benchy.gcode"}},
                12.5,
            )
            await eventually(lambda: len(recorder.changes) >= 2)
        return initial, recorder

    initial, recorder = asyncio.run(scenario())

    assert initial["print_stats"] == {"state": "standby", "filename": ""}
    assert recorder.klippy == ["ready"]
    assert recorder.changes[-1] == {
        "print_stats": {"state": "printing", "filename": "benchy.gcode"}
    }
    assert recorder.status["print_stats"] == {"state": "printing", "filename": "benchy.gcode"}
    assert recorder.status["toolhead"] == {"extruder": "extruder"}


def test_passes_history_notifications_on():
    async def scenario():
        recorder = Recorder()
        async with fake_moonraker() as (fake, base), running(session_for(base, recorder)):
            await eventually(lambda: recorder.status)
            job = {"job_id": "000042", "filename": "benchy.gcode", "status": "completed"}
            await fake.notify("notify_history_changed", {"action": "finished", "job": job})
            await eventually(lambda: recorder.history)
        return recorder.history

    history = asyncio.run(scenario())

    assert history == [
        ("finished", {"job_id": "000042", "filename": "benchy.gcode", "status": "completed"})
    ]


def test_tells_the_listener_each_time_it_connects_even_without_klipper():
    """So it can read job history, which Moonraker keeps whether or not Klipper is up."""

    async def scenario():
        recorder = Recorder()
        down = {"klippy_state": "disconnected"}
        async with fake_moonraker(**down) as (fake, base), running(session_for(base, recorder)):
            await eventually(lambda: recorder.connected)
            await fake.drop()
            await eventually(lambda: len(recorder.connected) == 2)
        return recorder

    recorder = asyncio.run(scenario())

    assert recorder.connected == [{"count": 0, "jobs": []}] * 2


def test_reconnects_after_moonraker_drops_and_sets_everything_up_again():
    async def scenario():
        async def on_set_spool(params):  # pragma: no cover - only registered here
            pass

        async with fake_moonraker() as (fake, base):
            session = session_for(base, Recorder(), remote_methods={"set_spool": on_set_spool})
            async with running(session):
                await eventually(lambda: fake.subscriptions)
                await fake.drop()
                await eventually(lambda: len(fake.subscriptions) >= 2)
            return fake

    fake = asyncio.run(scenario())

    assert len(fake.identified) == 2
    assert fake.registered == ["set_spool", "set_spool"]


def test_keeps_trying_while_moonraker_is_down():
    async def scenario():
        fake = FakeMoonraker()
        base = await fake.start()
        port_url = base
        await fake.close()  # nothing listening now

        recorder = Recorder()
        session = MoonrakerSession("u1", url=port_url, listener=recorder, backoff=FAST)
        async with running(session) as task:
            await asyncio.sleep(0.2)
            assert not task.done()

    asyncio.run(scenario())


def test_waits_for_klippy_before_subscribing():
    async def scenario():
        recorder = Recorder()
        async with fake_moonraker(klippy_state="startup") as (fake, base):
            async with running(session_for(base, recorder)):
                await eventually(lambda: fake.identified)
                await eventually(lambda: recorder.klippy)
                assert fake.subscriptions == []

                fake.klippy_state = "ready"
                await fake.notify("notify_klippy_ready")
                await eventually(lambda: len(recorder.klippy) >= 2)
            return fake, recorder

    fake, recorder = asyncio.run(scenario())

    assert len(fake.subscriptions) == 1
    assert recorder.klippy == ["startup", "ready"]


def test_resubscribes_after_a_klipper_restart():
    async def scenario():
        recorder = Recorder()
        async with fake_moonraker() as (fake, base), running(session_for(base, recorder)):
            await eventually(lambda: fake.subscriptions)

            fake.klippy_state = "disconnected"
            await fake.notify("notify_klippy_disconnected")
            await eventually(lambda: len(recorder.klippy) >= 2)

            fake.klippy_state = "ready"
            await fake.notify("notify_klippy_ready")
            await eventually(lambda: len(fake.subscriptions) >= 2)
            await eventually(lambda: len(recorder.klippy) >= 3)
        return recorder

    recorder = asyncio.run(scenario())

    assert recorder.klippy == ["ready", "disconnected", "ready"]


def test_registers_remote_methods_and_calls_them():
    async def scenario():
        calls: list[Mapping[str, Any]] = []

        async def set_spool(params):
            calls.append(params)

        async with fake_moonraker() as (fake, base):
            session = session_for(
                base, Recorder(), remote_methods={"printlog_set_spool": set_spool}
            )
            async with running(session):
                await eventually(lambda: fake.registered)
                await fake.call_remote_method("printlog_set_spool", {"tool": 1, "spool": 7})
                await eventually(lambda: calls)
            return fake.registered, calls

    registered, calls = asyncio.run(scenario())

    assert registered == ["printlog_set_spool"]
    assert calls == [{"tool": 1, "spool": 7}]


def test_passes_console_lines_to_the_listener():
    async def scenario():
        recorder = Recorder()
        async with fake_moonraker() as (fake, base), running(session_for(base, recorder)):
            await eventually(lambda: recorder.klippy)
            await fake.respond("// printlog: SET_SPOOL TOOL=1 TAG=04A23F11")
            await eventually(lambda: recorder.console)
        return recorder.console

    assert asyncio.run(scenario()) == ["// printlog: SET_SPOOL TOOL=1 TAG=04A23F11"]


def test_sends_the_moonraker_api_key():
    async def scenario():
        async with fake_moonraker(api_key="secret") as (fake, base):
            async with running(session_for(base, Recorder(), api_key="secret")):
                await eventually(lambda: fake.subscriptions)
            return fake

    fake = asyncio.run(scenario())

    assert fake.upgrade_headers[0]["X-Api-Key"] == "secret"
    assert fake.identified[0]["api_key"] == "secret"


@pytest.mark.parametrize("api_key", [None, "wrong"])
def test_says_what_to_do_when_moonraker_refuses_the_bridge(caplog, api_key):
    async def scenario():
        async with fake_moonraker(api_key="secret") as (fake, base):
            async with running(session_for(base, Recorder(), api_key=api_key)) as task:
                await eventually(lambda: any("rejected" in r.getMessage() for r in caplog.records))
                assert not task.done()  # keeps retrying; the key may be fixed on the printer
            return fake

    with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
        fake = asyncio.run(scenario())

    [message, *_] = [r.getMessage() for r in caplog.records if "rejected" in r.getMessage()]
    assert "[printer u1]" in message
    assert "moonraker_api_key" in message
    assert "trusted_clients" in message
    assert fake.subscriptions == []


def test_says_what_to_do_when_a_proxy_refuses_the_upgrade(caplog):
    async def scenario():
        async with fake_moonraker(reject_upgrade=True) as (fake, base):
            session = session_for(base, Recorder())
            async with running(session):
                await eventually(lambda: any("rejected" in r.getMessage() for r in caplog.records))

    with caplog.at_level(logging.ERROR, logger="printlog_bridge"):
        asyncio.run(scenario())

    [message, *_] = [r.getMessage() for r in caplog.records if "rejected" in r.getMessage()]
    assert "HTTP 401" in message


def test_says_so_when_another_bridge_already_has_the_printer(caplog):
    async def scenario():
        async with fake_moonraker() as (fake, base):
            first = session_for(base, Recorder())
            async with running(first) as first_task:
                await eventually(lambda: fake.subscriptions)
                second = Recorder()
                async with running(session_for(base, second)):
                    await eventually(
                        lambda: any("already" in r.getMessage() for r in caplog.records)
                    )
                    first_task.cancel()  # the other bridge goes away...
                    await eventually(lambda: second.status)  # ...and this one takes over

    with caplog.at_level(logging.WARNING, logger="printlog_bridge"):
        asyncio.run(scenario())

    [message, *_] = [r.getMessage() for r in caplog.records if "already" in r.getMessage()]
    assert "another printlog-bridge is already connected" in message
    assert "[printer u1]" in message


@pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")
def test_connects_over_the_local_unix_socket():
    async def scenario(sock: str):
        fake = FakeMoonraker()
        await fake.start_unix(sock)
        recorder = Recorder()
        try:
            session = MoonrakerSession("voron", socket=Path(sock), listener=recorder, backoff=FAST)
            async with running(session):
                await eventually(lambda: recorder.status)
                await fake.drop()
                await eventually(lambda: len(fake.identified) >= 2)
        finally:
            await fake.close()
        return fake, recorder

    with tempfile.TemporaryDirectory() as d:
        fake, recorder = asyncio.run(scenario(os.path.join(d, "m.sock")))

    assert fake.identified[0]["type"] == "agent"
    assert recorder.status["webhooks"]["state"] == "ready"


class TestBackoff:
    def test_doubles_from_one_second_up_to_a_minute(self):
        backoff = Backoff()

        delays = [backoff.next() for _ in range(9)]

        assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]

    def test_reset_starts_over(self):
        backoff = Backoff()
        backoff.next()
        backoff.next()

        backoff.reset()

        assert backoff.next() == 1


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("http://u1.local:7125", "ws://u1.local:7125/websocket"),
        ("https://u1.example", "wss://u1.example/websocket"),
        ("ws://u1.local:7125/websocket", "ws://u1.local:7125/websocket"),
        ("http://host/moonraker", "ws://host/moonraker/websocket"),
    ],
)
def test_websocket_url_from_the_configured_url(configured, expected):
    assert websocket_url(configured) == expected


def test_a_good_connection_resets_the_backoff():
    async def scenario():
        backoff = Backoff(initial=0.01, maximum=0.05)
        for _ in range(3):
            backoff.next()
        async with fake_moonraker() as (fake, base):
            session = session_for(base, Recorder(), backoff=backoff)
            async with running(session):
                await eventually(lambda: fake.subscriptions)
        return backoff

    backoff = asyncio.run(scenario())

    assert backoff.next() == 0.01


class TestStatusReport:
    def test_reports_connecting_and_klippers_state(self):
        async def scenario():
            report = ReportRecorder()
            async with (
                fake_moonraker(klippy_state="startup") as (fake, base),
                running(session_for(base, Recorder(), report=report)),
            ):
                await eventually(lambda: report.named("printer_state"))
                fake.klippy_state = "ready"
                await fake.notify("notify_klippy_ready")
                await eventually(lambda: len(report.named("printer_state")) >= 2)
                fake.klippy_state = "shutdown"
                await fake.notify("notify_klippy_shutdown")
                await eventually(lambda: len(report.named("printer_state")) >= 3)
            return report

        report = asyncio.run(scenario())

        assert report.calls[0] == ("connected",)
        assert report.named("printer_state") == [
            ("printer_state", "startup"),
            ("printer_state", "ready"),
            ("printer_state", "shutdown"),
        ]

    def test_reports_a_dropped_connection_and_the_reconnect(self):
        async def scenario():
            report = ReportRecorder()
            async with (
                fake_moonraker() as (fake, base),
                running(session_for(base, Recorder(), report=report)),
            ):
                await eventually(lambda: fake.subscriptions)
                await fake.drop()
                await eventually(lambda: len(report.named("connected")) >= 2)
            return report

        report = asyncio.run(scenario())

        [(_, reason)] = report.named("disconnected")
        assert "Moonraker" in reason
        # After the reconnect: connected, then Klipper's state and the versions (#37).
        assert report.calls[-1][0] in ("connected", "printer_state", "firmware", "host_software")

    def test_reports_moonrakers_and_klippers_versions(self):
        async def scenario():
            report = ReportRecorder()
            async with (
                fake_moonraker() as (fake, base),
                running(session_for(base, Recorder(), report=report)),
            ):
                await eventually(lambda: report.named("firmware") and report.named("host_software"))
            return report

        report = asyncio.run(scenario())

        assert report.named("firmware") == [("firmware", "Klipper", "v0.12.0-fake")]
        assert report.named("host_software") == [("host_software", "Moonraker", "v0.9.3-fake")]

    def test_reports_klippers_version_once_it_is_ready(self):
        async def scenario():
            report = ReportRecorder()
            async with (
                fake_moonraker(klippy_state="startup") as (fake, base),
                running(session_for(base, Recorder(), report=report)),
            ):
                await eventually(lambda: report.named("printer_state"))
                before = list(report.named("firmware"))
                fake.klippy_state = "ready"
                await fake.notify("notify_klippy_ready")
                await eventually(lambda: report.named("firmware"))
            return before, report

        before, report = asyncio.run(scenario())

        assert before == []
        assert report.named("host_software") == [("host_software", "Moonraker", "v0.9.3-fake")]
        assert ("firmware", "Klipper", "v0.12.0-fake") in report.named("firmware")

    def test_a_refusal_is_a_warning_saying_what_to_do(self):
        async def scenario():
            report = ReportRecorder()
            async with (
                fake_moonraker(api_key="secret") as (_fake, base),
                running(session_for(base, Recorder(), report=report)),
            ):
                await eventually(lambda: report.warnings)
            return report

        report = asyncio.run(scenario())

        assert "moonraker_api_key" in report.warnings["moonraker"]
        assert report.named("disconnected")

    def test_the_warning_clears_once_connected(self):
        async def scenario():
            async with fake_moonraker() as (fake, base):
                first = session_for(base, Recorder())
                async with running(first) as first_task:
                    await eventually(lambda: fake.subscriptions)
                    report = ReportRecorder()
                    async with running(session_for(base, Recorder(), report=report)):
                        await eventually(lambda: report.warnings)
                        during = dict(report.warnings)
                        first_task.cancel()
                        await eventually(lambda: report.named("connected"))
                        await eventually(lambda: not report.warnings)
            return during

        during = asyncio.run(scenario())

        assert "another printlog-bridge" in during["moonraker"]


class SilentTransport:
    """A connection that sends nothing back unless the test says so."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()

    async def send(self, message) -> None:
        pass

    async def receive(self):
        await self.closed.wait()
        return None

    async def close(self) -> None:
        self.closed.set()


def test_a_call_cancelled_as_its_answer_arrives_is_still_cancelled():
    """`asyncio.wait_for` before 3.12 returns the answer instead (bpo-42130), so a bridge told
    to stop mid-call would carry on, holding Moonraker's one agent slot."""

    async def scenario():
        transport = SilentTransport()
        rpc = _JsonRpc(transport)
        try:
            task = asyncio.ensure_future(rpc.call("server.info"))
            await asyncio.sleep(0)  # the call is sent and waiting for its answer
            rpc._dispatch({"jsonrpc": "2.0", "id": 1, "result": {"klippy_state": "ready"}})
            task.cancel()  # in the same loop iteration as the answer
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await rpc.close()

    asyncio.run(scenario())


def test_a_call_with_no_answer_times_out(monkeypatch):
    monkeypatch.setattr("printlog_bridge.adapters.moonraker.session.REQUEST_TIMEOUT_S", 0.05)

    async def scenario():
        rpc = _JsonRpc(SilentTransport())
        try:
            with pytest.raises(asyncio.TimeoutError):
                await rpc.call("server.info")
            assert rpc._pending == {}
        finally:
            await rpc.close()

    asyncio.run(scenario())


class TestBounded:
    def test_returns_the_result(self):
        async def answer():
            return 42

        assert asyncio.run(bounded(answer(), 5)) == 42

    def test_times_out(self):
        async def never():
            await asyncio.Event().wait()

        async def scenario():
            with pytest.raises(asyncio.TimeoutError):
                await bounded(never(), 0.05)

        asyncio.run(scenario())

    def test_a_cancel_as_it_completes_is_still_a_cancel(self):
        """Where `asyncio.wait_for` before 3.12 returns the result and drops the cancel."""

        async def scenario():
            gate = asyncio.get_running_loop().create_future()

            async def connect():
                return await gate

            task = asyncio.ensure_future(bounded(connect(), 5))
            await asyncio.sleep(0)  # connecting
            gate.set_result("connection")
            await asyncio.sleep(0)  # connected, but the caller hasn't been told yet
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
