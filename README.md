# printlog-bridge

An open-source bridge agent that watches Klipper/Moonraker printers and logs every print to [3D Print Log](https://www.3dprintlog.com) automatically: real outcome, duration, per-spool filament usage and a picture, with no manual entry and no printer config edits.

It runs in **local mode** on the printer's own host (Moonraker Unix socket) or in **remote mode** on any LAN host (Pi, Docker, NAS) over Moonraker's WebSocket, which covers locked-down printers such as the Snapmaker U1.

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
- adds an `[update_manager printlog-bridge]` section to `moonraker.conf`, so updates show up in Mainsail's and Fluidd's update panels, and restarts Moonraker to load it;
- copies the `PRINTLOG_SET_SPOOL` macros to `~/printer_data/config/printlog.cfg` and adds `[include printlog.cfg]` to `printer.cfg` (above its SAVE_CONFIG block), unless you pass `--no-macros`. Restart Klipper when it isn't printing to load them; the installer doesn't, since that would end a print.

Running it again is safe. Options: `--printer-data DIR` when Moonraker's data folder isn't `~/printer_data`, `--printer-id N`, and `--yes` to ask nothing; `install.sh --help` lists them all. If you leave the key for later, store it with `~/printlog-bridge-env/bin/printlog-bridge store-key`, as yourself rather than with `sudo` (it refuses to run as root); set the printer in `printlog-bridge.cfg` (Mainsail and Fluidd can edit it). Then restart printlog-bridge from their services menu. The service doesn't restart itself while its config needs fixing.

`./printlog-bridge/uninstall.sh` removes the service and the virtualenv, puts `moonraker.conf`, `moonraker.asvc` and `printer.cfg` back exactly as they were, and removes `printlog.cfg` unless you changed it. It keeps your config and the state directory (including prints not yet logged) for a reinstall; `--purge` removes those too.

## Run it in Docker (remote mode)

One container watches every printer in its config over the network, through each printer's Moonraker WebSocket. That suits a NAS, a home server or a Pi that isn't a printer. The image is `ghcr.io/hoffmanengineering/printlog-bridge`, for `linux/amd64`, `linux/arm64` and `linux/arm/v7` (32-bit Raspberry Pi OS):

| Tag | What it is |
| --- | --- |
| `latest`, `1.2`, `1.2.3` | releases |
| `edge` | the newest commit on `main` |

There's no release yet, so only `edge` exists for now; the examples below use it. Switch to `latest` once there is one.

Everything the bridge keeps is in one folder, mounted at `/data`:

| Path | What it is |
| --- | --- |
| `/data/printlog-bridge.cfg` | the config, which you write |
| `/data/state/api_key` | the API key, if you store it with `store-key` |
| `/data/state/outbox.sqlite3`, `images/` | job events and pictures not sent yet |
| `/data/state/printers/`, `status.json` | what has been logged, and what `status` shows |
| `/data/state/bridge.log` | the log (`docker logs` shows it too) |
| `/data/state/printlog-bridge-support.zip` | the support bundle `doctor` writes |

The image sets `PRINTLOG_BRIDGE_CONFIG=/data/printlog-bridge.cfg` and `PRINTLOG_BRIDGE_STATE_DIR=/data/state`; a `state_dir` in the config's `[bridge]` still wins, so leave it out.

The container runs as a normal user, never root: `1000:1000` unless you give it another one. It opens no ports.

### On a Synology NAS (Container Manager)

1. **Make the folder.** In File Station, make `docker/printlog-bridge` (on a NAS with one volume, that's `/volume1/docker/printlog-bridge`).
2. **Write the config** as `printlog-bridge.cfg` in that folder, with a `[printer <name>]` section per printer:
   ```ini
   [printer voron]
   printer_id = 41
   url = http://192.168.1.50:7125

   [printer u1]
   printer_id = 42
   url = http://192.168.1.51:7125

   [printer infimech]
   printer_id = 43
   url = http://192.168.1.52:7125
   ```
   Use the printers' IP addresses (give each a fixed one in your router): names like `voron.local` usually don't resolve inside a container. Moonraker sees the bridge coming from the NAS's address, so if a printer has `[authorization]` on, add the NAS to its `trusted_clients`, or set `moonraker_api_key` in that printer's section.
3. **Pick the user.** The container should run as the DSM user that owns the folder. Over SSH, `id <your DSM user>` shows its numbers, such as `uid=1026 ... gid=100(users)`; put them in `user:` below. (Without `user:`, the container runs as `1000:1000` and needs the folder to be writable by that uid.)
4. **Create the project.** Container Manager > Project > Create, with the folder above as its path and [`docker-compose.yml`](docker-compose.yml) as its file:
   ```yaml
   services:
     printlog-bridge:
       image: ghcr.io/hoffmanengineering/printlog-bridge:edge
       container_name: printlog-bridge
       restart: unless-stopped
       user: "1026:100"
       environment:
         TZ: Europe/Berlin
       volumes:
         - /volume1/docker/printlog-bridge:/data
   ```
   `restart: unless-stopped` brings it back after a crash and after the NAS restarts. `TZ` (a [tz database name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)) sets the times in its log; `status` and 3D Print Log use UTC either way.
5. **Give it the API key** from [3dprintlog.com/api-keys](https://www.3dprintlog.com/api-keys), one of two ways:
   - Store it in the folder, where only that user can read it. Over SSH:
     ```sh
     sudo docker run --rm -it --user 1026:100 -v /volume1/docker/printlog-bridge:/data \
       ghcr.io/hoffmanengineering/printlog-bridge:edge store-key
     ```
   - Or add `PRINTLOG_API_KEY: <key>` under `environment:`. That's simpler, but anyone who can manage containers on the NAS can read it in the container's settings.
6. **Check it.** In Container Manager the container shows as healthy once the bridge runs (the health check is `printlog-bridge status`). Over SSH:
   ```sh
   sudo docker exec printlog-bridge printlog-bridge doctor   # checks the config, the key and every printer
   sudo docker exec printlog-bridge printlog-bridge status   # what each printer is doing
   ```
   `doctor` leaves the support bundle at `/volume1/docker/printlog-bridge/state/printlog-bridge-support.zip`, which you can download from File Station.

If the config needs fixing (no key, a printer without `printer_id`), the bridge says what to fix in the container's log and exits, and the restart policy tries again every few seconds until you fix it. After editing `printlog-bridge.cfg`, restart the container.

### Anywhere else (Unraid, a Pi, any Linux host)

The same `docker-compose.yml` works after changing the folder (`/mnt/user/appdata/printlog-bridge` on Unraid, say) and `user:`. Without Compose:

```sh
docker run -d --name printlog-bridge --restart unless-stopped \
  -e TZ=Europe/Berlin -v "$PWD/printlog-bridge:/data" \
  ghcr.io/hoffmanengineering/printlog-bridge:edge
docker exec printlog-bridge printlog-bridge doctor
```

To find your printers' addresses for the config, run `docker run --rm <image> discover --subnet 192.168.1.0/24` with your LAN's network: with Docker's default (bridge) networking the container sees its own network, not your LAN's, so it needs telling which one to scan. Finding a printer again after its address changes works with bridge networking too; only the zeroconf shortcut needs host networking (`network_mode: host`), and the scan covers for it.

Any command runs the same way (`docker run --rm -v ...:/data <image> doctor`). `api_url` must stay `https` (plain `http` is only allowed to `localhost`, which inside a container is the container itself). To test against a development API on another computer on your network, set `PRINTLOG_ALLOW_HTTP_ON_LAN=1` (or `allow_http_on_lan = true` in `[bridge]`) and use its private IP address, such as `http://192.168.1.20:5000`; `run` and `doctor` then warn that the key travels unencrypted. Pairing (`docker run ... pair`, #2) isn't here yet: until it is, use an API key as above.

## Try it

```sh
python3 -m venv ~/printlog-bridge-env
~/printlog-bridge-env/bin/pip install git+https://github.com/HoffmanEngineering/printlog-bridge
cp printlog-bridge.example.cfg ~/printer_data/config/printlog-bridge.cfg   # then edit it
~/printlog-bridge-env/bin/printlog-bridge doctor
```

`doctor` checks the config, the state directory, the API key, this computer's clock and each printer, and says what to fix. `printlog-bridge run` then watches every printer until stopped (Ctrl-C, or SIGTERM from systemd), and shows up in Moonraker's extensions list (Mainsail and Fluidd show it too).

`printlog-bridge status` shows what the running bridge is doing. For each printer it lists the connection, the printer's state, the last job event, the last heartbeat, how many jobs are waiting to reach 3D Print Log, and anything you need to fix (the `Trace` line shows only with `trace = true`):

```
printlog-bridge 0.1.0 status
Bridge: running since 2026-10-10 09:30 UTC (version 0.1.0, pid 812)
Trace: on, recording every Moonraker message in /home/pi/.local/share/printlog-bridge/traces
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

When you need help, run `doctor` and attach the support bundle it writes (`printlog-bridge-support.zip` in the state directory). It holds the doctor report, your config, the bridge's status, its state, its recent log (`bridge.log`) and, with `trace = true`, the latest traces (see [Recording a trace](#recording-a-trace)), with every API key, token and password removed.

Each print appears in 3D Print Log as Printing when the job starts, titled from its file name, with the slicer's estimates. When it ends, the bridge records the outcome, the print time and the filament Klipper fed:

| Moonraker ends the job as | The print becomes | Note added |
| --- | --- | --- |
| `completed` | Success | |
| `cancelled` | Cancelled | |
| `error` | Failed | "Klipper error", with Klipper's message when the bridge saw it |
| `klippy_shutdown` | Failed | "Klipper shutdown" |
| `klippy_disconnect` | Failed | "Lost connection to Klipper" |
| `interrupted`, `server_exit` | Failed | "Moonraker stopped during the print" |

When you cancel objects mid-print with Klipper's [`[exclude_object]`](https://www.klipper3d.org/Exclude_Object.html), the print also gets a note naming them, such as "2 of 6 objects were cancelled: PART_3, PART_5" (Klipper keeps the slicer's object names in capitals), so the filament it used below the estimate has a reason. The bridge has to be running during the print to see them.

On a printer with more than one tool (a toolchanger, an IDEX, a Snapmaker U1), each tool gets its own usage row, numbered from 0 the way Klipper numbers its extruders (`extruder` is tool 0, `extruder1` tool 1). The bridge watches which extruder is active while Klipper's filament count grows, and scales what it saw to Moonraker's total for the job, so the rows always add up to it. When the slicer listed a weight per filament (PrusaSlicer, OrcaSlicer, Snapmaker Orca), the print starts with an estimate on each tool's row, and a job that ran while the bridge was away is split in those proportions. Not covered yet: a multi-material unit on one extruder (ERCF, Happy Hare, AFC), where everything is counted on tool 0, and IDEX copy or mirror mode, where Klipper counts what two heads print once.

### Spools

Each tool's usage row is linked to the spool loaded in its slot in 3D Print Log (in the web or Android app, on the printer's page). The bridge reads the loaded spools when it starts, every 5 minutes, and again when each print starts and ends, so changing a spool in the app between prints is on the next print. On a single-tool printer, a print also keeps its spool loaded in 3D Print Log.

From the printer's console or a macro, with [`klipper/printlog.cfg`](klipper/printlog.cfg) included (`install.sh` does it; in remote mode copy it to the printer's config folder and add `[include printlog.cfg]` yourself, where the printer allows it):

```
PRINTLOG_SET_SPOOL TOOL=2 TAG=04A23F11   ; T2 has the spool with this RFID/NFC tag
PRINTLOG_SET_SPOOL ID=<spool id>         ; the active tool has this spool
PRINTLOG_CLEAR_SPOOL TOOL=1              ; T1 has no spool
```

`TOOL` is 0 for T0 or a single-tool printer; without it, it's the active tool. A tag is the UID stored on the spool in 3D Print Log, in any common format (`04:a2:3f:11` is `04A23F11`). The bridge answers on the console within a second or two, and adds slots to the printer in 3D Print Log when it has fewer than its tools. The macros only print a line for the bridge to read, so they never fail and never stop a print, even with the bridge offline: if no answer comes, run it again once the bridge is back. Assign spools before a print starts; a print keeps the spools it started with, and you can change them on the print in the app. Moonraker's Spoolman integration is left alone unless it points at 3D Print Log (see [Spoolman](#spoolman)).

#### Spoolman

3D Print Log answers Spoolman's API, so Moonraker's `[spoolman]` can point at it and Mainsail, Fluidd and KlipperScreen list your 3D Print Log spools in their spool pickers, by spool number. When it does, the bridge keeps Moonraker's active spool and the tool slots in 3D Print Log in step, both ways. It does this only when the host in `[spoolman] server` is the host of the bridge's `api_url` (case doesn't matter; a user name, password or port in the address is ignored). With a Spoolman server of your own, spool numbers are that server's and mean nothing in 3D Print Log, so the bridge leaves it alone and logs one line saying so. A Moonraker with no `[spoolman]` section, or without the Spoolman component, is fine; the bridge ignores it.

- **Printer to app.** A spool you pick in Mainsail, Fluidd or KlipperScreen, or with `SET_ACTIVE_SPOOL`, is loaded into the active tool's slot in 3D Print Log. Picked during a print, it's a swap, split where it happened, as `PRINTLOG_SET_SPOOL` is. Clearing the active spool changes nothing. A number 3D Print Log doesn't know is logged and changes nothing. Nothing is queued while 3D Print Log is unreachable: pick it again once it is.
- **App to printer.** When the active tool changes, or when the active tool's slot gets another spool (in the app, with `PRINTLOG_SET_SPOOL`, or from the U1's RFID reader), the bridge sets Moonraker's active spool to it. A change made in the app reaches Moonraker when the bridge next reads your spools from 3D Print Log: within 5 minutes, or sooner when a print starts or ends. An empty slot, or a spool with no number, leaves Moonraker's as it was. When the bridge connects it does this only if the printer isn't printing or paused, so a print already running keeps reporting its usage on the spool it started on.

So on a toolchanger, the Snapmaker U1 included, Moonraker's active spool follows each tool change with no macros.

#### Changing spools mid-print

When a spool runs out or you change color partway through a print (a runout sensor, an `M600`), tell the bridge which spool you loaded: run `PRINTLOG_SET_SPOOL` while the print is paused, before you resume. The bridge splits that tool's usage where you changed it. What was printed before goes to the old spool and the rest to the new one, as two usage rows on the same tool, and the slicer's estimate stays on the first. The print also gets a note such as "T0: Spool A → Spool B at 63% (layer 112)". On a Snapmaker U1, putting a tagged spool in the slot does the same without the macro. Each tool of a multi-tool printer is split on its own, and the bridge remembers where it split a print across a restart. A `PRINTLOG_SET_SPOOL` before the tool has printed anything (in your start G-code, say) isn't a change: that spool gets the whole tool. A spool set mid-print also becomes the spool loaded in that slot, for the next print.

The bridge watches for a filament change: a runout sensor (`[filament_switch_sensor]` or `[filament_motion_sensor]`) finding no filament and the print pausing for it, or the extruder unloading 25 mm or more while the print is paused, as an `M600` macro does. A sensor that empties without a pause, such as during an unload in your end G-code, is ignored. When the print pauses for a runout it asks on the console: "Spool ran out on T0. Run PRINTLOG_SET_SPOOL to log the new one." (with `printlog.cfg` included). If the print resumes without a spool assigned to that tool, the bridge never guesses one: the tool's usage stays on the spool it had, and the print gets a note, "T0 was paused for a filament change; run PRINTLOG_SET_SPOOL during the pause to split usage". You can still split it by hand on the print in the app.

Limits worth knowing:

- A spool set before the tool has printed 1 mm counts as the one it started with, not a change. If 3D Print Log already has the print with another spool (the macro ran in your start G-code after the print was logged), that spool keeps the tool's usage and the print gets a note asking you to change it by hand. A `PRINTLOG_SET_SPOOL` after a purge line has printed more than 1 mm splits off the purge line's length to the old spool.
- A runout only counts when the print pauses before it has fed another 200 mm. A sensor with a longer `runout_distance`, such as one far up a Bowden tube, pauses too late to count; the hint note is then missing, but nothing is split wrongly.
- When 3D Print Log was unreachable until after a spool change, the print is created with the spool the tool started with, and the bridge then loads back the spools that were loaded just before. On a single-tool printer 3D Print Log keeps the starting spool loaded too, with no slot, until the next print replaces the list.

On a Snapmaker U1, when the RFID reader reads a Snapmaker spool's tag in a slot, the bridge assigns the spool in your 3D Print Log inventory that has that tag. When no material has the tag yet, the bridge says so once in its log. Add the tag to the material in the app and wait up to 5 minutes: the bridge looks a tag that no material has up again every 5 minutes while the spool stays in its slot (`UNKNOWN_TAG_RETRY_S` in `core/spools.py`), at most once per slot in that time. Pulling the spool out and putting it back is usually quicker, because the bridge looks the tag up again as soon as the reader reports the slot empty (not yet confirmed on a real U1). A tag that found its material isn't looked up again until the spool comes out. When no spool has the tag, and the tag's material or color doesn't match the spool assigned to that slot, the print gets a note saying so; it's logged all the same. The U1 support follows Snapmaker's published firmware, and its reader's data has been checked against a real U1; a whole print on one hasn't been recorded yet.

What you change in the app while it prints is kept: the title, your notes (a note is added after them), the spool on a usage row, and a status you already chose, such as Partial success. A print you delete stays deleted.

When 3D Print Log can't be reached (the internet is down, or the API is busy), nothing is lost. The bridge writes each job event to an outbox in its state directory (`outbox.sqlite3`) before it sends anything, and sends it from there, oldest first and each print's events in order. It retries with a wait that doubles from 2 seconds to a minute, or as long as the API asks, so a print shows up within a minute or two of the connection coming back, even if the bridge was restarted in between. A power cut in the middle of a send doesn't lose the event or log it twice. An event that still hasn't got through after 30 days is dropped, with a warning in the log. `status` shows how many jobs are waiting and why the last try failed. A full or read-only disk doesn't stop the bridge either: it keeps the job events it couldn't write in memory, warns in `status`, and writes them once it can.

The bridge reads Moonraker's job history, so `[history]` must be in moonraker.conf (most installs have it; `run` logs how to add it when it's missing). It remembers what it has logged in its state directory, and on every start or reconnect it catches up on jobs that started or ended while it was away. On its very first run it picks up a print already running, but not older history.

Until pairing lands, the bridge signs in with a personal API key from [3dprintlog.com/api-keys](https://www.3dprintlog.com/api-keys). Store it with `printlog-bridge store-key`, which keeps it in `api_key` in the state directory, readable only by you. The bridge looks for the key in the `PRINTLOG_API_KEY` environment variable, then that file, then `api_key` in `[bridge]`. A key in `[bridge]` still works, but in local mode the config sits in Klipper's config folder, which Moonraker serves to anyone who can open Mainsail or Fluidd: `doctor` warns about it, and `store-key` moves it out of the config for you.

## Privacy and security

The bridge sends 3D Print Log the print's file name (without its folders), times, duration, outcome, filament used, the slicer's estimates, its thumbnail, its slicer settings with hosts and credentials blanked (unless you turn them off), and, unless you turn snapshots off, one webcam frame when it ends. Of the G-code file it reads only the header and the settings block after the G-code, never the toolpaths, and it never uploads the file. It connects only to Moonraker, the webcam Moonraker names, and 3D Print Log over HTTPS (and, to find a printer whose address changed, to Moonraker's port on your local network), and never listens on a port. Keys, passwords and tokens never reach its log, status or support bundle. [docs/privacy.md](docs/privacy.md) lists every call and field, and the test that holds each guarantee.

## Config

One `printlog-bridge.cfg`, looked up in this order: `--config`, `$PRINTLOG_BRIDGE_CONFIG`, `~/printer_data/config/`, `~/.config/printlog-bridge/`. See [printlog-bridge.example.cfg](printlog-bridge.example.cfg). The state directory is `state_dir` in `[bridge]`, then `$PRINTLOG_BRIDGE_STATE_DIR`, then `~/.local/share/printlog-bridge`.

Each printer is a `[printer <name>]` section, so one bridge can watch several. Set `printer_id` in each to the printer's number in 3D Print Log, the end of its page's address (`https://www.3dprintlog.com/printers/42`); pairing will set it for you.

In remote mode, if Moonraker has `[authorization]` enabled and the bridge's host isn't in `trusted_clients`, set `moonraker_api_key` in the printer's section to Moonraker's API key. The local socket is always trusted, so local mode never needs one. Moonraker accepts one bridge per printer; a second one waits until the first stops.

Each print gets the largest thumbnail the slicer embedded in its file (OrcaSlicer, PrusaSlicer, Cura and SuperSlicer all embed one) as its picture when it starts. When it ends, the bridge also adds one frame from the printer's first webcam (its `snapshot_url` in Mainsail or Fluidd); for a failed print that frame becomes the picture shown. Set `snapshots = false` in the printer's section to never ask the webcam for anything. Pictures wait in the outbox with the print's other events (the image files in `images/` in the state directory), and never hold up logging the print: one that takes longer than 10 seconds, is over 10 MB or that 3D Print Log refuses (a print holds 5 pictures on the free plan) is skipped. A Moonraker or webcam address with a password or token in it is shown with them hidden, in the log, `status` and `doctor`.

### When a printer's address changes

Most printers get their IP address from the router (DHCP), and it can change, after a power cut say. In remote mode the bridge finds the printer again without you editing the config. The first time it connects, it remembers who the printer is: the MAC addresses Moonraker reports and Moonraker's instance id, in the printer's state file. When the printer can't be reached for 3 minutes, the bridge looks for that same printer on its last known network (the /24 around its last address): first over zeroconf, when Moonraker has `[zeroconf]` set up, then by asking each address on Moonraker's port for `/server/info`, at most once every 10 minutes. When it answers somewhere new, the bridge carries on there and remembers the new address in its state; `status` and the log say where it moved (`moved from 192.168.1.87 to 192.168.1.91`). Your config is never changed, and changing `url` in it starts afresh.

A different printer at the old address is never used: if another printer answers there, the bridge says so in `status`, logs nothing from it and looks for the right one. Set `discovery = false` in a printer's section to turn all of this off, or tune it there:

| Option | Default | What it does |
| --- | --- | --- |
| `discovery_after` | `3m` | how long the printer must be unreachable before the bridge looks for it |
| `discovery_interval` | `10m` | the least time between two searches (at least `30s`) |
| `discovery_timeout` | `2s` | how long to wait for each address to answer; raise it on a slow Wi-Fi network |
| `discovery_zeroconf` | `true` | ask over zeroconf (mDNS) before scanning |
| `discovery_networks` | the /24 around the printer's last address | where to look instead, such as a printer VLAN: private networks of a /24 or smaller, up to 16, separated by commas |

Times take `90`, `90s`, `3m` or `1h`.

The surest fix is still a DHCP reservation on the router, which keeps the printer at one address. To list the Moonraker printers on your network, with a `[printer]` section for each ready to paste into the config, run `printlog-bridge discover` (`--subnet 192.168.1.0/24` picks the network to scan; by default it scans the configured printers' networks and this computer's own).

### Prints you made yourself

If you create the print yourself, with the [Slic3r uploader](https://github.com/HoffmanEngineering/Slic3rPostProcessingUploader), the Cura plugin or by hand, the bridge logs the job to that print instead of creating a second one. When a job starts it looks for one of your prints that is:

- on the same printer, and still in Printing;
- not claimed by any connector yet;
- made within `adopt_window` before the job started (12 hours by default);
- for the same file: the names match once folders, the extension (`.gcode`, `.bgcode`, `.gcode.3mf`), and the suffix PrusaSlicer and OrcaSlicer add (`_0.2mm_PLA_MK4_1h2m`) are left out, ignoring case. A print with no file name matches on its title.

If there are several, it takes the newest. It claims the print, adds the slicer's thumbnail (as the picture shown only when the print has none yet), and completes it when the job ends, as it would a print it made. Your title, notes, estimate and the spool on your usage row are kept; the actual time and filament are filled in, into your rows rather than new ones. The log says `job 00001F: adopted print 110210 ('Benchy')`.

Set these in `[bridge]` for every printer, or in a printer's section for that one:

```ini
adopt_prints = true    ; the default: adopt a matching print, else create one
create_prints = false  ; finish-only: never create a print; a job with no match isn't logged
adopt_window = 12h     ; 90m, 2d; a bare number is hours
```

Finish-only mode is for uploader users who only want the bridge to report how each print ended. The bridge looks for a print to adopt again when the job ends, so a print uploaded after the job started is still found. Without a match, the job is skipped with a line in the log.

### Slicer settings

Each print records the slicer settings it was sliced with, as the Cura plugin and the Slic3r uploader do: the bridge sends the settings block the slicer wrote at the end of the file (OrcaSlicer, PrusaSlicer, Anycubic Slicer Next and their forks write one), the file's header, which names the slicer and its version, and the summary Moonraker read from the file (slicer, layer heights, nozzle, filament, first layer temperatures). 3D Print Log adds them to the print's notes. They are read once, when the job starts, by byte range: only the start of the file up to the G-code and its end after the G-code, never the toolpaths. Before they leave the printer, the values of every setting naming a host or holding a credential (`print_host`, `printhost_apikey`, passwords, tokens, `post_process`...) are blanked. They go through the outbox like the print's other events, and never hold up logging the print: when the file can't be read, Moonraker's summary is sent alone, and when 3D Print Log can't take them, they are dropped. A print you made with the Slic3r uploader or the Cura plugin keeps the settings those recorded.

Set `slicer_settings = false` in `[bridge]`, or in a printer's section for that one, to turn this off; the G-code file is then never read. [docs/privacy.md](docs/privacy.md) says exactly what is read and sent.

### Recording a trace

To report a problem with a printer, or to turn one of its prints into a test, set `trace = true` in `[bridge]` (in Docker, set the environment variable `PRINTLOG_BRIDGE_TRACE=1` instead) and restart the bridge. It then records every message it sends to and receives from each printer's Moonraker, one JSON line each with the time and the direction, in `traces/<printer>-<YYYYMMDD>.jsonl` in the state directory. `status` shows `Trace: on` while it records.

A trace holds what Moonraker tells the bridge: the printer's state as it changes (print progress, the active tool, filament used, the RFID reader's tags), its job history with file names and the slicer's metadata, its webcam list and console lines. It never holds G-code, and every API key, Moonraker key, oneshot token and password (in URLs too) is removed before it is written. Traces stay on this computer; nothing sends them anywhere. `doctor`'s support bundle includes the latest 20 MB, for you to attach.

Tracing never slows the bridge down: messages are written by a thread of their own, and dropped rather than queued when the disk can't keep up. Each printer's traces are kept under 50 MB in all, in files of up to 10 MB, the oldest deleted first; a full or read-only disk only pauses them. Set `trace = false` (or remove the line) and restart to stop.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

CI runs both on Python 3.9 and 3.12. It also runs `shellcheck` on the shell scripts, and `tests/install/smoke.sh`, which runs `install.sh` and `uninstall.sh` in a throwaway home inside a `python:3.9-slim` container, against a fake `systemctl`, and checks that Moonraker's files end up byte for byte as they started. The file edits themselves are in `scripts/install_helper.py`, tested by `tests/test_install_helper.py`.

The Docker workflow builds the image for amd64 and runs `tests/docker/smoke.sh` against it: `--version`, `store-key`, `doctor`, `run` and `status` inside the container, as a non-root user, against three fake Moonrakers and a fake 3D Print Log API on the runner (`tests/docker/serve_fakes.py`), so it never calls the real API. It then builds all three platforms, and pushes them to GHCR from `main` (`edge`) and from `v*` tags. Run the smoke test yourself with `docker build -t printlog-bridge:dev . && tests/docker/smoke.sh printlog-bridge:dev`.

`tests/fixtures/moonraker/` holds REAL answers from a Snapmaker U1 and an Infimech EX, which `tests/test_real_captures.py` runs the adapter against. `tests/trace_replay.py` replays a trace as Moonraker, so a recorded print becomes a regression test: see `tests/test_trace_replay.py`, which replays a SYNTHETIC U1 print from `tests/fixtures/traces/`.

`pytest -m integration` runs the bridge against a real Moonraker, built from source in Docker (set `MOONRAKER_REF` to pick a tag or commit; the default is `v0.9.3`). The first run builds the image, which takes a minute or two. CI runs it against the last release and a pinned recent commit. To support another kind of printer, see [docs/adapters.md](docs/adapters.md).

## License

[MIT](LICENSE)
