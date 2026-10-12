# printlog-bridge

An open-source bridge agent that watches Klipper/Moonraker printers and logs every print to [3D Print Log](https://www.3dprintlog.com) automatically: real outcome, duration, per-spool filament usage and a picture, with no manual entry and no printer config edits.

It runs in **local mode** on the printer's own host (Moonraker Unix socket) or in **remote mode** on any LAN host (Pi, Docker, NAS) over Moonraker's WebSocket, which covers locked-down printers such as the Snapmaker U1 and Creality K2.

> Status: early development. `run` logs each print when it starts and completes it when it ends, with its outcome, duration and filament used, adds the slicer's thumbnail and a webcam snapshot, and keeps what it can't send yet until 3D Print Log is reachable again. On a multi-tool printer the filament is split per tool. See the [issues](https://github.com/HoffmanEngineering/printlog-bridge/issues) and milestones for the roadmap.

## Requirements

Python 3.9 or newer (Raspberry Pi OS Bullseye and later) and a printer running Moonraker.

## Install on the printer (local mode)

On the printer's own host (MainsailOS, FluiddPi, or any Klipper install with Moonraker), as the user Klipper runs as:

```sh
cd ~ && git clone https://github.com/HoffmanEngineering/printlog-bridge.git
./printlog-bridge/install.sh
```

It asks for an API key and the printer's number in 3D Print Log (press Enter to fill them in later), then:

- makes a virtualenv in `~/printlog-bridge-env` and installs the bridge into it;
- stores the API key in `~/.local/share/printlog-bridge/api_key`, readable only by you (not in the config folder, which Moonraker serves to anyone who can open Mainsail or Fluidd);
- writes `~/printer_data/config/printlog-bridge.cfg`, next to `printer.cfg`, unless you already have one;
- installs and starts a `printlog-bridge` systemd service;
- adds `printlog-bridge` to `~/printer_data/moonraker.asvc`, so Mainsail and Fluidd can restart it;
- adds an `[update_manager printlog-bridge]` section to `moonraker.conf`, so updates show up in Mainsail's and Fluidd's update panels, and restarts Moonraker to load it.

Running it again is safe. Options: `--printer-data DIR` when Moonraker's data folder isn't `~/printer_data`, `--printer-id N`, and `--yes` to ask nothing; `install.sh --help` lists them all. If you leave the key for later, store it with `~/printlog-bridge-env/bin/printlog-bridge store-key`; set the printer in `printlog-bridge.cfg` (Mainsail and Fluidd can edit it). Then restart printlog-bridge from their services menu. The service doesn't restart itself while its config needs fixing.

`./printlog-bridge/uninstall.sh` removes the service and the virtualenv, and puts `moonraker.conf` and `moonraker.asvc` back exactly as they were. It keeps your config and the state directory (including prints not yet logged) for a reinstall; `--purge` removes those too.

## Try it

```sh
python3 -m venv ~/printlog-bridge-env
~/printlog-bridge-env/bin/pip install git+https://github.com/HoffmanEngineering/printlog-bridge
cp printlog-bridge.example.cfg ~/printer_data/config/printlog-bridge.cfg   # then edit it
~/printlog-bridge-env/bin/printlog-bridge doctor
```

`doctor` checks the config, the state directory, the API key, this computer's clock and each printer, and says what to fix. `printlog-bridge run` then watches every printer until stopped (Ctrl-C, or SIGTERM from systemd), and shows up in Moonraker's extensions list (Mainsail and Fluidd show it too).

`printlog-bridge status` shows what the running bridge is doing. For each printer it lists the connection, the printer's state, the last job event, the last heartbeat, how many jobs are waiting to reach 3D Print Log, and anything you need to fix:

```
printlog-bridge 0.1.0 status
Bridge: running since 2026-10-10 09:30 UTC (version 0.1.0, pid 812)
Printer voron (printer 42 in 3D Print Log)
  Connection: connected for 3h 12m (moonraker, local socket /home/pi/printer_data/comms/moonraker.sock)
  Printer state: ready
  Pairing: not paired; API key from /home/pi/.local/share/printlog-bridge/api_key
  Last event: job 00002A completed: print 1834 (14m ago)
  Heartbeat: 2m 10s ago
  Outbox: empty
  Warnings: none
```

It exits 0 while the bridge runs and 1 when it doesn't, so scripts can check it. Each printer shows up in 3D Print Log as a connection, with a heartbeat every 5 minutes; the app shows it as offline after 15 minutes without one.

When you need help, run `doctor` and attach the support bundle it writes (`printlog-bridge-support.zip` in the state directory). It holds the doctor report, your config, the bridge's status, its state and its recent log (`bridge.log`), with every API key, token and password removed.

Each print appears in 3D Print Log as Printing when the job starts, titled from its file name, with the slicer's estimates. When it ends, the bridge records the outcome, the print time and the filament Klipper fed:

| Moonraker ends the job as | The print becomes | Note added |
| --- | --- | --- |
| `completed` | Success | |
| `cancelled` | Cancelled | |
| `error` | Failed | "Klipper error", with Klipper's message when the bridge saw it |
| `klippy_shutdown` | Failed | "Klipper shutdown" |
| `klippy_disconnect` | Failed | "Lost connection to Klipper" |
| `interrupted`, `server_exit` | Failed | "Moonraker stopped during the print" |

On a printer with more than one tool (a toolchanger, an IDEX, a Snapmaker U1), each tool gets its own usage row, numbered from 0 the way Klipper numbers its extruders (`extruder` is tool 0, `extruder1` tool 1). The bridge watches which extruder is active while Klipper's filament count grows, and scales what it saw to Moonraker's total for the job, so the rows always add up to it. When the slicer listed a weight per filament (PrusaSlicer, OrcaSlicer, Snapmaker Orca), the print starts with an estimate on each tool's row, and a job that ran while the bridge was away is split in those proportions. Not covered yet: a multi-material unit on one extruder (ERCF, Happy Hare, AFC), where everything is counted on tool 0, and IDEX copy or mirror mode, where Klipper counts what two heads print once.

What you change in the app while it prints is kept: the title, your notes (a note is added after them), the spool on a usage row, and a status you already chose, such as Partial success. A print you delete stays deleted.

When 3D Print Log can't be reached (the internet is down, or the API is busy), nothing is lost. The bridge writes each job event to an outbox in its state directory (`outbox.sqlite3`) before it sends anything, and sends it from there, oldest first and each print's events in order. It retries with a wait that doubles from 2 seconds to a minute, or as long as the API asks, so a print shows up within a minute or two of the connection coming back, even if the bridge was restarted in between. A power cut in the middle of a send doesn't lose the event or log it twice. An event that still hasn't got through after 30 days is dropped, with a warning in the log. `status` shows how many jobs are waiting and why the last try failed. A full or read-only disk doesn't stop the bridge either: it keeps the job events it couldn't write in memory, warns in `status`, and writes them once it can.

The bridge reads Moonraker's job history, so `[history]` must be in moonraker.conf (most installs have it; `run` logs how to add it when it's missing). It remembers what it has logged in its state directory, and on every start or reconnect it catches up on jobs that started or ended while it was away. On its very first run it picks up a print already running, but not older history.

Until pairing lands, the bridge signs in with a personal API key from [3dprintlog.com/api-keys](https://www.3dprintlog.com/api-keys). Store it with `printlog-bridge store-key`, which keeps it in `api_key` in the state directory, readable only by you. The bridge looks for the key in the `PRINTLOG_API_KEY` environment variable, then that file, then `api_key` in `[bridge]`. A key in `[bridge]` still works, but in local mode the config sits in Klipper's config folder, which Moonraker serves to anyone who can open Mainsail or Fluidd: `doctor` warns about it, and `store-key` moves it out of the config for you.

## Privacy and security

The bridge sends 3D Print Log the print's file name (without its folders), times, duration, outcome, filament used, the slicer's estimates, its thumbnail and, unless you turn snapshots off, one webcam frame when it ends. It never reads or uploads G-code. It connects only to Moonraker, the webcam Moonraker names, and 3D Print Log over HTTPS, and never listens on a port. Keys, passwords and tokens never reach its log, status or support bundle. [docs/privacy.md](docs/privacy.md) lists every call and field, and the test that holds each guarantee.

## Config

One `printlog-bridge.cfg`, looked up in this order: `--config`, `$PRINTLOG_BRIDGE_CONFIG`, `~/printer_data/config/`, `~/.config/printlog-bridge/`. See [printlog-bridge.example.cfg](printlog-bridge.example.cfg).

Each printer is a `[printer <name>]` section, so one bridge can watch several. Set `printer_id` in each to the printer's number in 3D Print Log, the end of its page's address (`https://www.3dprintlog.com/printers/42`); pairing will set it for you.

In remote mode, if Moonraker has `[authorization]` enabled and the bridge's host isn't in `trusted_clients`, set `moonraker_api_key` in the printer's section to Moonraker's API key. The local socket is always trusted, so local mode never needs one. Moonraker accepts one bridge per printer; a second one waits until the first stops.

Each print gets the largest thumbnail the slicer embedded in its file (OrcaSlicer, PrusaSlicer, Cura and SuperSlicer all embed one) as its picture when it starts. When it ends, the bridge also adds one frame from the printer's first webcam (its `snapshot_url` in Mainsail or Fluidd); for a failed print that frame becomes the picture shown. Set `snapshots = false` in the printer's section to never ask the webcam for anything. Pictures wait in the outbox with the print's other events (the image files in `images/` in the state directory), and never hold up logging the print: one that takes longer than 10 seconds, is over 10 MB or that 3D Print Log refuses (a print holds 5 pictures on the free plan) is skipped. A Moonraker or webcam address with a password or token in it is shown with them hidden, in the log, `status` and `doctor`.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

CI runs both on Python 3.9 and 3.12. It also runs `shellcheck` on the shell scripts, and `tests/install/smoke.sh`, which runs `install.sh` and `uninstall.sh` in a throwaway home inside a `python:3.9-slim` container, against a fake `systemctl`, and checks that Moonraker's files end up byte for byte as they started. The file edits themselves are in `scripts/install_helper.py`, tested by `tests/test_install_helper.py`.

`pytest -m integration` runs the bridge against a real Moonraker, built from source in Docker (set `MOONRAKER_REF` to pick a tag or commit; the default is `v0.9.3`). The first run builds the image, which takes a minute or two. CI runs it against the last release and a pinned recent commit. To support another kind of printer, see [docs/adapters.md](docs/adapters.md).

## License

[MIT](LICENSE)
