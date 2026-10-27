"""Three fake Moonrakers, a fake 3D Print Log API and a fake Auth0 (approving every pairing) on
this computer, for tests/docker/smoke.sh and tests/docker/addon_smoke.sh.

Writes `<data>/printlog-bridge.cfg` pointing at them (remote mode, one section per printer), and
`<data>/options.json`, the same as a Home Assistant app's options, then serves until killed.
Once the bridge has subscribed to all three printers, a print starts and ends on each.
`<report>` is rewritten every half second with what the fakes saw, so the smoke test can
check the container logged all three prints without ever calling the real API.

Usage: python tests/docker/serve_fakes.py <data dir> <report.json>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fake_auth0 import FakeAuth0  # noqa: E402
from fake_moonraker import FakeMoonraker  # noqa: E402
from fake_printlog_api import FakePrintLogApi  # noqa: E402

PRINTERS = {"voron": 41, "u1": 42, "k2": 43}


async def main(data: Path, report: Path) -> None:
    api = FakePrintLogApi(printers=tuple(PRINTERS.values()))
    api_url = await api.start()
    # Pairing on, approved as soon as the bridge asks: `pair --yes` in the container works.
    auth0 = FakeAuth0(interval=0.2)
    await auth0.start()
    auth0.approve()
    api.auth0 = auth0
    api.pairing = auth0.discovery()
    moonrakers = {name: FakeMoonraker() for name in PRINTERS}
    urls = {name: await m.start() for name, m in moonrakers.items()}

    data.mkdir(parents=True, exist_ok=True)
    (data / "printlog-bridge.cfg").write_text(
        # Plain http is allowed only to this computer: the container shares the host's network.
        f"[bridge]\napi_url = {api_url}\n\n"
        + "".join(
            f"[printer {name}]\nprinter_id = {PRINTERS[name]}\nurl = {urls[name]}\n"
            "snapshots = false\n\n"
            for name in PRINTERS
        ),
        encoding="utf-8",
    )
    # The same setup as a Home Assistant app's options (tests/docker/addon_smoke.sh); the
    # normal image never reads this file.
    options = {
        "api_url": api_url,
        "trace": False,
        "printers": [
            {"name": name, "adapter": "moonraker", "printer_id": PRINTERS[name], "url": urls[name]}
            for name in PRINTERS
        ],
    }
    (data / "options.json").write_text(json.dumps(options, indent=1), encoding="utf-8")
    print(f"fakes up: api {api_url}, moonrakers {urls}", flush=True)

    printed = False
    while True:
        if not printed and all(m.subscriptions for m in moonrakers.values()):
            printed = True
            for name, m in moonrakers.items():
                job = await m.start_job(f"{name}_smoke.gcode")
                await asyncio.sleep(1)
                await m.finish_job(job)
        # Replaced in one step, so the smoke test never reads half a file.
        temporary = report.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "subscribed": sorted(n for n, m in moonrakers.items() if m.subscriptions),
                    "prints": [
                        {
                            "printerId": p["printerId"],
                            "fileName": p["fileName"],
                            "status": p["status"],
                        }
                        for p in api.prints.values()
                    ],
                    "connections": len(api.connections),
                }
            ),
            encoding="utf-8",
        )
        os.replace(temporary, report)
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2])))
