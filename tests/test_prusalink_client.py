"""The PrusaLink HTTP client's authentication, against `tests/fake_prusalink.py`: Digest as
Buddy firmware does it (no qop, a nonce good for 300 s), and X-Api-Key."""

from __future__ import annotations

import asyncio
import contextlib
import tracemalloc
import zlib

import aiohttp
import pytest
from fake_prusalink import PASSWORD, FakePrusaLink
from http_helpers import gzip_bomb, host_of, http_server

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


class TestOrigin:
    """A path from the printer (a job's `refs`) is only ever requested from the printer: the
    credentials go with every request."""

    @pytest.mark.parametrize(
        "path",
        ["@{other}/thumb.png", "thumb.png", "http://{other}/thumb.png", "\\{other}/thumb.png"],
    )
    def test_a_path_that_leaves_the_printer_is_not_requested(self, path):
        async def scenario():
            async with http_server() as other, http_server() as printer:
                client = PrusaLinkClient(printer.url, api_key="TEST-ONLY-KEY")
                where = path.format(other=host_of(other))
                try:
                    with pytest.raises(aiohttp.InvalidURL):
                        await client.get_json(where)
                    with pytest.raises(aiohttp.InvalidURL):
                        async with client.stream(where):
                            pass
                finally:
                    await client.close()
                return other.requests, printer.requests

        assert run(scenario()) == ([], [])

    def test_a_double_slash_stays_a_path_on_the_printer(self):
        async def scenario():
            async with http_server() as other, http_server() as printer:
                client = PrusaLinkClient(printer.url, api_key="TEST-ONLY-KEY")
                try:
                    await client.get_json(f"//{host_of(other)}/thumb.png")
                finally:
                    await client.close()
                return other.requests, [path for path, _ in printer.requests]

        other, printer = run(scenario())
        assert other == [] and len(printer) == 1


class TestBodyCap:
    """aiohttp inflates a gzipped answer by itself, so the cap counts what it inflates to."""

    def test_an_answer_inflating_past_the_cap_is_refused_without_reading_it_all(self):
        async def scenario():
            async with http_server() as server:
                gzip_bomb(server)
                client = PrusaLinkClient(server.url, api_key="TEST-ONLY-KEY")
                tracemalloc.start()
                try:
                    with pytest.raises(aiohttp.ClientPayloadError):
                        await client.get_json("/api/v1/status")
                    return tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                    await client.close()

        assert run(scenario()) < 32 * 1024 * 1024

    def test_a_compressed_answer_under_the_cap_is_read(self):
        async def scenario():
            async with http_server() as server:
                gzip_bomb(server, inflated=2 * 1024 * 1024)
                client = PrusaLinkClient(server.url, api_key="TEST-ONLY-KEY")
                try:
                    return await client.get_json("/api/v1/status")
                finally:
                    await client.close()

        status, body = run(scenario())
        assert status == 200 and len(body["a"]) == 2 * 1024 * 1024

    def test_an_answer_in_pieces_is_read_whole(self):
        async def scenario():
            async with http_server() as server:
                server.pieces = [b'{"a": "', b"y" * 200_000, b"y" * 200_000, b'"}']
                client = PrusaLinkClient(server.url, api_key="TEST-ONLY-KEY")
                try:
                    return await client.get_json("/api/v1/status")
                finally:
                    await client.close()

        assert len(run(scenario())[1]["a"]) == 400_000

    def test_a_compressed_download_is_inflated_a_piece_at_a_time(self):
        async def scenario():
            async with http_server() as server:
                gzip_bomb(server)
                client = PrusaLinkClient(server.url, api_key="TEST-ONLY-KEY")
                tracemalloc.start()
                try:
                    pieces = []
                    async with client.stream("/usb/BOMB.BGC") as chunks:
                        async for chunk in chunks:
                            pieces.append(len(chunk))
                            if sum(pieces) >= 1024 * 1024:
                                break
                    return pieces, tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                    await client.close()

        pieces, peak = run(scenario())
        assert max(pieces) <= 64 * 1024
        assert peak < 8 * 1024 * 1024

    @pytest.mark.parametrize("encoding", ["deflate", "identity"])
    def test_a_deflated_or_plain_answer_is_read(self, encoding):
        body = b'{"state": "IDLE"}'

        async def scenario():
            async with http_server() as server:
                server.body = zlib.compress(body) if encoding == "deflate" else body
                server.headers = {"Content-Type": "application/json", "Content-Encoding": encoding}
                client = PrusaLinkClient(server.url, api_key="TEST-ONLY-KEY")
                try:
                    return await client.get_json("/api/v1/status")
                finally:
                    await client.close()

        assert run(scenario()) == (200, {"state": "IDLE"})

    def test_an_answer_in_an_encoding_it_did_not_ask_for_is_refused(self):
        async def scenario():
            async with http_server() as server:
                server.headers = {"Content-Type": "application/json", "Content-Encoding": "br"}
                client = PrusaLinkClient(server.url, api_key="TEST-ONLY-KEY")
                try:
                    with pytest.raises(aiohttp.ClientPayloadError):
                        await client.get_json("/api/v1/status")
                    return server.requests[0][1].get("Accept-Encoding")
                finally:
                    await client.close()

        assert run(scenario()) == "gzip, deflate"
