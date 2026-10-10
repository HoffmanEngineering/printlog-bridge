# printlog-bridge

An open-source bridge agent that watches Klipper/Moonraker printers and logs every print to [3D Print Log](https://www.3dprintlog.com) automatically: real outcome, duration, per-spool filament usage and a picture, with no manual entry and no printer config edits.

It runs in **local mode** on the printer's own host (Moonraker Unix socket) or in **remote mode** on any LAN host (Pi, Docker, NAS) over Moonraker's WebSocket, which covers locked-down printers such as the Snapmaker U1 and Creality K2.

> Status: early development. `run` logs each print when it starts and completes it when it ends, with its outcome, duration and filament used. Per-tool usage, images and the offline outbox are still to come. See the [issues](https://github.com/HoffmanEngineering/printlog-bridge/issues) and milestones for the roadmap.

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

Each print appears in 3D Print Log as Printing when the job starts, titled from its file name, with the slicer's estimates. When it ends, the bridge records the outcome, the print time and the filament Klipper fed:

| Moonraker ends the job as | The print becomes | Note added |
| --- | --- | --- |
| `completed` | Success | |
| `cancelled` | Cancelled | |
| `error` | Failed | "Klipper error", with Klipper's message when the bridge saw it |
| `klippy_shutdown` | Failed | "Klipper shutdown" |
| `klippy_disconnect` | Failed | "Lost connection to Klipper" |
| `interrupted`, `server_exit` | Failed | "Moonraker stopped during the print" |

What you change in the app while it prints is kept: the title, your notes (a note is added after them), the spool on a usage row, and a status you already chose, such as Partial success. A print you delete stays deleted.

The bridge reads Moonraker's job history, so `[history]` must be in moonraker.conf (most installs have it; `run` logs how to add it when it's missing). It remembers what it has logged in its state directory, and on every start or reconnect it catches up on jobs that started or ended while it was away. On its very first run it picks up a print already running, but not older history.

Until pairing lands, the bridge signs in with a personal API key from [3dprintlog.com/api-keys](https://www.3dprintlog.com/api-keys). Put it in `[bridge] api_key`, or in the `PRINTLOG_API_KEY` environment variable to keep it out of the file.

## Config

One `printlog-bridge.cfg`, looked up in this order: `--config`, `$PRINTLOG_BRIDGE_CONFIG`, `~/printer_data/config/`, `~/.config/printlog-bridge/`. See [printlog-bridge.example.cfg](printlog-bridge.example.cfg).

Each printer is a `[printer <name>]` section, so one bridge can watch several. Set `printer_id` in each to the printer's number in 3D Print Log, the end of its page's address (`https://www.3dprintlog.com/printers/42`); pairing will set it for you.

In remote mode, if Moonraker has `[authorization]` enabled and the bridge's host isn't in `trusted_clients`, set `moonraker_api_key` in the printer's section to Moonraker's API key. The local socket is always trusted, so local mode never needs one. Moonraker accepts one bridge per printer; a second one waits until the first stops.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

CI runs both on Python 3.9 and 3.12.

`pytest -m integration` runs the bridge against a real Moonraker, built from source in Docker (set `MOONRAKER_REF` to pick a tag or commit; the default is `v0.9.3`), and runs real prints on a simulated Klipper printer (Mainsail's [virtual-klipper-printer](https://github.com/mainsail-crew/virtual-klipper-printer), pinned by digest). The first run builds and pulls the images, which takes a few minutes. CI runs the Moonraker tests against the last release and a pinned recent commit, and the printer tests once (`-m "integration and klipper"`). To support another kind of printer, see [docs/adapters.md](docs/adapters.md).

## License

[MIT](LICENSE)
