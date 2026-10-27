"""The Docker image's setup agrees with the code. The image itself is built and smoke-tested in CI
(.github/workflows/docker.yml, tests/docker/smoke.sh); these checks run anywhere."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from printlog_bridge.core.config import API_KEY_ENV, CONFIG_PATH_ENV, STATE_DIR_ENV

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
IMAGE = "ghcr.io/hoffmanengineering/printlog-bridge"
PLATFORMS = ("linux/amd64", "linux/arm64", "linux/arm/v7")


def dockerfile_env(name: str) -> str:
    match = re.search(rf"\b{name}=(\S+)", DOCKERFILE)
    assert match, f"{name} isn't set in the Dockerfile"
    return match.group(1)


def test_config_and_state_live_in_the_data_volume():
    assert dockerfile_env(CONFIG_PATH_ENV) == "/data/printlog-bridge.cfg"
    assert dockerfile_env(STATE_DIR_ENV) == "/data/state"
    assert dockerfile_env("HOME") == "/data"
    assert 'VOLUME ["/data"]' in DOCKERFILE
    assert ":/data" in COMPOSE


def stages() -> dict[str, str]:
    """Each stage of the Dockerfile by name (the unnamed last one as ""), from its FROM line."""
    found = {}
    for chunk in re.split(r"^(?=FROM )", DOCKERFILE, flags=re.MULTILINE)[1:]:
        header = chunk.splitlines()[0]
        match = re.search(r"\sAS\s+(\S+)", header, re.IGNORECASE)
        found[match.group(1) if match else ""] = chunk
    return found


def test_the_default_target_is_the_runtime_stage_unchanged():
    # `docker build .` and the published image get the normal image; the app's stage is built
    # only with --target addon. The last stage adds nothing to the runtime stage.
    assert stages()[""].strip() == "FROM runtime"
    assert list(stages()) == ["build", "runtime", "addon", ""]


def test_runs_as_a_non_root_user():
    users = re.findall(r"^USER\s+(\S+)", stages()["runtime"], re.MULTILINE)
    assert users, "the runtime stage has no USER"
    assert users[-1].split(":")[0] not in ("root", "0"), users[-1]


def test_the_app_stage_starts_as_root_and_runs_the_app_glue():
    # The Supervisor's /data is root's; printlog-bridge-addon drops to the printlog user.
    addon = stages()["addon"]
    assert addon.splitlines()[0] == "FROM runtime AS addon"
    assert re.search(r"^USER root$", addon, re.MULTILINE)
    assert 'ENTRYPOINT ["printlog-bridge-addon"]' in addon
    assert "HEALTHCHECK" not in addon  # the runtime stage's own health check is the watchdog


def test_runs_the_bridge_and_checks_its_health_with_the_light_health_command():
    assert 'ENTRYPOINT ["printlog-bridge"]' in DOCKERFILE
    assert 'CMD ["run"]' in DOCKERFILE
    assert 'CMD ["printlog-bridge-health"]' in DOCKERFILE


def test_the_image_never_bakes_in_an_api_key():
    assert API_KEY_ENV not in DOCKERFILE
    assert re.search(rf"^\s*{API_KEY_ENV}:\s*\S", COMPOSE, re.MULTILINE) is None


def test_compose_and_workflow_name_the_same_image():
    assert f"image: {IMAGE}:" in COMPOSE
    assert f"IMAGE: {IMAGE}\n" in WORKFLOW
    assert f"PLATFORMS: {','.join(PLATFORMS)}\n" in WORKFLOW


def workflow_job(name: str) -> str:
    """The text of one job in docker.yml, from its `  name:` line to the next job's."""
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", WORKFLOW, re.MULTILINE | re.DOTALL)
    assert match, f"docker.yml has no {name} job"
    return match.group(1)


def test_every_platform_is_smoke_tested_before_anything_is_published():
    smoke = workflow_job("smoke")
    for platform in PLATFORMS:
        assert f"platform: {platform}," in smoke, f"{platform} isn't smoke-tested"
    assert 'tests/docker/smoke.sh printlog-bridge:smoke "${{ matrix.platform }}"' in smoke
    # Branch protection requires a check called `smoke`: the amd64 leg keeps that name.
    assert "matrix.platform == 'linux/amd64' && 'smoke'" in smoke
    for job in ("build", "publish"):
        assert "needs: smoke\n" in workflow_job(job)


def test_the_published_image_is_checked_without_logging_in():
    verify = workflow_job("verify")
    assert "needs: publish\n" in verify
    assert "tests/docker/check_published.sh" in verify
    assert "${{ needs.publish.outputs.tag }}" in verify
    assert "packages:" not in verify
    assert "login-action" not in verify
    assert "tag: ${{ steps.meta.outputs.version }}" in workflow_job("publish")


def unraid_config(template: ET.Element, target: str) -> ET.Element:
    configs = [c for c in template.iter("Config") if c.get("Target") == target]
    assert len(configs) == 1, f"the Unraid template has {len(configs)} {target} settings"
    return configs[0]


def test_the_unraid_template_matches_the_image():
    template = ET.parse(ROOT / "unraid" / "printlog-bridge.xml").getroot()
    compose_tag = re.search(rf"image: {IMAGE}:(\S+)", COMPOSE).group(1)
    assert template.findtext("Repository") == f"{IMAGE}:{compose_tag}"
    assert template.findtext("Network") == "bridge"
    assert template.findtext("Privileged") == "false"
    # Unraid's appdata belongs to nobody:users; the container must not run as root.
    assert "--user 99:100" in template.findtext("ExtraParams")
    assert template.findtext("PostArgs", "") == ""

    data = unraid_config(template, "/data")
    assert data.get("Type") == "Path"
    assert data.get("Default") == "/mnt/user/appdata/printlog-bridge"
    assert data.text == data.get("Default")

    # Unraid passes its own time zone to every container as TZ; a second one would clash.
    assert not [c for c in template.iter("Config") if c.get("Target") == "TZ"]

    # Optional, masked, and empty: a blank PRINTLOG_API_KEY falls back to /data/state/api_key.
    key = unraid_config(template, API_KEY_ENV)
    assert (key.get("Mask"), key.get("Required")) == ("true", "false")
    assert not key.get("Default") and not (key.text or "").strip()

    # The bridge only connects out.
    assert not [c for c in template.iter("Config") if c.get("Type") == "Port"]
