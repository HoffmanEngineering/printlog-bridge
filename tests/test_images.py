from __future__ import annotations

import asyncio
import contextlib

import pytest
from fake_webcam import JPEG, PNG, FakeWebcam

from printlog_bridge.core.images import (
    MAX_IMAGE_BYTES,
    ImageUnavailable,
    fetch_image,
    image_type,
    mask_url,
    read_image_file,
)


class TestImageType:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (PNG, "image/png"),
            (JPEG, "image/jpeg"),
            (b"GIF89a" + b"\x00" * 10, "image/gif"),
            (b"RIFF\x10\x00\x00\x00WEBPVP8 ", "image/webp"),
            (b"BM" + b"\x00" * 20, "image/bmp"),
        ],
    )
    def test_knows_each_type_the_api_accepts(self, data, expected):
        assert image_type(data) == expected

    @pytest.mark.parametrize("data", [b"qoif\x00\x00", b"<html>", b""])
    def test_anything_else_is_none(self, data):
        assert image_type(data) is None


class TestMaskUrl:
    def test_hides_the_user_and_password(self):
        assert (
            mask_url("http://admin:hunter2@cam.local/snapshot") == "http://***@cam.local/snapshot"
        )

    def test_hides_query_values_that_look_like_secrets(self):
        masked = mask_url("http://cam.local/snap.jpg?action=snapshot&token=abc123&pwd=x")
        assert "abc123" not in masked
        assert "pwd=x" not in masked
        assert "action=snapshot" in masked

    def test_leaves_a_plain_url_alone(self):
        assert mask_url("http://cam.local:8080/?action=snapshot") == (
            "http://cam.local:8080/?action=snapshot"
        )


@contextlib.asynccontextmanager
async def webcam(**options):
    cam = FakeWebcam(**options)
    url = await cam.start()
    try:
        yield cam, url
    finally:
        await cam.close()


class TestFetchImage:
    def test_returns_the_image(self):
        async def scenario():
            async with webcam() as (_, url):
                return await fetch_image(f"{url}/?action=snapshot", timeout=5)

        assert asyncio.run(scenario()) == JPEG

    def test_gives_up_after_the_timeout(self):
        async def scenario():
            async with webcam(delay=2) as (_, url):
                loop = asyncio.get_running_loop()
                began = loop.time()
                with pytest.raises(ImageUnavailable, match="0.2s"):
                    await fetch_image(f"{url}/snap", timeout=0.2)
                return loop.time() - began

        assert asyncio.run(scenario()) < 1.5

    def test_refuses_an_image_over_the_limit(self):
        async def scenario():
            async with webcam(body=JPEG + b"\x00" * 2000) as (_, url):
                with pytest.raises(ImageUnavailable, match="larger than"):
                    await fetch_image(f"{url}/snap", timeout=5, max_bytes=1000)

        asyncio.run(scenario())

    def test_refuses_what_is_not_an_image(self):
        async def scenario():
            async with webcam(body=b"<html>login</html>", content_type="text/html") as (_, url):
                with pytest.raises(ImageUnavailable, match="not an image"):
                    await fetch_image(f"{url}/snap", timeout=5)

        asyncio.run(scenario())

    def test_an_error_status_is_unavailable(self):
        async def scenario():
            async with webcam(status=503) as (_, url):
                with pytest.raises(ImageUnavailable, match="503"):
                    await fetch_image(f"{url}/snap", timeout=5)

        asyncio.run(scenario())

    def test_signs_in_with_credentials_in_the_url_and_never_repeats_them(self):
        async def scenario():
            async with webcam(credentials=("admin", "hunter2")) as (_, url):
                good = await fetch_image(url.replace("://", "://admin:hunter2@") + "/s", timeout=5)
                with pytest.raises(ImageUnavailable) as bad:
                    await fetch_image(url.replace("://", "://admin:wrong-pass@") + "/s", timeout=5)
                return good, str(bad.value)

        good, message = asyncio.run(scenario())

        assert good == JPEG
        assert "401" in message
        assert "wrong-pass" not in message

    def test_an_unreachable_camera_never_shows_its_password(self):
        async def scenario():
            with pytest.raises(ImageUnavailable) as e:
                await fetch_image("http://admin:hunter2@127.0.0.1:1/snapshot", timeout=2)
            return str(e.value)

        assert "hunter2" not in asyncio.run(scenario())

    def test_sends_the_headers_it_is_given(self):
        async def scenario():
            async with webcam(credentials=("u", "p")) as (_, url):
                return await fetch_image(
                    f"{url}/s", timeout=5, headers={"Authorization": "Basic dTpw"}
                )

        assert asyncio.run(scenario()) == JPEG


class TestReadImageFile:
    def test_reads_an_image(self, tmp_path):
        (tmp_path / "thumb.png").write_bytes(PNG)
        assert asyncio.run(read_image_file(tmp_path / "thumb.png")) == PNG

    def test_refuses_a_file_over_the_limit_without_reading_it(self, tmp_path):
        (tmp_path / "big.png").write_bytes(PNG + b"\x00" * 2000)
        with pytest.raises(ImageUnavailable, match="larger than"):
            asyncio.run(read_image_file(tmp_path / "big.png", max_bytes=1000))

    def test_a_missing_file_is_unavailable(self, tmp_path):
        with pytest.raises(ImageUnavailable):
            asyncio.run(read_image_file(tmp_path / "nope.png"))

    def test_the_default_limit_is_the_apis(self):
        assert MAX_IMAGE_BYTES == 10 * 1024 * 1024
