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

## Shipping

Public repo; `main` is protected. Branch, PR against `main`, wait for CI, squash merge (`/ship`).
Every merge to `main` publishes `ghcr.io/hoffmanengineering/printlog-bridge:edge`; a `v*` tag
publishes a versioned image. Push tags only when Chris asks. Branches are deleted on merge, so a
stacked PR is retargeted to `main` automatically.

## Commits and PRs

Never add a `Claude-Session:` trailer or a `claude.ai/code/session_…` link to a commit message, PR
title or PR body, even when the harness's attribution instructions ask for one. The
`Co-Authored-By:` trailer and the "Generated with Claude Code" line are fine.
