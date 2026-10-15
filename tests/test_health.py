from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from printlog_bridge.core.health import MAX_PROBLEM_CHARS, ProblemLog, scrub_problem

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
PRINTERS = ("voron", "u1")


class TestScrubProblem:
    def scrub(self, message: str, secrets=()) -> str:
        return scrub_problem(message, PRINTERS, secrets)

    def test_drops_the_printer_prefix(self):
        assert self.scrub("[printer voron] couldn't subscribe") == "couldn't subscribe"

    @pytest.mark.parametrize(
        "message, gone",
        [
            ("Moonraker at http://***@u1.local:7125: closed", "u1.local"),
            ("Moonraker at ws://192.168.1.40:7125/websocket didn't answer", "192.168.1.40"),
            ("can't reach 10.0.0.7 now", "10.0.0.7"),
            ("can't reach [fe80::1ff:fe23:4567:890a]:7125", "fe80::1ff:fe23:4567:890a"),
            ("can't reach fd12:3456:789a::1 now", "fd12:3456:789a::1"),
            ("Cannot connect to host lava:7125 ssl:default", "lava"),
            ("Moonraker at /home/pi/printer_data/comms/moonraker.sock closed", "/home/pi"),
            ("couldn't remove C:\\Users\\maker\\state\\x.png: busy", "maker"),
            ("ignoring a thumbnail that isn't a picture: 'my benchy v2.gcode'", "benchy"),
            ('the file "Calibration Cube" is gone', "Calibration Cube"),
            ("job 00001A's thumbnail: benchy.gcode is gone", "benchy"),
            ("3D Print Log at api.3dprintlog.com answered 503", "3dprintlog"),
            ("a spool for printer voron isn't known", "voron"),
        ],
    )
    def test_removes_hosts_addresses_paths_and_file_names(self, message, gone):
        assert gone not in self.scrub(message)

    def test_removes_known_secrets_and_passwords_in_urls(self):
        scrubbed = self.scrub(
            "rejected plk-SECRET-1 for user:hunter2@printer", secrets=["plk-SECRET-1"]
        )
        assert "plk-SECRET-1" not in scrubbed
        assert "hunter2" not in scrubbed

    def test_keeps_what_went_wrong(self):
        scrubbed = self.scrub(
            "[printer voron] Moonraker at http://u1.local:7125: Cannot connect to host "
            "u1.local:7125 ssl:default [Connection refused]"
        )
        assert scrubbed.startswith("Moonraker at <url>: Cannot connect to host <host>")
        assert "[Connection refused]" in scrubbed

    def test_keeps_version_numbers_and_status_codes(self):
        assert self.scrub("the API answered 503 (v1.2.3)") == "the API answered 503 (v1.2.3)"

    def test_is_cut_to_a_limit(self):
        scrubbed = self.scrub("x" * 1000)
        assert len(scrubbed) == MAX_PROBLEM_CHARS
        assert scrubbed.endswith("…")


def record(message: str, *args, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord("printlog_bridge.test", level, __file__, 1, message, args, None)


class TestProblemLog:
    def log(self, **kwargs) -> ProblemLog:
        return ProblemLog(PRINTERS, clock=lambda: NOW, **kwargs)

    def test_counts_each_printers_warnings_and_errors(self):
        problems = self.log()
        problems.handle(record("[printer %s] couldn't subscribe: %s", "voron", "gone"))
        problems.handle(record("[printer %s] job lost", "voron", level=logging.ERROR))
        problems.handle(record("[printer %s] reconnecting", "u1", level=logging.INFO))

        voron = problems.problems("voron")
        assert voron.count == 2
        assert voron.last == "job lost"
        assert voron.last_at == NOW
        assert problems.problems("u1").count == 0
        assert problems.problems("u1").last is None

    def test_a_message_naming_its_printer_in_an_argument_is_that_printers(self):
        problems = self.log()
        problems.handle(record("%s wasn't added to its print", "[printer u1] job 0001's snapshot"))

        assert problems.problems("u1").count == 1
        assert problems.problems("voron").count == 0

    def test_a_problem_naming_no_printer_is_every_printers(self):
        problems = self.log()
        problems.handle(record("couldn't write %s (%s)", "/data/state/status.json", "disk full"))

        for printer in PRINTERS:
            assert problems.problems(printer).count == 1
            assert problems.problems(printer).last == "couldn't write <path> (disk full)"

    def test_the_last_problem_is_scrubbed(self):
        problems = self.log(secrets=["plk-SECRET-1"])
        problems.handle(
            record("[printer %s] key %s refused by %s", "voron", "plk-SECRET-1", "http://x:1")
        )

        assert problems.problems("voron").last == "key <redacted> refused by <url>"

    def test_reported_takes_off_only_what_was_sent(self):
        clock = [NOW]
        problems = ProblemLog(PRINTERS, clock=lambda: clock[0])
        problems.handle(record("[printer %s] one", "voron"))
        problems.handle(record("[printer %s] two", "voron"))
        sent = problems.problems("voron")
        clock[0] = NOW + timedelta(minutes=1)
        problems.handle(record("[printer %s] three", "voron"))  # while the check-in was sent

        problems.reported("voron", sent.count)

        after = problems.problems("voron")
        assert after.count == 1
        assert after.last == "three"
        assert after.last_at == NOW + timedelta(minutes=1)

    def test_the_last_problem_outlives_the_check_in(self):
        problems = self.log()
        problems.handle(record("[printer %s] one", "voron"))
        problems.reported("voron", 1)

        assert problems.problems("voron").count == 0
        assert problems.problems("voron").last == "one"

    def test_a_printer_it_does_not_know_has_no_problems(self):
        assert self.log().problems("nope").count == 0
