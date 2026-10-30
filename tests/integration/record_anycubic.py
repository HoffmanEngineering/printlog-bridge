"""Records what a real Anycubic printer in LAN mode answers, for `tests/fixtures/anycubic/` and
`tests/fake_anycubic.py` (#96, #97).

    python tests/integration/record_anycubic.py <printer ip> <raw dir> [--seconds 60]
    python tests/integration/record_anycubic.py --redact <raw dir>/<file>.json <fixture>.json

The first form does the handshake (`/info`, `/ctrl`), connects to the printer's MQTT broker with
the credentials it gives, subscribes to its reports and sends only the read-only queries in
`QUERIES` (the same the adapter sends: `adapters/anycubic/protocol.py`). It never sends anything
that changes the printer. It writes everything as received, **secrets included**, so keep
`<raw dir>` outside the repository. Run it while a print is under way to record one end to end:
`--seconds` as long as the print, plus a minute.

The second form writes a copy with the token, the serial, the credentials, certificates and keys,
device ids, IP and MAC addresses, Wi-Fi names and file names replaced by stable placeholders
(`printlog_bridge.probe.redact`, plus the fields that are secrets here), for the fixtures. Check
the result by eye before committing it: the repository is public.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

from printlog_bridge.adapters.anycubic import handshake as hs
from printlog_bridge.adapters.anycubic import protocol
from printlog_bridge.adapters.common.mqtt import MqttClient
from printlog_bridge.probe.redact import REDACTED, Redactor

SECRET_FIELDS = {"token", "info", "username", "password", "devicecrt", "devicepk", "cacrt",
                 "accountDeleteUrl", "fileUploadUrl", "fileUploadurl", "sign", "nonce",
                 "clientId", "deviceId", "cn", "usn", "mac", "ip", "rtsp", "rtspUrl"}  # fmt: skip


async def record(host: str, out: Path, seconds: float) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    start = time.time()

    def add(kind: str, **fields: Any) -> None:
        events.append({"t": round(time.time() - start, 3), "kind": kind, **fields})

    base = f"http://{host}:{hs.HTTP_PORT}"
    async with aiohttp.ClientSession() as http:
        info_raw, info = await hs.get_info(http, base)
        add("http", request="GET /info", answer=info_raw)
        ctrl_raw, credentials = await hs.get_credentials(http, base, info)
        add("http", request="POST /ctrl", answer=ctrl_raw)
    opened = hs._json_object(  # the whole plaintext, for the record
        hs.aes.decrypt_cbc(
            hs.base64.b64decode(ctrl_raw["data"]["info"]),
            info.token[16:32].encode(),
            ctrl_raw["data"]["token"].encode()[:16].ljust(16, b"\x00"),
        )
    )
    add("opened", value=opened)
    topics = protocol.Topics(info.model_id, credentials.device_id, resin=info.resin)
    kind = "lcd" if info.resin else "fdm"
    path = out / f"anycubic-{kind}-{int(start)}.json"
    try:
        client = await MqttClient.connect(
            credentials.broker_host,
            credentials.broker_port,
            protocol.tls_context(),
            client_id=credentials.client_id or protocol.client_id(),
            username=credentials.username,
            password=credentials.password,
        )
    except Exception as e:  # recorded: a broker that isn't there is worth a fixture too
        add("mqtt", event="connect failed", error=f"{e.__class__.__name__}: {e}")
        path.write_text(json.dumps(events, indent=1), encoding="utf-8")
        return path
    add("mqtt", event="connected")
    try:
        for topic in topics.subscriptions:
            await client.subscribe(topic)
            add("mqtt", event="subscribe", topic=topic)
        for query in protocol.queries(info.resin):
            topic, payload = topics.request(query)
            await client.publish(topic, payload.encode())
            add("send", topic=topic, payload=json.loads(payload))
        deadline = time.time() + seconds
        next_queries = time.time() + protocol.QUERY_INTERVAL_S
        while time.time() < deadline:
            try:
                topic, payload = await asyncio.wait_for(client.receive(), 5)
            except asyncio.TimeoutError:
                topic = None
            if topic is not None:
                text = payload.decode("utf-8", errors="replace")
                try:
                    body: Any = json.loads(text)
                except ValueError:
                    body = text
                add("receive", topic=topic, payload=body)
            if time.time() >= next_queries:
                next_queries = time.time() + protocol.QUERY_INTERVAL_S
                for query in protocol.queries(info.resin, periodic=True):
                    topic, payload = topics.request(query)
                    await client.publish(topic, payload.encode())
                    add("send", topic=topic, payload=json.loads(payload))
    finally:
        await client.close()
    path.write_text(json.dumps(events, indent=1), encoding="utf-8")
    return path


def redact(raw: Path, fixture: Path) -> None:
    events = json.loads(raw.read_text(encoding="utf-8"))
    redactor = Redactor(ips=True)

    def learn_secrets(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                learn_secrets(v, k)
        elif isinstance(value, list):
            for v in value:
                learn_secrets(v, key)
        elif isinstance(value, str) and key in SECRET_FIELDS and len(value) >= 4 and value != key:
            kind = {"deviceId": "device", "cn": "serial", "usn": "serial", "mac": "mac",
                    "ip": "ip", "clientId": "client"}.get(key)  # fmt: skip
            redactor.values[value] = redactor.placeholder(kind, value) if kind else REDACTED

    learn_secrets(events)
    redactor.learn(events)
    out = redactor.apply(events)
    text = json.dumps(out, indent=1)
    # Nothing learned may be left anywhere, in a topic or a URL either.
    for value in redactor.values:  # (the P1 says "clientId": "clientId": not a secret)
        if value in text:
            raise SystemExit(f"a secret is still in the redacted trace ({len(value)} characters)")
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(text + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("out")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--redact", action="store_true")
    args = parser.parse_args()
    if args.redact:
        redact(Path(args.target), Path(args.out))
    else:
        print(asyncio.run(record(args.target, Path(args.out), args.seconds)))


if __name__ == "__main__":
    sys.exit(main())
