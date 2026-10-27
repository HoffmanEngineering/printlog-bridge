"""The Docker image's setup agrees with the code. The image itself is built and smoke-tested in CI
(.github/workflows/docker.yml, tests/docker/smoke.sh); these checks run anywhere."""

from __future__ import annotations

import re
from pathlib import Path

from printlog_bridge.cli.data_owner import DATA_DIR_ENV, IMAGE_GID, IMAGE_UID
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


def test_starts_as_root_only_to_hand_the_data_folder_to_the_bridges_user():
    # cli/data_owner.py switches to the data folder's user before any command runs, in the
    # image only: where PRINTLOG_BRIDGE_DATA_DIR names the folder.
    runtime = stages()["runtime"]
    users = re.findall(r"^USER\s+(\S+)", runtime, re.MULTILINE)
    assert users[-1] == "root"
    assert dockerfile_env(DATA_DIR_ENV) == "/data"
    assert f"--uid {IMAGE_UID} --gid {IMAGE_GID}" in runtime
    assert f"chown {IMAGE_UID}:{IMAGE_GID} /data" in runtime
    # The app's start-up hands /data over itself; the bridge it runs isn't root.
    assert re.search(rf"^ENV {DATA_DIR_ENV}=$", stages()["addon"], re.MULTILINE)


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
