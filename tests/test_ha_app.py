"""The Home Assistant app (home-assistant/, repository.yaml) agrees with the code: its version is
the bridge's, its image is the one docker.yml publishes for its arches, and its options are the
ones cli/addon.py reads. CI also lints config.yaml against the Supervisor's schema (ci.yml)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from printlog_bridge import __version__
from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.cli.addon import ADAPTER_FIELDS, PRINTER_FIELDS, REQUIRED
from printlog_bridge.cli.main import LOG_FILE
from printlog_bridge.core.support import BUNDLE_NAME
from printlog_bridge.core.trace import TRACE_DIR

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "home-assistant"
CONFIG = yaml.safe_load((APP / "config.yaml").read_text(encoding="utf-8"))
TRANSLATIONS = yaml.safe_load((APP / "translations" / "en.yaml").read_text(encoding="utf-8"))
WORKFLOW = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
BRIDGE_OPTIONS = {"device_name", "api_key", "api_url", "trace", "printers"}
"""The top-level options cli/addon.py reads."""
ARCH_PLATFORMS = {"amd64": "linux/amd64", "aarch64": "linux/arm64"}


def test_the_repository_is_named():
    repository = yaml.safe_load((ROOT / "repository.yaml").read_text(encoding="utf-8"))
    assert repository["name"]
    assert repository["url"] == "https://github.com/HoffmanEngineering/printlog-bridge"


def test_the_app_version_is_the_bridges():
    # The Supervisor pulls image:version, and a v* tag publishes that tag: bump both together.
    assert CONFIG["version"] == __version__


def test_the_image_is_the_one_docker_yml_publishes_for_every_arch():
    image = CONFIG["image"]
    assert ":" not in image.split("/")[-1], "the Supervisor adds the tag (the version)"
    assert f"ADDON_IMAGE: {image}\n" in WORKFLOW
    platforms = re.search(r"ADDON_PLATFORMS: (\S+)\n", WORKFLOW).group(1).split(",")
    assert sorted(ARCH_PLATFORMS[a] for a in CONFIG["arch"]) == sorted(platforms)
    assert "type=semver,pattern={{version}}" in WORKFLOW
    assert "target: addon" in WORKFLOW
    # Smoke-tested in the smoke job, which publish needs.
    assert 'tests/docker/addon_smoke.sh printlog-bridge-addon:smoke "${{ matrix.platform }}"' in (
        WORKFLOW
    )


def test_the_app_is_on_the_hosts_network_and_has_its_config_folder():
    assert CONFIG["host_network"] is True
    assert "app_config:rw" in CONFIG["map"]
    # The image's HEALTHCHECK is the watchdog; the linter refuses the old `watchdog` URL.
    assert "watchdog" not in CONFIG
    # The image has no init of its own: keep Docker's (the default, `init: true`).
    assert "init" not in CONFIG


def test_the_options_are_the_ones_the_start_up_reads():
    schema = CONFIG["schema"]
    assert set(schema) == BRIDGE_OPTIONS
    [printer] = schema["printers"]
    fields = set(PRINTER_FIELDS) | {f for fs in ADAPTER_FIELDS.values() for f in fs}
    assert set(printer) == fields
    adapters = re.fullmatch(r"list\((.*)\)", printer["adapter"]).group(1).split("|")
    assert set(adapters) == set(ADAPTER_FIELDS) == set(ADAPTERS)
    assert set(REQUIRED) <= set(ADAPTER_FIELDS)


def test_secrets_are_password_fields():
    schema = CONFIG["schema"]
    [printer] = schema["printers"]
    assert schema["api_key"] == "password?"
    for secret in ("access_code", "check_code", "password", "api_key"):
        assert printer[secret] == "password?", secret


def test_every_required_option_has_a_default():
    schema, options = CONFIG["schema"], CONFIG["options"]
    for key, kind in schema.items():
        if isinstance(kind, str) and not kind.endswith("?"):
            assert key in options, key
    assert options["printers"] == []


def test_every_option_has_a_name_and_description():
    configuration = TRANSLATIONS["configuration"]
    assert set(configuration) == set(CONFIG["schema"])
    for key, text in configuration.items():
        assert text["name"] and text["description"], key
    [printer] = CONFIG["schema"]["printers"]
    fields = configuration["printers"]["fields"]
    assert set(fields) == set(printer)
    for key, text in fields.items():
        assert text["name"] and text["description"], key


def test_backups_leave_out_the_log_traces_and_support_bundle():
    excluded = CONFIG["backup_exclude"]
    assert f"state/{TRACE_DIR}/*" in excluded
    assert f"state/{BUNDLE_NAME}" in excluded
    assert any(re.fullmatch(p.replace(".", r"\.").replace("*", ".*"), f"state/{LOG_FILE}.1")
               for p in excluded)  # fmt: skip


def test_the_supervisor_finds_only_this_app():
    # It looks for config.yaml, config.yml and config.json at any depth, skipping hidden folders
    # and rootfs: any other such file would be read as an app.
    found = [
        p.relative_to(ROOT).as_posix()
        for name in ("config.yaml", "config.yml", "config.json")
        for p in ROOT.rglob(name)
        if not any(part.startswith(".") or part == "rootfs" for part in p.relative_to(ROOT).parts)
        and not any(part in ("node_modules", "build", "dist") for part in p.parts)
    ]
    assert found == ["home-assistant/config.yaml"]


def test_the_app_has_its_store_pages():
    for name in ("README.md", "DOCS.md", "CHANGELOG.md"):
        assert (APP / name).is_file(), name
    assert f"## {__version__}" in (APP / "CHANGELOG.md").read_text(encoding="utf-8")


def test_the_status_page_is_the_apps_ingress_panel():
    from printlog_bridge.cli.addon import INGRESS_PORT

    assert CONFIG["ingress"] is True
    assert CONFIG["ingress_port"] == INGRESS_PORT
    assert CONFIG["panel_icon"].startswith("mdi:")
    assert CONFIG["panel_title"]
