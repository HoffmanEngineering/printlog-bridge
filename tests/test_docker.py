"""The Docker image's setup agrees with the code. The image itself is built and smoke-tested in CI
(.github/workflows/docker.yml, tests/docker/smoke.sh); these checks run anywhere."""

from __future__ import annotations

import re
from pathlib import Path

from printlog_bridge.core.config import API_KEY_ENV, CONFIG_PATH_ENV, STATE_DIR_ENV

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")


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


def test_runs_as_a_non_root_user():
    users = re.findall(r"^USER\s+(\S+)", DOCKERFILE, re.MULTILINE)
    assert users, "the final stage has no USER"
    assert users[-1].split(":")[0] not in ("root", "0"), users[-1]


def test_runs_the_bridge_and_checks_its_health_with_status():
    assert 'ENTRYPOINT ["printlog-bridge"]' in DOCKERFILE
    assert 'CMD ["run"]' in DOCKERFILE
    assert 'CMD ["printlog-bridge", "status"]' in DOCKERFILE


def test_the_image_never_bakes_in_an_api_key():
    assert API_KEY_ENV not in DOCKERFILE
    assert re.search(rf"^\s*{API_KEY_ENV}:\s*\S", COMPOSE, re.MULTILINE) is None


def test_compose_and_workflow_name_the_same_image():
    image = "ghcr.io/hoffmanengineering/printlog-bridge"
    assert f"image: {image}:" in COMPOSE
    assert f"IMAGE: {image}\n" in WORKFLOW
    assert "linux/amd64,linux/arm64,linux/arm/v7" in WORKFLOW
