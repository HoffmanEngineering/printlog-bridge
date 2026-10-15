"""moonraker-timelapse videos, attached to their print (#42): the watcher, fetching the video,
the options, and the whole bridge between a fake Moonraker and a fake 3D Print Log."""

from __future__ import annotations

import asyncio
from pathlib import Path

import bridge_helpers as helpers
import pytest
from fake_moonraker import FakeMoonraker, eventually
from fake_printlog_api import FakePrintLogApi

from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.adapters.moonraker import MoonrakerAdapter
from printlog_bridge.adapters.moonraker.timelapse import TimelapseWatcher
from printlog_bridge.core.config import ConfigError, load_config
from printlog_bridge.core.events import AttachmentKind, JobAttachment
from printlog_bridge.core.images import FileUnavailable

MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 4000
WAIT_S = 10.0
VIDEO = "timelapse_benchy.gcode_2026-10-15_12-00-00.mp4"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)

    async def resume_after(self, printer):
        return None


async def server_info(components):
    async def call(method, params=None):
        assert method == "server.info"
        return {"klippy_state": "ready", "components": components}

    return call


def success(printfile: str = "benchy.gcode", filename: str = VIDEO) -> list:
    return [{"action": "render", "status": "success", "filename": filename, "printfile": printfile}]


async def watching(*, components=("history", "timelapse"), wait: float = 60.0):
    sink = RecordingSink()
    watcher = TimelapseWatcher("voron", sink, url="http://voron:7125", socket=None, wait=wait)
    await watcher.on_connected(await server_info(list(components)))
    return watcher, sink


async def settle(watcher: TimelapseWatcher) -> None:
    await asyncio.sleep(0)
    await asyncio.gather(*list(watcher._tasks))


class TestWatcher:
    def test_the_video_rendered_after_a_job_ends_is_that_jobs(self):
        async def scenario():
            watcher, sink = await watching()
            watcher.job_ended("00000A", "parts/benchy.gcode")
            await watcher.on_event([{"action": "render", "status": "started"}])
            await watcher.on_event([{"action": "render", "status": "running", "progress": 40}])
            await watcher.on_event(success())
            await settle(watcher)
            return sink.events

        [attachment] = asyncio.run(scenario())

        assert isinstance(attachment, JobAttachment)
        assert (attachment.printer, attachment.job_id) == ("voron", "00000A")
        assert attachment.kind is AttachmentKind.TIMELAPSE
        assert attachment.file_name == VIDEO

    def test_a_video_rendered_by_the_end_macro_before_the_job_ended_is_that_jobs(self):
        async def scenario():
            watcher, sink = await watching()
            await watcher.on_event(success())
            watcher.job_ended("00000A", "benchy.gcode")
            await settle(watcher)
            return sink.events

        [attachment] = asyncio.run(scenario())
        assert attachment.job_id == "00000A"

    def test_only_the_first_video_is_reported(self):
        async def scenario():
            watcher, sink = await watching()
            watcher.job_ended("00000A", "benchy.gcode")
            await watcher.on_event(success())
            await watcher.on_event(success(filename="timelapse_benchy.gcode_again.mp4"))
            await settle(watcher)
            return sink.events

        assert len(asyncio.run(scenario())) == 1

    def test_a_video_of_another_file_is_not_the_jobs(self):
        async def scenario():
            watcher, sink = await watching()
            watcher.job_ended("00000A", "benchy.gcode")
            await watcher.on_event(success(printfile="cube.gcode"))
            await settle(watcher)
            return sink.events

        assert asyncio.run(scenario()) == []

    def test_a_video_rendered_too_long_after_the_job_ended_is_not_the_jobs(self):
        async def scenario():
            watcher, sink = await watching(wait=0.05)
            watcher.job_ended("00000A", "benchy.gcode")
            await asyncio.sleep(0.1)
            await watcher.on_event(success())
            await settle(watcher)
            return sink.events

        assert asyncio.run(scenario()) == []

    def test_a_video_rendered_too_long_before_the_job_ended_is_not_the_jobs(self):
        async def scenario():
            watcher, sink = await watching(wait=0.05)
            await watcher.on_event(success())
            await asyncio.sleep(0.1)
            watcher.job_ended("00000A", "benchy.gcode")
            await settle(watcher)
            return sink.events

        assert asyncio.run(scenario()) == []

    @pytest.mark.parametrize("status", ["error", "skipped", "running"])
    def test_a_render_that_did_not_succeed_is_not_reported(self, status):
        async def scenario():
            watcher, sink = await watching()
            watcher.job_ended("00000A", "benchy.gcode")
            await watcher.on_event([{"action": "render", "status": status, "filename": VIDEO}])
            await settle(watcher)
            return sink.events

        assert asyncio.run(scenario()) == []

    @pytest.mark.parametrize(
        "name", ["../config/printer.cfg", "sub/video.mp4", "video.mkv", ".hidden.mp4", ""]
    )
    def test_only_a_plain_mp4_name_is_reported(self, name):
        async def scenario():
            watcher, sink = await watching()
            watcher.job_ended("00000A", "benchy.gcode")
            await watcher.on_event(success(filename=name))
            await settle(watcher)
            return sink.events

        assert asyncio.run(scenario()) == []

    def test_without_the_component_nothing_is_reported(self):
        async def scenario():
            watcher, sink = await watching(components=("history", "webcam"))
            watcher.job_ended("00000A", "benchy.gcode")
            await watcher.on_event(success())
            await settle(watcher)
            return watcher.available, sink.events

        assert asyncio.run(scenario()) == (False, [])


class TestFetch:
    def test_remote_downloads_the_video_with_moonrakers_key(self, tmp_path):
        async def scenario():
            moonraker = FakeMoonraker(api_key="mrk", timelapse=True)
            moonraker.timelapse_files[VIDEO] = MP4
            url = await moonraker.start()
            try:
                watcher = TimelapseWatcher(
                    "voron", RecordingSink(), url=url, socket=None, api_key="mrk"
                )
                assert await watcher.fetch(VIDEO, tmp_path / "video.tmp", len(MP4))
                return moonraker.timelapse_downloads
            finally:
                await moonraker.close()

        assert asyncio.run(scenario()) == [VIDEO]
        assert (tmp_path / "video.tmp").read_bytes() == MP4

    def test_remote_refuses_a_video_over_the_limit_and_leaves_nothing(self, tmp_path):
        async def scenario():
            moonraker = FakeMoonraker(timelapse=True)
            moonraker.timelapse_files[VIDEO] = MP4
            url = await moonraker.start()
            try:
                watcher = TimelapseWatcher("voron", RecordingSink(), url=url, socket=None)
                await watcher.fetch(VIDEO, tmp_path / "video.tmp", len(MP4) - 1)
            finally:
                await moonraker.close()

        with pytest.raises(FileUnavailable, match="larger than"):
            asyncio.run(scenario())
        assert not (tmp_path / "video.tmp").exists()

    def test_local_copies_the_video_from_the_timelapse_folder(self, tmp_path):
        root = tmp_path / "timelapse"
        root.mkdir()
        (root / VIDEO).write_bytes(MP4)

        async def calls(method, params=None):
            assert method == "server.files.roots"
            return [
                {"name": "gcodes", "path": str(tmp_path)},
                {"name": "timelapse", "path": str(root)},
            ]

        async def scenario():
            watcher = TimelapseWatcher("voron", RecordingSink(), url=None, socket=tmp_path / "s")
            watcher._call = calls
            return await watcher.fetch(VIDEO, tmp_path / "video.tmp", len(MP4))

        assert asyncio.run(scenario())
        assert (tmp_path / "video.tmp").read_bytes() == MP4

    def test_local_refuses_a_video_over_the_limit_before_copying(self, tmp_path):
        root = tmp_path / "timelapse"
        root.mkdir()
        (root / VIDEO).write_bytes(MP4)

        async def calls(method, params=None):
            return [{"name": "timelapse", "path": str(root)}]

        async def scenario():
            watcher = TimelapseWatcher("voron", RecordingSink(), url=None, socket=tmp_path / "s")
            watcher._call = calls
            await watcher.fetch(VIDEO, tmp_path / "video.tmp", 100)

        with pytest.raises(FileUnavailable, match="larger than"):
            asyncio.run(scenario())
        assert not (tmp_path / "video.tmp").exists()

    @pytest.mark.parametrize("name", ["../printer.cfg", "../../secrets.mp4", "x/../../a.mp4"])
    def test_never_reads_outside_the_timelapse_folder(self, tmp_path, name):
        async def calls(method, params=None):
            raise AssertionError(f"unexpected call {method}")

        async def scenario():
            watcher = TimelapseWatcher("voron", RecordingSink(), url=None, socket=tmp_path / "s")
            watcher._call = calls
            await watcher.fetch(name, tmp_path / "video.tmp", 1000)

        with pytest.raises(FileUnavailable):
            asyncio.run(scenario())


class TestOptions:
    def config(self, tmp_path: Path, bridge: str = "", printer: str = ""):
        path = tmp_path / "printlog-bridge.cfg"
        path.write_text(
            f"[bridge]\napi_key = k\n{bridge}\n[printer voron]\nurl = http://voron:7125\n{printer}",
            encoding="utf-8",
        )
        return load_config(path, adapters=ADAPTERS, env={}, home=tmp_path)

    def test_timelapse_is_on_by_default(self, tmp_path):
        adapter = self.config(tmp_path).printers[0].adapter
        assert isinstance(adapter, MoonrakerAdapter) and adapter.timelapse

    def test_timelapse_false_turns_it_off(self, tmp_path):
        assert not self.config(tmp_path, printer="timelapse = false").printers[0].adapter.timelapse

    def test_timelapse_must_be_true_or_false(self, tmp_path):
        with pytest.raises(ConfigError, match="timelapse must be true or false"):
            self.config(tmp_path, printer="timelapse = sometimes")

    def test_max_attachment_mb_is_100_by_default(self, tmp_path):
        assert self.config(tmp_path).max_attachment_mb == 100

    def test_max_attachment_mb_can_be_set(self, tmp_path):
        assert self.config(tmp_path, bridge="max_attachment_mb = 25").max_attachment_mb == 25

    @pytest.mark.parametrize("value", ["0", "201", "1.5", "lots"])
    def test_max_attachment_mb_is_1_to_200(self, tmp_path, value):
        with pytest.raises(ConfigError, match="max_attachment_mb"):
            self.config(tmp_path, bridge=f"max_attachment_mb = {value}")


async def a_timelapse_print(
    tmp_path: Path,
    *,
    component: bool = True,
    option: str | None = None,
    pro: bool = True,
    renders: int = 1,
    attached: bool = True,
):
    """A print through the whole bridge in remote mode, with moonraker-timelapse rendering its
    video `renders` times after it ends."""
    moonraker = FakeMoonraker(api_key="mrk", timelapse=component)
    api = FakePrintLogApi()
    api.pro = pro
    moonraker_url, api_url = await moonraker.start(), await api.start()
    config_path = tmp_path / "printlog-bridge.cfg"
    config_path.write_text(
        f"[bridge]\napi_url = {api_url}\napi_key = key-123\nstate_dir = {tmp_path / 'state'}\n"
        f"[printer voron]\nprinter_id = 42\nurl = {moonraker_url}\nmoonraker_api_key = mrk\n"
        + (f"timelapse = {option}\n" if option is not None else ""),
        encoding="utf-8",
    )
    try:
        async with helpers.bridge(
            config_path,
            connected=lambda: moonraker.client_count == 1,
            disconnected=lambda: moonraker.client_count == 0,
        ):
            job = await moonraker.start_job("benchy.gcode")
            await eventually(lambda: api.prints, WAIT_S)
            await moonraker.finish_job(job)
            await eventually(lambda: api.prints[1]["status"] == 3, WAIT_S)
            for _ in range(renders):
                await moonraker.render_timelapse("benchy.gcode", MP4)
            if attached:
                await eventually(lambda: api.attachments.get(1), WAIT_S)
            await asyncio.sleep(0.5)  # time for anything more it would wrongly do
    finally:
        await moonraker.close()
        await api.close()
    return moonraker, api


def file_requests(api: FakePrintLogApi) -> list:
    return [r for r in api.requests if "/files" in r[1] or "Subscription" in r[1]]


class TestTheWholeBridge:
    def test_the_video_is_attached_to_its_print_once(self, tmp_path):
        moonraker, api = asyncio.run(a_timelapse_print(tmp_path, renders=2))

        [attachment] = api.attachments[1]
        assert attachment["originalFileName"] == VIDEO
        assert attachment["contentType"] == "video/mp4"
        assert attachment["sizeBytes"] == len(MP4)
        assert api.attached(1) == [MP4]
        assert moonraker.timelapse_downloads == [VIDEO]
        assert not list((tmp_path / "state" / "attachments").iterdir())  # sent, so removed

    def test_without_the_component_nothing_is_requested(self, tmp_path):
        moonraker, api = asyncio.run(a_timelapse_print(tmp_path, component=False, attached=False))

        assert moonraker.timelapse_downloads == []
        assert file_requests(api) == []

    def test_with_timelapse_off_nothing_is_requested(self, tmp_path):
        moonraker, api = asyncio.run(a_timelapse_print(tmp_path, option="false", attached=False))

        assert moonraker.timelapse_downloads == []
        assert file_requests(api) == []

    def test_without_pro_the_video_is_never_fetched_or_uploaded(self, tmp_path):
        moonraker, api = asyncio.run(a_timelapse_print(tmp_path, pro=False, attached=False))

        assert moonraker.timelapse_downloads == []
        assert file_requests(api) == [("GET", "/api/Subscription/me")]
        assert api.blob_puts == []
