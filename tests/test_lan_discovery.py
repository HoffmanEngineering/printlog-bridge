"""The bridge's discovery while it runs (#100): mDNS, the adapters' hooks, and the service that
keeps only recognized printers, bounded, de-duplicated and expiring."""

from __future__ import annotations

import asyncio
import ipaddress
import struct

import pytest
from fake_moonraker import FakeMoonraker

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.adapters.bambu.discovery import BambuDiscovery
from printlog_bridge.adapters.moonraker.discovery import MoonrakerDiscovery
from printlog_bridge.adapters.prusalink.discovery import PrusaLinkDiscovery
from printlog_bridge.core import mdns
from printlog_bridge.core.discovery import Discovery, DiscoveryService, Found, clean_mac
from printlog_bridge.core.mdns import MdnsAnswer

NOTIFY = (
    b"NOTIFY * HTTP/1.1\r\n"
    b"Host: 239.255.255.250:1990\r\n"
    b"Server: UPnP/1.0\r\n"
    b"Location: 192.168.1.60\r\n"
    b"NT: urn:bambulab-com:device:3dprinter:1\r\n"
    b"USN: 01P00A123456789\r\n"
    b"DevModel.bambu.com: BL-P001\r\n"
    b"DevName.bambu.com: Workshop X1C\r\n"
    b"\r\n"
)


def record(owner: bytes, kind: int, rdata: bytes) -> bytes:
    return owner + struct.pack("!HHIH", kind, 0x8001, 120, len(rdata)) + rdata


def txt(*pairs: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in pairs)


def answer_packet(service: str, instance: str, host: str, address: str, port: int, *pairs: str):
    s, i, h = (mdns.encode_name(n) for n in (service, f"{instance}.{service}", host))
    records = [
        record(s, 12, i),
        record(i, 33, struct.pack("!HHH", 0, 0, port) + h),
        record(h, 1, ipaddress.IPv4Address(address).packed),
    ]
    if pairs:
        records.append(record(i, 16, txt(*pairs)))
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, len(records) - 1)
    return header + b"".join(records)


class TestMdns:
    def test_reads_instances_with_their_txt(self):
        packet = answer_packet(
            "_prusa-link._tcp.local", "Prusa MK4", "mk4.local", "192.168.1.70", 80,
            "printer_type=MK4", "path=/",
        )  # fmt: skip

        [answer] = mdns.parse(packet, ["_prusa-link._tcp.local"])

        assert answer == MdnsAnswer(
            "_prusa-link._tcp.local",
            "prusa mk4",
            "mk4.local",
            "192.168.1.70",
            80,
            {"printer_type": "MK4", "path": "/"},
        )

    def test_answers_for_other_services_are_ignored(self):
        packet = answer_packet("_http._tcp.local", "nas", "nas.local", "192.168.1.2", 80)
        assert mdns.parse(packet, ["_moonraker._tcp.local"]) == []

    def test_one_query_asks_for_every_service(self):
        query = mdns.query_packet(["_moonraker._tcp.local", "_prusa-link._tcp.local"])
        assert struct.unpack_from("!H", query, 4)[0] == 2
        assert query.count(b"\x00\x0c\x00\x01") == 2


class TestHooks:
    def test_every_adapter_has_a_hook(self):
        hooks = {name: adapter.discovery() for name, adapter in ADAPTERS.items()}
        assert {name: h.kind for name, h in hooks.items()} == {n: n for n in ADAPTERS}

    def test_bambu_announcements_are_read_without_sending_anything(self):
        found = BambuDiscovery().from_datagram(2021, NOTIFY, "192.168.1.60")
        assert found == Found(
            "bambu", "192.168.1.60", 8883, model="X1 Carbon", name="Workshop X1C",
            serial="01P00A123456789",
        )  # fmt: skip

    def test_anything_else_on_bambus_port_is_not_a_printer(self):
        assert BambuDiscovery().from_datagram(2021, b"M-SEARCH * HTTP/1.1\r\n", "1.2.3.4") is None

    def test_prusalink_is_recognized_under_its_own_service(self):
        answer = MdnsAnswer("_prusa-link._tcp.local", "mk4", "mk4.local", "192.168.1.70", 80,
                            {"printer_type": "MK4S"})  # fmt: skip
        found = asyncio.run(PrusaLinkDiscovery().from_mdns(answer))
        assert found == Found("prusalink", "192.168.1.70", 80, model="MK4S", name="mk4")

    def test_octoprint_counts_only_when_it_names_prusa(self):
        hook = PrusaLinkDiscovery()
        prusa = MdnsAnswer("_octoprint._tcp.local", "PrusaLink MINI", "mini.local",
                           "192.168.1.71", 80, {})  # fmt: skip
        octo = MdnsAnswer("_octoprint._tcp.local", "OctoPrint on ender", "ender.local",
                          "192.168.1.72", 80, {"vendor": "Creality"})  # fmt: skip
        assert asyncio.run(hook.from_mdns(prusa)) is not None
        assert asyncio.run(hook.from_mdns(octo)) is None

    def test_a_moonraker_that_answers_is_identified_by_its_macs(self):
        async def scenario():
            moonraker = FakeMoonraker(mac="2c:cf:67:00:00:01", hostname="voron")
            url = await moonraker.start()
            port = int(url.rsplit(":", 1)[1].split("/")[0])
            try:
                answer = MdnsAnswer("_moonraker._tcp.local", "Moonraker on voron", "voron.local",
                                    "127.0.0.1", port, {})  # fmt: skip
                return await MoonrakerDiscovery().from_mdns(answer), port
            finally:
                await moonraker.close()

        found, port = asyncio.run(scenario())
        assert found == Found("moonraker", "127.0.0.1", port, name="voron",
                              macs=("2c:cf:67:00:00:01",))  # fmt: skip

    def test_something_else_on_the_port_isnt_a_moonraker(self):
        answer = MdnsAnswer("_moonraker._tcp.local", "x", "x.local", "127.0.0.1", 9, {})
        assert asyncio.run(MoonrakerDiscovery(timeout=0.5).from_mdns(answer)) is None


class FakeHook(Discovery):
    kind = "moonraker"
    mdns_services = ("_moonraker._tcp.local",)
    udp_ports = (2021,)

    def __init__(self) -> None:
        self.asked: list[MdnsAnswer] = []
        self.swept: list = []

    async def from_mdns(self, answer):
        self.asked.append(answer)
        return Found("moonraker", answer.address, answer.port, name=answer.instance,
                     macs=("2c:cf:67:00:00:0a",))  # fmt: skip

    def from_datagram(self, port, data, sender):
        return Found("moonraker", sender, 7125) if data == b"printer" else None

    async def sweep(self, networks):
        self.swept.append(list(networks))
        return [Found("moonraker", "192.168.1.90", 7125, name="swept")]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def service(hook=None, answers=(), clock=None, **options):
    async def ask(services, wait):
        return list(answers)

    listeners = {}

    async def bind(port, on_datagram):
        listeners[port] = on_datagram
        return None

    found = DiscoveryService(
        [hook or FakeHook()], ask=ask, bind=bind, clock=clock or Clock(), **options
    )
    found.listeners = listeners
    return found


MOONRAKER = MdnsAnswer("_moonraker._tcp.local", "voron", "voron.local", "192.168.1.50", 7125, {})


class TestService:
    def test_only_what_a_hook_recognizes_is_kept(self):
        hook = FakeHook()
        other = MdnsAnswer("_http._tcp.local", "nas", "nas.local", "192.168.1.2", 80, {})
        s = service(hook, [MOONRAKER, other])

        asyncio.run(s.mdns_round(0))

        assert [f.address for f in s.found()] == ["192.168.1.50"]
        assert [a.address for a in hook.asked] == ["192.168.1.50"]

    def test_a_public_address_is_never_kept_or_asked(self):
        hook = FakeHook()
        public = MdnsAnswer("_moonraker._tcp.local", "x", "x", "8.8.8.8", 7125, {})
        s = service(hook, [public])
        asyncio.run(s.mdns_round(0))
        assert s.found() == [] and hook.asked == []
        assert s.heard(Found("moonraker", "8.8.8.8", 7125), "test") is None

    def test_a_known_printer_isnt_asked_again(self):
        hook = FakeHook()
        s = service(hook, [MOONRAKER])
        asyncio.run(s.mdns_round(0))
        asyncio.run(s.mdns_round(0))
        assert len(hook.asked) == 1 and len(s.found()) == 1

    def test_the_same_printer_at_a_new_address_is_one_entry(self):
        s = service()
        s.heard(Found("bambu", "192.168.1.60", 8883, serial="01P00A1"), "udp")
        s.heard(Found("bambu", "192.168.1.61", 8883, serial="01P00A1", name="X1C"), "udp")
        [only] = s.found()
        assert only.address == "192.168.1.61" and only.name == "X1C"

    def test_an_entry_learns_its_mac_without_becoming_two(self):
        s = service()
        s.heard(Found("moonraker", "192.168.1.50", 7125), "scan")
        s.heard(Found("moonraker", "192.168.1.50", 7125, macs=("2c:cf:67:00:00:01",)), "mdns")
        assert len(s.found()) == 1

    def test_entries_expire(self):
        clock = Clock()
        s = service(clock=clock, ttl=60)
        s.heard(Found("moonraker", "192.168.1.50", 7125), "mdns")
        clock.now += 61
        assert s.found() == []

    def test_a_swept_printer_lasts_longer(self):
        clock = Clock()
        s = service(clock=clock, ttl=60, sweep_ttl=600)
        s.heard(Found("moonraker", "192.168.1.50", 7125), "scan", swept=True)
        clock.now += 300
        assert len(s.found()) == 1

    def test_memory_is_bounded_keeping_the_newest(self):
        clock = Clock()
        s = service(clock=clock, max_found=3)
        for i in range(10):
            clock.now += 1
            s.heard(Found("moonraker", f"192.168.1.{10 + i}", 7125), "mdns")
        assert [f.address for f in s.found()] == ["192.168.1.17", "192.168.1.18", "192.168.1.19"]

    def test_datagrams_go_through_the_hooks(self):
        async def scenario():
            s = service()
            task = asyncio.ensure_future(s.run())
            await asyncio.sleep(0.01)
            s.listeners[2021](b"noise", "192.168.1.3")
            s.listeners[2021](b"printer", "192.168.1.4")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return s

        s = asyncio.run(scenario())
        assert [f.address for f in s.found()] == ["192.168.1.4"]
        assert s.status()["sources"]["UDP 2021 (moonraker)"] == "listening"

    def test_a_port_it_cant_listen_on_is_reported_and_mdns_goes_on(self, caplog):
        async def bind(port, on_datagram):
            raise OSError(98, "Address already in use")

        async def ask(services, wait):
            return [MOONRAKER]

        async def scenario():
            s = DiscoveryService([FakeHook()], ask=ask, bind=bind, mdns_interval=3600)
            task = asyncio.ensure_future(s.run())
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return s

        s = asyncio.run(scenario())
        assert "can't listen" in s.status()["sources"]["UDP 2021 (moonraker)"]
        assert len(s.found()) == 1
        assert "host's network" in caplog.text

    def test_a_sweep_runs_every_hook_and_is_rate_limited(self):
        hook = FakeHook()
        clock = Clock()
        s = service(hook, clock=clock, min_sweep_interval=60)
        networks = [ipaddress.ip_network("192.168.1.0/24")]

        assert asyncio.run(s.sweep(networks)) is True
        assert asyncio.run(s.sweep(networks)) is False
        clock.now += 61
        assert asyncio.run(s.sweep(networks)) is True
        assert hook.swept == [networks, networks]
        assert [f.name for f in s.found()] == ["swept"]

    def test_a_reported_printer_is_cleaned(self):
        dirty = Found(
            "moonraker",
            "192.168.1.50",
            7125,
            name="voron\x00\n",
            serial="bad serial!",
            macs=("2C-CF-67-00-00-01", "ff:ff:ff:ff:ff:ff"),
        )
        assert dirty.cleaned() == Found(
            "moonraker", "192.168.1.50", 7125, name="voron", macs=("2c:cf:67:00:00:01",)
        )

    @pytest.mark.parametrize(
        "mac", ["00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff", "01:00:5e:00:00:fb", "nonsense"]
    )
    def test_macs_3d_print_log_refuses_are_dropped(self, mac):
        assert clean_mac(mac) is None

    def test_identity_is_what_3d_print_log_computes(self):
        assert Found("bambu", "192.168.1.6", 8883, serial="01p0").identity == "bambu:sn:01P0"
        assert (
            Found(
                "moonraker", "192.168.1.5", 7126, macs=("b8:27:eb:00:00:02", "b8:27:eb:00:00:01")
            ).identity
            == "moonraker:mac:b8:27:eb:00:00:01:7126"
        )
        assert Found("prusalink", "192.168.1.7", 80).identity == "prusalink:ip:192.168.1.7:80"
