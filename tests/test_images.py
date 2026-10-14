from __future__ import annotations

import asyncio
import contextlib
import time

import pytest
from fake_webcam import JPEG, PNG, FakeWebcam

from printlog_bridge.core.images import (
    MAX_ERROR_CHARS,
    MAX_IMAGE_BYTES,
    ImageUnavailable,
    fetch_image,
    image_type,
    mask_url,
    read_image_file,
    scrub_url,
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


class TestScrubUrl:
    """An error that repeats a URL, as aiohttp and yarl write it, which needn't be how the
    config spells it: yarl upper-cases percent escapes, decodes some, and keeps others."""

    def test_hides_the_url_as_given(self):
        url = "http://maker:hunter22@cam.local/snap?token=abc123"
        text = scrub_url(ValueError(f"bad URL {url}"), url)

        assert "hunter22" not in text and "abc123" not in text
        assert "cam.local/snap" in text

    def test_hides_a_password_yarl_writes_with_upper_case_escapes(self):
        url = "http://maker:s3cr%2fet@cam.local/snap"
        shown = "http://maker:s3cr%2Fet@cam.local/snap"  # str(yarl.URL(url))
        text = scrub_url(OSError(f"can't fetch {shown}; password s3cr%2Fet"), url)

        assert "s3cr" not in text
        assert "cam.local/snap" in text

    def test_hides_a_password_shown_decoded(self):
        url = "http://maker:p%40ssw0rd@cam.local/snap"
        text = scrub_url(ValueError("login 'maker' with 'p@ssw0rd' was refused"), url)

        assert "p@ssw0rd" not in text
        assert "was refused" in text

    def test_hides_a_query_value_yarl_keeps_encoded(self):
        url = "http://cam.local/snap?token=tok%20en12"
        # parse_qsl decodes the value to "tok en12"; yarl shows it as it was
        text = scrub_url(OSError("token tok%20en12 expired"), url)

        assert "tok%20en12" not in text

    def test_hides_a_query_value_shown_with_a_plus(self):
        url = "http://cam.local/snap?token=tok%20en12"
        text = scrub_url(OSError("token tok+en12 expired"), url)

        assert "tok+en12" not in text

    def test_hides_a_short_user_and_password_in_a_url(self):
        url = "http://pi:ab@Cam.local:8080/snap"
        shown = "http://pi:ab@cam.local:8080/snap"  # yarl writes the host in lower case
        text = scrub_url(OSError(f"can't fetch {shown}/"), url)

        assert "pi:ab" not in text
        assert "http://***@cam.local:8080/snap/" in text

    def test_hides_a_short_secret_query_value_in_a_url(self):
        url = "http://cam.local/snap?token=ab"
        text = scrub_url(OSError("can't fetch http://cam.local/snap/?token=ab&x=1"), url)

        assert "token=ab" not in text
        assert "x=1" in text

    def test_leaves_a_short_credential_alone_elsewhere(self):
        """A two-letter user is far too common to scrub everywhere: `pi` would blank the host
        name, and every word that holds it."""
        url = "http://pi:raspberry@pi.local:7125"
        text = scrub_url(
            OSError("Cannot connect to host pi.local:7125 ssl:default [api pipe: raspberry]"),
            url,
        )

        assert text == "Cannot connect to host pi.local:7125 ssl:default [api pipe: ***]"

    def test_leaves_short_query_values_alone_outside_a_url(self):
        url = "http://cam.local/snap?token=ab&action=snapshot"
        text = scrub_url(OSError("about a tab: 404"), url)

        assert text == "about a tab: 404"


LONG_MESSAGES = {
    "one long word": "a" * 200_000,
    "a long query name": "?" + "x" * 200_000,
    "a query name of secret words": "?" + "token" * 40_000,
    "schemes": "a://" * 50_000,
    "secret query names": "&token" * 33_000,
    "user infos": "http://u:p" * 20_000,
}
"""Error text can come from the network (Moonraker's own error messages, up to a 4 MB frame):
scrubbing must take time in proportion to it, never its square."""


class TestScrubUrlOnLongText:
    @pytest.mark.parametrize("message", LONG_MESSAGES.values(), ids=LONG_MESSAGES.keys())
    def test_is_quick(self, message):
        began = time.perf_counter()
        scrub_url(OSError(message), "http://pi:ab@cam.local/x?token=abc123")
        assert time.perf_counter() - began < 1

    def test_keeps_the_start_of_a_long_message(self):
        text = scrub_url(OSError("Moonraker says: " + "x " * 5000), "http://cam.local/x")

        assert text.startswith("Moonraker says: x x")
        assert len(text) <= MAX_ERROR_CHARS + 1
        assert text.endswith("…")

    def test_a_secret_cut_in_two_shows_no_part_of_itself(self):
        url = "http://maker:hunter22hunter22@cam.local/x?token=abc123"
        for start in range(MAX_ERROR_CHARS - 30, MAX_ERROR_CHARS + 2):
            message = "x" * start + " hunter22hunter22 tail http://u:pw@h/?token=abc123"
            text = scrub_url(OSError(message), url)

            assert "hunt" not in text, start
            assert "pw" not in text, start
            assert "abc" not in text, start

    def test_a_short_message_is_not_cut(self):
        assert scrub_url(OSError("x" * MAX_ERROR_CHARS), "http://cam.local/x") == (
            "x" * MAX_ERROR_CHARS
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
