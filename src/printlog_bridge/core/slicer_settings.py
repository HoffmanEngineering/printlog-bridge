"""A print's slicer settings (#36): what is kept of the file, how it is scrubbed, and what is sent.

Of a job's G-code file the adapter reads two byte ranges and nothing else: the header (the start
of the file, through `; HEADER_BLOCK_END`), which names the slicer and its version, and the
settings block the slicer writes after the last G-code command. `header_from` and
`config_block_from` cut those out of what was read, so even a read that had to guess where the
G-code ends (and so may hold some of it) keeps only the comments after the last command.

Before anything leaves the bridge, `scrub` blanks the value of every setting that names a host or
holds a credential, as 3D Print Log does again on its side (api#164): a slicer writes how it
reaches the printer (`print_host`, `printhost_apikey`, ...) into every file, and a post-processing
script's command line (`post_process`) can carry a key and always carries a local path.

`request_body` builds what `POST /api/Prints/{id}/slicer-settings` takes, within the API's
limits, so one odd value can't get the whole request refused.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from printlog_bridge.core.events import SlicerFile, SlicerMetadata

CONFIG_BLOCK_MAX_BYTES = 512 * 1024
"""The largest settings block the API takes, in UTF-8 bytes. Real ones are tens of kilobytes."""
HEADER_MAX_BYTES = 64 * 1024
"""The largest header the API takes, in UTF-8 bytes. Real ones are a few hundred bytes."""
HEADER_END = "; HEADER_BLOCK_END"
OUTBOX_KIND = "slicer_settings"
"""The outbox entry that holds a job's slicer settings until they are sent: the request body."""
TEXT_FIELDS = ("configBlock", "header")

SENSITIVE = (
    "print_host", "printhost", "apikey", "api_key", "password", "passwd", "token", "secret",
    "user", "cookie", "credential", "authorization", "access_code", "post_process",
)  # fmt: skip
"""A setting whose key holds any of these has its value blanked: the API's list
(`SlicerSettingsScrubber`), with `post_process` matched within a key as well as whole."""

_LINE_BREAK = re.compile(r"\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")
"""Every line break `str.splitlines()` knows, a superset of the API's: so a line the API reads
starts where one of these does, and a CR-only block can't slip a key past."""
_SETTING_KEY = re.compile(r"[A-Za-z0-9_]+")
"""A setting's name, as the API reads one from a `; key = value` line."""
_API_LINE_BREAK = re.compile(r"[\n\r\f\x85\u2028\u2029]")
"""Where the API (`MemoryExtensions.EnumerateLines`) ends a line, and so where a value ends."""
_TRAILING_LINE_BREAK = re.compile(r"(?:\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029])\Z")
_SETTING = re.compile(r"[ \t]*;[ \t]*([A-Za-z0-9_.\-]+)[ \t]*[=:]")
"""`; key = value` or `; key: value`, up to the separator. No part of it matches a line break,
so a match never runs past its line."""


def is_sensitive(key: str) -> bool:
    lower = key.strip().lower()
    return any(fragment in lower for fragment in SENSITIVE)


def scrub(text: str) -> str:
    """`text` with the value of every sensitive `; key = value` line blanked, the key kept so
    the line still reads as an empty setting. Every other character is unchanged.

    Linear in the length of the text: each line is looked at once, from its start."""
    parts: list[str] = []
    copied = 0
    position = 0
    end = len(text)
    while position < end:
        setting = _SETTING.match(text, position)
        if setting is not None and is_sensitive(setting.group(1)):
            # The value runs to where the API ends the line, which may be past where Python
            # does (`\v`, `\x1c`...): blanked through there, so none of it reaches the API.
            api_break = _API_LINE_BREAK.search(text, setting.end())
            value_end = api_break.start() if api_break is not None else end
            parts.append(text[copied : setting.end()])
            copied = position = value_end
        line_break = _LINE_BREAK.search(text, position)
        if line_break is None:
            break
        position = line_break.end()
    if not parts:
        return text
    parts.append(text[copied:])
    return "".join(parts)


def header_from(data: bytes) -> str | None:
    """The header from the start of a file: the comment lines it starts with, through
    `; HEADER_BLOCK_END` when that is among them. PrusaSlicer's is its first line, which names
    it and its version; a blank line ends it. Never a line of G-code. None for anything but
    text, or a file that doesn't start with a comment."""
    text = _text(data)
    if text is None:
        return None
    end = 0
    for line in _lines(text):
        if not line.startswith(";") or not _TRAILING_LINE_BREAK.search(line):
            break  # G-code, a blank line, or a line the read may have cut
        end += len(line)
        if line.startswith(HEADER_END):
            break
    header = _TRAILING_LINE_BREAK.sub("", text[:end])
    return header if header.strip() else None


def config_block_from(
    data: bytes, *, starts_mid_line: bool = False, after_gcode: bool = False
) -> str | None:
    """The settings block from a file's tail. `starts_mid_line`: the read began within a line,
    which is dropped. `after_gcode`: the read may begin before the G-code ends, so only what
    follows its last command is kept. None for anything but text, or nothing but blank lines."""
    text = _text(data)
    if text is None:
        return None
    if starts_mid_line:
        line_break = _LINE_BREAK.search(text)
        text = text[line_break.end() :] if line_break is not None else ""
    if after_gcode:
        # Only the comments and blank lines the read ends with: whatever a toolpath looks like
        # (`G1X10E.5`, indented, lower case, `T1`), it isn't one of those.
        lines = text.splitlines(keepends=True)
        keep = len(lines)
        while keep and (not lines[keep - 1].strip() or lines[keep - 1].lstrip().startswith(";")):
            keep -= 1
        text = "".join(lines[keep:])
    return text if text.strip() else None


def config_block_from_settings(settings: Mapping[str, Any]) -> str | None:
    """Settings that the file doesn't hold as text, such as a `.bgcode` file's slicer metadata
    block or a 3MF's `Metadata/project_settings.config`, as the `; key = value` block a G-code
    file ends with, so they go to 3D Print Log the same way (`SlicerFile.config_block`).

    A list is joined with `;`, as PrusaSlicer and OrcaSlicer write one value per extruder, and a
    value over several lines is kept on one with `\\n`, as they write `start_gcode`. A key that
    isn't a setting's name (letters, digits, `_`), a nested value and None are left out. None
    when nothing is left."""
    lines = []
    for key, value in settings.items():
        if not isinstance(key, str) or not _SETTING_KEY.fullmatch(key):
            continue
        if isinstance(value, (list, tuple)):
            if not all(_is_scalar(v) for v in value):
                continue
            text = ";".join(_setting_text(v) for v in value)
        elif _is_scalar(value):
            text = _setting_text(value)
        else:
            continue
        lines.append(f"; {key} = {text}\n")
    return "".join(lines) or None


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def _setting_text(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return _LINE_BREAK.sub("\\\\n", str(value))


def _lines(text: str):
    """Each line of `text` with its line break, as `_LINE_BREAK` splits them."""
    position = 0
    while position < len(text):
        line_break = _LINE_BREAK.search(text, position)
        end = line_break.end() if line_break is not None else len(text)
        yield text[position:end]
        position = end


def request_body(metadata: SlicerMetadata | None, file: SlicerFile | None) -> dict[str, Any] | None:
    """The body of `POST /api/Prints/{id}/slicer-settings`, scrubbed and within the API's caps,
    or None when there's nothing the API could record: no settings block, and no metadata
    naming the slicer."""
    body: dict[str, Any] = {}
    if file is not None and file.config_block and file.config_block.strip():
        body["configBlock"] = _fit_from_end(scrub(file.config_block), CONFIG_BLOCK_MAX_BYTES)
        if file.header and file.header.strip():
            body["header"] = _fit_from_start(scrub(file.header), HEADER_MAX_BYTES)
    fields = metadata_fields(metadata) if metadata is not None else {}
    if fields:
        body["metadata"] = fields
    if "configBlock" not in body and not fields.get("slicer"):
        return None
    return body


_TEXT_FIELDS = (
    ("slicer", "slicer", 50),
    ("slicerVersion", "slicer_version", 50),
    ("filamentName", "filament_name", 200),
    ("filamentType", "filament_type", 100),
)
_NUMBER_FIELDS = (
    ("layerHeightMm", "layer_height_mm", 0.0, 100.0),
    ("firstLayerHeightMm", "first_layer_height_mm", 0.0, 100.0),
    ("nozzleDiameterMm", "nozzle_diameter_mm", 0.0, 100.0),
    ("firstLayerNozzleTemperatureC", "first_layer_nozzle_temperature_c", -100.0, 1000.0),
    ("firstLayerBedTemperatureC", "first_layer_bed_temperature_c", -100.0, 1000.0),
)
_ORDER = (
    "slicer", "slicerVersion", "layerHeightMm", "firstLayerHeightMm", "nozzleDiameterMm",
    "filamentName", "filamentType", "firstLayerNozzleTemperatureC", "firstLayerBedTemperatureC",
)  # fmt: skip


def metadata_fields(metadata: SlicerMetadata) -> dict[str, Any]:
    """The metadata under the API's names (`SlicerMetadataDto`). Text is cut to the API's
    length (in UTF-16 code units, as it counts), and a number outside its range (or not finite)
    is left out."""
    fields: dict[str, Any] = {}
    for name, attribute, limit in _TEXT_FIELDS:
        value = getattr(metadata, attribute)
        if isinstance(value, str) and value.strip():
            fields[name] = _utf16_prefix(value.strip(), limit)
    for name, attribute, low, high in _NUMBER_FIELDS:
        value = getattr(metadata, attribute)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value) and low <= value <= high:
            fields[name] = value
    return {name: fields[name] for name in _ORDER if name in fields}


def _utf16_prefix(text: str, limit: int) -> str:
    """The longest start of `text` that is at most `limit` UTF-16 code units, which is how the
    API counts a length; a character outside the BMP (an emoji) is two, and never split."""
    units = 0
    for n, character in enumerate(text):
        units += 2 if ord(character) > 0xFFFF else 1
        if units > limit:
            return text[:n]
    return text


def _text(data: bytes) -> str | None:
    text = data.decode("utf-8", errors="replace")
    return None if "\x00" in text else text


def _fit_from_end(text: str, max_bytes: int) -> str:
    """`text`, less whole lines from its start until it fits in `max_bytes` of UTF-8. Bytes
    read as a replacement character take three, so a block read to the cap can be over it."""
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    tail = encoded[len(encoded) - max_bytes :].decode("utf-8", errors="ignore")
    line_break = _LINE_BREAK.search(tail)
    return tail[line_break.end() :] if line_break is not None else ""


def _fit_from_start(text: str, max_bytes: int) -> str:
    """`text` cut to fit in `max_bytes` of UTF-8, keeping its start."""
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
