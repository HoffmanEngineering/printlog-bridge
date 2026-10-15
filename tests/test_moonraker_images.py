"""Thumbnails from Moonraker's file metadata, and snapshots from its webcams."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
from fake_moonraker import FakeMoonraker, eventually
from fake_webcam import JPEG, FakeWebcam
from session_helpers import running

from printlog_bridge.adapters.moonraker.images import MoonrakerImages, snapshot_url
from printlog_bridge.adapters.moonraker.jobs import JobTracker
from printlog_bridge.adapters.moonraker.session import Backoff, MoonrakerSession
from printlog_bridge.core.events import (
    ImageKind,
    JobFinished,
    JobImage,
    JobSlicerSettings,
    JobStarted,
)
from printlog_bridge.core.images import ImageUnavailable


def png(label: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + label.encode()


def thumb(width: int, height: int, path: str, size: int = 1000) -> dict:
    return {"width": width, "height": height, "size": size, "relative_path": path}


# What Moonraker's metadata holds for each slicer's thumbnails, with their default sizes.
SLICERS = {
    "orcaslicer": [thumb(32, 32, ".thumbs/part-32x32.png"), thumb(300, 300, ".thumbs/part.png")],
    "prusaslicer": [
        thumb(16, 16, ".thumbs/part-16x16.png"),
        thumb(400, 300, ".thumbs/part.png"),
        thumb(220, 124, ".thumbs/part-220x124.png"),
    ],
    "cura": [thumb(300, 300, ".thumbs/part.png")],
}


class Calls:
    """Stands in for a Moonraker connection's `call`, with set answers, keeping what's asked."""

    def __init__(self, **answers) -> None:
        self.answers = {name.replace("_", "."): value for name, value in answers.items()}
        self.asked: list[str] = []

    async def __call__(self, method, params=None):
        self.asked.append(method)
        answer = self.answers.get(method)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@contextlib.asynccontextmanager
async def moonraker(**options):
    fake = FakeMoonraker(**options)
    url = await fake.start()
    try:
        yield fake, url
    finally:
        await fake.close()


def remote(url: str, **options) -> MoonrakerImages:
    return MoonrakerImages("voron", url=url, socket=None, **options)


class TestThumbnail:
    @pytest.mark.parametrize("slicer", sorted(SLICERS))
    def test_is_the_largest_one_the_slicer_put_in_the_file(self, slicer):
        async def scenario():
            async with moonraker() as (fake, url):
                for t in SLICERS[slicer]:
                    fake.files[f"parts/{t['relative_path']}"] = png(t["relative_path"])
                job = {"filename": "parts/part.gcode", "metadata": {"thumbnails": SLICERS[slicer]}}
                return await remote(url).thumbnail(Calls(), job)

        assert asyncio.run(scenario()) == png(".thumbs/part.png")

    def test_a_remote_bridge_downloads_it_with_moonrakers_api_key(self):
        async def scenario():
            async with moonraker(api_key="mr-key") as (fake, url):
                fake.files[".thumbs/a.png"] = png("a")
                job = {
                    "filename": "a.gcode",
                    "metadata": {"thumbnails": [thumb(9, 9, ".thumbs/a.png")]},
                }
                return await remote(url, api_key="mr-key").thumbnail(Calls(), job)

        assert asyncio.run(scenario()) == png("a")

    def test_a_local_bridge_reads_it_from_the_gcodes_folder(self, tmp_path):
        (tmp_path / "parts" / ".thumbs").mkdir(parents=True)
        (tmp_path / "parts" / ".thumbs" / "a.png").write_bytes(png("local"))
        calls = Calls(server_files_roots=[{"name": "gcodes", "path": str(tmp_path)}])
        images = MoonrakerImages("voron", url=None, socket=Path("/tmp/moonraker.sock"))
        job = {
            "filename": "parts/a.gcode",
            "metadata": {"thumbnails": [thumb(9, 9, ".thumbs/a.png")]},
        }

        assert asyncio.run(images.thumbnail(calls, job)) == png("local")

    def test_never_reads_outside_the_gcodes_folder(self, tmp_path):
        root = tmp_path / "gcodes"
        root.mkdir()
        (tmp_path / "secret.png").write_bytes(png("secret"))
        calls = Calls(server_files_roots=[{"name": "gcodes", "path": str(root)}])
        images = MoonrakerImages("voron", url=None, socket=Path("/tmp/moonraker.sock"))
        job = {"filename": "a.gcode", "metadata": {"thumbnails": [thumb(9, 9, "../secret.png")]}}

        assert asyncio.run(images.thumbnail(calls, job)) is None

    def test_asks_for_the_files_metadata_when_history_has_none(self):
        async def scenario():
            async with moonraker() as (fake, url):
                fake.files[".thumbs/a.png"] = png("a")
                calls = Calls(server_files_metadata={"thumbnails": [thumb(9, 9, ".thumbs/a.png")]})
                data = await remote(url).thumbnail(calls, {"filename": "a.gcode", "metadata": {}})
                return data, calls.asked

        data, asked = asyncio.run(scenario())

        assert data == png("a")
        assert asked == ["server.files.metadata"]

    def test_a_file_without_thumbnails_has_none(self):
        calls = Calls(server_files_metadata={"estimated_time": 10})
        job = {"filename": "a.gcode", "metadata": {"estimated_time": 10}}

        assert asyncio.run(remote("http://127.0.0.1:9").thumbnail(calls, job)) is None

    def test_a_thumbnail_missing_from_the_printer_is_unavailable(self):
        async def scenario():
            async with moonraker() as (_, url):
                job = {
                    "filename": "a.gcode",
                    "metadata": {"thumbnails": [thumb(9, 9, ".thumbs/gone.png")]},
                }
                with pytest.raises(ImageUnavailable, match="404"):
                    await remote(url).thumbnail(Calls(), job)

        asyncio.run(scenario())


@contextlib.asynccontextmanager
async def webcam(**options):
    cam = FakeWebcam(**options)
    url = await cam.start()
    try:
        yield cam, url
    finally:
        await cam.close()


def webcams(*urls: str, enabled: bool = True) -> Calls:
    return Calls(
        server_webcams_list={
            "webcams": [
                {"name": f"cam{i}", "enabled": enabled, "snapshot_url": u, "stream_url": ""}
                for i, u in enumerate(urls)
            ]
        }
    )


class TestSnapshot:
    def test_is_one_frame_from_the_first_webcam(self):
        async def scenario():
            async with webcam() as (cam, url), webcam(body=png("second")) as (_, other):
                calls = webcams(f"{url}/?action=snapshot", f"{other}/?action=snapshot")
                return await remote("http://printer.local:7125").snapshot(calls), cam.requests

        data, requests = asyncio.run(scenario())

        assert data == JPEG
        assert requests == ["/?action=snapshot"]

    def test_a_disabled_webcam_is_skipped(self):
        calls = webcams("http://127.0.0.1:9/?action=snapshot", enabled=False)

        assert asyncio.run(remote("http://printer.local").snapshot(calls)) is None

    def test_without_a_webcam_there_is_none(self):
        assert asyncio.run(remote("http://printer.local").snapshot(webcams())) is None

    def test_a_webcam_slower_than_the_limit_is_given_up_on(self):
        async def scenario():
            async with webcam(delay=5) as (_, url):
                loop = asyncio.get_running_loop()
                began = loop.time()
                with pytest.raises(ImageUnavailable):
                    await remote("http://p.local", timeout=0.3).snapshot(webcams(f"{url}/s"))
                return loop.time() - began

        assert asyncio.run(scenario()) < 2

    def test_the_limit_is_ten_seconds(self):
        assert remote("http://p.local").timeout == 10


class TestSnapshotUrl:
    @pytest.mark.parametrize(
        ("printer", "snapshot", "expected"),
        [
            # Mainsail and Fluidd serve /webcam/ through nginx, on the printer's web port.
            ("http://voron.local:7125", "/webcam/?action=snapshot", "http://voron.local/webcam/?action=snapshot"),
            ("ws://voron.local:7125/websocket", "/webcam/?action=snapshot", "http://voron.local/webcam/?action=snapshot"),
            ("https://voron.local", "/webcam/?action=snapshot", "https://voron.local/webcam/?action=snapshot"),
            ("http://k2.local:4408", "/webcam/?action=snapshot", "http://k2.local:4408/webcam/?action=snapshot"),
            ("http://voron.local:7125", "http://cam.local:8080/snap.jpg", "http://cam.local:8080/snap.jpg"),
        ],
    )  # fmt: skip
    def test_a_relative_url_is_on_the_printers_web_server(self, printer, snapshot, expected):
        assert snapshot_url(snapshot, printer_url=printer) == expected

    def test_a_local_bridge_uses_this_hosts_web_server(self):
        assert snapshot_url("/webcam/?action=snapshot", printer_url=None) == (
            "http://127.0.0.1/webcam/?action=snapshot"
        )


class RecordingSink:
    """Keeps every event, and fetches each picture as the job logger would."""

    def __init__(self) -> None:
        self.events: list = []
        self.images: dict[ImageKind, bytes | None] = {}

    async def emit(self, event) -> None:
        self.events.append(event)
        if isinstance(event, JobImage):
            self.images[event.kind] = await event.fetch()

    async def resume_after(self, printer):
        return None

    def kinds(self) -> list[str]:
        return [e.kind.value if isinstance(e, JobImage) else type(e).__name__ for e in self.events]


@contextlib.asynccontextmanager
async def tracking(sink: RecordingSink, fake: FakeMoonraker, url: str, *, snapshots: bool = True):
    images = MoonrakerImages("voron", url=url, socket=None, snapshots=snapshots)
    session = MoonrakerSession(
        "voron",
        url=url,
        listener=JobTracker("voron", sink, images=images),
        backoff=Backoff(initial=0.01, maximum=0.05),
    )
    async with running(session):
        await eventually(lambda: fake.subscriptions)
        yield


METADATA = {"thumbnails": [thumb(300, 300, ".thumbs/benchy.png")]}


class TestJobTracker:
    def test_a_job_brings_its_thumbnail_when_it_starts_and_a_snapshot_when_it_ends(self):
        async def scenario():
            sink = RecordingSink()
            async with moonraker() as (fake, url), webcam() as (_, cam_url):
                fake.files[".thumbs/benchy.png"] = png("benchy")
                fake.webcams = [{"name": "cam", "enabled": True, "snapshot_url": f"{cam_url}/s"}]
                async with tracking(sink, fake, url):
                    job = await fake.start_job("benchy.gcode", METADATA)
                    await eventually(lambda: len(sink.events) == 3)
                    await fake.finish_job(job, "error")
                    await eventually(lambda: ImageKind.SNAPSHOT in sink.images)
            return sink

        sink = asyncio.run(scenario())

        assert sink.kinds() == [
            "JobStarted", "thumbnail", "JobSlicerSettings",  # added
            "JobStarted", "thumbnail", "JobSlicerSettings", "JobFinished", "snapshot",  # finished
        ]  # fmt: skip
        assert sink.images == {ImageKind.THUMBNAIL: png("benchy"), ImageKind.SNAPSHOT: JPEG}

    def test_with_snapshots_off_the_camera_is_never_asked(self):
        async def scenario():
            sink = RecordingSink()
            async with moonraker() as (fake, url), webcam() as (cam, cam_url):
                fake.webcams = [{"name": "cam", "enabled": True, "snapshot_url": f"{cam_url}/s"}]
                async with tracking(sink, fake, url, snapshots=False):
                    job = await fake.start_job("benchy.gcode")
                    await fake.finish_job(job, "error")
                    await eventually(lambda: any(isinstance(e, JobFinished) for e in sink.events))
                    await asyncio.sleep(0.2)
            return sink, cam.requests, fake.calls

        sink, camera_requests, calls = asyncio.run(scenario())

        assert ImageKind.SNAPSHOT not in sink.images
        assert camera_requests == []
        assert "server.webcams.list" not in calls

    def test_a_job_that_ended_while_the_bridge_was_away_gets_no_snapshot(self):
        async def scenario():
            sink = RecordingSink()
            async with moonraker() as (fake, url), webcam() as (cam, cam_url):
                fake.webcams = [{"name": "cam", "enabled": True, "snapshot_url": f"{cam_url}/s"}]
                job = await fake.start_job("benchy.gcode", notify=False)
                await fake.finish_job(job, notify=False)
                sink.resume_after = _resume_at_zero
                async with tracking(sink, fake, url):
                    await eventually(lambda: any(isinstance(e, JobFinished) for e in sink.events))
                    await asyncio.sleep(0.2)
            return sink, cam.requests

        sink, camera_requests = asyncio.run(scenario())

        assert [type(e) for e in sink.events if not isinstance(e, JobImage)] == [
            JobStarted,
            JobSlicerSettings,
            JobFinished,
        ]
        assert camera_requests == []


async def _resume_at_zero(printer):
    from datetime import datetime, timezone

    return datetime(2000, 1, 1, tzinfo=timezone.utc)
