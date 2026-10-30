"""`printlog-bridge discover`: lists the Moonraker, Bambu Lab and Elegoo (SDCP) printers on the
local network, with a `[printer]` section for each, ready to paste into the config."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence

from printlog_bridge.adapters.bambu.discovery import SSDP_PORT, FoundBambu
from printlog_bridge.adapters.moonraker import discovery
from printlog_bridge.adapters.moonraker.discovery import Found
from printlog_bridge.adapters.sdcp.discovery import FoundSdcp
from printlog_bridge.adapters.sdcp.protocol import DISCOVERY_PORT as SDCP_DISCOVERY_PORT
from printlog_bridge.core.config import BridgeConfig


def subnets_to_scan(
    given: Sequence[ipaddress.IPv4Network], config: BridgeConfig | None
) -> list[ipaddress.IPv4Network]:
    """`--subnet`s when given; otherwise the /24 of every configured printer's address, and
    this host's own."""
    if given:
        return list(dict.fromkeys(given))
    networks = []
    for host in _configured(config):
        network = discovery.subnet_of(host[0])
        if network is not None:
            networks.append(network)
    networks.extend(discovery.local_subnets())
    return list(dict.fromkeys(networks))


async def discover(
    networks: Sequence[ipaddress.IPv4Network],
    *,
    port: int = discovery.DEFAULT_PORT,
    zeroconf: bool = True,
    timeout: float = discovery.PROBE_TIMEOUT_S,
) -> list[Found]:
    """Every Moonraker that answers on `networks` at `port`, or over zeroconf."""
    found = await discovery.scan(
        networks, lambda address: f"http://{address}:{port}", timeout=timeout, details=True
    )
    seen = {(f.address, f.port) for f in found}
    if zeroconf:
        for address, mdns_port in await discovery.browse_mdns():
            if (address, mdns_port) in seen:
                continue
            seen.add((address, mdns_port))
            one = await discovery.probe(
                f"http://{address}:{mdns_port}", timeout=timeout, details=True
            )
            if one is not None:
                found.append(one)
    found.sort(key=lambda f: (ipaddress.ip_address(f.address), f.port))
    return found


def format_found(
    found: Sequence[Found],
    networks: Sequence[ipaddress.IPv4Network],
    config: BridgeConfig | None,
    *,
    zeroconf: bool = True,
) -> str:
    where = ", ".join(str(n) for n in networks) or "no network"
    lines = [f"Looked on {where}{' and over zeroconf' if zeroconf else ''}."]
    if not found:
        lines.append("No Moonraker answered.")
        lines.extend(_hints())
        return "\n".join(lines)

    configured = {(host, port): name for host, port, name in _configured_names(config)}
    lines.append(f"Found {len(found)} Moonraker printer{'' if len(found) == 1 else 's'}:")
    for f in found:
        url = f"http://{f.address}:{f.port}"
        lines.append("")
        if f.unauthorized:
            lines.append(
                f"{url}: asks for an API key (Moonraker's [authorization]). Set "
                "moonraker_api_key in its [printer] section, or add this host to "
                "trusted_clients in moonraker.conf."
            )
            continue
        hostname = str(f.printer_info.get("hostname") or "")
        details = [
            f"host name {hostname}" if hostname else "",
            f"Klipper {f.printer_info['software_version']}"
            if f.printer_info.get("software_version")
            else f"Klipper {f.server_info.get('klippy_state', 'unknown')}",
            f"Moonraker {f.server_info['moonraker_version']}"
            if f.server_info.get("moonraker_version")
            else "",
            f"{f.tools} tool{'' if f.tools == 1 else 's'}" if f.tools else "",
        ]
        lines.append(f"{url}: {', '.join(d for d in details if d)}")
        name = configured.get((f.address, f.port))
        if name is not None:
            lines.append(f"  already in the config as [printer {name}]")
            continue
        lines.extend(
            [
                f"  [printer {_section_name(hostname or f.address)}]",
                "  printer_id =      ; the number at the end of its page in 3D Print Log",
                f"  url = {url}",
            ]
        )
    lines.append("")
    lines.append(
        "Paste a section into printlog-bridge.cfg (without the indent) and fill in printer_id. "
        "Give each printer a DHCP reservation on the router if you can; the bridge also finds "
        "one again when its address changes."
    )
    return "\n".join(lines)


def _hints() -> list[str]:
    hints = [
        "Is the printer on and on this network? Moonraker listens on port 7125 unless "
        "moonraker.conf says otherwise (try --port).",
    ]
    if discovery.in_container():
        hints.append(
            "This looks like a container: with Docker's default (bridge) networking it sees its "
            "own network, not your LAN's. Name your LAN with --subnet, such as "
            "--subnet 192.168.1.0/24."
        )
    return hints


def _configured(config: BridgeConfig | None) -> list[tuple[str, int]]:
    return [(host, port) for host, port, _ in _configured_names(config)]


def _configured_names(config: BridgeConfig | None) -> list[tuple[str, int, str]]:
    if config is None:
        return []
    out = []
    for printer in config.printers:
        url = getattr(printer.adapter, "url", None)
        if url:
            out.append((discovery.host_of(url), discovery.port_of(url), printer.name))
    return out


def _section_name(text: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", text.strip()).strip("-").lower()
    return name or "printer"


def format_bambu(
    found: Sequence[FoundBambu] | None,
    config: BridgeConfig | None,
    seconds: float,
    error: str | None = None,
) -> str:
    """The Bambu Lab printers that announced themselves, with a section for each. `found` is
    None when the bridge couldn't listen (`error` says why)."""
    if found is None:
        return f"Couldn't listen for Bambu Lab printers on UDP port {SSDP_PORT}: {error}."
    lines = [f"Listened {seconds:g}s for Bambu Lab printers (UDP port {SSDP_PORT})."]
    if not found:
        lines.append("No Bambu Lab printer announced itself.")
        if discovery.in_container():
            lines.append(
                "This looks like a container: a printer's announcement only reaches one on the "
                "host's network (network_mode: host)."
            )
        return "\n".join(lines)
    configured = {
        getattr(p.adapter, "serial", None): p.name for p in (config.printers if config else [])
    }
    lines.append(f"Found {len(found)} Bambu Lab printer{'' if len(found) == 1 else 's'}:")
    for f in found:
        details = [f.model_name or "", f'"{f.name}"' if f.name else "", f"serial {f.serial}"]
        lines.append("")
        lines.append(f"{f.address}: {', '.join(d for d in details if d)}")
        name = configured.get(f.serial)
        if name is not None:
            lines.append(f"  already in the config as [printer {name}]")
            continue
        lines.extend(
            [
                f"  [printer {_section_name(f.name or f.model_name or f.serial)}]",
                "  adapter = bambu",
                "  printer_id =      ; the number at the end of its page in 3D Print Log",
                f"  host = {f.address}",
                f"  serial = {f.serial}",
                "  access_code =     ; on the printer's screen (LAN Only settings)",
            ]
        )
    lines.append("")
    lines.append(
        "Fill in printer_id and access_code. Bambu firmware with Authorization Control also "
        "needs LAN Only Mode and Developer Mode on the printer."
    )
    return "\n".join(lines)


def format_sdcp(
    found: Sequence[FoundSdcp] | None,
    config: BridgeConfig | None,
    seconds: float,
    error: str | None = None,
) -> str:
    """The Elegoo (SDCP) printers that answered `M99999`, once per address, with a section for
    each. `found` is None when the bridge couldn't ask (`error` says why)."""
    if found is None:
        return f"Couldn't ask for Elegoo printers on UDP port {SDCP_DISCOVERY_PORT}: {error}."
    lines = [
        f"Asked for Elegoo (SDCP) printers on UDP port {SDCP_DISCOVERY_PORT} and waited "
        f"{seconds:g}s."
    ]
    by_address: dict[str, FoundSdcp] = {}
    for f in found:
        by_address.setdefault(f.address, f)
    if not by_address:
        lines.append("No Elegoo printer answered.")
        if discovery.in_container():
            lines.append(
                "This looks like a container: the question is a broadcast, which only reaches "
                "the printers from the host's network (network_mode: host)."
            )
        return "\n".join(lines)
    configured = {
        getattr(p.adapter, "host", None): p.name
        for p in (config.printers if config else [])
        if getattr(p.adapter, "name", None) == "sdcp"
    }
    count = len(by_address)
    lines.append(f"Found {count} Elegoo printer{'' if count == 1 else 's'}:")
    for f in by_address.values():
        details = [
            f.model or "",
            f'"{f.name}"' if f.name else "",
            f"firmware {f.firmware}" if f.firmware else "",
        ]
        lines.append("")
        lines.append(f"{f.address}: {', '.join(d for d in details if d)}")
        name = configured.get(f.address)
        if name is not None:
            lines.append(f"  already in the config as [printer {name}]")
            continue
        if f.resin:
            lines.append(
                "  a resin printer: the sdcp adapter doesn't follow resin prints yet (#97)"
            )
            continue
        lines.extend(
            [
                f"  [printer {_section_name(f.name or f.model or f.address)}]",
                "  adapter = sdcp",
                "  printer_id =      ; the number at the end of its page in 3D Print Log",
                f"  host = {f.address}",
            ]
        )
    lines.append("")
    lines.append(
        "Fill in printer_id. SDCP has no password: the bridge only reads the printer's status."
    )
    return "\n".join(lines)
