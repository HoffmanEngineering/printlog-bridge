# printlog-bridge

An open-source bridge agent that watches Klipper/Moonraker printers and logs every print to [3D Print Log](https://www.3dprintlog.com) automatically: real outcome, duration, per-spool filament usage and a picture, with no manual entry and no printer config edits.

It runs in **local mode** on the printer's own host (Moonraker Unix socket) or in **remote mode** on any LAN host (Pi, Docker, NAS) over Moonraker's WebSocket, which covers locked-down printers such as the Snapmaker U1 and Creality K2.

> Status: early development. `doctor` works, and `run` connects to each printer and stays connected, but doesn't log prints yet (#4). See the [issues](https://github.com/HoffmanEngineering/printlog-bridge/issues) and milestones for the roadmap.

## Requirements

Python 3.9 or newer (Raspberry Pi OS Bullseye and later) and a printer running Moonraker.

## Try it

```sh
python3 -m venv ~/printlog-bridge-env
~/printlog-bridge-env/bin/pip install git+https://github.com/HoffmanEngineering/printlog-bridge
cp printlog-bridge.example.cfg ~/printer_data/config/printlog-bridge.cfg   # then edit it
~/printlog-bridge-env/bin/printlog-bridge doctor
```

`doctor` checks the config, the state directory, the API key and each printer, and says what to fix. `printlog-bridge run` then watches every printer until stopped, and shows up in Moonraker's extensions list (Mainsail and Fluidd show it too).

Until pairing lands, the bridge signs in with a personal API key from [3dprintlog.com/api-keys](https://www.3dprintlog.com/api-keys). Put it in `[bridge] api_key`, or in the `PRINTLOG_API_KEY` environment variable to keep it out of the file.

## Config

One `printlog-bridge.cfg`, looked up in this order: `--config`, `$PRINTLOG_BRIDGE_CONFIG`, `~/printer_data/config/`, `~/.config/printlog-bridge/`. See [printlog-bridge.example.cfg](printlog-bridge.example.cfg).

Each printer is a `[printer <name>]` section, so one bridge can watch several.

In remote mode, if Moonraker has `[authorization]` enabled and the bridge's host isn't in `trusted_clients`, set `moonraker_api_key` in the printer's section to Moonraker's API key. The local socket is always trusted, so local mode never needs one. Moonraker accepts one bridge per printer; a second one waits until the first stops.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

CI runs both on Python 3.9 and 3.12.

`pytest -m integration` runs the bridge against a real Moonraker, built from source in Docker (set `MOONRAKER_REF` to pick a tag or commit; the default is `v0.9.3`). The first run builds the image, which takes a minute or two. CI runs it against the last release and a pinned recent commit. To support another kind of printer, see [docs/adapters.md](docs/adapters.md).

## License

[MIT](LICENSE)
