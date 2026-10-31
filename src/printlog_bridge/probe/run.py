"""Running a probe: discovery, the port checks and each open port's identification, all within
one window; then the trace file and the summary."""

from __future__ import annotations

import asyncio
import contextlib
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
    MAX_KEPT_BYTES,
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


RESOLVE_TIMEOUT_S = 5.0


async def _getaddrinfo(host: str) -> list:
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)


async def resolve(host: str) -> str:
    """`host`'s IPv4 address. Raises `ProbeError` when it doesn't resolve, or not within
    `RESOLVE_TIMEOUT_S` (a dead DNS server)."""
    try:
        infos = await asyncio.wait_for(_getaddrinfo(host), RESOLVE_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise ProbeError(
            f"can't resolve {host}: no answer within {RESOLVE_TIMEOUT_S:g} s"
        ) from None
    except (socket.gaierror, UnicodeError) as e:
        raise ProbeError(f"can't resolve {host}: {e}") from None
    if not infos:
        raise ProbeError(f"can't resolve {host}")
    return infos[0][4][0]


async def _identify(
    rec: Recorder,
    client,
    address: str,
    port: ports.Port,
    deadline: float,
    disc: Discovery,
    photon: bool,
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
    elif kind == ports.PHOTON and photon:
        await services.photon(rec, address, port.port)
    elif kind == ports.PHOTON:
        await services.banner(rec, address, port.port)
        rec.note(
            f"port {port.port} is open, and nothing was sent to it but a connect: the "
            "Anycubic Photon's sysinfo and getstatus go only to a printer known to be a "
            "Photon (run again with --photon)"
        )
    else:
        await services.banner(rec, address, port.port)


class Probe:
    """One probe of `host`. `run` fills the recorder as it goes, so `trace` has whatever was
    recorded even when the run was stopped (Ctrl+C)."""

    def __init__(
        self,
        host: str,
        seconds: float = 30,
        *,
        port_list: Sequence[ports.Port] = ports.PORTS,
        extra_ports: Sequence[int] = (),
        network: Network | None = None,
        photon: bool = False,
    ) -> None:
        self.host = host
        self.seconds = seconds
        known = {p.port for p in port_list}
        self.checked = list(port_list) + [ports.extra(p) for p in extra_ports if p not in known]
        self.network = network
        self.photon = photon
        self.rec = Recorder()
        self.address: str | None = None
        self.port_rows: list[dict[str, Any]] = [{**p.to_json(), "open": None} for p in self.checked]
        self.interrupted = False

    async def run(self) -> None:
        """Raises `ProbeError` when the host doesn't resolve."""
        rec = self.rec
        self.address = address = await resolve(self.host)
        deadline = time.monotonic() + self.seconds
        disc = Discovery(rec, address, self.network)
        discovery = asyncio.ensure_future(disc.run(deadline))
        try:
            results = await asyncio.gather(
                *(services.tcp_check(rec, address, p.port) for p in self.checked)
            )
            self.port_rows = [{**p.to_json(), **r} for p, r in zip(self.checked, results)]
            async with services.session() as client:
                work = [
                    asyncio.ensure_future(
                        _identify(rec, client, address, p, deadline, disc, self.photon)
                    )
                    for p, result in zip(self.checked, results)
                    if result["open"]
                ]
                await self._wait(work + [discovery])
        finally:
            if not discovery.done():
                discovery.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait([discovery], timeout=2)

    async def _wait(self, tasks: list[asyncio.Future]) -> None:
        rec = self.rec
        try:
            done, pending = await asyncio.wait(tasks, timeout=self.seconds + GRACE_S)
        except asyncio.CancelledError:  # Ctrl+C: stop the work, keep what was recorded
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks, timeout=2)
            raise
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2)
            rec.note(f"{len(pending)} task(s) were still running after the window and were stopped")
        for task in done:
            error = task.exception()
            if isinstance(error, allowlist.NotAllowed):
                rec.error("probe", f"a task stopped at a send the allow-list refused: {error}")
            elif error is not None:
                rec.error("probe", f"{error.__class__.__name__}: {error}")

    def trace(self) -> dict[str, Any]:
        """The trace so far (not redacted)."""
        rec = self.rec
        if self.interrupted:
            rec.note("stopped early with Ctrl+C: the trace holds what was recorded until then")
        if sys.platform == "win32":
            rec.note(
                "Windows: Windows Defender Firewall drops unsolicited UDP (announcements, "
                "discovery replies) for Python unless it is allowed on private networks. The "
                "'discovery' event counts datagrams from other hosts: if every count is 0, the "
                "firewall or the network kept them out, not the printer."
            )
        return {
            "probe": {
                "tool": "printlog-bridge probe",
                "version": __version__,
                "host": self.host,
                "address": self.address,
                "started": datetime.fromtimestamp(rec.started, timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "seconds": self.seconds,
                "photon": self.photon,
                "interrupted": self.interrupted,
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
                "keptBytes": MAX_KEPT_BYTES,
            },
            "ports": self.port_rows,
            "events": rec.events,
            "notCounted": dict(rec.dropped),
            "notes": rec.notes,
            "sentCount": len(rec.sent),
            "refused": [{"rule": r, "error": e} for r, e in rec.refused],
        }


async def probe(host: str, seconds: float = 30, **options: Any) -> dict[str, Any]:
    """Probes `host` for `seconds` and returns the trace (not redacted)."""
    run = Probe(host, seconds, **options)
    await run.run()
    return run.trace()


REDACTED_NOTE = (
    "redacted: credentials removed; serial numbers, MAC and IP addresses, Wi-Fi and host names "
    "and file names replaced with placeholders; binary payloads dropped. Look it over before "
    "you post it: free text, such as a printer's own name, can still identify it."
)
RAW_NOTE = (
    "NOT redacted: it may hold serial numbers, MAC and IP addresses, Wi-Fi names, file "
    "names and tokens. Keep it "
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
    """A short human summary. It never shows mDNS records or a Photon's answers as they came
    (the trace has them), and hides what `--redact` hides but IP addresses."""
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
            lines += _mdns_lines(received)
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
    if trace.get("notCounted"):
        lines += ["", "Past the trace's caps (see 'limits'):"]
        for channel, count in sorted(trace["notCounted"].items()):
            lines.append(f"  {count} message(s) on {channel} were not kept")
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
    if trace.get("refused"):
        lines.append(
            f"Refused by the allow-list and not sent: {len(trace['refused'])} (see 'refused')"
        )
    if out is not None:
        lines.append(f"Trace: {out}")
        lines.append(f"  {info['mode']}")
    lines.append(
        "Summary: credentials, serial numbers, MAC addresses, Wi-Fi, host and file names are "
        "hidden; IP addresses are shown. mDNS records and raw answers are only in the trace."
    )
    redactor = summary_redactor(trace)
    return "\n".join(redactor.text(line) for line in lines)


def _mdns_lines(received: list[dict[str, Any]]) -> list[str]:
    """Each service type the printer answered for, its port and its TXT keys (not values)."""
    services: dict[str, dict[str, Any]] = {}
    for event in received:
        for record in event.get("records", []):
            name = str(record.get("name", ""))
            service = ".".join(name.split(".")[-3:]) if name.count(".") >= 2 else name
            if record["type"] == "SRV" and isinstance(record.get("data"), dict):
                entry = services.setdefault(service, {"ports": set(), "txt": set()})
                entry["ports"].add(record["data"].get("port"))
            elif record["type"] == "TXT" and isinstance(record.get("data"), list):
                entry = services.setdefault(service, {"ports": set(), "txt": set()})
                entry["txt"].update(str(t).partition("=")[0] for t in record["data"] if t)
    lines = []
    for service, entry in sorted(services.items())[:12]:
        ports_text = ", ".join(str(p) for p in sorted(entry["ports"]) if p) or "?"
        keys = ", ".join(sorted(entry["txt"])) or "none"
        lines.append(_cut(f"    {service}: port {ports_text}; TXT keys: {keys}", 200))
    return lines


def _photon_answer(text: str) -> str:
    """A Photon's answer without its serial or Wi-Fi name: its size, and for `sysinfo` the
    model and firmware, for `getstatus` the state."""
    fields = text.split(",")
    if fields[:1] == ["sysinfo"] and len(fields) >= 3:
        return _cut(f"model {fields[1]}, firmware {fields[2]} ({len(text)} bytes)", 150)
    if fields[:1] == ["getstatus"] and len(fields) >= 2:
        return f"state {fields[1]} ({len(text)} bytes)"
    return f"{len(text)} bytes"


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
                lines.append(f"    GET {event['request']} -> {event['status']} ({detail})")
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
            lines.append(f"    {event['query']}: {_photon_answer(_text(event['message']))}")
        elif event["kind"] == "receive" and event.get("banner"):
            lines.append("    banner: " + _cut(_text(event["banner"]), 150))
        elif event["kind"] == "error":
            lines.append(f"    {event['error']}")
    return lines
