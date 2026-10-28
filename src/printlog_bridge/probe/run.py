"""Running a probe: discovery, the port checks and each open port's identification, all within
one window; then the trace file and the summary."""

from __future__ import annotations

import asyncio
import json
import platform
import re
import socket
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from printlog_bridge import __version__
from printlog_bridge.probe import allowlist, ports, services
from printlog_bridge.probe.discovery import Discovery, Network
from printlog_bridge.probe.record import (
    MAX_BODY,
    MAX_EVENTS,
    MAX_MESSAGE,
    MAX_PER_CHANNEL,
    Recorder,
)
from printlog_bridge.probe.redact import redact_trace, summary_redactor

GRACE_S = 15.0
"""How long past the window a slow request may take before it is cancelled."""
MIN_SECONDS, MAX_SECONDS = 5, 600


class ProbeError(Exception):
    """The probe couldn't start (a host that doesn't resolve)."""


async def resolve(host: str) -> str:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as e:
        raise ProbeError(f"can't resolve {host}: {e}") from None
    if not infos:
        raise ProbeError(f"can't resolve {host}")
    return infos[0][4][0]


async def _identify(
    rec: Recorder, client, address: str, port: ports.Port, deadline: float, disc: Discovery
) -> None:
    kind = port.kind
    if kind in (ports.HTTP, ports.MOONRAKER):
        for path in port.paths:
            await services.http_get(rec, client, "http", address, port.port, path)
    elif kind == ports.HTTPS:
        rec.send("tls-handshake", f"https:{port.port}", None)
        for path in port.paths:
            await services.http_get(rec, client, "https", address, port.port, path)
    elif kind == ports.EXTRA:
        await services.banner(rec, address, port.port)
        await services.http_get(rec, client, "http", address, port.port, "/")
    elif kind == ports.SDCP:
        await services.http_get(rec, client, "http", address, port.port, "/")
        await services.sdcp(rec, client, address, port.port, deadline, disc.board)
    elif kind == ports.CREALITY_WS:
        await services.creality(rec, client, address, port.port, deadline)
    elif kind in (ports.MQTT, ports.MQTT_TLS):
        await services.mqtt(rec, address, port.port, kind == ports.MQTT_TLS, deadline)
    elif kind == ports.PHOTON:
        await services.photon(rec, address, port.port)
    else:
        await services.banner(rec, address, port.port)


async def probe(
    host: str,
    seconds: float = 30,
    *,
    port_list: Sequence[ports.Port] = ports.PORTS,
    extra_ports: Sequence[int] = (),
    network: Network | None = None,
) -> dict[str, Any]:
    """Probes `host` for `seconds` and returns the trace (not redacted)."""
    address = await resolve(host)
    rec = Recorder()
    started = rec.started
    deadline = time.monotonic() + seconds
    known = {p.port for p in port_list}
    checked = list(port_list) + [ports.extra(p) for p in extra_ports if p not in known]

    disc = Discovery(rec, address, network)
    discovery = asyncio.ensure_future(disc.run(deadline))
    results = await asyncio.gather(*(services.tcp_check(rec, address, p.port) for p in checked))
    port_rows = [{**p.to_json(), **result} for p, result in zip(checked, results)]

    async with services.session() as client:
        work = [
            asyncio.ensure_future(_identify(rec, client, address, p, deadline, disc))
            for p, result in zip(checked, results)
            if result["open"]
        ]
        everything = work + [discovery]
        done, pending = await asyncio.wait(everything, timeout=seconds + GRACE_S)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2)
            rec.note(f"{len(pending)} task(s) were still running after the window and were stopped")
        for task in done:
            error = task.exception()
            if isinstance(error, allowlist.NotAllowed):
                raise error
            if error is not None:
                rec.error("probe", f"{error.__class__.__name__}: {error}")

    if sys.platform == "win32":
        rec.note(
            "Windows: Windows Defender Firewall drops unsolicited UDP (announcements, discovery "
            "replies) for Python unless it is allowed on private networks. The 'discovery' "
            "event counts datagrams from other hosts: if every count is 0, the firewall or "
            "the network kept them out, not the printer."
        )

    return {
        "probe": {
            "tool": "printlog-bridge probe",
            "version": __version__,
            "host": host,
            "address": address,
            "started": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
            "seconds": seconds,
            "platform": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "redacted": False,
        },
        "allowList": allowlist.to_json(),
        "limits": {
            "httpBodyBytes": MAX_BODY,
            "messageBytes": MAX_MESSAGE,
            "messagesPerChannel": MAX_PER_CHANNEL,
            "events": MAX_EVENTS,
        },
        "ports": port_rows,
        "events": rec.events,
        "notCounted": dict(rec.dropped),
        "notes": rec.notes,
        "sentCount": len(rec.sent),
    }


REDACTED_NOTE = (
    "redacted: credentials removed; serial numbers, MAC and IP addresses replaced with "
    "placeholders; binary payloads dropped. Look it over before you post it."
)
RAW_NOTE = (
    "NOT redacted: it may hold serial numbers, MAC and IP addresses and tokens. Keep it "
    "private, or run the probe again with --redact before attaching it to a public issue."
)


def finish(trace: dict[str, Any], redact: bool) -> dict[str, Any]:
    """The trace as it is written, saying whether it is redacted. `trace` itself gets the same
    note but keeps everything, for the summary (which hides what it must itself)."""
    trace["probe"]["redacted"] = redact
    trace["probe"]["mode"] = REDACTED_NOTE if redact else RAW_NOTE
    if redact:
        return redact_trace(trace, trace["probe"]["address"])
    return trace


def default_name(host: str, redact: bool, now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    label = "redacted" if redact else re.sub(r"[^A-Za-z0-9.-]", "_", host)
    return f"probe-{label}-{stamp}.json"


def write(trace: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(trace, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


# -- the summary -------------------------------------------------------------------------------


def _cut(text: str, length: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= length else text[: length - 3] + "..."


def _text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    if "text" in message:
        return message["text"]
    return f"<{message.get('size', 0)} bytes of binary>"


def _keys(texts: Sequence[str], limit: int = 20) -> list[str]:
    seen: dict[str, None] = {}
    for text in texts:
        try:
            value = json.loads(text)
        except ValueError:
            continue
        stack = [value]
        while stack and len(seen) < limit:
            item = stack.pop()
            if isinstance(item, dict):
                for key, sub in item.items():
                    seen.setdefault(key)
                    if isinstance(sub, (dict, list)):
                        stack.append(sub)
            elif isinstance(item, list):
                stack.extend(item[:3])
    return list(seen)[:limit]


def _title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return _cut(match.group(1), 80) if match else None


def summarize(trace: dict[str, Any], out: Path | None) -> str:
    """A short human summary. Credentials, identifiers and MACs are always hidden in it."""
    info = trace["probe"]
    events = trace["events"]
    lines = [
        f"printlog-bridge probe {info['version']}: {info['host']} ({info['address']}), "
        f"{info['seconds']:g} s, read-only",
        "",
        "Discovery (datagrams from the printer):",
    ]
    by_channel: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_channel.setdefault(event["channel"], []).append(event)

    heard = False
    for source, label in (
        ("mdns", "mDNS"),
        ("ssdp", "SSDP answer"),
        ("ssdp-notify", "SSDP NOTIFY"),
        ("bambu", "Bambu Lab NOTIFY"),
        ("chitu", "Chitu/SDCP reply"),
    ):
        received = [e for e in by_channel.get(f"udp:{source}", []) if e["kind"] == "receive"]
        if not received:
            continue
        heard = True
        lines.append(f"  {label}: {len(received)} datagram(s)")
        if source == "mdns":
            shown = set()
            for event in received:
                for record in event.get("records", []):
                    if record["type"] in ("SRV", "TXT") and len(shown) < 12:
                        item = f"    {record['type']} {record['name']}: {record['data']}"
                        if item not in shown:
                            shown.add(item)
                            lines.append(_cut(item, 200))
        else:
            for text in dict.fromkeys(_text(e.get("message")) for e in received[:3]):
                lines.append("    " + _cut(text, 200))
    if not heard:
        lines.append("  nothing")
    for event in by_channel.get("discovery", []):
        others = event.get("fromOtherHosts") or {}
        if others:
            counts = ", ".join(f"{k} {v}" for k, v in sorted(others.items()))
            lines.append(f"  (from other hosts, not recorded: {counts})")
        else:
            lines.append(
                "  (no datagrams from any other host either: multicast and broadcast "
                "may not reach this computer)"
            )

    open_ports = [p for p in trace["ports"] if p.get("open")]
    closed = [str(p["port"]) for p in trace["ports"] if not p.get("open")]
    lines += ["", "Open ports:" if open_ports else "Open ports: none on the list"]
    for row in open_ports:
        port = row["port"]
        lines.append(f"  {port}: {_cut(row['suspected'], 110)}")
        lines += _port_lines(port, by_channel)
    if closed:
        lines.append(f"Closed or filtered: {', '.join(closed)}")
    if trace["notes"]:
        lines += ["", "Notes:"] + [f"  - {note}" for note in trace["notes"]]
    by_rule: dict[str, int] = {}
    for event in events:
        if event["kind"] == "send":
            by_rule[event["rule"]] = by_rule.get(event["rule"], 0) + 1
    lines += [
        "",
        f"Sent {trace['sentCount']} message(s), every one on the allow-list printed above "
        "(and by --help). Nothing that changes the printer's state.",
    ]
    if by_rule:
        lines.append(_cut("  " + ", ".join(f"{k} {v}" for k, v in by_rule.items()), 200))
    if out is not None:
        lines.append(f"Trace: {out}")
        lines.append(f"  {info['mode']}")
    lines.append("Summary: credentials, serial numbers and MAC addresses hidden.")
    redactor = summary_redactor(trace)
    return "\n".join(redactor.text(line) for line in lines)


def _port_lines(port: int, by_channel: dict[str, list[dict[str, Any]]]) -> list[str]:
    lines = []
    for scheme in ("http", "https"):
        for event in by_channel.get(f"{scheme}:{port}", []):
            if event["kind"] == "receive":
                body = event["body"]
                headers = {k.lower(): v for k, v in event["headers"].items()}
                kind = headers.get("content-type", "")
                server = headers.get("server")
                text = body.get("text", "")
                detail = f"{kind or 'no type'}, {body['size']} bytes"
                if body.get("truncated"):
                    detail += "+"
                if server:
                    detail += f", Server: {server}"
                title = _title(text) if "html" in kind else None
                if title:
                    detail += f', title "{title}"'
                lines.append(f"    GET {event['path']} -> {event['status']} ({detail})")
                if "json" in kind and text:
                    lines.append("      " + _cut(text, 150))
            elif event["kind"] == "error":
                lines.append(f"    {event['error']}")
    for channel, events in by_channel.items():
        if channel.startswith(f"ws:{port}/"):
            texts = [
                _text(e["message"]) for e in events if "message" in e and e["kind"] == "receive"
            ]
            sent = sum(1 for e in events if e["kind"] == "send" and e["rule"] != "ws-connect")
            errors = [e["error"] for e in events if e["kind"] == "error"]
            if errors and not texts:
                lines.append(f"    websocket {channel[len(f'ws:{port}') :]}: {errors[0]}")
                continue
            keys = _keys(texts)
            lines.append(
                f"    websocket {channel[len(f'ws:{port}') :]}: {len(texts)} message(s) received, "
                f"{sent} request(s) sent"
            )
            if keys:
                lines.append(_cut("      fields: " + ", ".join(keys), 200))
    for event in by_channel.get(f"mqtt:{port}", []):
        if "connack" in event:
            lines.append(f"    MQTT: {event['meaning']}")
        elif "tls" in event:
            lines.append(f"    TLS {event['tls']['version']}")
        elif event["kind"] == "error":
            lines.append(f"    MQTT: {event['error']}")
    topics = {e["topic"] for e in by_channel.get(f"mqtt:{port}", []) if "topic" in e}
    if topics:
        lines.append(_cut(f"    MQTT topics: {', '.join(sorted(topics))}", 200))
    for event in by_channel.get(f"tcp:{port}", []):
        if event["kind"] == "receive" and "query" in event:
            lines.append(f"    {event['query']}: " + _cut(_text(event["message"]), 150))
        elif event["kind"] == "receive" and event.get("banner"):
            lines.append("    banner: " + _cut(_text(event["banner"]), 150))
        elif event["kind"] == "error":
            lines.append(f"    {event['error']}")
    return lines
