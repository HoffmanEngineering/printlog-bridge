"""The Unraid template (unraid/printlog-bridge.xml) and the Community Applications profile
(ca_profile.xml): well formed, with what CA needs (ca.unraid.net/submit/help), and every path
and variable one the image actually reads."""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from printlog_bridge.cli.data_owner import DATA_DIR_ENV, PGID_ENV, PUID_ENV
from printlog_bridge.core.config import (
    API_KEY_ENV,
    CONFIG_PATH_ENV,
    STATE_DIR_ENV,
    STATUS_PAGE_ENV,
)
from printlog_bridge.core.pairing import NAME_ENV

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_FILE = ROOT / "unraid" / "printlog-bridge.xml"
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
IMAGE = "ghcr.io/hoffmanengineering/printlog-bridge"
RAW = "https://raw.githubusercontent.com/HoffmanEngineering/printlog-bridge/main/"
VARIABLES = {API_KEY_ENV, NAME_ENV, PUID_ENV, PGID_ENV, STATUS_PAGE_ENV}
"""The environment variables the template may set: each one the bridge reads."""


@pytest.fixture(scope="module")
def template() -> ET.Element:
    root = ET.parse(TEMPLATE_FILE).getroot()
    assert root.tag == "Container" and root.get("version") == "2"
    return root


def configs(template: ET.Element, kind: str | None = None) -> list[ET.Element]:
    return [c for c in template.iter("Config") if kind is None or c.get("Type") == kind]


def config(template: ET.Element, target: str) -> ET.Element:
    found = [c for c in configs(template) if c.get("Target") == target]
    assert len(found) == 1, f"the template has {len(found)} {target} settings"
    return found[0]


def dockerfile_env(name: str) -> str:
    match = re.search(rf"\b{name}=(\S+)", DOCKERFILE)
    assert match, f"{name} isn't set in the Dockerfile"
    return match.group(1)


def test_has_what_community_applications_needs(template):
    for tag in ("Name", "Repository", "Overview", "Category", "Support", "Project"):
        assert (template.findtext(tag) or "").strip(), f"<{tag}> is empty"
    assert template.findtext("Name") == "printlog-bridge"
    for tag in ("Support", "Project", "Registry", "ReadMe", "Icon", "TemplateURL"):
        assert template.findtext(tag).startswith("https://"), tag
    # TemplateURL is the raw URL of this very file, on the default branch.
    assert template.findtext("TemplateURL") == RAW + "unraid/printlog-bridge.xml"
    assert template.findtext("License") == "MIT"


def test_the_categories_are_cas(template):
    # CA's categories are `Name:` or `Name:Sub`, separated by spaces.
    for category in template.findtext("Category").split():
        assert re.fullmatch(r"[A-Za-z]+:[A-Za-z]*", category), category


def test_the_icon_is_a_square_png_in_this_repository(template):
    icon = template.findtext("Icon")
    assert icon.startswith(RAW)
    path = ROOT / icon[len(RAW) :]
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert width == height >= 128


def test_runs_the_image_compose_runs(template):
    compose_tag = re.search(rf"image: {IMAGE}:(\S+)", COMPOSE).group(1)
    assert template.findtext("Repository") == f"{IMAGE}:{compose_tag}"
    assert template.findtext("Privileged") == "false"


def test_defaults_to_host_networking_and_opens_no_ports(template):
    # Bambu Lab discovery (UDP broadcasts) and zeroconf need the host's network.
    assert template.findtext("Network") == "host"
    assert "host network" in template.findtext("Overview").lower()
    assert not configs(template, "Port")
    assert not (template.findtext("WebUI") or "").strip()  # the bridge serves no page


def test_the_data_folder_is_the_images(template):
    paths = configs(template, "Path")
    assert [p.get("Target") for p in paths] == [dockerfile_env(DATA_DIR_ENV)]
    data = paths[0]
    assert dockerfile_env(CONFIG_PATH_ENV).startswith(data.get("Target") + "/")
    assert dockerfile_env(STATE_DIR_ENV).startswith(data.get("Target") + "/")
    assert data.get("Default") == "/mnt/user/appdata/printlog-bridge"
    assert data.text == data.get("Default")
    assert data.get("Mode") == "rw"


def test_every_variable_is_one_the_bridge_reads(template):
    targets = {c.get("Target") for c in configs(template, "Variable")}
    assert targets <= VARIABLES, targets - VARIABLES
    # Unraid passes its own time zone to every container as TZ; a second one would clash.
    assert "TZ" not in targets


def test_runs_as_nobody_users_through_puid_not_a_user_flag(template):
    # The image starts as root, gives /data to PUID:PGID and drops to them (cli/data_owner.py).
    assert (
        config(template, PUID_ENV).get("Default"),
        config(template, PGID_ENV).get("Default"),
    ) == (
        "99",
        "100",
    )
    for name in (PUID_ENV, PGID_ENV):
        setting = config(template, name)
        assert setting.text == setting.get("Default")
    assert "--user" not in (template.findtext("ExtraParams") or "")
    assert not (template.findtext("PostArgs") or "").strip()


def test_the_api_key_is_optional_masked_and_empty(template):
    key = config(template, API_KEY_ENV)
    assert (key.get("Mask"), key.get("Required")) == ("true", "false")
    assert not key.get("Default") and not (key.text or "").strip()


def test_the_bridge_name_is_optional(template):
    name = config(template, NAME_ENV)
    assert name.get("Required") == "false"
    assert not (name.text or "").strip()


def test_every_setting_is_complete(template):
    for setting in configs(template):
        for attribute in ("Name", "Target", "Description", "Type", "Display", "Required", "Mask"):
            assert setting.get(attribute) is not None, (setting.get("Name"), attribute)
        assert setting.get("Display") in ("always", "advanced")


def test_the_ca_profile_is_at_the_root():
    profile = ET.parse(ROOT / "ca_profile.xml").getroot()
    assert profile.tag == "CommunityApplications"
    assert (profile.findtext("Profile") or "").strip()
    assert profile.findtext("Icon").startswith(RAW)
    assert (ROOT / profile.findtext("Icon")[len(RAW) :]).is_file()


def test_ca_finds_no_other_xml_in_the_repository():
    # CA scans the repository's XML files for apps: only the template and the profile.
    skip = {".git", ".venv", "build", "dist", "node_modules"}
    found = sorted(
        p.relative_to(ROOT).as_posix()
        for p in ROOT.rglob("*.xml")
        if not skip & set(p.relative_to(ROOT).parts)
    )
    assert found == ["ca_profile.xml", "unraid/printlog-bridge.xml"]


def test_the_local_page_is_optional_and_stays_off_the_lan_by_default(template):
    # Empty: the bridge's own default, 127.0.0.1:7180 (on host networking, the server itself).
    page = config(template, STATUS_PAGE_ENV)
    assert (page.get("Required"), page.get("Display")) == ("false", "advanced")
    assert not page.get("Default") and not (page.text or "").strip()
    assert "0.0.0.0:7180" in page.get("Description")
    assert "opens no ports" not in template.findtext("Overview")
