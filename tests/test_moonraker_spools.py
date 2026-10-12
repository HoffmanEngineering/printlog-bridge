"""PRINTLOG_SET_SPOOL's console lines, and the Snapmaker U1's RFID reader, through Moonraker."""

from __future__ import annotations

import asyncio
import contextlib
import copy

import pytest
from fake_moonraker import FakeMoonraker, eventually
from session_helpers import running

from printlog_bridge.adapters.moonraker.session import Backoff, MoonrakerSession
from printlog_bridge.adapters.moonraker.spools import SpoolWatcher, parse_command
from printlog_bridge.adapters.moonraker.u1_rfid import U1RfidReader
from printlog_bridge.core.events import SpoolCommand, SpoolReading

# SYNTHETIC: no U1 was at hand. This is `filament_detect`'s status as Snapmaker's u1-klipper
# builds it (klippy/extras/filament_detect.py `get_status`, filament_protocol.py
# `FILAMENT_INFO_STRUCT` and `m1_proto_data_parse`), with made-up values: channel 0 holds a
# Snapmaker PLA spool whose tag was read and verified, channel 1 is empty, and channels 2 and 3
# hold spools without a Snapmaker tag. A real U1's matches it field for field: see
# tests/fixtures/moonraker/u1/status.json (REAL) and test_real_captures.py.
EMPTY_CHANNEL = {
    "VERSION": 0,
    "VENDOR": "NONE",
    "MANUFACTURER": "NONE",
    "MAIN_TYPE": "NONE",
    "SUB_TYPE": "NONE",
    "TRAY": 0,
    "ALPHA": 0xFF,
    "MULTI_MODE": 0,
    "COLOR_NUMS": 1,
    "ARGB_COLOR": 0xFFFFFFFF,
    "RGB_1": 0xFFFFFF,
    "RGB_2": 0xFFFFFF,
    "RGB_3": 0xFFFFFF,
    "RGB_4": 0xFFFFFF,
    "RGB_5": 0xFFFFFF,
    "DIAMETER": 0,
    "WEIGHT": 0,
    "LENGTH": 0,
    "DRYING_TEMP": 0,
    "DRYING_TIME": 0,
    "HOTEND_MAX_TEMP": 0,
    "HOTEND_MIN_TEMP": 0,
    "BED_TYPE": 0,
    "BED_TEMP": 0,
    "FIRST_LAYER_TEMP": 0,
    "OTHER_LAYER_TEMP": 0,
    "SKU": 0,
    "MF_DATE": "19700101",
    "RSA_KEY_VERSION": 0,
    "OFFICIAL": False,
    "CARD_UID": 0,
}
SNAPMAKER_PLA_RED = {
    **EMPTY_CHANNEL,
    "VERSION": 1,
    "VENDOR": "Snapmaker",
    "MANUFACTURER": "Polymaker",
    "MAIN_TYPE": "PLA",
    "SUB_TYPE": "Basic",
    "ARGB_COLOR": 0xFFE02020,
    "RGB_1": 0xE02020,
    "DIAMETER": 175,
    "WEIGHT": 1000,
    "HOTEND_MAX_TEMP": 230,
    "HOTEND_MIN_TEMP": 190,
    "SKU": 10001,
    "MF_DATE": "20260901",
    "OFFICIAL": True,
    "CARD_UID": [0x04, 0xA2, 0x3F, 0x11],  # a list of byte values: block 0's first 4 bytes
}
FILAMENT_DETECT = {
    "info": [SNAPMAKER_PLA_RED, EMPTY_CHANNEL, EMPTY_CHANNEL, EMPTY_CHANNEL],
    "state": [0, 0, 0, 0],
    "config": {"startup_stay": False},
}


class TestParseCommand:
    def test_set_spool_by_tag(self):
        assert parse_command("u1", "// printlog: SET_SPOOL TOOL=2 TAG=04A23F11") == SpoolCommand(
            "u1", 2, tag="04A23F11"
        )

    def test_set_spool_by_id(self):
        command = parse_command(
            "u1", "// printlog: SET_SPOOL TOOL=0 ID=0e9c1d8e-1b2c-4d5e-8f90-123456789abc"
        )
        assert command == SpoolCommand("u1", 0, filament_id="0e9c1d8e-1b2c-4d5e-8f90-123456789abc")

    def test_clear_spool(self):
        assert parse_command("u1", "// printlog: CLEAR_SPOOL TOOL=3") == SpoolCommand("u1", 3)

    def test_without_a_tool_it_is_the_active_one(self):
        assert parse_command("u1", "// printlog: CLEAR_SPOOL", active=1) == SpoolCommand("u1", 1)

    @pytest.mark.parametrize(
        "line",
        [
            "// printlog-bridge: T2 is now Silk Gold in 3D Print Log",  # the bridge's own answer
            "// SET_SPOOL TOOL=2 TAG=04A23F11",
            "echo: printlog: SET_SPOOL TOOL=2 TAG=04A23F11",
            "// printlog: SET_SPOOL TOOL=two TAG=04A23F11",
            "// printlog: SET_SPOOL TOOL=2",  # neither a spool id nor a tag
            "// printlog: SET_SPOOL TOOL=2 ID=a TAG=b",
            "// printlog: EAT_SPOOL TOOL=2",
            "!! printlog: SET_SPOOL TOOL=2 TAG=04A23F11",
        ],
    )
    def test_anything_else_is_not_a_command(self, line):
        assert parse_command("u1", line) is None


class TestU1Reader:
    def test_a_read_tag_is_its_uid_material_and_color(self):
        readings = U1RfidReader("u1").update(FILAMENT_DETECT)

        assert readings[0] == SpoolReading(
            "u1", 0, tag="04A23F11", material="PLA", color_hex="#E02020", slot_count=4
        )

    def test_a_channel_with_no_tag_is_an_empty_reading(self):
        readings = U1RfidReader("u1").update(FILAMENT_DETECT)

        assert readings[1:] == [SpoolReading("u1", slot, slot_count=4) for slot in (1, 2, 3)]

    def test_only_what_changed_is_reported_again(self):
        reader = U1RfidReader("u1")
        reader.update(FILAMENT_DETECT)
        changed = copy.deepcopy(FILAMENT_DETECT)
        changed["info"][2] = {**SNAPMAKER_PLA_RED, "CARD_UID": [1, 2, 3, 4], "MAIN_TYPE": "PETG"}
        changed["state"] = [0, 0, 1, 0]

        assert reader.update(FILAMENT_DETECT) == []
        [reading] = reader.update(changed)

        assert reading.slot == 2
        assert reading.tag == "01020304"
        assert reading.material == "PETG"

    def test_a_tag_read_without_a_uid_still_says_material_and_color(self):
        no_uid = copy.deepcopy(FILAMENT_DETECT)
        no_uid["info"][0]["CARD_UID"] = 0

        [reading, *_] = U1RfidReader("u1").update(no_uid)

        assert reading.tag is None
        assert reading.material == "PLA"

    @pytest.mark.parametrize("info", [None, "x", [None, 5]])
    def test_something_unexpected_is_no_reading(self, info):
        assert all(r.tag is None for r in U1RfidReader("u1").update({"info": info}))


class Recorder:
    """A SpoolSink that keeps what it's told and answers each command with `answer`."""

    def __init__(self, answer: str = 'T2 is now "Silk" Gold #1; in 3D Print Log') -> None:
        self.answer = answer
        self.commands: list[SpoolCommand] = []
        self.readings: list[SpoolReading] = []

    async def spool_command(self, command: SpoolCommand) -> str:
        self.commands.append(command)
        return self.answer

    async def spool_reading(self, reading: SpoolReading) -> None:
        self.readings.append(reading)


@contextlib.asynccontextmanager
async def watching(fake: FakeMoonraker, sink: Recorder):
    base = await fake.start()
    watcher = SpoolWatcher("u1", sink, U1RfidReader("u1"))
    session = MoonrakerSession(
        "u1", url=base, listener=watcher, backoff=Backoff(initial=0.01, maximum=0.05)
    )
    try:
        async with running(session):
            await eventually(lambda: fake.subscriptions)
            yield watcher
    finally:
        await fake.close()


class TestWatcher:
    def test_a_console_command_is_carried_out_and_answered(self):
        fake, sink = FakeMoonraker(), Recorder()

        async def scenario():
            async with watching(fake, sink):
                await fake.respond("// printlog: SET_SPOOL TOOL=1 TAG=04A23F11")
                await eventually(lambda: fake.scripts)

        asyncio.run(scenario())

        # The fake has extruder and extruder1: a printer with two tools.
        assert sink.commands == [SpoolCommand("u1", 1, tag="04A23F11", slot_count=2)]
        # Quotes and Klipper's comment characters can't break the answer's G-code.
        assert fake.scripts == [
            "_PRINTLOG_RESPOND MSG=\"printlog-bridge: T2 is now 'Silk' Gold 1 in 3D Print Log\""
        ]

    def test_without_a_tool_the_active_one_is_used(self):
        fake, sink = FakeMoonraker(), Recorder()

        async def scenario():
            async with watching(fake, sink):
                await fake.set_status(toolhead={"extruder": "extruder1"})
                await fake.respond("// printlog: CLEAR_SPOOL")
                await eventually(lambda: sink.commands)

        asyncio.run(scenario())

        assert sink.commands[0].slot == 1

    def test_other_console_lines_are_ignored(self):
        fake, sink = FakeMoonraker(), Recorder()

        async def scenario():
            async with watching(fake, sink):
                await fake.respond("// printlog-bridge: T2 is now Silk Gold in 3D Print Log")
                await fake.respond("// Klipper state: Ready")
                await asyncio.sleep(0.1)

        asyncio.run(scenario())

        assert sink.commands == []
        assert fake.scripts == []

    def test_the_u1_reader_reports_each_slot(self):
        fake, sink = (
            FakeMoonraker(objects=(*FakeMoonraker().objects, "filament_detect")),
            Recorder(),
        )
        fake.status["filament_detect"] = copy.deepcopy(FILAMENT_DETECT)

        async def scenario():
            async with watching(fake, sink):
                await eventually(lambda: len(sink.readings) == 4)
                inserted = copy.deepcopy(FILAMENT_DETECT["info"])
                inserted[3] = {**SNAPMAKER_PLA_RED, "CARD_UID": [9, 9, 9, 9]}
                await fake.set_status(filament_detect={"info": inserted})
                await eventually(lambda: len(sink.readings) == 5)

        asyncio.run(scenario())

        assert sink.readings[0].tag == "04A23F11"
        assert sink.readings[4] == SpoolReading(
            "u1", 3, tag="09090909", material="PLA", color_hex="#E02020", slot_count=4
        )

    def test_an_answer_that_cant_be_shown_is_only_logged(self):
        fake, sink = FakeMoonraker(), Recorder()

        async def scenario():
            async with watching(fake, sink):
                fake.klippy_state = "shutdown"  # printer.gcode.script fails
                await fake.respond("// printlog: CLEAR_SPOOL TOOL=0")
                await eventually(lambda: sink.commands)
                await asyncio.sleep(0.05)
                fake.klippy_state = "ready"
                await fake.respond("// printlog: CLEAR_SPOOL TOOL=1")
                await eventually(lambda: fake.scripts)

        asyncio.run(scenario())

        assert len(sink.commands) == 2

    def test_a_command_among_several_console_lines_is_found(self):
        fake, sink = FakeMoonraker(), Recorder()

        async def scenario():
            async with watching(fake, sink):
                await fake.respond("// one\n// printlog: CLEAR_SPOOL TOOL=1\n// two")
                await eventually(lambda: sink.commands)

        asyncio.run(scenario())

        assert sink.commands[0].slot == 1
