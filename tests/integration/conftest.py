"""Fixtures that run a real Moonraker, and a simulated Klipper printer, in Docker.

Run with `pytest -m integration`. Needs Docker; the tests skip when it isn't available.
MOONRAKER_REF picks the Moonraker tag or commit to build (default: v0.9.3). The simulated
printer tests are also marked `klipper`, so CI runs them once rather than per Moonraker version.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
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


def require_docker() -> None:
    if shutil.which("docker") is None or docker("info", check=False, timeout=30) == "":
        if os.environ.get("CI"):
            pytest.fail("Docker isn't available, and CI must not skip these silently")
        pytest.skip("Docker isn't available")


@pytest.fixture(scope="session")
def moonraker_image() -> str:
    require_docker()
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


def _run_container(image: str, *options: str) -> tuple[str, str]:
    """Starts `image` with Moonraker's port on a free local one. Returns (name, url)."""
    name = f"printlog-it-{uuid.uuid4().hex[:8]}"
    # A fixed host port: with a random one, Docker picks a new port on restart.
    port = _free_port()
    docker("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:7125", *options, image)
    return name, f"http://127.0.0.1:{port}"


def _start(image: str) -> RealMoonraker:
    name, url = _run_container(image, "-v", f"{REPO}:/src:ro")
    moonraker = RealMoonraker(name, url)
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


# -- a simulated printer: Klipper on a simulated board, with Moonraker ---------------------------

KLIPPY_READY_TIMEOUT_S = 180


class SimulatedPrinter:
    """Mainsail's virtual-klipper-printer. Moonraker trusts every client, as many printers do."""

    def __init__(self, container: str, url: str) -> None:
        self.container = container
        self.url = url

    def request(self, method: str, path: str, **params: Any) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        request = urllib.request.Request(f"{self.url}{path}{query}", method=method)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())["result"]

    def upload(self, name: str, gcode: str) -> None:
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            f"{gcode}\r\n--{boundary}--\r\n"
        ).encode()
        request = urllib.request.Request(
            f"{self.url}/server/files/upload",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        urllib.request.urlopen(request, timeout=30).close()
        # Moonraker reads the slicer's estimates from the file after the upload returns.
        self.wait(lambda: self.metadata(name).get("estimated_time"), "metadata", 30)

    def metadata(self, name: str) -> dict[str, Any]:
        try:
            return self.request("GET", "/server/files/metadata", filename=name)
        except urllib.error.HTTPError:
            return {}

    def start_print(self, name: str) -> None:
        self.request("POST", "/printer/print/start", filename=name)

    def cancel(self) -> None:
        self.request("POST", "/printer/print/cancel")

    def emergency_stop(self) -> None:
        self.request("POST", "/printer/emergency_stop")

    def print_state(self) -> str:
        status = self.request("GET", "/printer/objects/query", print_stats="state")["status"]
        return status["print_stats"]["state"]

    def klippy_state(self) -> str:
        try:
            return self.request("GET", "/server/info")["klippy_state"]
        except OSError:
            return "down"

    def jobs(self) -> list[dict[str, Any]]:
        return self.request("GET", "/server/history/list", limit=50, order="asc")["jobs"]

    def agents(self) -> list[dict[str, Any]]:
        return self.request("GET", "/server/extensions/list")["agents"]

    def klippy_log(self, lines: int = 60) -> str:
        """The lines of klippy.log that explain a print going wrong, without the stats."""
        pattern = "pause|resum|print|sdcard|error|shutdown|timer|exception|gcode|echo|//|!!"
        log = "~/printer_data/logs/klippy.log"
        script = f"grep -iE '{pattern}' {log} | grep -v '^Stats' | tail -{lines}"
        return docker("exec", self.container, "sh", "-c", script, check=False)

    def wait(self, condition, what: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                logs = docker("logs", "--tail", "60", self.container, check=False)
                raise RuntimeError(f"{what}: not within {timeout}s\n{logs}")
            time.sleep(0.5)

    def wait_until_ready(self) -> None:
        self.wait(lambda: self.klippy_state() == "ready", "Klipper ready", KLIPPY_READY_TIMEOUT_S)

    def wait_until_idle(self) -> None:
        self.wait(lambda: self.print_state() != "printing", "printer idle", 120)


@pytest.fixture(scope="session")
def klipper_image() -> str:
    require_docker()
    tag = "printlog-bridge-it-klipper:latest"
    docker("build", "-q", "-t", tag, str(HERE / "klipper"), timeout=900)
    return tag


def _start_printer(image: str) -> SimulatedPrinter:
    # Its comms folder holds Unix sockets, which a tmpfs keeps off the image's overlay.
    name, url = _run_container(image, "--tmpfs", "/home/printer/printer_data/comms")
    printer = SimulatedPrinter(name, url)
    try:
        printer.wait_until_ready()
    except BaseException:
        docker("rm", "-f", name, check=False)
        raise
    return printer


@pytest.fixture(scope="module")
def shared_printer(klipper_image: str) -> Iterator[SimulatedPrinter]:
    printer = _start_printer(klipper_image)
    try:
        yield printer
    finally:
        docker("rm", "-f", printer.container, check=False)


@pytest.fixture
def printer(shared_printer: SimulatedPrinter) -> SimulatedPrinter:
    """The module's printer, idle, with no bridge left connected by an earlier test."""
    shared_printer.wait_until_idle()
    shared_printer.wait(lambda: not shared_printer.agents(), "no agents", 15)
    return shared_printer


@pytest.fixture
def own_printer(klipper_image: str) -> Iterator[SimulatedPrinter]:
    """A printer for one test only, for tests that shut Klipper down."""
    printer = _start_printer(klipper_image)
    try:
        yield printer
    finally:
        docker("rm", "-f", printer.container, check=False)
