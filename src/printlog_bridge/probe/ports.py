"""The ports the probe checks, what each is suspected to be, and how an open one is identified.

Not a port scan: only ports that a printer family is known or reported to use (from #87, #95,
#96, #97, #114 and the community projects named here). `--port` adds others.
"""

from __future__ import annotations

from dataclasses import dataclass

HTTP = "http"
"""GET of each of `paths`."""
HTTPS = "https"
MOONRAKER = "moonraker"
"""GET of Moonraker's read-only endpoints."""
CREALITY_WS = "creality-ws"
SDCP = "sdcp"
"""SDCP v3: GET / and the websocket at /websocket."""
MQTT = "mqtt"
MQTT_TLS = "mqtt-tls"
PHOTON = "photon"
TCP = "tcp"
"""Connect only, and record anything the device sends first (a banner)."""
EXTRA = "extra"
"""A port added with `--port`: a banner, then GET /."""

MOONRAKER_PATHS = (
    "/server/info",
    "/printer/info",
    "/printer/objects/list",
    "/printer/objects/query?print_stats&virtual_sdcard&display_status&toolhead",
    "/machine/system_info",
    "/server/history/list?limit=3&order=desc",
)


@dataclass(frozen=True)
class Port:
    port: int
    kind: str
    suspected: str
    paths: tuple[str, ...] = ("/",)
    """For HTTP(S): what to GET."""

    def to_json(self) -> dict:
        return {"port": self.port, "kind": self.kind, "suspected": self.suspected}


PORTS = (
    Port(
        80,
        HTTP,
        "web UI: PrusaLink, Creality's stock page (K1, Hi), Mainsail/Fluidd on rooted Creality "
        "and on Rinkhals (Anycubic), OctoPrint behind a proxy, HeyGears (unknown)",
        ("/", "/api/version", "/api/v1/info", "/api/v1/status"),
    ),
    Port(443, HTTPS, "HTTPS web UI"),
    Port(322, TCP, "RTSPS camera (Bambu Lab X1/P2)"),
    Port(554, TCP, "RTSP camera"),
    Port(990, TCP, "FTPS, implicit TLS (Bambu Lab file access)"),
    Port(1883, MQTT, "plain MQTT broker (some resin and Chinese mainboards)"),
    Port(3000, TCP, "Chitu mainboard TCP (UDP 3000 is its discovery)"),
    Port(3030, SDCP, "SDCP v3 websocket at /websocket (Elegoo Centauri, Saturn 4, Mars 5)"),
    Port(4408, HTTP, "Fluidd (Creality K1/Hi stock or Helper Script)"),
    Port(4409, HTTP, "Mainsail (Creality Helper Script)"),
    Port(5000, HTTP, "OctoPrint's own server", ("/", "/api/version")),
    Port(6000, PHOTON, "Anycubic Photon (Mono X family) TCP text protocol"),
    Port(
        7125,
        MOONRAKER,
        "Moonraker (Klipper): Creality stock on some models, rooted K1, Rinkhals",
        MOONRAKER_PATHS,
    ),
    Port(8000, HTTP, "Creality WebRTC camera signaling (K2, Hi; ha_creality_ws const.py)"),
    Port(8080, HTTP, "webcam (mjpg-streamer on Creality K1/Hi), or OctoPrint"),
    Port(8081, HTTP, "second webcam"),
    Port(8883, MQTT_TLS, "Bambu Lab MQTT over TLS (needs the access code)"),
    Port(8899, TCP, "FlashForge TCP control"),
    Port(9883, MQTT_TLS, "Anycubic LAN-mode MQTT over mutual TLS (Kobra 3, S1; kobra-connect)"),
    Port(
        9999, CREALITY_WS, "Creality stock websocket, subprotocol wsslicer (K1, K2, Hi, Ender-3 V3)"
    ),
    Port(18910, HTTP, "Anycubic LAN-mode HTTP: GET /info (kobra-connect handshake.py)", ("/info",)),
)


def extra(port: int) -> Port:
    """A port the user added with `--port`: connect, read a banner, and GET /."""
    return Port(port, EXTRA, "added with --port")
