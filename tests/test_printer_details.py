"""What each adapter suggests for a new printer in 3D Print Log when the bridge pairs."""

from __future__ import annotations

import asyncio

from fake_moonraker import FakeMoonraker
from fake_prusalink import PASSWORD, FakePrusaLink

from printlog_bridge.adapters.bambu import BambuAdapter
from printlog_bridge.adapters.moonraker import MoonrakerAdapter
from printlog_bridge.adapters.prusalink import PrusaLinkAdapter
from printlog_bridge.core.adapter import PrinterDetails


def test_moonraker_names_the_host_kinematics_and_extruders():
    async def scenario():
        fake = FakeMoonraker(hostname="voron24")
        url = await fake.start()
        try:
            return await MoonrakerAdapter.from_config("voron", {"url": url}).printer_details()
        finally:
            await fake.close()

    details = asyncio.run(scenario())
    assert details == PrinterDetails(
        name="voron24",
        make="Klipper",
        model="corexy",
        slot_count=2,
        description="corexy kinematics, 2 extruders",
    )


def test_moonraker_out_of_reach_suggests_only_the_section_name():
    adapter = MoonrakerAdapter.from_config("voron", {"url": "http://127.0.0.1:9"})
    details = asyncio.run(adapter.printer_details())
    assert details == PrinterDetails(name="voron", make="Klipper")


def test_bambu_reads_the_model_from_the_serial_number():
    options = {"host": "127.0.0.1", "access_code": "12345678"}
    p1s = BambuAdapter.from_config("p1s", {**options, "serial": "01P00A123456789"})
    unknown = BambuAdapter.from_config("x", {**options, "serial": "ZZZ00A123456789"})
    assert asyncio.run(p1s.printer_details()) == PrinterDetails("p1s", "Bambu Lab", "P1S")
    assert asyncio.run(unknown.printer_details()).model is None


def test_prusalink_names_the_host():
    async def scenario():
        fake = FakePrusaLink()
        await fake.start()
        try:
            adapter = PrusaLinkAdapter.from_config(
                "mk4s", {"host": "127.0.0.1", "port": str(fake.port), "password": PASSWORD}
            )
            return await adapter.printer_details()
        finally:
            await fake.close()

    assert asyncio.run(scenario()) == PrinterDetails(name="mk4s", make="Prusa")
