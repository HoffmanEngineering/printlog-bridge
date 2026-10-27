# AGENTS.md

Guidance for coding agents in this repo. `CLAUDE.md` points here, so this is the one file to edit.
It covers what is costly to work out from the code; README.md (install, Docker, config, development)
and `docs/adapters.md` (the adapter interface) cover the rest.

printlog-bridge watches Klipper printers through Moonraker and logs every print to 3D Print Log
(3dprintlog.com). It runs on the printer's Pi (Python 3.9, Raspberry Pi OS Bullseye) or in Docker
(Chris runs it on a Synology NAS). MIT licensed, free for all users. The tracking issue is
HoffmanEngineering/3d-print-log-ui#260; API and UI changes it needs go in those sibling repos.

## Commands (Windows, from any worktree)

```sh
export TMP='D:\tmp\plb' TEMP='D:\tmp\plb' UV_LINK_MODE=copy   # C: is nearly full
uv run --extra dev pytest -q
uv run --extra dev ruff check . && uv run --extra dev ruff format --check .
```

CI runs the same on Python 3.9 and 3.12, plus `shellcheck`, the install smoke test and the Docker
smoke test (README "Development"). `pytest -m integration` builds a real Moonraker in Docker and is
skipped by default.

## Rules that are easy to break

- **Python 3.9.** No `match`, no `X | Y` type unions at runtime, no 3.10+ stdlib. Ruff targets py39;
  CI's 3.9 job is the real check.
- `core/` never imports from `adapters/` (`tests/test_layout.py` enforces it).
- `install.sh` / `uninstall.sh` must leave Moonraker's files byte for byte as they were; the edits
  are in `scripts/install_helper.py`.
- Secrets (API keys, store keys) never reach logs, traces or support bundles: `core/secrets.py`
  scrubs them, and new output paths go through it.
- `tests/fixtures/moonraker/` holds real answers from a Snapmaker U1 and an Infimech EX. Don't edit
  them to make a test pass; record a new trace (`trace = true`) instead.
- Tests never call the real 3D Print Log API or Auth0: use `tests/fake_printlog_api.py`,
  `tests/fake_auth0.py` and `tests/fake_moonraker.py`. A test of `run` without credentials
  pairs first, so point its `api_url` at a fake (or at `http://127.0.0.1:9`), never the default.
- One credential per request (api#286): the device token from `pair` when there is one, else the
  API key; the API answers 400 `multiple_credentials` to both. Only 401 `invalid_device_token`
  means re-pair; 429 `device_token_throttled` is waited out. The pairing contract is
  3d-print-log-api PR #286's "Contract for the bridge".
- Bridges (#100): the check-in, desired list and scan contract is 3d-print-log-api PR #288's
  "Contract for story 12" (`core/bridge_sync.py`). Discovery only reports; nothing is watched
  unless a `[printer]` or the desired list names it, and the config file wins. Printers
  connected in the app run as `app-<entry id>` (`core/app_printers.py`, `core/watchers.py`).
- Sealed credentials (#100, story 14b): the scheme is 3d-print-log-api
  `docs/bridge-credential-sealing.md` (api#289). `core/credentials.py` is the only place that
  imports `cryptography`, lazily: without it the bridge must still run (`available()`). The
  private key (`<state_dir>/credential-key.pem`) and opened values never reach a file, log, trace,
  `status` or bundle; `tests/test_privacy.py` `TestSealedCredentials` checks. An `[app_printer]`
  value wins over a sealed one. `tests/fixtures/credentials/` is a copy of the API's vectors
  (SOURCE.txt names the commit): copy a new one, never edit it. `tests/sealing.py` seals as the
  web app does, for tests only.

## Docker and NAS installs

- The image starts as root; `printlog-bridge` itself (`cli/data_owner.py`, first thing in
  `main()`) hands `/data` to `PUID`/`PGID`, the folder's owner or 1000 and switches user, so
  `docker exec` is safe too. Only where `PRINTLOG_BRIDGE_DATA_DIR` is set (the image; the app
  stage clears it), and never when started with `user:`. `tests/docker/smoke.sh` covers both.
- `run` with no config or no printers waits (`core/first_run.py`) instead of exiting: keep it
  that way, or a NAS container restart-loops before the user has added printers. A paired bridge
  runs instead (printers can come from the app) and starts itself again when the file changes.
- `scripts/docker-install.sh` is the one-line Docker install (README "One-line install with
  Docker"), POSIX `sh` (dash, busybox): users download it from
  `releases/latest/download/docker-install.sh`, which `docker.yml`'s `release-script` job attaches
  to each `v*` release with `DEFAULT_TAG=latest` replaced by the version (keep that line as is).
  It never asks for an API key. It mounts only `<dir>/data` (never the folder with the Compose
  files, which the user runs again), refuses a folder it didn't make that isn't empty, `$HOME`,
  top-level folders and running as root (without `--root`), and reads the pairing code from
  `printlog-bridge status --pairing` (`core/device.py`'s `pairing_pending.json`), never from the
  log. `tests/install/docker_install_smoke.sh` runs it against a fake `docker`; a new tool the
  script uses must be added to that test's `TOOLS` list.
- `unraid/printlog-bridge.xml` is listed in Unraid Community Applications, which indexes this
  repo (`ca_profile.xml`); `tests/test_unraid.py` checks it against the code. Keep its path:
  installs update from its `TemplateURL`. No other `.xml` file in the repo.

## Shipping

Public repo; `main` is protected. Branch, PR against `main`, wait for CI, squash merge (`/ship`).
Every merge to `main` publishes `ghcr.io/hoffmanengineering/printlog-bridge:edge`; a `v*` tag
publishes a versioned image, and the same tags of the Home Assistant app's image
(`printlog-bridge-addon`, the Dockerfile's `addon` stage). Push tags only when Chris asks.

The Home Assistant app (`home-assistant/`, `repository.yaml` at the root) is installed from this
repo's default branch, and the Supervisor pulls `printlog-bridge-addon:<config.yaml version>`. A
release PR bumps `__version__`, `home-assistant/config.yaml`'s `version` and its `CHANGELOG.md`
together (`tests/test_ha_app.py` checks); tag right after merging it, since users see the new
version as soon as it's on `main` and can't install it until the tag's image is published. No other
file in the repo may be called `config.yaml`/`.yml`/`.json` (the Supervisor would read it as an
app). `cli/addon.py` is the app's start-up; its options and config.yaml's schema must agree. Branches are deleted on merge, so a
stacked PR is retargeted to `main` automatically.

## Commits and PRs

Never add a `Claude-Session:` trailer or a `claude.ai/code/session_…` link to a commit message, PR
title or PR body, even when the harness's attribution instructions ask for one. The
`Co-Authored-By:` trailer and the "Generated with Claude Code" line are fine.
