"""The PrusaLink HTTP client's authentication, against `tests/fake_prusalink.py`: Digest as
Buddy firmware does it (no qop, a nonce good for 300 s), and X-Api-Key."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from fake_prusalink import PASSWORD, FakePrusaLink

from printlog_bridge.adapters.prusalink.client import PrusaLinkClient, Refused, digest_header


@contextlib.asynccontextmanager
async def printer():
    fake = FakePrusaLink()
    address = await fake.start()
    try:
        yield fake, f"http://{address}"
    finally:
        await fake.close()


def run(coroutine):
    return asyncio.run(coroutine)


class TestDigest:
    def test_answers_the_challenge_and_reuses_the_nonce(self):
        async def scenario():
            async with printer() as (fake, url):
                client = PrusaLinkClient(url, password=PASSWORD)
                try:
                    first = await client.get_json("/api/version")
                    second = await client.get_json("/api/v1/status")
                finally:
                    await client.close()
                return fake, first, second

        fake, first, second = run(scenario())

        assert first == (200, fake.version)
        assert second[0] == 200 and second[1]["printer"]["state"] == "IDLE"
        assert fake.unauthorized == 1  # the second request reused the nonce

    def test_a_stale_nonce_is_answered_again(self):
        async def scenario():
            async with printer() as (fake, url):
                client = PrusaLinkClient(url, password=PASSWORD)
                try:
                    await client.get_json("/api/version")
                    fake.expire_nonces()
                    return await client.get_json("/api/v1/status")
                finally:
                    await client.close()

        assert run(scenario())[0] == 200

    def test_a_wrong_password_is_refused(self):
        async def scenario():
            async with printer() as (fake, url):
                client = PrusaLinkClient(url, password="wrong")
                try:
                    with pytest.raises(Refused):
                        await client.get_json("/api/v1/status")
                finally:
                    await client.close()
                return fake.unauthorized

        assert run(scenario()) == 2  # the challenge, and the wrong answer: no loop

    def test_qop_auth_when_the_server_offers_it(self):
        header = digest_header(
            "GET",
            "/api/v1/status",
            "maker",
            "pw",
            {"realm": "r", "nonce": "n", "qop": "auth,auth-int", "opaque": "o"},
            nc=1,
            cnonce="c",
        )

        assert "qop=auth" in header and "nc=00000001" in header and 'cnonce="c"' in header
        assert 'opaque="o"' in header


class TestApiKey:
    def test_the_api_key_is_sent_as_x_api_key(self):
        async def scenario():
            async with printer() as (fake, url):
                fake.api_key_only = True
                client = PrusaLinkClient(url, api_key=PASSWORD)
                try:
                    return await client.get_json("/api/v1/status"), fake.unauthorized
                finally:
                    await client.close()

        (status, _), unauthorized = run(scenario())
        assert status == 200 and unauthorized == 0

    def test_a_password_goes_as_the_key_to_firmware_that_asks_for_one(self):
        async def scenario():
            async with printer() as (fake, url):
                fake.api_key_only = True
                client = PrusaLinkClient(url, password=PASSWORD)
                try:
                    return await client.get_json("/api/v1/status")
                finally:
                    await client.close()

        assert run(scenario())[0] == 200


def test_no_job_is_204_without_a_body():
    async def scenario():
        async with printer() as (fake, url):
            client = PrusaLinkClient(url, password=PASSWORD)
            try:
                return await client.get_json("/api/v1/job")
            finally:
                await client.close()

    assert run(scenario()) == (204, None)


def test_a_stream_stops_where_the_reader_does():
    async def scenario():
        async with printer() as (fake, url):
            fake.files["BIG.BGC"] = b"x" * 2_000_000
            fake.chunk_delay = 0.002
            client = PrusaLinkClient(url, password=PASSWORD)
            try:
                seen = 0
                async with client.stream("/usb/BIG.BGC") as chunks:
                    async for chunk in chunks:
                        seen += len(chunk)
                        if seen >= 100_000:
                            break
            finally:
                await client.close()
            await asyncio.sleep(0.1)
            return seen, fake.downloaded_bytes

    seen, sent = run(scenario())
    assert seen >= 100_000
    assert sent < 1_000_000
