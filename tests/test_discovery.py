"""#32: a remote printer is found again after its IP address changes, and never mistaken for
another printer that took its old address.

The fake Moonraker moves between loopback addresses (127.0.0.87 to 127.0.0.91, on the same
port), as a printer given a new address by DHCP, and the bridge finds it by scanning
127.0.0.0/24.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import socket
import struct
from pathlib import Path

import pytest
from fake_moonraker import FakeMoonraker, eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.adapters.moonraker import MoonrakerAdapter
from printlog_bridge.adapters.moonraker import discovery as disc
from printlog_bridge.adapters.moonraker.discovery import Fingerprint
from printlog_bridge.adapters.moonraker.locator import Locator
from printlog_bridge.cli.discover import discover, format_found
from printlog_bridge.cli.main import main
from printlog_bridge.core.bridge import run_bridge
from printlog_bridge.core.config import ConfigError, load_config
from printlog_bridge.core.state import PrinterState
from printlog_bridge.core.status import format_status, read_status

OLD = "127.0.0.87"
NEW = "127.0.0.91"
FAST = {"after": 0, "interval": 0, "zeroconf": False}
"""Look for the printer as soon as it goes missing, every time, and only by scanning."""

ALICE = {"mac": "2c:cf:67:00:00:01", "instance_id": "alice", "hostname": "alice"}
BOB = {"mac": "2c:cf:67:00:00:02", "instance_id": "bob", "hostname": "bob"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- identity ----------------------------------------------------------------------------------


SYSTEM_INFO = {
    "instance_ids": {"moonraker": "uuid-1", "klipper": "k"},
    "network": {
        "lo": {"mac_address": "00:00:00:00:00:00", "ip_addresses": []},
        "eth0": {"mac_address": "", "ip_addresses": []},
        "wlan0": {
            "mac_address": "2C:CF:67:AA:BB:CC",
            "ip_addresses": [
                {"family": "ipv4", "address": "192.168.1.87", "is_link_local": False},
                {"family": "ipv6", "address": "fe80::1", "is_link_local": True},
            ],
        },
    },
}


class TestFingerprint:
    def test_from_system_info(self):
        fp = Fingerprint.from_system_info(SYSTEM_INFO)

        assert fp.instance_id == "uuid-1"
        assert fp.macs == {"2c:cf:67:aa:bb:cc"}
        assert fp.addresses == ("192.168.1.87",)

    def test_a_shared_mac_or_instance_id_is_the_same_printer(self):
        fp = Fingerprint("uuid-1", frozenset({"aa"}))

        assert fp.matches(Fingerprint("uuid-2", frozenset({"aa"})))  # Moonraker reinstalled
        assert fp.matches(Fingerprint("uuid-1", frozenset({"bb"})))  # same data, new board
        assert not fp.matches(Fingerprint("uuid-2", frozenset({"bb"})))
        assert not Fingerprint().matches(Fingerprint())

    def test_round_trips_through_json(self):
        fp = Fingerprint.from_system_info(SYSTEM_INFO)

        assert Fingerprint.from_json(json.loads(json.dumps(fp.to_json()))) == fp

    def test_nothing_to_go_by_is_no_fingerprint(self):
        assert not Fingerprint.from_system_info({"network": {"lo": {"mac_address": "x"}}})


class TestAddresses:
    def test_with_host_keeps_everything_but_the_host(self):
        url = "http://user:pw@192.168.1.87:7125/websocket"
        assert disc.with_host(url, "192.168.1.91") == "http://user:pw@192.168.1.91:7125/websocket"
        assert disc.with_host("http://u1.local", "10.0.0.5") == "http://10.0.0.5"

    def test_http_base(self):
        assert disc.http_base("ws://10.0.0.5:7125/websocket") == "http://10.0.0.5:7125"

    @pytest.mark.parametrize("host", ["8.8.8.8", "u1.local", "169.254.1.1", "fd00::1"])
    def test_only_private_ipv4_networks_are_searched(self, host):
        assert disc.subnet_of(host) is None

    def test_a_subnet_is_a_slash_24(self):
        assert str(disc.subnet_of("192.168.1.87")) == "192.168.1.0/24"

    @pytest.mark.parametrize("text", ["192.168.0.0/16", "8.8.8.0/24", "fd00::/120"])
    def test_a_large_or_public_subnet_is_refused(self, text):
        with pytest.raises(ValueError):
            disc.parse_subnet(text)


class TestMdns:
    def test_reads_the_address_and_port_of_each_instance(self):
        def name(text):
            return disc._encode_name(text)

        def record(owner, kind, rdata):
            return owner + struct.pack("!HHIH", kind, 0x8001, 120, len(rdata)) + rdata

        service = name("_moonraker._tcp.local")
        instance = name("U1._moonraker._tcp.local")
        host = name("u1.local")
        packet = (
            struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 2)
            + record(service, 12, instance)
            + record(instance, 33, struct.pack("!HHH", 0, 0, 7125) + host)
            + record(host, 1, bytes([192, 168, 1, 91]))
        )

        assert disc.parse_mdns_answer(packet, "_moonraker._tcp.local") == [("192.168.1.91", 7125)]

    def test_follows_compressed_names(self):
        # "local" at offset 12, then "u1" + a pointer back to it.
        data = b"\x00" * 12 + b"\x05local\x00" + b"\x02u1\xc0\x0c"
        assert disc._read_name(data, 19) == ("u1.local", 24)

    def test_the_query_asks_for_ptr_records(self):
        query = disc.mdns_query("_moonraker._tcp.local")
        assert query.endswith(b"\x05local\x00\x00\x0c\x00\x01")


# -- config ------------------------------------------------------------------------------------


class TestConfig:
    def test_on_by_default_in_remote_mode(self):
        assert MoonrakerAdapter.from_config("u1", {"url": "http://10.0.0.5:7125"}).discovery

    def test_can_be_turned_off(self):
        options = {"url": "http://10.0.0.5:7125", "discovery": "false"}
        assert not MoonrakerAdapter.from_config("u1", options).discovery

    def test_must_be_true_or_false(self):
        with pytest.raises(ConfigError, match="discovery must be true or false"):
            MoonrakerAdapter.from_config("u1", {"url": "http://x:7125", "discovery": "maybe"})

    def test_is_refused_in_local_mode(self):
        with pytest.raises(ConfigError, match="only for remote mode"):
            MoonrakerAdapter.from_config("u1", {"discovery": "true"})


# -- the bridge, end to end --------------------------------------------------------------------


def write_config(tmp_path: Path, url: str, api_url: str, extra: str = "") -> Path:
    path = tmp_path / "printlog-bridge.cfg"
    path.write_text(
        f"[bridge]\napi_url = {api_url}\napi_key = key-123\nstate_dir = {tmp_path / 'state'}\n"
        f"[printer u1]\nprinter_id = 42\nurl = {url}\n{extra}",
        encoding="utf-8",
    )
    return path


@contextlib.asynccontextmanager
async def bridge(config_path: Path, **locator):
    config = load_config(config_path, adapters=ADAPTERS, env={}, home=config_path.parent)
    for printer in config.printers:
        printer.adapter.locator_options = dict(locator)
    task = asyncio.ensure_future(run_bridge(config))
    try:
        yield config
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def state_of(tmp_path: Path) -> PrinterState:
    return PrinterState.load(tmp_path / "state", "u1")


def test_a_printer_that_changes_address_is_found_and_logging_continues(tmp_path):
    port = free_port()

    async def scenario():
        api = FakePrintLogApi()
        printer = FakeMoonraker(**ALICE)
        config_path = write_config(tmp_path, f"http://{OLD}:{port}", await api.start())
        await printer.start(OLD, port)
        try:
            async with bridge(config_path, **FAST) as config:
                await eventually(lambda: printer.subscriptions, timeout=10)
                await printer.finish_job(await printer.start_job("before.gcode"))
                await eventually(lambda: len(api.completions) == 1, timeout=10)

                await printer.stop()  # DHCP gives the printer a new address
                await printer.start(NEW, port)
                await eventually(lambda: len(printer.subscriptions) == 2, timeout=20)
                await printer.finish_job(await printer.start_job("after.gcode"))
                await eventually(lambda: len(api.completions) == 2, timeout=10)

                status = read_status(tmp_path / "state")
                text, _ = format_status(config)
            return api, status, text
        finally:
            await printer.close()
            await api.close()

    api, status, text = asyncio.run(scenario())

    assert [p["fileName"] for p in api.prints.values()] == ["before.gcode", "after.gcode"]
    assert status["printers"]["u1"]["moved"]["from"] == OLD
    assert status["printers"]["u1"]["moved"]["to"] == NEW
    assert f"Address: moved from {OLD} to {NEW}" in text
    state = state_of(tmp_path)
    assert state.discovery["address"] == NEW
    assert state.discovery["fingerprint"]["instance_id"] == "alice"
    assert f"{OLD}:{port}" in (tmp_path / "printlog-bridge.cfg").read_text()  # unchanged


def test_the_next_run_starts_at_the_new_address(tmp_path):
    port = free_port()

    async def scenario():
        api = FakePrintLogApi()
        printer = FakeMoonraker(**ALICE)
        config_path = write_config(tmp_path, f"http://{OLD}:{port}", await api.start())
        await printer.start(OLD, port)
        try:
            async with bridge(config_path, **FAST):
                await eventually(lambda: printer.subscriptions, timeout=10)
                await printer.stop()
                await printer.start(NEW, port)
                await eventually(lambda: len(printer.subscriptions) == 2, timeout=20)
            # Restarted, slow to search: it can only connect if it starts at the new address.
            async with bridge(config_path, after=3600, interval=3600, zeroconf=False):
                await eventually(lambda: len(printer.subscriptions) == 3, timeout=10)
                status = read_status(tmp_path / "state")
            return status
        finally:
            await printer.close()
            await api.close()

    status = asyncio.run(scenario())

    assert status["printers"]["u1"]["moved"]["to"] == NEW


def test_a_different_printer_at_the_old_address_is_never_used(tmp_path):
    port = free_port()

    def warnings() -> dict:
        return read_status(tmp_path / "state")["printers"]["u1"]["warnings"]

    async def scenario():
        api = FakePrintLogApi()
        alice, bob = FakeMoonraker(**ALICE), FakeMoonraker(**BOB)
        config_path = write_config(tmp_path, f"http://{OLD}:{port}", await api.start())
        await alice.start(OLD, port)
        try:
            async with bridge(config_path, **FAST):
                await eventually(lambda: alice.subscriptions, timeout=10)
                await alice.stop()
                await bob.start(OLD, port)  # DHCP hands the old address to another printer
                await bob.start_job("not-mine.gcode")
                # Until the bridge has connected to bob and turned it down. Not just until bob
                # is asked who it is: the scan asks that too, and it reaches the old address
                # before the rest of 127.0.0.0/24, which takes seconds on Windows.
                await eventually(lambda: "discovery" in warnings(), timeout=30)
                warning = warnings()

                await alice.start(NEW, port)  # alice turns up elsewhere
                await eventually(lambda: len(alice.subscriptions) == 2, timeout=20)
            return api, bob, warning
        finally:
            await alice.close()
            await bob.close()
            await api.close()

    api, bob, warning = asyncio.run(scenario())

    assert bob.subscriptions == []
    assert "server.history.list" not in bob.calls
    assert api.prints == {}
    assert "a different printer answers at 127.0.0.87" in warning["discovery"]
    assert state_of(tmp_path).discovery["address"] == NEW


def test_with_discovery_off_the_printer_isnt_identified_or_looked_for(tmp_path):
    port = free_port()

    async def scenario():
        api = FakePrintLogApi()
        printer = FakeMoonraker(**ALICE)
        config_path = write_config(
            tmp_path, f"http://{OLD}:{port}", await api.start(), "discovery = false\n"
        )
        await printer.start(OLD, port)
        try:
            async with bridge(config_path, **FAST):
                await eventually(lambda: printer.subscriptions, timeout=10)
                await printer.stop()
                await printer.start(NEW, port)
                await asyncio.sleep(2)
            return printer
        finally:
            await printer.close()
            await api.close()

    printer = asyncio.run(scenario())

    assert "machine.system_info" not in printer.calls
    assert printer.http_requests == []
    assert len(printer.subscriptions) == 1
    assert state_of(tmp_path).discovery == {}


def test_changing_the_url_in_the_config_forgets_what_was_found(tmp_path):
    state = state_of(tmp_path)
    state.discovery = {"config": "old", "address": NEW, "fingerprint": {"instance_id": "x"}}
    state.save()
    adapter = MoonrakerAdapter.from_config("u1", {"url": f"http://{OLD}:7125"})
    adapter.use_state(state_of(tmp_path))
    locator = Locator("u1", configured_url=adapter.url, state=adapter.state)

    assert locator.url == f"http://{OLD}:7125"
    assert "fingerprint" not in state_of(tmp_path).discovery


def test_no_password_in_the_url_reaches_the_state_file(tmp_path):
    url = "http://pi:hunter2@192.168.1.87:7125"
    locator = Locator("u1", configured_url=url, state=state_of(tmp_path))
    locator._moved(url, disc.with_host(url, "192.168.1.91"), Fingerprint("a"))

    assert locator.url == "http://pi:hunter2@192.168.1.91:7125"
    assert "hunter2" not in (tmp_path / "state" / "printers" / "u1.json").read_text()


def test_the_moonraker_key_goes_only_to_an_address_that_answers_as_moonraker():
    async def scenario():
        printer = FakeMoonraker(api_key="mk-secret", **ALICE)
        base = await printer.start()
        try:
            return await disc.probe(base, api_key="mk-secret"), printer
        finally:
            await printer.close()

    found, printer = asyncio.run(scenario())

    assert found.fingerprint.instance_id == "alice"
    assert not found.unauthorized
    # Asked without the key first; sent it only once Moonraker asked for authorization.
    assert printer.http_requests[:2] == ["/server/info", "/server/info"]


# -- printlog-bridge discover ------------------------------------------------------------------


def test_discover_lists_the_printers_on_the_network():
    port = free_port()

    async def scenario():
        alice, bob = FakeMoonraker(**ALICE), FakeMoonraker(**BOB)
        await alice.start(OLD, port)
        await bob.start(NEW, port)
        try:
            networks = [ipaddress.ip_network("127.0.0.0/24")]
            return await discover(networks, port=port, zeroconf=False), networks
        finally:
            await alice.close()
            await bob.close()

    found, networks = asyncio.run(scenario())
    text = format_found(found, networks, None, zeroconf=False)

    assert [f.address for f in found] == [OLD, NEW]
    details = "host name alice, Klipper v0.12.0-fake, Moonraker v0.9.3-fake, 2 tools"
    assert f"http://{OLD}:{port}: {details}" in text
    assert "  [printer alice]\n  printer_id =" in text
    assert f"  url = http://{NEW}:{port}" in text


def test_discover_command(tmp_path, capsys, monkeypatch):
    port = free_port()
    monkeypatch.setenv("PRINTLOG_BRIDGE_CONFIG", str(tmp_path / "missing.cfg"))

    async def serve_and_run():
        printer = FakeMoonraker(**ALICE)
        await printer.start(OLD, port)
        try:
            loop = asyncio.get_running_loop()
            args = ["discover", "--subnet", "127.0.0.0/24", "--port", str(port), "--no-zeroconf"]
            return await loop.run_in_executor(None, main, args)
        finally:
            await printer.close()

    code = asyncio.run(serve_and_run())

    out = capsys.readouterr().out
    assert code == 0
    assert "Looked on 127.0.0.0/24." in out
    assert f"url = http://{OLD}:{port}" in out


def test_discover_says_when_nothing_answers(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("PRINTLOG_BRIDGE_CONFIG", str(tmp_path / "missing.cfg"))
    args = ["discover", "--subnet", "127.0.5.0/28", "--port", str(free_port()), "--no-zeroconf"]

    assert main(args) == 1
    assert "No Moonraker answered." in capsys.readouterr().out


class TestSettings:
    URL = {"url": "http://192.168.1.87:7125"}

    def test_defaults_leave_the_locator_to_its_own(self):
        assert MoonrakerAdapter.from_config("u1", self.URL).locator_options == {}

    def test_each_can_be_set(self):
        options = {
            **self.URL,
            "discovery_after": "90s",
            "discovery_interval": "1h",
            "discovery_timeout": "5",
            "discovery_zeroconf": "no",
            "discovery_networks": "192.168.1.0/24, 10.0.5.0/25",
        }

        settings = MoonrakerAdapter.from_config("u1", options).locator_options

        assert settings == {
            "after": 90,
            "interval": 3600,
            "timeout": 5,
            "zeroconf": False,
            "networks": [
                ipaddress.ip_network("192.168.1.0/24"),
                ipaddress.ip_network("10.0.5.0/25"),
            ],
        }

    @pytest.mark.parametrize(
        ("option", "value", "message"),
        [
            ("discovery_after", "soon", "discovery_after must be a time"),
            ("discovery_interval", "10s", r"at least 30s"),
            ("discovery_timeout", "0", "discovery_timeout must be a time"),
            ("discovery_zeroconf", "perhaps", "discovery_zeroconf must be true or false"),
            ("discovery_networks", "192.168.0.0/16", "larger than a /24"),
            ("discovery_networks", "8.8.8.0/24", "isn't a private"),
        ],
    )
    def test_bad_values_say_what_to_write(self, option, value, message):
        with pytest.raises(ConfigError, match=message):
            MoonrakerAdapter.from_config("u1", {**self.URL, option: value})

    def test_are_refused_in_local_mode(self):
        with pytest.raises(ConfigError, match="discovery_after is only for remote mode"):
            MoonrakerAdapter.from_config("u1", {"discovery_after": "3m"})

    def test_networks_in_the_config_replace_the_automatic_choice(self, tmp_path):
        state = state_of(tmp_path)
        state.discovery = {"fingerprint": Fingerprint("a", addresses=("10.0.0.5",)).to_json()}
        network = ipaddress.ip_network("192.168.7.0/24")

        automatic = Locator("u1", configured_url="http://192.168.1.87:7125", state=state)
        configured = Locator(
            "u1", configured_url="http://192.168.1.87:7125", state=state, networks=[network]
        )

        assert [str(n) for n in automatic.networks()] == ["192.168.1.0/24"]
        assert configured.networks() == [network]

    def test_a_printer_on_another_network_is_found_with_discovery_networks(self, tmp_path):
        port = free_port()

        async def scenario():
            api = FakePrintLogApi()
            printer = FakeMoonraker(**ALICE)
            config_path = write_config(
                tmp_path,
                f"http://{OLD}:{port}",
                await api.start(),
                "discovery_after = 0\ndiscovery_zeroconf = false\n"
                "discovery_networks = 127.0.9.0/24\n",
            )
            await printer.start(OLD, port)
            try:
                config = load_config(config_path, adapters=ADAPTERS, env={}, home=tmp_path)
                task = asyncio.ensure_future(run_bridge(config))
                try:
                    await eventually(lambda: printer.subscriptions, timeout=10)
                    # Up at its new address (a different /24) before the old one goes, so the
                    # first search finds it whenever it runs.
                    moved = FakeMoonraker(**ALICE)
                    await moved.start("127.0.9.5", port)
                    await printer.stop()
                    await eventually(lambda: moved.subscriptions, timeout=20)
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    await moved.close()
            finally:
                await printer.close()
                await api.close()

        asyncio.run(scenario())

        assert state_of(tmp_path).discovery["address"] == "127.0.9.5"
