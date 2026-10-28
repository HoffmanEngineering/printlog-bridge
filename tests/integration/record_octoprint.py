"""Records what a real OctoPrint answers and pushes through a print, for `tests/fixtures/octoprint/`
and `tests/fake_octoprint.py` (#17, #83).

Run it against an OctoPrint whose printer is the bundled virtual printer, with an API key:

    python tests/integration/record_octoprint.py http://127.0.0.1:15180 <api key> <out dir>

It uploads `tests/fixtures/gcode/prusaslicer-2.9.2-calibration-cube.gcode` (as `cube.gcode`),
prints it to the end, prints it again and cancels it, and writes each REST answer and every
socket message as JSON. The API key and the socket's session are replaced by placeholders.
It changes OctoPrint: it uploads a file and starts prints. Never point it at a real printer.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import aiohttp

GCODE = Path(__file__).resolve().parents[1] / "fixtures" / "gcode"
CUBE = GCODE / "prusaslicer-2.9.2-calibration-cube.gcode"


async def main(base: str, key: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    headers = {"X-Api-Key": key}
    messages: list[dict] = []

    def save(name: str, body) -> None:
        text = json.dumps(body, indent=1, sort_keys=True)
        text = text.replace(key, "<api key>")
        (out / name).write_text(text + "\n", encoding="utf-8")

    async with aiohttp.ClientSession(headers=headers) as http:

        async def get(path: str):
            async with http.get(base + path) as r:
                return r.status, await r.json(content_type=None)

        for name, path in [
            ("version.json", "/api/version"),
            ("printer_idle.json", "/api/printer"),
            ("job_idle.json", "/api/job"),
        ]:
            save(name, (await get(path))[1])
        async with http.get(base + "/api/version", headers={"X-Api-Key": "wrong"}) as r:
            save("version_wrong_key.json", {"status": r.status, "body": await r.text()})
        async with http.post(base + "/api/login", json={"passive": True}) as r:
            login = await r.json()
        session = login["session"]
        save("login.json", {**login, "session": "<session>", "apikey": "<api key>"})
        form = aiohttp.FormData()
        form.add_field("file", CUBE.read_bytes(), filename="cube.gcode")
        async with http.post(base + "/api/files/local", data=form) as r:
            save("upload.json", await r.json())
        # Analysis runs in the background: wait for it.
        for _ in range(60):
            status, entry = await get("/api/files/local/cube.gcode")
            if entry.get("gcodeAnalysis"):
                break
            await asyncio.sleep(1)
        save("file_cube.json", entry)
        status, settings = await get("/api/settings")
        save("settings_plugins_keys.json", sorted((settings.get("plugins") or {}).keys()))
        save("settings_webcam.json", settings.get("webcam"))
        async with http.get(
            base + "/downloads/files/local/cube.gcode", headers={"Range": "bytes=0-99"}
        ) as r:
            save(
                "download_range.json",
                {
                    "status": r.status,
                    "content_range": r.headers.get("Content-Range"),
                    "length": len(await r.read()),
                },
            )

        ws_url = "ws" + base[4:] + "/sockjs/websocket"
        async with http.ws_connect(ws_url) as ws:
            started = time.monotonic()

            async def pump(until, timeout: float = 600):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    msg = await ws.receive(timeout=deadline - time.monotonic())
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        messages.append({"type": str(msg.type)})
                        return
                    body = json.loads(msg.data)
                    messages.append({"t": round(time.monotonic() - started, 2), "msg": body})
                    if until(body):
                        return
                raise TimeoutError

            await pump(lambda m: "connected" in m, 10)
            await ws.send_str(json.dumps({"auth": f"{login['name']}:{session}"}))
            await pump(lambda m: "history" in m or "current" in m, 10)
            await ws.send_str(json.dumps({"throttle": 4}))

            def event(kind):
                return lambda m: m.get("event", {}).get("type") == kind

            async with http.post(
                base + "/api/files/local/cube.gcode", json={"command": "select", "print": True}
            ) as r:
                assert r.status == 204, r.status
            await pump(event("PrintStarted"))
            save("job_printing.json", (await get("/api/job"))[1])
            save("printer_printing.json", (await get("/api/printer"))[1])
            await pump(event("PrintDone"))
            save("job_done.json", (await get("/api/job"))[1])
            await pump(lambda m: "current" in m, 10)

            async with http.post(base + "/api/job", json={"command": "start"}) as r:
                assert r.status == 204, r.status
            await pump(event("PrintStarted"))
            await pump(lambda m: "current" in m, 10)
            async with http.post(base + "/api/job", json={"command": "cancel"}) as r:
                assert r.status == 204, r.status
            await pump(event("PrintCancelled"))
            await pump(
                lambda m: "current" in m and not m["current"]["state"]["flags"]["cancelling"], 30
            )
            save("job_cancelled.json", (await get("/api/job"))[1])

    text = "\n".join(json.dumps(m, sort_keys=True) for m in messages)
    (out / "socket.jsonl").write_text(text.replace(session, "<session>") + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1].rstrip("/"), sys.argv[2], Path(sys.argv[3])))
