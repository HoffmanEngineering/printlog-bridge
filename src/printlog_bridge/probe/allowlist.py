"""Everything `printlog-bridge probe` may send to the network, and the checks that hold it to that.

The probe never sends a command that changes a printer's state: no print, pause, cancel, move,
heat, fan, light, file upload or delete, firmware, or settings. Each rule below is a read-only
request or a discovery message that the printer's own software sends; each names where its
messages come from. `check(rule, payload)` raises `NotAllowed` for anything else, and the
recorder calls it before every send, so a mistake here stops the probe instead of reaching a
printer.
"""

from __future__ import annotations

import json
import struct
import textwrap
from dataclasses import dataclass
from typing import Any

from printlog_bridge.core.mdns import read_name


class NotAllowed(Exception):
    """The probe was about to send something that isn't on the allow-list."""


@dataclass(frozen=True)
class Rule:
    id: str
    what: str
    source: str


MDNS_SERVICES = (
    "_services._dns-sd._udp.local",
    "_http._tcp.local",
    "_https._tcp.local",
    "_moonraker._tcp.local",
    "_octoprint._tcp.local",
    "_prusa._tcp.local",
    "_mqtt._tcp.local",
    "_ws._tcp.local",
    "_workstation._tcp.local",
    "_printer._tcp.local",
    "_ipp._tcp.local",
    "_ssh._tcp.local",
    "_sftp-ssh._tcp.local",
)
"""Asked for at the start; any other service type the printer lists under
`_services._dns-sd._udp.local` (RFC 6763 9) is asked for too."""

SSDP_MSEARCH = (
    b"M-SEARCH * HTTP/1.1\r\n"
    b"HOST: 239.255.255.250:1900\r\n"
    b'MAN: "ssdp:discover"\r\n'
    b"MX: 2\r\n"
    b"ST: ssdp:all\r\n"
    b"\r\n"
)
CHITU_DISCOVERY = b"M99999"

HTTP_PATHS = frozenset(
    {
        "/",
        # Moonraker: read-only queries (moonraker.readthedocs.io, "Printer Administration",
        # "Server Administration", "Machine Requests", "History").
        "/server/info",
        "/printer/info",
        "/printer/objects/list",
        "/printer/objects/query?print_stats&virtual_sdcard&display_status&toolhead",
        "/machine/system_info",
        "/server/history/list?limit=3&order=desc",
        # PrusaLink (Prusa-Link-Web spec/openapi.yaml) and OctoPrint (docs.octoprint.org,
        # "Version information"): a 401/403 without credentials is recorded as such.
        "/api/version",
        "/api/v1/info",
        "/api/v1/status",
        # Anycubic LAN mode (Kobra 3/S1) on port 18910: the first, read-only step of the
        # handshake (rvanderp3/kobra-connect, kobra_connect/handshake.py: "GET /info"). Its
        # second step, the signed POST /ctrl that returns MQTT credentials, is never sent.
        "/info",
    }
)

CREALITY_REQUESTS = (
    '{"method":"get","params":{"ReqPrinterPara":1}}',
    '{"method":"get","params":{"reqPrintObjects":1}}',
    '{"method":"get","params":{"boxsInfo":1}}',
)
"""Creality's stock websocket (port 9999, subprotocol `wsslicer`): the `get` requests the Home
Assistant integration 3dg1luk43/ha_creality_ws sends for status, current objects and CFS boxes
(docs/internal/reference/01-protocol-and-core.md, "Requests"). Its `set` requests (pause, stop,
temperatures, fans, lights, G-code) are never sent, and neither is `reqGcodeFile`: an adapter
needs only its shape, and its answer is the largest message and full of file names."""
CREALITY_HEARTBEAT_REPLY = "ok"
"""The literal text Creality's websocket expects in answer to `{"ModeCode":"heart_beat"}`
(same source, 1.2)."""

SDCP_COMMANDS = {0: "status refresh", 1: "attributes"}
"""SDCP v3 (cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0): Cmd 0 asks the printer to
report its status again, Cmd 1 its attributes. Nothing else (Cmd 128 starts a print, 129 pauses,
130 stops...) is sent."""
SDCP_PING = "ping"

PHOTON_QUERIES = ("sysinfo", "getstatus")
"""Anycubic Photon (Mono X and relatives) TCP 6000: `sysinfo` returns model, firmware, serial
and Wi-Fi network; `getstatus` the print status (adamoutler/anycubic-python, README "Command:").
`goprint`, `gostop`, `gopause`, `delfile`, `delhistory` and the rest are never sent."""

MQTT_SUBSCRIBE_TOPIC = "#"

RULES = (
    Rule(
        "tcp-connect",
        "open a TCP connection to each port on the list and close it (no bytes sent); on a "
        "port with no known protocol, wait briefly for anything the device sends first",
        "-",
    ),
    Rule(
        "tls-handshake",
        "a TLS handshake (ClientHello, no client certificate) on 443, 8883 and 9883",
        "-",
    ),
    Rule(
        "udp-mdns-query",
        "mDNS PTR questions for service types (no answers, no records of our own) to "
        "224.0.0.251:5353 and to the printer's port 5353",
        "RFC 6762, RFC 6763",
    ),
    Rule(
        "udp-ssdp-msearch",
        "one SSDP M-SEARCH for ssdp:all to 239.255.255.250:1900 and to the printer's port 1900",
        "UPnP Device Architecture 1.1, 1.3.2",
    ),
    Rule(
        "udp-chitu-discovery",
        'the text "M99999" to the broadcast address and to the printer, UDP port 3000',
        "SDCP v3 spec (cbd-tech), Chitu discovery",
    ),
    Rule(
        "http-get",
        "HTTP(S) GET without credentials, redirects not followed, of only these paths: "
        + ", ".join(sorted(HTTP_PATHS)),
        "Moonraker docs, Prusa-Link-Web openapi.yaml, OctoPrint docs, kobra-connect",
    ),
    Rule(
        "ws-connect",
        "a websocket handshake to port 9999 (path /) and 3030 (path /websocket), and the "
        "protocol's own pong and close frames",
        "RFC 6455",
    ),
    Rule(
        "creality-ws",
        "on Creality's websocket: "
        + "; ".join(CREALITY_REQUESTS)
        + f'; and "{CREALITY_HEARTBEAT_REPLY}" in answer to its heart_beat',
        "3dg1luk43/ha_creality_ws docs/internal/reference/01-protocol-and-core.md",
    ),
    Rule(
        "sdcp-ws",
        "on SDCP v3's websocket: Cmd 0 (status refresh) and Cmd 1 (attributes) with empty "
        f'Data, and "{SDCP_PING}"',
        "cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0",
    ),
    Rule(
        "mqtt",
        "MQTT 3.1.1: CONNECT with no user name or password, SUBSCRIBE to '#' at QoS 0, PINGREQ "
        "and DISCONNECT. Never PUBLISH. A broker that asks for credentials is left there",
        "MQTT 3.1.1 (OASIS)",
    ),
    Rule(
        "photon-query",
        'only with --photon, on TCP 6000: the texts "'
        + '" and "'.join(PHOTON_QUERIES)
        + '". Without it, port 6000 only gets a connect and a banner read',
        "adamoutler/anycubic-python README",
    ),
)
RULE_IDS = frozenset(rule.id for rule in RULES)


def describe() -> str:
    """The allow-list as text, for `--help`."""
    lines = []
    for rule in RULES:
        source = f" [source: {rule.source}]" if rule.source != "-" else ""
        lines += textwrap.wrap(
            f"{rule.id}: {rule.what}{source}",
            width=96,
            initial_indent="  ",
            subsequent_indent="      ",
            break_on_hyphens=False,
        )
    return "\n".join(lines)


def to_json() -> list[dict[str, str]]:
    return [{"id": r.id, "what": r.what, "source": r.source} for r in RULES]


# -- the checks ---------------------------------------------------------------------------------


def check(rule: str, payload: Any = None) -> None:
    """Raises `NotAllowed` unless `payload` is something `rule` allows. What `payload` is
    depends on the rule: bytes for UDP and MQTT, the request line `GET <path>` for HTTP, the
    path for a websocket handshake, the text for websocket and TCP messages, None for a TCP
    connect or a TLS handshake."""
    checker = _CHECKS.get(rule)
    if checker is None:
        raise NotAllowed(f"no such rule: {rule}")
    checker(payload)


def _nothing(payload: Any) -> None:
    if payload is not None:
        raise NotAllowed("a connect check sends no data")


def _mdns(payload: Any) -> None:
    data = _bytes(payload)
    try:
        ident, flags, questions, answers, authority, additional = struct.unpack_from(
            "!HHHHHH", data
        )
        if flags != 0 or answers or authority or additional or not 0 < questions <= 32:
            raise NotAllowed("an mDNS message other than a plain query")
        offset = 12
        for _ in range(questions):
            name, offset = read_name(data, offset)
            kind, klass = struct.unpack_from("!HH", data, offset)
            offset += 4
            if any(len(label.encode("utf-8")) > 63 for label in name.split(".")):
                raise NotAllowed(f"an mDNS name with a label over 63 bytes: {name[:80]}")
            if kind != 12 or klass & 0x7FFF != 1 or not name.lower().endswith(".local"):
                raise NotAllowed(f"an mDNS question other than PTR for .local: {name}")
        if offset != len(data):
            raise NotAllowed("trailing bytes after the mDNS questions")
    except (struct.error, IndexError, ValueError) as e:
        raise NotAllowed(f"a malformed mDNS query: {e}") from None


def _exact(*allowed: bytes):
    def check_exact(payload: Any) -> None:
        if _bytes(payload) not in allowed:
            raise NotAllowed(f"not on the allow-list: {payload!r}")

    return check_exact


def _http(payload: Any) -> None:
    if not isinstance(payload, str):
        raise NotAllowed("an HTTP request is checked as its request line")
    method, _, path = payload.partition(" ")
    if method != "GET":
        raise NotAllowed(f"only GET is allowed, not {method}")
    if path not in HTTP_PATHS:
        raise NotAllowed(f"not an allowed path: {path}")


def _ws_connect(payload: Any) -> None:
    if payload not in ("/", "/websocket"):
        raise NotAllowed(f"not an allowed websocket path: {payload!r}")


def _creality(payload: Any) -> None:
    if payload == CREALITY_HEARTBEAT_REPLY:
        return
    if not isinstance(payload, str):
        raise NotAllowed("a websocket message is checked as text")
    try:
        message = json.loads(payload)
    except ValueError:
        raise NotAllowed(f"not on the allow-list: {payload!r}") from None
    if message not in [json.loads(r) for r in CREALITY_REQUESTS]:
        raise NotAllowed(f"not on the allow-list: {payload!r}")


def _sdcp(payload: Any) -> None:
    if payload == SDCP_PING:
        return
    try:
        message = json.loads(payload)
        data = message["Data"]
        board = data["MainboardID"]
        allowed = (
            set(message) == {"Id", "Data", "Topic"}
            and set(data) == {"Cmd", "Data", "RequestID", "MainboardID", "TimeStamp", "From"}
            and data["Cmd"] in SDCP_COMMANDS
            and type(data["Cmd"]) is int
            and data["Data"] == {}
            and isinstance(board, str)
            and message["Topic"] == f"sdcp/request/{board}"
        )
    except (ValueError, TypeError, KeyError):
        allowed = False
    if not allowed:
        raise NotAllowed(f"not on the allow-list: {payload!r}")


def _mqtt(payload: Any) -> None:
    data = _bytes(payload)
    if data in (b"\xc0\x00", b"\xe0\x00"):  # PINGREQ, DISCONNECT
        return
    try:
        first = data[0]
        length, offset = _remaining_length(data)
        body = data[offset:]
        if len(body) != length:
            raise NotAllowed("an MQTT packet whose length doesn't match")
        if first == 0x10:  # CONNECT
            name_length = struct.unpack_from(">H", body)[0]
            name = body[2 : 2 + name_length]
            level, flags = body[2 + name_length], body[3 + name_length]
            if name != b"MQTT" or level != 4 or flags != 0x02:
                raise NotAllowed("CONNECT other than clean session without credentials or will")
            client_length = struct.unpack_from(">H", body, 6 + name_length)[0]
            if 8 + name_length + client_length != len(body):
                raise NotAllowed("CONNECT with more than a client id")
            return
        if first == 0x82:  # SUBSCRIBE
            topic_length = struct.unpack_from(">H", body, 2)[0]
            topic = body[4 : 4 + topic_length].decode("utf-8")
            if topic != MQTT_SUBSCRIBE_TOPIC or body[4 + topic_length :] != b"\x00":
                raise NotAllowed(f"SUBSCRIBE other than '#' at QoS 0: {topic}")
            return
    except (IndexError, struct.error, UnicodeDecodeError) as e:
        raise NotAllowed(f"a malformed MQTT packet: {e}") from None
    raise NotAllowed(f"MQTT packet type {first >> 4} is not allowed")


def _photon(payload: Any) -> None:
    if payload not in PHOTON_QUERIES:
        raise NotAllowed(f"not on the allow-list: {payload!r}")


def _remaining_length(data: bytes) -> tuple[int, int]:
    length, shift = 0, 0
    for index in range(1, 5):
        byte = data[index]
        length |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return length, index + 1
    raise NotAllowed("a malformed MQTT length")


def _bytes(payload: Any) -> bytes:
    if not isinstance(payload, (bytes, bytearray)):
        raise NotAllowed("expected bytes")
    return bytes(payload)


_CHECKS = {
    "tcp-connect": _nothing,
    "tls-handshake": _nothing,
    "udp-mdns-query": _mdns,
    "udp-ssdp-msearch": _exact(SSDP_MSEARCH),
    "udp-chitu-discovery": _exact(CHITU_DISCOVERY),
    "http-get": _http,
    "ws-connect": _ws_connect,
    "creality-ws": _creality,
    "sdcp-ws": _sdcp,
    "mqtt": _mqtt,
    "photon-query": _photon,
}
assert set(_CHECKS) == RULE_IDS
