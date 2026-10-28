"""Removing credentials, identifiers and file names from a probe trace, so it can go on a public
issue.

Two passes. `learn` walks the whole trace and collects:

- the values of fields named like a credential (`token`, `password`, `access_code`,
  `Set-Cookie`...), like an identifier (`serial`, `sn`, `deviceSn`, `printerSN`, `cn`, `usn`,
  `MainboardID`, `device_id`, `mac`, `wifiSsid`, `hostname`, `host`...) or like a file
  (`filename`, `printFileName`, `path`, anything with `file`). Names are split into words
  (camelCase, `_`, `-`, `.`), so `deviceSn` matches `sn`. It looks in JSON objects, in JSON held
  in a string (an HTTP body, a websocket message) and in `name: value` / `name=value` text (SSDP
  headers, mDNS TXT, a Chitu reply);
- the serial and Wi-Fi name in an Anycubic Photon's positional `sysinfo,<model>,<firmware>,
  <serial>,<ssid>,end`;
- long letter-and-digit segments of an MQTT topic (`device/<serial>/report`).

`apply` then replaces each such value wherever it appears, and by pattern: MAC addresses
(`aa:bb:..`, `aa-bb-..`, `aabb.ccdd.eeff`, or 12 hex digits standing alone, as in an mDNS name),
`user:password@` in URLs, sliced files (`*.gcode`, `*.3mf`, `*.ctb`...), and with `ips` IP
addresses. Each gets a stable placeholder (`<serial-1>`, `<mac-2>`, `<file-1>`, `<ip-1>`): the
same value always gets the same one, so the trace still shows that two fields hold the same
thing. Credentials become `<redacted>`. Binary payloads (base64) are dropped, since nothing in
them can be checked.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections import Counter
from typing import Any

REDACTED = "<redacted>"
BINARY_OMITTED = "<binary payload omitted in redacted mode>"

SECRET_KEY = re.compile(
    r"token|passw|pwd|secret|access.?code|authoriz|api.?key|cookie|credential|devicepk|"
    r"devicecrt|private.?key|signature|^sign$|^key$|^pin$|^nonce$|^ak$|^sk$",
    re.IGNORECASE,
)
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
IDENTIFIER_WORDS = frozenset(
    {
        "sn",
        "serial",
        "cn",
        "usn",
        "uuid",
        "guid",
        "mac",
        "ssid",
        "bssid",
        "wifi",
        "wlan",
        "hostname",
        "hostid",
        "did",
        "imei",
        "cid",
    }
)
"""A field whose name has one of these words holds an identifier."""
ID_OWNERS = frozenset({"device", "machine", "printer", "mainboard", "board", "chip", "cpu", "hw"})
"""... and so does `<one of these> id` (`MainboardID`, `device_id`)."""
FILE_WORDS = frozenset({"file", "filename", "files", "path", "gcode", "filepath"})
HOST_WORDS = frozenset({"hostname", "host"})
SSID_WORDS = frozenset({"ssid", "bssid", "wifi", "wlan"})

_PAIR = re.compile(r'(?P<key>[A-Za-z][\w.\-]{0,40})"?\s*[:=]\s*"?(?P<value>[^\s",;&<>{}\[\]]+)')
_HEX = "0-9A-Fa-f"
_MAC = re.compile(
    rf"(?<![{_HEX}:-])[{_HEX}]{{2}}([:-])(?:[{_HEX}]{{2}}\1){{4}}[{_HEX}]{{2}}(?![{_HEX}:-])"
    rf"|(?<![0-9A-Za-z.])[{_HEX}]{{4}}\.[{_HEX}]{{4}}\.[{_HEX}]{{4}}(?![0-9A-Za-z.])"
    rf"|(?<![0-9A-Za-z])[{_HEX}]{{12}}(?![0-9A-Za-z])"
)
_URL_USERINFO = re.compile(r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s\"'\\]+@")
FILE_EXTENSIONS = (
    "gcode",
    "bgcode",
    "gco",
    "3mf",
    "stl",
    "ufp",
    "ctb",
    "cbddlp",
    "pwmx",
    "pwma",
    "pwmo",
    "pwms",
    "pws",
    "pw0",
    "pwx",
    "goo",
    "prz",
    "cxdlp",
    "photon",
    "photons",
    "jxs",
    "zip",
)
_FILE = re.compile(
    r"[^\s\"'<>,;:=|*?]*?[^\s\"'<>,;:=|*?/\\]+\.(?:" + "|".join(FILE_EXTENSIONS) + r")(?![\w.])",
    re.IGNORECASE,
)
_SYSINFO = re.compile(
    r"sysinfo,(?P<model>[^,]*),(?P<firmware>[^,]*),(?P<serial>[^,]*),"
    r"(?P<ssid>[^,]*),end"
)
_TOPIC_SEGMENT = re.compile(r"^(?=.*\d)[A-Za-z0-9_-]{12,}$")
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![0-9A-Fa-f:])")
KEEP = ("allowList",)
"""Parts of the trace the probe wrote itself, with nothing to hide."""
MIN_SECRET = 4
MIN_IDENTIFIER = 4


def words(key: str) -> list[str]:
    """`key`'s words, lowercased: `printFileName` -> print, file, name; `wifiSsid` -> wifi,
    ssid; `MainboardID` -> mainboard, id."""
    return [w.lower() for w in _WORD.findall(key)]


def key_kind(key: str) -> str | None:
    """What a field named `key` holds: `secret`, `host`, `ssid`, `mac`, `serial`, `file`, or
    None."""
    if not key:
        return None
    if SECRET_KEY.search(key):
        return "secret"
    parts = words(key)
    found = set(parts)
    if found & HOST_WORDS or parts[-2:] == ["host", "name"]:
        return "host"
    if found & SSID_WORDS:
        return "ssid"
    if "mac" in found:
        return "mac"
    if found & IDENTIFIER_WORDS or parts == ["id"]:
        return "serial"
    if "id" in found and found & ID_OWNERS:
        return "serial"
    if found & FILE_WORDS:
        return "file"
    return None


def _ipv4_to_hide(text: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(text)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified or text == "255.255.255.255":
        return False
    # "1.2.3.4" is more often a firmware version than an address; private ones always hide.
    return ip.is_private or ip.is_link_local or int(text.split(".")[0]) >= 10


class Redactor:
    def __init__(self, ips: bool = True) -> None:
        self.ips = ips
        self.values: dict[str, str] = {}
        """Each learned value and what replaces it."""
        self._counts: Counter[str] = Counter()
        self._placeholders: dict[tuple[str, str], str] = {}

    def placeholder(self, kind: str, value: str) -> str:
        key = (kind, value.lower())
        if key not in self._placeholders:
            self._counts[kind] += 1
            self._placeholders[key] = f"<{kind}-{self._counts[kind]}>"
        return self._placeholders[key]

    # -- learning ------------------------------------------------------------------------------

    def learn_ip(self, address: str) -> None:
        """Gives `address` (the probed printer) the first IP placeholder."""
        if self.ips:
            self.values.setdefault(address, self.placeholder("ip", address))

    def learn(self, value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                self.learn(v, str(k))
        elif isinstance(value, list):
            for item in value:
                self.learn(item, key)
        elif isinstance(value, str):
            self._classify(key, value)
            if key == "topic":
                for segment in value.split("/"):
                    if _TOPIC_SEGMENT.match(segment):
                        self._remember(segment, "serial")
            stripped = value.strip()
            if stripped[:1] in ("{", "["):
                self.learn(_json_or_none(stripped))
            for match in _PAIR.finditer(value):
                self._classify(match.group("key"), match.group("value"))
            for match in _SYSINFO.finditer(value):
                self._remember(match.group("serial"), "serial")
                self._remember(match.group("ssid"), "ssid")
        elif (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and key_kind(key) in ("serial", "mac")
            and len(str(value)) >= 8
        ):
            self._remember(str(value), "serial")

    def _classify(self, key: str, value: str) -> None:
        if not key or not value:
            return
        kind = key_kind(key)
        if kind == "secret":
            if len(value) >= MIN_SECRET:
                self.values[value] = REDACTED
        elif kind == "file":
            # A request path the probe made itself is never under one of these names, so
            # only values that look like a file or a folder count.
            if "/" in value or "." in value:
                self._remember(value, "file")
                name = re.split(r"[/\\]", value)[-1]
                if name != value:
                    self._remember(name, "file")
        elif kind is not None:
            self._remember(value, kind)

    def _remember(self, value: str, kind: str) -> None:
        value = value.strip()
        if len(value) < MIN_IDENTIFIER or value in self.values or value.startswith("<"):
            return
        if kind in ("mac", "serial") and _MAC.fullmatch(value):
            self.values[value] = self.placeholder("mac", _mac_key(value))
        else:
            self.values[value] = self.placeholder(kind, value)

    # -- applying ------------------------------------------------------------------------------

    def apply(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if k == "base64" and isinstance(v, str):
                    out[k] = BINARY_OMITTED
                else:
                    out[k] = self.apply(v, str(k))
            return out
        if isinstance(value, list):
            return [self.apply(item, key) for item in value]
        if isinstance(value, str):
            if key and SECRET_KEY.search(key) and value:
                return REDACTED
            return self.text(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            replaced = self.values.get(str(value))
            return replaced if replaced is not None else value
        return value

    def text(self, text: str) -> str:
        text = _URL_USERINFO.sub(rf"\1{REDACTED}@", text)
        for value in sorted(self.values, key=len, reverse=True):
            if value in text:
                text = text.replace(value, self.values[value])
        text = _FILE.sub(lambda m: self.placeholder("file", m.group(0)), text)
        text = _MAC.sub(lambda m: self.placeholder("mac", _mac_key(m.group(0))), text)
        if self.ips:
            text = _IPV4.sub(self._ipv4, text)
            text = _IPV6.sub(self._ipv6, text)
        return text

    def _ipv4(self, match: re.Match[str]) -> str:
        found = match.group(0)
        return self.placeholder("ip", found) if _ipv4_to_hide(found) else found

    def _ipv6(self, match: re.Match[str]) -> str:
        found = match.group(0)
        try:
            ip = ipaddress.IPv6Address(found)
        except ValueError:
            return found
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast:
            return found
        return self.placeholder("ip", found)


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _mac_key(text: str) -> str:
    return re.sub(r"[:.-]", "", text).lower()


def redact_trace(trace: dict[str, Any], address: str | None = None) -> dict[str, Any]:
    """`trace` with credentials, identifiers, file names, MACs and IP addresses replaced."""
    redactor = Redactor(ips=True)
    if address:
        redactor.learn_ip(address)
    rest = {k: v for k, v in trace.items() if k not in KEEP}
    redactor.learn(rest)
    out = redactor.apply(rest)
    return {k: trace[k] if k in KEEP else out[k] for k in trace}


def summary_redactor(trace: dict[str, Any]) -> Redactor:
    """For the printed summary: everything `redact_trace` hides but IP addresses."""
    redactor = Redactor(ips=False)
    redactor.learn({k: v for k, v in trace.items() if k not in KEEP and k != "probe"})
    return redactor
