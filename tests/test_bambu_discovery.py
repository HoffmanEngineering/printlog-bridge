"""Hearing Bambu Lab printers announce themselves, for `printlog-bridge discover`."""

from __future__ import annotations

import asyncio
import socket

from fake_bambu import SERIAL

from printlog_bridge.adapters.bambu import discovery
from printlog_bridge.adapters.bambu.discovery import FoundBambu, listen, parse_notify
from printlog_bridge.cli.discover import format_bambu
from printlog_bridge.cli.main import main

NOTIFY = (
    "NOTIFY * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1990\r\n"
    "Server: UPnP/1.0\r\n"
    "Location: 192.168.1.50\r\n"
    "NT: urn:bambulab-com:device:3dprinter:1\r\n"
    f"USN: {SERIAL}\r\n"
    "Cache-Control: max-age=1800\r\n"
    "DevModel.bambu.com: C12\r\n"
    "DevName.bambu.com: Workshop P1S\r\n"
    "DevSignal.bambu.com: -44\r\n"
    "DevConnect.bambu.com: lan\r\n"
    "DevBind.bambu.com: free\r\n\r\n"
).encode()
"""As OpenBambuAPI and ha-bambulab describe the printer's announcement; not yet recorded (#17)."""


def test_reads_a_printers_announcement():
    assert parse_notify(NOTIFY, "192.168.1.50") == FoundBambu(
        "192.168.1.50", SERIAL, "C12", "Workshop P1S"
    )
    assert parse_notify(NOTIFY, "x").model_name == "P1S"


def test_ignores_anything_else():
    assert parse_notify(b"M-SEARCH * HTTP/1.1\r\n\r\n", "x") is None
    assert parse_notify(NOTIFY.replace(b"bambulab", b"other"), "x") is None
    assert parse_notify(b"\xff\xfe", "x") is None


def test_listens_for_announcements():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    async def scenario():
        listening = asyncio.ensure_future(listen(0.5, port=port))
        await asyncio.sleep(0.1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(NOTIFY, ("127.0.0.1", port))
            sender.sendto(NOTIFY, ("127.0.0.1", port))
        return await listening

    assert [f.serial for f in asyncio.run(scenario())] == [SERIAL]


def test_lists_a_section_to_paste():
    text = format_bambu([parse_notify(NOTIFY, "x")], None, 6)

    assert '192.168.1.50: P1S, "Workshop P1S", serial' in text
    assert "  [printer workshop-p1s]\n  adapter = bambu\n" in text
    assert f"  serial = {SERIAL}" in text
    assert "  access_code =" in text


def test_says_when_it_couldnt_listen():
    assert "Address already in use" in format_bambu(None, None, 6, "Address already in use")


def test_the_command_lists_a_printer_heard(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("PRINTLOG_BRIDGE_CONFIG", str(tmp_path / "missing.cfg"))

    async def heard(*args, **kwargs):
        return [parse_notify(NOTIFY, "x")]

    monkeypatch.setattr(discovery, "listen", heard)

    code = main(["discover", "--subnet", "127.0.6.0/30", "--port", "1", "--no-zeroconf"])

    out = capsys.readouterr().out
    assert code == 0
    assert "No Moonraker answered." in out
    assert "Found 1 Bambu Lab printer:" in out
