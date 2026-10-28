"""Removing credentials and identifiers from a probe trace, so it can go on a public issue.

Two passes. `learn` walks the whole trace and collects the values of fields named like a
credential (`token`, `password`, `access_code`, `Set-Cookie`...) or like an identifier (`serial`,
`sn`, `cn`, `usn`, `MainboardID`, `device_id`, `mac`, `ssid`...), in JSON objects, in JSON held
in a string (an HTTP body, a websocket message) and in `name: value` / `name=value` text (SSDP
headers, mDNS TXT, a Chitu reply). `apply` then replaces each such value wherever it appears,
plus anything shaped like a MAC address and, with `ips`, like an IP address, by a stable
placeholder (`<serial-1>`, `<mac-2>`, `<ip-1>`): the same value always gets the same one, so the
trace still shows that two fields hold the same thing. Credentials become `<redacted>`.
Binary payloads (base64) are dropped, since nothing in them can be checked.
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
IDENTIFIER_KEY = re.compile(
    r"serial|^sn$|^cn$|^usn$|mainboard.?id|device.?id|machine.?id|printer.?id|chip.?id|"
    r"cpu.?id|^uuid$|^mac$|mac.?addr|hw.?addr|^hostid$|^did$|^ssid$|^bssid$|wifi.?name|"
    r"^id$",
    re.IGNORECASE,
)
_PAIR = re.compile(r'(?P<key>[A-Za-z][\w.\-]{0,40})"?\s*[:=]\s*"?(?P<value>[^\s",;&<>{}\[\]]+)')
_MAC = re.compile(
    r"(?<![0-9A-Fa-f:-])[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}"
    r"(?![0-9A-Fa-f:-])"
)
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![0-9A-Fa-f:])")
KEEP = ("allowList",)
"""Parts of the trace the probe wrote itself, with nothing to hide."""
MIN_SECRET = 4
MIN_IDENTIFIER = 5


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
            stripped = value.strip()
            if stripped[:1] in ("{", "["):
                self.learn(_json_or_none(stripped))
            for match in _PAIR.finditer(value):
                self._classify(match.group("key"), match.group("value"))
        elif (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and IDENTIFIER_KEY.search(key)
            and len(str(value)) >= 8
        ):
            self._classify(key, str(value))

    def _classify(self, key: str, value: str) -> None:
        if not key or not value:
            return
        if SECRET_KEY.search(key) and len(value) >= MIN_SECRET:
            self.values[value] = REDACTED
        elif (
            IDENTIFIER_KEY.search(key)
            and len(value) >= MIN_IDENTIFIER
            and value not in self.values
            and not value.startswith("<")
        ):
            if _MAC.fullmatch(value):
                self.values[value] = self.placeholder("mac", _mac_key(value))
            else:
                self.values[value] = self.placeholder("serial", value)

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
        for value in sorted(self.values, key=len, reverse=True):
            if value in text:
                text = text.replace(value, self.values[value])
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
    return re.sub(r"[:-]", "", text).lower()


def redact_trace(trace: dict[str, Any], address: str | None = None) -> dict[str, Any]:
    """`trace` with credentials, identifiers, MACs and IP addresses replaced."""
    redactor = Redactor(ips=True)
    if address:
        redactor.learn_ip(address)
    rest = {k: v for k, v in trace.items() if k not in KEEP}
    redactor.learn(rest)
    out = redactor.apply(rest)
    return {k: trace[k] if k in KEEP else out[k] for k in trace}


def summary_redactor(trace: dict[str, Any]) -> Redactor:
    """For the printed summary: credentials, identifiers and MACs hidden, IPs kept."""
    redactor = Redactor(ips=False)
    redactor.learn({k: v for k, v in trace.items() if k not in KEEP})
    return redactor
