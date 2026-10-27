"""`scrub`, which the support bundle and traces run over everything they write."""

from __future__ import annotations

import time

import pytest

from printlog_bridge.core.secrets import REDACTED, URL_USERINFO, scrub

LONG_TEXT = {
    "one long word": "a" * 200_000,
    "a long query name": "?" + "x" * 200_000,
    "a query name of secret words": "?" + "token" * 40_000,
    "schemes": "a://" * 50_000,
    "secret query names": "&token" * 33_000,
    "user infos": "http://u:p" * 20_000,
    "letters after digits": "1a" * 100_000,
    "schemes after digits": "1http://u:p" * 20_000,
    "query values of nested URLs": "?a=http://h/?b=" * 15_000,
}


@pytest.mark.parametrize("text", LONG_TEXT.values(), ids=LONG_TEXT.keys())
def test_is_quick_on_long_text(text):
    """A trace line can be a 4 MB WebSocket frame from Moonraker."""
    began = time.perf_counter()
    scrub(text, ["k-secret"])
    assert time.perf_counter() - began < 1


def test_hides_passwords_in_urls_and_secret_query_values():
    text = scrub(
        '{"url": "http://admin:hunter2@cam.local/?action=snapshot&api_key=abc&x=1"} k-1',
        ["k-1"],
    )

    assert text == (
        f'{{"url": "http://{REDACTED}@cam.local/?action=snapshot&api_key={REDACTED}&x=1"}} '
        f"{REDACTED}"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see rtsp://u:p@cam/", f"see rtsp://{REDACTED}@cam/"),
        ("(git+ssh://u:p@host/x)", f"(git+ssh://{REDACTED}@host/x)"),
        ('"http://u:p@h"', f'"http://{REDACTED}@h"'),
        ("1http://u:pw@h/", f"1http://{REDACTED}@h/"),
        ("éhttp://u:pw@h/", f"éhttp://{REDACTED}@h/"),
        ("x_http://u:pw@h/", f"x_http://{REDACTED}@h/"),
        ("mailto:me@example.com", "mailto:me@example.com"),
        ("http://cam.local/a@b", "http://cam.local/a@b"),
    ],
)
def test_url_userinfo(text, expected):
    assert URL_USERINFO.sub(rf"\1{REDACTED}@", text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "GET http://h/redirect?next=http://cam/snap?token=SECRET123",
        "GET http://h/x?a=1?token=SECRET123",
    ],
)
def test_hides_a_secret_query_value_after_a_nested_url(text):
    assert "SECRET123" not in scrub(text, [])


def test_a_bambu_access_code_in_the_config_is_a_known_secret(tmp_path):
    from printlog_bridge.core.support import known_secrets, redact_config

    config = tmp_path / "printlog-bridge.cfg"
    text = "[printer x1c]\nadapter = bambu\naccess_code = 12345678\n"
    config.write_text(text, encoding="utf-8")

    assert "12345678" in known_secrets(config, tmp_path / "state", {})
    assert "12345678" not in redact_config(text)
