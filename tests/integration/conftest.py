"""Fixtures that run a real Moonraker in Docker.

Run with `pytest -m integration`. Needs Docker; the tests skip when it isn't available.
MOONRAKER_REF picks the Moonraker tag or commit to build (default: v0.9.3).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).parent
REPO = HERE.parent.parent
MOONRAKER_REF = os.environ.get("MOONRAKER_REF", "v0.9.3")
STARTUP_TIMEOUT_S = 60


def docker(*args: str, check: bool = True, timeout: float = 600) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


class RealMoonraker:
    """One Moonraker container. The tests reach it from outside, as an untrusted host."""

    def __init__(self, container: str, url: str) -> None:
        self.container = container
        self.url = url

    def exec(self, *command: str, check: bool = True, timeout: float = 300) -> str:
        return docker("exec", self.container, *command, check=check, timeout=timeout)

    def local_get(self, path: str) -> Any:
        """GET from inside the container, which Moonraker trusts."""
        return json.loads(self.exec("curl", "-s", f"http://127.0.0.1:7125{path}"))["result"]

    @property
    def api_key(self) -> str:
        return self.local_get("/access/api_key")

    def agents(self) -> list[dict[str, Any]]:
        return self.local_get("/server/extensions/list")["agents"]

    def restart(self) -> None:
        docker("restart", "-t", "2", self.container)

    def wait_until_up(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"{self.url}/server/info", timeout=2)
                return
            except urllib.error.HTTPError:
                return  # 401: up, and not trusting us, as intended
            except OSError:
                time.sleep(0.5)
        logs = docker("logs", "--tail", "40", self.container, check=False)
        raise RuntimeError(f"Moonraker didn't start within {STARTUP_TIMEOUT_S}s:\n{logs}")

    def wait_for_no_agents(self, timeout: float = 15) -> None:
        """Moonraker allows one agent per name, so a test starts once the last one has gone."""
        deadline = time.monotonic() + timeout
        while self.agents():
            if time.monotonic() > deadline:
                raise RuntimeError(f"agents still connected: {self.agents()}")
            time.sleep(0.2)


@pytest.fixture(scope="session")
def moonraker_image() -> str:
    if shutil.which("docker") is None or docker("info", check=False, timeout=30) == "":
        if os.environ.get("CI"):
            pytest.fail("Docker isn't available, and CI must not skip these silently")
        pytest.skip("Docker isn't available")
    tag = f"printlog-bridge-it-moonraker:{MOONRAKER_REF.replace('/', '-')}"
    docker(
        "build",
        "-q",
        "--build-arg",
        f"MOONRAKER_REF={MOONRAKER_REF}",
        "-t",
        tag,
        str(HERE / "moonraker"),
    )
    return tag


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(image: str) -> RealMoonraker:
    name = f"printlog-it-{uuid.uuid4().hex[:8]}"
    # A fixed host port: with a random one, Docker picks a new port on restart.
    port = _free_port()
    docker(
        "run", "-d", "--name", name,
        "-p", f"127.0.0.1:{port}:7125",
        "-v", f"{REPO}:/src:ro",
        image,
    )  # fmt: skip
    moonraker = RealMoonraker(name, f"http://127.0.0.1:{port}")
    try:
        moonraker.wait_until_up()
    except BaseException:
        docker("rm", "-f", name, check=False)
        raise
    return moonraker


@pytest.fixture(scope="module")
def shared_moonraker(moonraker_image: str) -> Iterator[RealMoonraker]:
    moonraker = _start(moonraker_image)
    try:
        yield moonraker
    finally:
        docker("rm", "-f", moonraker.container, check=False)


@pytest.fixture
def moonraker(shared_moonraker: RealMoonraker) -> RealMoonraker:
    """The module's Moonraker, with no agent left connected by an earlier test."""
    shared_moonraker.wait_for_no_agents()
    return shared_moonraker


@pytest.fixture
def own_moonraker(moonraker_image: str) -> Iterator[RealMoonraker]:
    """A Moonraker for one test only, for tests that restart or break it."""
    moonraker = _start(moonraker_image)
    try:
        yield moonraker
    finally:
        docker("rm", "-f", moonraker.container, check=False)
