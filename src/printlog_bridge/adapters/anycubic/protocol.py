"""The MQTT side of Anycubic's LAN mode: topics, the read-only queries the bridge sends, and TLS.

Checked against a Kobra S1 (firmware 2.x, `deviceType` `fdm`) and a Photon P1 Max (`lcd`) in
October 2026, with the sources `handshake.py` names.

- **Topics.** The printer reports on `anycubic/anycubicCloud/v1/printer/public/<modelId>/
  <deviceId>/<type>/report` (and deeper), and takes requests on `anycubic/anycubicCloud/v1/
  <origin>/printer/<modelId>/<deviceId>/<type>`, where `origin` is `web` for the FDM printers (as
  Anycubic's app) and `pc` for the resin ones (as Photon Workshop). `deviceId` comes from `/ctrl`.
- **A request** is `{"type", "action", "timestamp" (ms), "msgid" (a UUID), "data": null}`; the
  answer comes on the report topic with the same `type` and `action`.
- **What the bridge sends** is `READ_ONLY` and nothing else (`request()` refuses any other):
  `info`/`query` (the FDM printers' whole state, the job in `project`), `print`/`query` (the job;
  silent while idle), `multiColorBox`/`getInfo` (the ACE units' slots) and `peripherie`/`query`
  (what is fitted) on an FDM printer; `status`/`query`, `lanInfo`/`query` (both on the `status`
  topic, as Photon Workshop sends them) and `print`/`query` on a resin one. Never `start`,
  `pause`, `resume`, `stop`, a light, fan or temperature, a file, `ota` or any `set`.
- **TLS**: the broker's certificate is self-signed (on the P1, RSA 1024 with SHA-1, from "TLS
  Project Dodgy Certificate Authority"), so it isn't verified, and the cipher list allows the
  weak key (`SECLEVEL=0`). The broker doesn't ask for a client certificate. What that leaves
  open: something on the LAN posing as the printer would get this connection's MQTT login,
  which works for nothing but this printer's broker and changes with every handshake.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import ssl
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

PREFIX = "anycubic/anycubicCloud/v1"
QUERY_INTERVAL_S = 30.0
"""How often the queries go again: the broker doesn't push everything by itself."""

FDM_QUERIES = (("info", "query"), ("print", "query"), ("multiColorBox", "getInfo"),
               ("peripherie", "query"))  # fmt: skip
FDM_PERIODIC = (("info", "query"), ("print", "query"), ("multiColorBox", "getInfo"))
RESIN_QUERIES = (("status", "query"), ("lanInfo", "query"), ("print", "query"))
RESIN_PERIODIC = (("status", "query"), ("print", "query"))
READ_ONLY = frozenset(FDM_QUERIES + RESIN_QUERIES)
"""Every (type, action) the bridge may send. Each is a query: none changes the printer."""
TOPIC_OF = {"lanInfo": "status"}
"""A request whose topic isn't its type: Photon Workshop asks `lanInfo` on `status`."""


class NotReadOnly(ValueError):
    """A request that isn't on the read-only list."""


@dataclass(frozen=True)
class Query:
    type: str
    action: str


@dataclass(frozen=True)
class Topics:
    model_id: str
    device_id: str
    resin: bool = False

    @property
    def reports(self) -> str:
        return f"{PREFIX}/printer/public/{self.model_id}/{self.device_id}"

    @property
    def subscriptions(self) -> tuple[str, ...]:
        return (f"{self.reports}/#",)

    def is_report(self, topic: str) -> bool:
        return topic.startswith(self.reports + "/")

    def request(self, query: Query) -> tuple[str, str]:
        """The topic and the JSON text of `query`. Raises NotReadOnly for anything but a
        read-only query."""
        if (query.type, query.action) not in READ_ONLY:
            raise NotReadOnly(f"{query.type}/{query.action} isn't a read-only query")
        origin = "pc" if self.resin else "web"
        kind = TOPIC_OF.get(query.type, query.type)
        topic = f"{PREFIX}/{origin}/printer/{self.model_id}/{self.device_id}/{kind}"
        body = {
            "type": query.type,
            "action": query.action,
            "timestamp": int(time.time() * 1000),
            "msgid": str(uuid.uuid4()),
            "data": None,
        }
        return topic, json.dumps(body)


def queries(resin: bool, *, periodic: bool = False) -> Sequence[Query]:
    if resin:
        pairs = RESIN_PERIODIC if periodic else RESIN_QUERIES
    else:
        pairs = FDM_PERIODIC if periodic else FDM_QUERIES
    return [Query(t, a) for t, a in pairs]


def tls_context() -> ssl.SSLContext:
    """TLS without verifying the broker's self-signed certificate, allowing its weak key."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    # An OpenSSL without security levels refuses the string; its defaults allow the key.
    with contextlib.suppress(ssl.SSLError):
        context.set_ciphers("DEFAULT:@SECLEVEL=0")
    return context


def client_id() -> str:
    """A client id of the bridge's own, for a broker that takes any (the FDM printers)."""
    return f"printlog-bridge-{secrets.token_hex(6)}"


def kind_of(topic: str) -> str | None:
    """The report's type from its topic (`.../<deviceId>/<type>/report`), for a message whose
    `type` is missing."""
    parts = topic.split("/")
    if len(parts) >= 2 and parts[-1] == "report":
        return parts[-2]
    return None
