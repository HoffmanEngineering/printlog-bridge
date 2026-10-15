"""klipper/printlog.cfg's macros, rendered the way Klipper renders them, and read by the bridge."""

from __future__ import annotations

import configparser
import re
import shlex
from pathlib import Path

import pytest

from printlog_bridge.adapters.moonraker.spools import (
    ANSWER_PREFIX,
    MAX_ANSWER,
    answer_script,
    parse_command,
)
from printlog_bridge.core.events import SpoolCommand

jinja2 = pytest.importorskip("jinja2")

MACROS = Path(__file__).resolve().parents[1] / "klipper" / "printlog.cfg"


def run_macro(name: str, params: dict[str, str], active: str = "extruder") -> list[str]:
    """What the macro prints to the console, as `// ` lines, rendered like Klipper's
    gcode_macro (jinja2 with `{` `}` for expressions) with the action functions it uses."""
    config = configparser.RawConfigParser()
    config.read(MACROS, encoding="utf-8")
    template = jinja2.Environment(
        block_start_string="{%",
        block_end_string="%}",
        variable_start_string="{",
        variable_end_string="}",
    ).from_string(config[f"gcode_macro {name}"]["gcode"])
    lines: list[str] = []

    def respond_info(message: str) -> str:
        lines.append(f"// {message}")
        return ""

    template.render(
        params=params,
        printer={"toolhead": {"extruder": active}},
        action_respond_info=respond_info,
    )
    return lines


# Klipper's GCodeDispatch.extended_r and _get_extended_params (klippy/gcode.py): how it reads the
# parameters of a command like _PRINTLOG_RESPOND MSG="...". A line it can't split with shlex is a
# "Malformed command" error, and the answer never shows.
KLIPPER_EXTENDED = re.compile(
    r"^\s*(?:N[0-9]+\s*)?"
    r"(?P<cmd>[a-zA-Z_][a-zA-Z0-9_]+)(?:\s+|$)"
    r"(?P<args>[^#*;]*?)"
    r"\s*(?:[#*;].*)?$"
)


def klipper_params(script: str) -> dict[str, str]:
    """The parameters Klipper gives the macro a one-line script runs, as it parses them."""
    assert "\n" not in script
    match = KLIPPER_EXTENDED.match(script)
    assert match is not None, script
    pairs = [arg.split("=", 1) for arg in shlex.split(match.group("args"))]
    return {key.upper(): value for key, value in pairs}


def commands(lines: list[str]) -> list[SpoolCommand]:
    return [c for c in (parse_command("u1", line) for line in lines) if c is not None]


def test_set_spool_by_tag_reaches_the_bridge():
    lines = run_macro("PRINTLOG_SET_SPOOL", {"TOOL": "2", "TAG": "04A23F11"})

    assert commands(lines) == [SpoolCommand("u1", 2, tag="04A23F11")]


def test_set_spool_by_id_reaches_the_bridge():
    lines = run_macro("PRINTLOG_SET_SPOOL", {"TOOL": "0", "ID": "0e9c1d8e-1b2c"})

    assert commands(lines) == [SpoolCommand("u1", 0, filament_id="0e9c1d8e-1b2c")]


@pytest.mark.parametrize(("active", "slot"), [("extruder", 0), ("extruder3", 3)])
def test_without_a_tool_it_is_the_active_one(active, slot):
    lines = run_macro("PRINTLOG_SET_SPOOL", {"TAG": "04A23F11"}, active=active)

    assert commands(lines) == [SpoolCommand("u1", slot, tag="04A23F11")]


@pytest.mark.parametrize(
    "params",
    [{"TOOL": "x", "TAG": "04A23F11"}, {"TOOL": "1"}, {"TAG": "04", "ID": "f1"}],
)
def test_a_mistake_says_so_and_sends_nothing(params):
    lines = run_macro("PRINTLOG_SET_SPOOL", params)

    assert commands(lines) == []
    assert lines and lines[0].startswith("// PRINTLOG_SET_SPOOL: ")


def test_clear_spool_reaches_the_bridge():
    assert commands(run_macro("PRINTLOG_CLEAR_SPOOL", {"TOOL": "1"})) == [SpoolCommand("u1", 1)]
    assert commands(run_macro("PRINTLOG_CLEAR_SPOOL", {}, active="extruder2")) == [
        SpoolCommand("u1", 2)
    ]


def test_the_bridges_answer_is_shown_and_isnt_taken_for_a_command():
    script = answer_script("T2 is now Silk Gold in 3D Print Log")
    message = klipper_params(script)["MSG"]

    lines = run_macro("_PRINTLOG_RESPOND", {"MSG": message})

    assert lines == ["// printlog-bridge: T2 is now Silk Gold in 3D Print Log"]
    assert commands(lines) == []


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("Spool\\", "Spool"),  # a backslash before the closing quote leaves it unclosed
        ("Back\\\\slash", "Backslash"),  # shlex turns two into one
        ("Back\\slash", "Backslash"),
        ('Say "hi"', "Say 'hi'"),
        ("It's mine", "It's mine"),
        ("Gold; #2 *new*", "Gold 2 new"),
        ("$HOME {printer} %s", "$HOME {printer} %s"),
        ("Tab\tand  spaces", "Tab and spaces"),
        ("Two\nlines\r", "Two lines"),
    ],
)
def test_an_answer_with_shlex_special_characters_still_shows(name, shown):
    """The answer reaches the console whole, as Klipper parses it, whatever the spool's name."""
    for answer, expected in [
        (f"T0 is now {name} in 3D Print Log", f"T0 is now {shown} in 3D Print Log"),
        (f"T0 is now {name}", f"T0 is now {shown}"),  # at the end, next to the closing quote
    ]:
        params = klipper_params(answer_script(answer))

        assert params == {"MSG": ANSWER_PREFIX + expected}
        lines = run_macro("_PRINTLOG_RESPOND", params)
        assert lines == [f"// {ANSWER_PREFIX}{expected}"]
        assert commands(lines) == []


def test_an_answer_cut_short_after_a_backslash_still_shows():
    answer = "x" * (MAX_ANSWER - 1) + "\\" + "and more"

    params = klipper_params(answer_script(answer))

    assert params["MSG"].startswith(ANSWER_PREFIX + "x" * (MAX_ANSWER - 1))
