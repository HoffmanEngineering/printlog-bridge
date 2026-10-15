import pytest

from printlog_bridge import __version__
from printlog_bridge.core.updates import is_newer, update_notice


@pytest.mark.parametrize(
    ("latest", "current", "expected"),
    [
        ("0.2.0", "0.1.0", True),
        ("0.10.0", "0.9.0", True),
        ("1.0.0", "0.99.99", True),
        ("0.2.0", "0.2.0", False),
        ("0.2.0", "0.3.0", False),  # edge, ahead of the last release
        ("0.2.0", "0.2.0.dev1", False),  # only the first three numbers count
        ("0.2.0", "0.1.0+local", True),
        ("v0.2.0", "0.1.0", True),
        ("0.2", "0.1.0", False),
        ("latest", "0.1.0", False),
        ("", "0.1.0", False),
        (None, "0.1.0", False),
        (42, "0.1.0", False),
        ("0.2.0", "unknown", False),
    ],
)
def test_is_newer(latest, current, expected):
    assert is_newer(latest, current) is expected


def test_is_newer_compares_with_this_bridge_by_default():
    assert not is_newer(__version__)
    assert is_newer("999.0.0")


def test_the_notice_names_both_versions_and_how_to_update():
    text = update_notice("0.2.0", "0.1.0")

    assert text.startswith("printlog-bridge 0.2.0 is available (this is 0.1.0).")
    assert "docker compose pull && docker compose up -d" in text
    assert "update manager" in text
