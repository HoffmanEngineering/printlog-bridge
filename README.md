# printlog-bridge

An open-source bridge agent that watches Klipper/Moonraker printers and logs every print to [3D Print Log](https://www.3dprintlog.com) automatically: real outcome, duration, per-spool filament usage and a picture, with no manual entry and no printer config edits.

It runs in **local mode** on the printer's own host (Moonraker Unix socket) or in **remote mode** on any LAN host (Pi, Docker, NAS) over Moonraker's WebSocket, which covers locked-down printers such as the Snapmaker U1 and Creality K2.

> Status: early development. The skeleton, config and `doctor` work; watching prints does not yet. See the [issues](https://github.com/HoffmanEngineering/printlog-bridge/issues) and milestones for the roadmap.

## Requirements

Python 3.9 or newer (Raspberry Pi OS Bullseye and later) and a printer running Moonraker.

## Try it

```sh
python3 -m venv ~/printlog-bridge-env
~/printlog-bridge-env/bin/pip install git+https://github.com/HoffmanEngineering/printlog-bridge
cp printlog-bridge.example.cfg ~/printer_data/config/printlog-bridge.cfg   # then edit it
~/printlog-bridge-env/bin/printlog-bridge doctor
```

`doctor` checks the config, the state directory, the API key and each printer, and says what to fix.

Until pairing lands, the bridge signs in with a personal API key from [3dprintlog.com/api-keys](https://www.3dprintlog.com/api-keys). Put it in `[bridge] api_key`, or in the `PRINTLOG_API_KEY` environment variable to keep it out of the file.

## Config

One `printlog-bridge.cfg`, looked up in this order: `--config`, `$PRINTLOG_BRIDGE_CONFIG`, `~/printer_data/config/`, `~/.config/printlog-bridge/`. See [printlog-bridge.example.cfg](printlog-bridge.example.cfg).

Each printer is a `[printer <name>]` section, so one bridge can watch several.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

CI runs both on Python 3.9 and 3.12. To support another kind of printer, see [docs/adapters.md](docs/adapters.md).

## License

[MIT](LICENSE)
