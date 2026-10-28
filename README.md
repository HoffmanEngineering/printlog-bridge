# printlog-bridge

An open-source bridge agent that watches Klipper/Moonraker printers and logs every print to [3D Print Log](https://www.3dprintlog.com) automatically: real outcome, duration, per-spool filament usage and a picture, with no manual entry and no printer config edits.

It runs in **local mode** on the printer's own host (Moonraker Unix socket) or in **remote mode** on any LAN host (Pi, Docker, NAS) over Moonraker's WebSocket, which covers locked-down printers such as the Snapmaker U1.

It also watches **Bambu Lab printers** (X1, P1, A1, H2) over their LAN connection, **Prusa printers with PrusaLink** (MK4/MK4S, MK3.5/3.9, Core One, MINI, XL) over its local API, and **printers run by OctoPrint** over OctoPrint's API, from any host on the same network: see [Bambu Lab printers](#bambu-lab-printers), [Prusa printers (PrusaLink)](#prusa-printers-prusalink) and [OctoPrint](#octoprint).

> Status: early development. `run` logs each print when it starts and completes it when it ends, with its outcome, duration and filament used, adds the slicer's thumbnail and a webcam snapshot (and, with Pro, the moonraker-timelapse video), and keeps what it can't send yet until 3D Print Log is reachable again. On a multi-tool printer the filament is split per tool. See the [issues](https://github.com/HoffmanEngineering/printlog-bridge/issues) and milestones for the roadmap.

## Requirements

On the printer's own host: Python 3.9 or newer (Raspberry Pi OS Bullseye and later) and Moonraker. Anywhere else: a Linux computer with Docker on the same network as the printers, a NAS, or Home Assistant.

## Install

Pick one:

- **A computer on your network, with Docker** (a Raspberry Pi, a mini PC, a home server, a NAS with a shell): the one-line install below. One bridge watches every printer on the network, and finds them by itself.
- **A NAS from its app store:** [Synology](#on-a-synology-nas-container-manager) or [Unraid](#on-unraid). **Home Assistant:** [the app](#run-it-as-a-home-assistant-app).
- **The printer's own Pi** (Klipper with Moonraker): [`install.sh`](#install-on-the-printer-local-mode), with Mainsail's and Fluidd's update panels.

### One-line install with Docker

> **From release 0.2.0 on.** The command below downloads the script attached to the latest release, and the first release with it is 0.2.0. Until then, download the script from `main`, read it, and run it with the `edge` image (the newest commit on `main`):
>
> ```sh
> curl -fsSLO https://raw.githubusercontent.com/HoffmanEngineering/printlog-bridge/main/scripts/docker-install.sh
> less docker-install.sh
> sh docker-install.sh --tag edge
> ```
>
> Or set it up by hand with [`docker-compose.yml`](#on-a-raspberry-pi).

On a Linux computer with [Docker](https://docs.docker.com/engine/install/) and Docker Compose, as your own user (not root):

```sh
curl -fsSL https://github.com/HoffmanEngineering/printlog-bridge/releases/latest/download/docker-install.sh | sh
```

It checks Docker, writes a Compose project to `~/printlog-bridge`, pulls the image and starts the bridge, then shows the code to approve it in 3D Print Log (scan the QR code in the log, or open the address and type the code) and waits until you have. The bridge:

- runs on the **host's network** (`network_mode: host`): finding printers on your network (mDNS, Bambu Lab's announcements) doesn't work through Docker's own network;
- starts again with Docker when the computer boots (`restart: unless-stopped`);
- keeps its config (`printlog-bridge.cfg`) and its state (the pairing, prints not sent yet) in `~/printlog-bridge/data`, the only folder the container sees, so nothing in the container can change the Compose files you run;
- never needs an API key: pairing replaces it;
- serves its [local page](#the-local-page) at <http://127.0.0.1:7180/>, on that computer only. From another computer, `ssh -L 7180:127.0.0.1:7180 you@that-computer` and open the same address. To open it to your network instead, read [the warning](#the-local-page) first, then put this in `~/printlog-bridge/compose.override.yaml` and run the command again:

  ```yaml
  services:
    printlog-bridge:
      environment:
        PRINTLOG_BRIDGE_STATUS_PAGE: 0.0.0.0:7180
  ```

Then connect your printers on 3D Print Log's Bridges page, which lists the ones the bridge found.

**Running it again** updates the bridge to the latest release and keeps your config, your pairing and `compose.override.yaml` (Docker Compose reads it on top of `compose.yaml`, which the script rewrites each time: put your own settings, such as `PRINTLOG_BRIDGE_NAME`, there). It says what to do instead, and changes nothing, when it's given a folder it didn't make that has anything in it but a `data/` of yours holding only `printlog-bridge.cfg` and `state/` (the bridge takes over `data/`), a `data/` or `compose.override.yaml` that's a symbolic link, your home folder, `/` or another top-level folder, a folder that isn't yours, or when there's a `printlog-bridge` container from elsewhere. It shows the pairing code from the bridge's own state (`printlog-bridge status --pairing`), never from its log, which a device on your network could put text into; and when the bridge already runs but needs pairing again, it shows that code too.

**Options**, after `sh -s --`: `--dir DIR` for another folder, `--tag TAG` for another image tag (`edge`, to try what's on `main`; running the command again without it goes back to the release), `--no-wait` to not wait for the approval, and `--uninstall`:

```sh
curl -fsSL https://github.com/HoffmanEngineering/printlog-bridge/releases/latest/download/docker-install.sh | sh -s -- --uninstall
```

That stops and removes the container and keeps the folder; `rm -rf ~/printlog-bridge` then removes the config and pairing too, and you can remove the bridge on 3D Print Log's Bridges page.

**Not as root.** Run it as your own user: it uses `sudo` for Docker by itself when your user can't use Docker yet, and the folder stays yours. It refuses to run as root, because the folder and everything the script and Docker Compose do would then be root's; where root is the only user (some NAS shells), add `--root`.

**Which version.** The script comes from the latest [release](https://github.com/HoffmanEngineering/printlog-bridge/releases) (not from `main`), and installs exactly that release's image (`printlog-bridge:<version>`, not `latest`), so what it does and what it installs change only with a release, never under you between runs. `docker compose pull` alone doesn't move to a newer release: run the command again.

**Read it first.** `curl … | sh` runs whatever the URL returns, with your user's rights (and Docker's, which amount to root). It comes over HTTPS from this repository's releases, but if you'd rather read it before it runs, download it and its checksum, check and read it, then run it:

```sh
base=https://github.com/HoffmanEngineering/printlog-bridge/releases/latest/download
curl -fsSLO "$base/docker-install.sh" && curl -fsSLO "$base/docker-install.sh.sha256"
sha256sum -c docker-install.sh.sha256
less docker-install.sh
sh docker-install.sh
```

A given release's copy is at `.../releases/download/v0.2.0/docker-install.sh` (and `.sha256`). It changes only if that release's file is replaced (the release job re-uploads it when a tag's workflow runs again), so keep the checksum you checked if you want to notice that. The checksum comes from the same place as the script: it catches a damaged download, not a tampered release.

**Where it doesn't install.** It stops and says why, rather than set up a bridge that can't find your printers:

- **Docker Desktop** (macOS, Windows, and Docker Desktop on Linux) and **WSL**: Docker runs in a virtual machine there, and its host network isn't your network. Run the bridge on a Linux computer on the printers' network, or [run it by hand](#run-it-in-docker-remote-mode) and connect printers by their address.
- **Rootless Docker**: its host network is a private one too. Use Docker as root (Docker's default install), or run it by hand.
- **Podman**: run the image by hand with `--network host` (see [Anywhere else](#anywhere-else-any-linux-host)) and enable `podman-restart.service`.
- **A Raspberry Pi Zero or 1** (armv6): there's no image for it. Every other Pi (32-bit `linux/arm/v7` or 64-bit `linux/arm64`) and `linux/amd64` work.

If your user can't use Docker yet, it runs Docker with `sudo` (the folder stays yours), and says how to join the `docker` group instead. Without Docker, it shows Docker's own install command (`curl -fsSL https://get.docker.com | sh`) rather than run it for you.

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

Running it again is safe. Options: `--printer-data DIR` when Moonraker's data folder isn't `~/printer_data`, `--printer-id N`, and `--yes` to ask nothing; `install.sh --help` lists them all. If you leave the key for later, pair the bridge instead with `~/printlog-bridge-env/bin/printlog-bridge pair` (see [Signing in](#signing-in-pairing)), or store a key with `~/printlog-bridge-env/bin/printlog-bridge store-key`, as yourself rather than with `sudo` (both refuse to run as root); set the printer in `printlog-bridge.cfg` (Mainsail and Fluidd can edit it, and `pair` can set it for you). Then restart printlog-bridge from their services menu. The service doesn't restart itself while its config needs fixing.

`./printlog-bridge/uninstall.sh` removes the service and the virtualenv, puts `moonraker.conf`, `moonraker.asvc` and `printer.cfg` back exactly as they were, and removes `printlog.cfg` unless you changed it. It keeps your config and the state directory (including prints not yet logged) for a reinstall; `--purge` removes those too.

## Run it in Docker (remote mode)

One container watches every printer in its config over the network: through Moonraker's WebSocket for a Klipper printer, and directly for a Bambu Lab or PrusaLink printer, which have no host of their own to run the bridge on. That suits a NAS, a home server or a Pi that isn't a printer. The image is `ghcr.io/hoffmanengineering/printlog-bridge`, for `linux/amd64`, `linux/arm64` and `linux/arm/v7` (32-bit Raspberry Pi OS):

| Tag | What it is |
| --- | --- |
| `latest`, `1.2`, `1.2.3` | releases |
| `edge` | the newest commit on `main` |

The examples below use `latest`, the newest release. `edge` gets each change as soon as it's merged, before it's released, so use it only to try a fix early.

Everything the bridge keeps is in one folder, mounted at `/data`:

| Path | What it is |
| --- | --- |
| `/data/printlog-bridge.cfg` | the config: your printers. The bridge writes a starter one when there is none |
| `/data/state/device_token`, `device_id` | the bridge's device token and its id, once you pair it |
| `/data/state/pairing_pending.json` | the code to approve, while the bridge pairs (`status --pairing`, for scripts) |
| `/data/state/api_key` | the API key, if you store one with `store-key` instead |
| `/data/state/outbox.sqlite3`, `images/` | job events and pictures not sent yet |
| `/data/state/printers/`, `status.json` | what has been logged, and what `status` shows |
| `/data/state/bridge.log` | the log (`docker logs` shows it too) |
| `/data/state/printlog-bridge-support.zip` | the support bundle `doctor` writes |

The image sets `PRINTLOG_BRIDGE_CONFIG=/data/printlog-bridge.cfg` and `PRINTLOG_BRIDGE_STATE_DIR=/data/state`; a `state_dir` in the config's `[bridge]` still wins, so leave it out.

**The first start.** With no API key or device token, the bridge pairs itself: its log shows a QR code, the address and the code (a new one each time one expires) until you approve it. With no printers yet, it then writes a starter `printlog-bridge.cfg` in the folder, with a commented example for each kind of printer, and waits, healthy, instead of exiting. Add a `[printer <name>]` section per printer to that file (over the network, File Station or the Unraid share; uncomment an example and change it) and save it: the bridge starts watching within a few seconds, with no restart. `printlog-bridge discover`, run in the container's console, lists the printers on your network with a section for each to paste in. Use the printers' IP addresses (give each a fixed one in your router): names like `voron.local` usually don't resolve inside a container. A printer still needs its `printer_id` (the number at the end of its page in 3D Print Log); until it has one, the log says so and lists your printers.

**Who it runs as.** The container starts as root only to give the folder to the user the bridge runs as, then switches to that user before doing anything else, for `docker exec` commands too: `PUID`/`PGID` when you set them, otherwise the folder's own owner, or the image's user `1000:1000` when root owns the folder. So the folder needs no permission set up by hand. A container started with `user: "<uid>:<gid>"` (installs from before this) runs as that user, as before.

**The local page.** The bridge serves a small read-only page with its status, the pairing code and QR code, and `doctor`'s checks (see [The local page](#the-local-page)). By default it listens on `127.0.0.1:7180` only, which inside a container is the container itself (or, with `--network host`, the NAS itself). To open it from another computer, set `PRINTLOG_BRIDGE_STATUS_PAGE=0.0.0.0:7180` (with `--network host`), or publish the port on the host's own address only (`-p 127.0.0.1:7180:7180`, with `PRINTLOG_BRIDGE_STATUS_PAGE=0.0.0.0:7180` inside), and read the warning in that section first.

### On a Synology NAS (Container Manager)

1. In DSM, open Package Center and install **Container Manager** (keep the default bridge network it offers).
2. Container Manager > **Project** > **Create**: name it `printlog-bridge`, set its path to a new folder (**Set path** > **Create folder**, such as `docker/printlog-bridge`), choose **Create compose.yaml** and paste [`docker-compose.yml`](docker-compose.yml). Change `TZ` to your [time zone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) if you like, then **Next** and **Done**.
3. Open Container > **printlog-bridge** > **Log**, and scan the QR code with your phone (or open the address shown and type the code). Sign in to 3D Print Log and approve the bridge.
4. In File Station, open `docker/printlog-bridge/printlog-bridge.cfg`, add your printers and save. To list the printers on your network, run `printlog-bridge discover` in Container > printlog-bridge > **Terminal** > **Create**, or `sudo docker exec printlog-bridge printlog-bridge discover` over SSH.

The container shows as healthy while the bridge runs. `printlog-bridge doctor` (in the same Terminal) checks the config, the sign-in and every printer, and leaves the support bundle in the folder's `state/`, which you can download from File Station; `printlog-bridge status` shows what each printer is doing. The project folder is the bridge's: it runs as the DSM user who made it. DSM 7.0 and 7.1 call the package **Docker** and have no Project tab: use the `docker run` command under [Anywhere else](#anywhere-else-any-linux-host) over SSH. If pairing is off on 3D Print Log, the log says so: put `PRINTLOG_API_KEY: <key>` from [3dprintlog.com/api-keys](https://www.3dprintlog.com/api-keys) under `environment:` (anyone who can manage containers on the NAS can read it there), or store it in the folder with `printlog-bridge store-key` in the Terminal.

### On Unraid

1. In the **Apps** tab, search for **printlog-bridge** and install it. The defaults are fine: the data folder is `/mnt/user/appdata/printlog-bridge`, it runs as `nobody:users` (99:100, `PUID`/`PGID`), and on the host's network. Set **Bridge name** if you like.
2. Docker tab > the printlog-bridge icon > **Logs**: scan the QR code to pair it.
3. Add your printers to `printlog-bridge.cfg` in the data folder (the `appdata` share) and save. The icon's **Console** runs `printlog-bridge discover`, `doctor` and `status`.

Until it's listed in the Apps tab, add the template [`unraid/printlog-bridge.xml`](unraid/printlog-bridge.xml) by hand, in the web terminal (`>_`):

```sh
wget -O /boot/config/plugins/dockerMan/templates-user/my-printlog-bridge.xml \
  https://raw.githubusercontent.com/HoffmanEngineering/printlog-bridge/main/unraid/printlog-bridge.xml
```

Then Docker > **Add Container** > Template: **printlog-bridge**, and Apply. A container from the earlier template, which ran with `--user 99:100`, keeps working as it is.

### On a Raspberry Pi

To run the bridge on the printer's own Pi, use `install.sh` above instead. Docker suits a Pi that isn't a printer, watching printers over the network. A Pi 4 or 5 on 64-bit Raspberry Pi OS gets the `linux/arm64` image; on 32-bit Raspberry Pi OS (and a Pi 2 or 3), `linux/arm/v7`. Docker picks the right one by itself.

```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # then log out and back in
curl -fsSL https://github.com/HoffmanEngineering/printlog-bridge/releases/latest/download/docker-install.sh | sh
```

That's the [one-line install](#one-line-install-with-docker). By hand instead:

```sh
mkdir ~/printlog-bridge && cd ~/printlog-bridge
curl -fsSLO https://raw.githubusercontent.com/HoffmanEngineering/printlog-bridge/main/docker-compose.yml
docker compose up -d && docker compose logs -f   # scan the QR code, then add printers to printlog-bridge.cfg here
```

To switch a folder set up by hand to the one-line install, keeping its config and pairing: `docker compose down` there, `mkdir -p ~/printlog-bridge/data`, move `printlog-bridge.cfg` and `state/` into that `data/`, then run the one-line install. (The one-line install mounts only `data/`, unlike `docker-compose.yml`, which mounts its whole folder so a NAS project stays one folder.)

### Anywhere else (any Linux host)

The [one-line install](#one-line-install-with-docker), or [`docker-compose.yml`](docker-compose.yml) in an empty folder and `docker compose up -d` there, as on a Pi. Without Compose:

```sh
docker run -d --name printlog-bridge --restart unless-stopped --network host \
  -e TZ=America/New_York -v "$PWD/printlog-bridge:/data" \
  ghcr.io/hoffmanengineering/printlog-bridge:latest
docker logs -f printlog-bridge   # the first time: scan the QR code to pair it
docker exec printlog-bridge printlog-bridge discover   # your printers, to add to printlog-bridge/printlog-bridge.cfg
docker exec printlog-bridge printlog-bridge doctor
```

To pair (or pair again) in a terminal instead, run `docker run --rm -it -v "$PWD/printlog-bridge:/data" ghcr.io/hoffmanengineering/printlog-bridge:latest pair`, or `docker compose run --rm printlog-bridge pair` next to the Compose file; then `docker restart printlog-bridge`. Set `PRINTLOG_BRIDGE_NAME` on the container to choose the name 3D Print Log shows for it (otherwise `printlog-bridge on <host name>`). While pairing is off on 3D Print Log, store an API key from [3dprintlog.com/api-keys](https://www.3dprintlog.com/api-keys) the same way with `store-key` in place of `pair`, or set `PRINTLOG_API_KEY`.

`discover` finds Bambu Lab printers only on the host's network (`--network host`, `network_mode: host`), which the examples above use. With Docker's default (bridge) networking the container sees its own network, not your LAN's, so give `discover` your LAN with `--subnet 192.168.1.0/24`; finding a printer again after its address changes works either way.

Any command runs the same way (`docker exec printlog-bridge printlog-bridge <command>`, or `docker run --rm -v ...:/data <image> <command>`). A broken config (a typo, a value the bridge can't read) still makes it exit and say what to fix in its log; the restart policy tries again every few seconds until you fix it.

## Run it as a Home Assistant app

With Home Assistant OS (or Supervised) on the same network as the printers, the bridge runs as a Home Assistant app (formerly add-on), with no Docker or SSH:

1. In Home Assistant, open **Settings > Apps**, then the app store, and in its menu **Repositories**. Add `https://github.com/HoffmanEngineering/printlog-bridge`.
2. Install **3D Print Log Bridge**, add your printers on its **Configuration** tab (adapter, printer id, and the address and secrets for that adapter), turn on **Watchdog** and start it.
3. Scan the QR code in its **Log** tab to pair it.

The app's Documentation tab ([home-assistant/DOCS.md](home-assistant/DOCS.md)) has the details. It runs the image `ghcr.io/hoffmanengineering/printlog-bridge-addon`, the Docker image plus a start-up that writes the config from the app's options, on the host's network, for `amd64` and `aarch64` (Home Assistant no longer supports 32-bit Arm). For settings the options don't have, put a `printlog-bridge.cfg` in the app's config folder. Home Assistant shows its updates; the app's version is the bridge's. `api_url` must stay `https` (plain `http` is only allowed to `localhost`, which inside a container is the container itself). To test against a development API on another computer on your network, set `PRINTLOG_ALLOW_HTTP_ON_LAN=1` (or `allow_http_on_lan = true` in `[bridge]`) and use its private IP address, such as `http://192.168.1.20:5000`; `run` and `doctor` then warn that the key travels unencrypted.

## Updating

The bridge tells you when a newer release is out. 3D Print Log answers each heartbeat with the latest version, so the bridge never asks anyone else. When yours is older, you get:

- one line in the log per new version: `printlog-bridge 0.2.0 is available (this is 0.1.0)`;
- an `Update:` line in `printlog-bridge status`, and a warning in `printlog-bridge doctor`;
- a note on the printer's Connections in 3D Print Log.

Set `update_check = false` in `[bridge]` to turn the notice off. The heartbeat sends the same thing either way.

To update:

- **Docker, from the one-line install:** run the [install command](#one-line-install-with-docker) again. It installs the latest release's image, keeping your config and pairing (`docker compose pull` alone stays on the version it installed).
- **Docker, with Compose:** in the folder with the Compose file, run `docker compose pull && docker compose up -d`. On a Synology NAS, over SSH: `cd /volume1/docker/printlog-bridge && sudo docker compose pull && sudo docker compose up -d` (Container Manager saves the project's file there as `compose.yaml`). The project then runs the new image, with the same config and state.
- **Docker, without Compose:** `docker pull ghcr.io/hoffmanengineering/printlog-bridge:latest`, then `docker rm -f printlog-bridge` and the same `docker run` as before. Your config and state are in `/data`, so nothing is lost.
- **Home Assistant app:** Home Assistant shows the update on the app's page (and in Settings); the app doesn't print its own notice.
- **On the printer (local mode):** update printlog-bridge in Mainsail's or Fluidd's update panel, where `install.sh` added it. Moonraker's update manager follows `main`, so it can be newer than the last release. Over SSH, `git -C ~/printlog-bridge pull && ~/printlog-bridge/install.sh --yes` does the same.

## Try it

```sh
python3 -m venv ~/printlog-bridge-env
~/printlog-bridge-env/bin/pip install git+https://github.com/HoffmanEngineering/printlog-bridge
cp printlog-bridge.example.cfg ~/printer_data/config/printlog-bridge.cfg   # then edit it
~/printlog-bridge-env/bin/printlog-bridge doctor
```

`doctor` checks the config, the state directory, the device token or API key (and whether 3D Print Log takes it), this computer's clock and each printer, and says what to fix. `printlog-bridge run` then watches every printer until stopped (Ctrl-C, or SIGTERM from systemd), and shows up in Moonraker's extensions list (Mainsail and Fluidd show it too).

`printlog-bridge status` shows what the running bridge is doing. For each printer it lists the connection, the printer's state, the last job event, the last heartbeat, how many jobs are waiting to reach 3D Print Log, and anything you need to fix (the `Trace` line shows only with `trace = true`):

```
printlog-bridge 0.1.0 status
Bridge: running since 2026-10-10 09:30 UTC (version 0.1.0, pid 812)
Pairing: paired as 'printlog-bridge on voron' (device token in /home/pi/.local/share/printlog-bridge/device_token)
Trace: on, recording every Moonraker message in /home/pi/.local/share/printlog-bridge/traces
Printer voron (printer 42 in 3D Print Log)
  Connection: connected for 3h 12m (moonraker, local socket /home/pi/printer_data/comms/moonraker.sock)
  Printer state: ready
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

Each tool's usage row is linked to the spool loaded in its slot in 3D Print Log (in the web or Android app, on the printer's page). The bridge reads the loaded spools when it starts, every 5 minutes, and again when each print starts and ends, so changing a spool in the app between prints is on the next print. On a single-tool printer, a print also keeps its spool loaded in 3D Print Log; so when the read as a print starts fails, the print waits up to an hour for a read that works rather than use the spool read earlier, which may have been unloaded in the app since. After that it's logged with the earlier read, and a note on the print says so.

From the printer's console or a macro, with [`klipper/printlog.cfg`](klipper/printlog.cfg) included (`install.sh` does it; in remote mode copy it to the printer's config folder and add `[include printlog.cfg]` yourself, where the printer allows it):

```
PRINTLOG_SET_SPOOL TOOL=2 TAG=04A23F11   ; T2 has the spool with this RFID/NFC tag
PRINTLOG_SET_SPOOL ID=<spool id>         ; the active tool has this spool
PRINTLOG_CLEAR_SPOOL TOOL=1              ; T1 has no spool
```

`TOOL` is 0 for T0 or a single-tool printer; without it, it's the active tool. A tag is the UID stored on the spool in 3D Print Log, in any common format (`04:a2:3f:11` is `04A23F11`). The bridge answers on the console within a second or two, and adds slots to the printer in 3D Print Log when it has fewer than its tools. 3D Print Log takes more slots only with the whole printer saved again, so that needs the printer's Make, Model and Name filled in there (the answer says which is missing), and a spool loaded or unloaded in the app at that same moment may be undone; you can also set the number of slots on the printer in the app yourself. The macros only print a line for the bridge to read, so they never fail and never stop a print, even with the bridge offline: if no answer comes, run it again once the bridge is back. Assign spools before a print starts; a print keeps the spools it started with, and you can change them on the print in the app. With Moonraker's Spoolman integration, the spool active there is assigned too (see [Spoolman](#spoolman)).

#### Spoolman

3D Print Log answers Spoolman's API, so Moonraker's `[spoolman]` can point at it and Mainsail, Fluidd and KlipperScreen list your 3D Print Log spools in their spool pickers, by spool number. When it does, the bridge keeps Moonraker's active spool and the tool slots in 3D Print Log in step, both ways. It does this only when the host in `[spoolman] server` is the host of the bridge's `api_url` (case doesn't matter; a user name, password or port in the address is ignored). With a Spoolman server of your own, the bridge follows it one way instead (see [Your own Spoolman](#your-own-spoolman)). A Moonraker with no `[spoolman]` section, or without the Spoolman component, is fine; the bridge ignores it.

- **Printer to app.** A spool you pick in Mainsail, Fluidd or KlipperScreen, or with `SET_ACTIVE_SPOOL`, is loaded into the active tool's slot in 3D Print Log. Picked during a print, it's a swap, split where it happened, as `PRINTLOG_SET_SPOOL` is. Clearing the active spool changes nothing. A number 3D Print Log doesn't know is logged and changes nothing. Nothing is queued while 3D Print Log is unreachable: pick it again once it is.
- **App to printer.** When the active tool changes, or when the active tool's slot gets another spool (in the app, with `PRINTLOG_SET_SPOOL`, or from the U1's RFID reader), the bridge sets Moonraker's active spool to it. A change made in the app reaches Moonraker when the bridge next reads your spools from 3D Print Log: within 5 minutes, or sooner when a print starts or ends. An empty slot, or a spool with no number, leaves Moonraker's as it was. When the bridge connects it does this only if the printer isn't printing or paused, so a print already running keeps reporting its usage on the spool it started on.

So on a toolchanger, the Snapmaker U1 included, Moonraker's active spool follows each tool change with no macros.

#### Your own Spoolman

If you already track spools in your own [Spoolman](https://github.com/Donkie/Spoolman) through Moonraker's `[spoolman]`, you don't have to assign them twice. When the spool active in Spoolman changes, the bridge loads the material it belongs to into that tool's slot in 3D Print Log. It only reads: it never writes to Spoolman (Moonraker reports usage there already) and never changes Moonraker's active spool.

- **Which material.** The bridge reads the spool from Spoolman through Moonraker, then finds the material in 3D Print Log linked to that Spoolman spool. If none is, it uses the spool's tag UID: the `nfc_id` extra field, where [nfc2klipper](https://code.thetadev.de/ThetaDev/nfc2klipper) and FilaMan keep it, matched against the tags on your materials. With neither, nothing changes and the bridge logs a hint, such as "no material in 3D Print Log is linked to Spoolman spool 12 (Polymaker PolyTerra PLA Red) yet; nothing changed. Load the spool on T0 in 3D Print Log."
- **Which slot.** The active spool goes to the active tool: slot 0 on a single-tool printer. On a toolchanger whose tool macros keep each tool's spool, as Fluidd and Mainsail do with a `spool_id` variable on `[gcode_macro T0]`, `[gcode_macro T1]`..., or as `t0__spool_id`, `t1__spool_id`... in `[save_variables]`, each tool's spool goes to its own slot, and the active spool is left to them.
- **When.** Each change, as it happens; one made during a print is a swap, split where it happened. When the bridge connects, it assigns what's active unless a print is running or paused, and after a reconnect only what changed while it was away. Clearing the active spool changes nothing. Nothing is queued while 3D Print Log is unreachable: pick the spool again once it is.

Linking a material to its Spoolman spool needs 3D Print Log to store the Spoolman id (HoffmanEngineering/3d-print-log-api#208); until it does, tag UIDs are the link.

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

### Signing in (pairing)

`printlog-bridge pair` signs the bridge in to your account with nothing to copy:

```
$ printlog-bridge pair
To pair this bridge with your 3D Print Log account, scan the QR code with your phone, or open
  https://auth.3dprintlog.com/activate
and enter the code  QWER-TYUI
(or open https://auth.3dprintlog.com/activate?user_code=QWER-TYUI)
The code works for 15 minutes.

<QR code>
```

Approve it on the page that opens, signed in to 3D Print Log. The bridge then stores its own device token in `device_token` in the state directory, readable only by you, and, for each `[printer]` without a `printer_id`, asks which of your printers it logs to, or creates a new one filled in from the printer (its host name, Klipper's kinematics and extruder count, a Bambu printer's model), and writes the number into the config. Restart the bridge to use the token.

- `--yes` (or no terminal, as in Docker) asks nothing: it pairs, then lists your printers and says which `printer_id` to set. `--name` sets the name 3D Print Log shows for the bridge (default `printlog-bridge on <host name>`, or `PRINTLOG_BRIDGE_NAME`), and `--switch-printers` also asks again for printers that already have a `printer_id`.
- Running it again pairs the same bridge again: 3D Print Log keeps one entry for it, with a new token, and the old token stops working.
- The device token works only for what the bridge does (logging prints, its spools and its heartbeat), not for the rest of your account, and you can remove it in 3D Print Log at any time. A bridge whose token was removed says "re-pair needed" in its log, `status` and `doctor`, keeps every job in its outbox, and carries on by itself within a minute of `printlog-bridge pair`.
- `run` with no API key and no device token pairs by itself, with the QR code in its log, so a Docker container or an add-on needs no setup beyond its printers.
- Pairing is off on 3D Print Log until its app can list and remove paired bridges. Until then `pair` says so, and you use a personal API key instead.

`store-key` stays for scripted installs and for servers without pairing: it stores a personal API key from [3dprintlog.com/api-keys](https://www.3dprintlog.com/api-keys) in `api_key` in the state directory, readable only by you. The bridge looks for a key in the `PRINTLOG_API_KEY` environment variable, then that file, then `api_key` in `[bridge]`; a device token wins over all of them, since the bridge never sends both. A key in `[bridge]` still works, but in local mode the config sits in Klipper's config folder, which Moonraker serves to anyone who can open Mainsail or Fluidd: `doctor` warns about it, and `store-key` moves it out of the config for you.

## Privacy and security

The bridge sends 3D Print Log the print's file name (without its folders), times, duration, outcome, filament used, the slicer's estimates, its thumbnail, its slicer settings with hosts and credentials blanked (unless you turn them off), and, unless you turn snapshots off, one webcam frame when it ends; with Pro and moonraker-timelapse, also the print's timelapse video. Of the G-code file it reads only the header and the settings block after the G-code, never the toolpaths, and it never uploads the file. It connects only to Moonraker, the webcam Moonraker names, and 3D Print Log over HTTPS (with a timelapse, also the file storage 3D Print Log names for it; while pairing, 3D Print Log's sign-in service; and, to find a printer whose address changed, to Moonraker's port on your local network), and opens one port: the read-only [local page](#the-local-page), on `127.0.0.1:7180` unless you set otherwise. Paired, it also listens on UDP port 2021 for Bambu Lab printers' announcements. What it finds on your network is reported to 3D Print Log only for printers an adapter recognizes. Keys, passwords and tokens never reach its log, status or support bundle. [docs/privacy.md](docs/privacy.md) lists every call and field, and the test that holds each guarantee.

## Config

One `printlog-bridge.cfg`, looked up in this order: `--config`, `$PRINTLOG_BRIDGE_CONFIG`, `~/printer_data/config/`, `~/.config/printlog-bridge/`. See [printlog-bridge.example.cfg](printlog-bridge.example.cfg). The state directory is `state_dir` in `[bridge]`, then `$PRINTLOG_BRIDGE_STATE_DIR`, then `~/.local/share/printlog-bridge`.

Each printer is a `[printer <name>]` section, so one bridge can watch several. Set `printer_id` in each to the printer's number in 3D Print Log, the end of its page's address (`https://www.3dprintlog.com/printers/42`); pairing will set it for you.

In remote mode, if Moonraker has `[authorization]` enabled and the bridge's host isn't in `trusted_clients`, set `moonraker_api_key` in the printer's section to Moonraker's API key. The local socket is always trusted, so local mode never needs one. Moonraker accepts one bridge per printer; a second one waits until the first stops.

Each print gets the largest thumbnail the slicer embedded in its file (OrcaSlicer, PrusaSlicer, Cura and SuperSlicer all embed one) as its picture when it starts. When it ends, the bridge also adds one frame from the printer's first webcam (its `snapshot_url` in Mainsail or Fluidd); for a failed print that frame becomes the picture shown. Set `snapshots = false` in the printer's section to never ask the webcam for anything. Pictures wait in the outbox with the print's other events (the image files in `images/` in the state directory), and never hold up logging the print: one that takes longer than 10 seconds, is over 10 MB or that 3D Print Log refuses (a print holds 5 pictures on the free plan) is skipped. A Moonraker or webcam address with a password or token in it is shown with them hidden, in the log, `status` and `doctor`.

### Bambu Lab printers

`adapter = bambu` watches a Bambu Lab printer the way Bambu Studio does in LAN mode: the printer's own MQTT broker (TLS, port 8883) for its status, and its FTPS server (port 990) for the job's 3MF, from which it takes the slicer's grams per filament, the plate's thumbnail and the slicer settings. It runs anywhere on the printer's network (Docker, a NAS, a Pi), and never sends the printer a command or writes a file to it.

```ini
[printer x1c]
adapter = bambu
printer_id = 44
host = 192.168.1.50
serial = 01S00C123456789
access_code = 12345678
```

1. On the printer, turn on **LAN Only Mode** and then **Developer Mode** (X1 and H2: Settings > LAN Only; P1: Settings > WLAN; A1: Settings > LAN Only Mode). Since Bambu's 2025 "Authorization Control" firmware, third-party software needs both to read the printer's status and files; Bambu Studio keeps working in LAN mode, but Bambu Handy and the cloud don't.
2. The **access code** is on the same page. The **serial** is on the printer's device information page, on its sticker, in Bambu Studio's device list, or in `printlog-bridge discover`, which lists the Bambu printers that announce themselves on your network.
3. Keep the config where nothing serves it: the access code lets anyone on your network control the printer. It is treated like a key (never logged, traced or bundled), and `doctor` warns when it's in a folder Moonraker serves.

Each job is logged when it starts printing (after the printer has heated and leveled; its start time is the printer's), with the slicer's estimates from the 3MF, and completed when it ends: finished, cancelled (from the screen or the slicer) or failed (with the printer's error code in the print's notes). Usage is the slicer's grams per filament, scaled by how far a cancelled or failed job got, and marked as an estimate in 3D Print Log, since the printer doesn't measure what it feeds. With an AMS, each filament is logged on its tray's slot: slot 0 is the external spool, slots 1-4 the first AMS's trays A1-A4, 5-8 the second's, and so on (17-24 for AMS HT units, 25 for the H2D's second external spool). The AMS's RFID readings (a Bambu spool's tag, material and color) are passed on like any slot reader's, so a spool with that tag in 3D Print Log is assigned to its tray.

Without the 3MF (`ftps = false`, Developer Mode off, or a file the printer no longer has), prints are logged with their outcome and times but no filament usage. Bambu's local connection keeps no job history, so **a job that starts and ends while the bridge is down isn't logged**; one that ends while it's down is logged when it's back, from what the printer still shows. The printer's TLS certificate is self-signed by Bambu: the bridge pins the one it sees first and refuses a different one (set `tls_fingerprint` to accept a new printer). This adapter is built from Bambu's and the community's documentation of the protocol and hasn't been checked against a recorded session yet; if something looks wrong, run with `trace = true` and attach the support bundle to an issue.

### Prusa printers (PrusaLink)

`adapter = prusalink` watches a Prusa printer through PrusaLink, the local web API built into its firmware (MK4/MK4S, MK3.5/3.9, Core One, MINI/MINI+, XL). A Prusa printer has no computer of its own to run the bridge on, so run it in Docker, on a NAS or on a Raspberry Pi on the same network (see [Run it in Docker](#run-it-in-docker-remote-mode)); you don't need Klipper or Moonraker for it. It only ever reads from the printer: it never starts, pauses or stops anything, and never uploads or deletes a file.

```ini
[printer mk4s]
adapter = prusalink
printer_id = 45
host = 192.168.1.60
password = <the password the printer shows>
```

1. On the printer, open **Settings > Network > PrusaLink**, turn PrusaLink on if it's off, and note the **user** (`maker`) and the **password** shown there. Firmware that asks for an API key instead (older MINI firmware) takes `api_key = ...` in place of `password`. Prusa Connect can stay on: they don't get in each other's way.
2. Find the printer's address on the same screen or under Settings > Network, and give it a fixed one (a DHCP reservation in your router), so `host` stays right.
3. Keep the config where nothing serves it, as for any password. `password` and `api_key` are treated like keys (never logged, traced or bundled), and `doctor` warns when they're in a folder Moonraker serves.
4. Run `printlog-bridge doctor`: it says whether the printer answered, and which firmware it runs, or why it refused.

The bridge asks the printer for its status every 5 seconds while a job is under way and every 30 seconds between jobs (`poll_interval`, `idle_poll_interval`), and backs off up to a minute while it can't reach it. Each job is logged when it starts printing (its start time is the printer's), with the slicer's estimates, and completed when it ends: finished, stopped (cancelled) or failed (an error on the printer). Usage is the slicer's estimate per tool, scaled by how far a stopped or failed job got, and marked as an estimate in 3D Print Log, since the printer doesn't measure what it feeds. On an XL each tool is its own slot (T1 is slot 0, T5 slot 4); with an MMU3, each filament is.

The estimates, thumbnail and slicer settings come from the job's file. For a `.bgcode` file (PrusaSlicer's default for these printers) the bridge reads only its start, up to the G-code: usually a few hundred kilobytes. A plain `.gcode` file keeps its estimates at its end, so it is read whole, as it prints, up to 64 MB; a larger one is logged without estimates. `files = false` turns these reads off; prints are then logged with outcome and times but no usage. PrusaLink keeps no job history, so **a job that starts and ends while the bridge is down isn't logged**; one that ends while it's down is logged when it's back, from what the printer still shows (a finished or stopped job stays on its screen until someone dismisses it). This adapter is built from Prusa's published PrusaLink API and firmware sources and hasn't been checked against a recorded session yet; if something looks wrong, run with `trace = true` and attach the support bundle to an issue.

### OctoPrint

`adapter = octoprint` watches a printer run by [OctoPrint](https://octoprint.org) (an OctoPi, or OctoPrint on any computer), over OctoPrint's REST API and its push socket. Run the bridge in Docker, on a NAS or on another computer on the same network. It only ever reads from OctoPrint: it never starts, pauses or cancels a print, never uploads or deletes a file, and never sends G-code.

```ini
[printer ender]
adapter = octoprint
printer_id = 46
url = http://octopi.local
api_key = <a key from OctoPrint>
```

1. In OctoPrint, open **Settings > Application Keys** and make a key for the bridge (or copy your user's key from **Settings > API Key**). The user it belongs to needs the Status, File List, File Download and Settings Read permissions; an admin has them all. Put it in `api_key`.
2. `url` is the address you open OctoPrint at, with its port when it isn't 80 (`http://192.168.1.70:5000`) and its path when it's behind a prefix (`http://pi.local/octoprint`). Give the host a fixed address (a DHCP reservation in your router).
3. Keep the config where nothing serves it. `api_key` is treated like any key (never logged, traced or bundled), and `doctor` warns when the config is in OctoPrint's `uploads` folder, which OctoPrint serves.
4. Run `printlog-bridge doctor`: it says whether OctoPrint answered, its version, whether it's connected to the printer, and whether its Webhooks plugin still logs to 3D Print Log (below).

Each print is logged when OctoPrint starts printing it, with its start time from OctoPrint's print chart, and completed when it ends: done, cancelled, or failed (with the firmware's error in the print's notes). Usage is the slicer's estimate per tool (PrusaSlicer's and OrcaSlicer's `filament used [g]` and `[mm]`, read from the end of the file; else the length per tool from OctoPrint's own analysis), scaled by how far a cancelled or failed print got, and marked as an estimate in 3D Print Log: OctoPrint doesn't measure what the printer feeds. The thumbnail is the one the slicer embedded in the file (or the PrusaSlicer Thumbnails plugin's), and with a webcam set up in OctoPrint (Settings > Webcam & Timelapse, a snapshot URL), a frame is added when the print ends (`snapshots = false` turns that off). The bridge reads two byte ranges of each print's file from OctoPrint, its first 256 KB and last 512 KB, never the toolpaths; `files = false` turns that off, and prints are then logged with OctoPrint's estimates only. A print from the printer's own SD card has only those.

OctoPrint keeps no job history, so **a print that starts and ends while the bridge is down isn't logged**. One that ends while the bridge is down is logged when it's back, from OctoPrint's print chart (when and how it ended), as long as OctoPrint itself wasn't restarted.

**Moving from the Webhooks plugin.** If you set up 3D Print Log's [OctoPrint webhook](https://www.3dprintlog.com/docs/octoprint-webhook) before the bridge, both would log each print. The bridge reads OctoPrint's settings each time it connects, and while the Webhooks plugin has a hook posting to 3dprintlog.com it warns in its log, `status` and `doctor`: turn that hook off in OctoPrint's Settings > Webhooks. It never shows the hook's settings, which hold your API key.

### Timelapse videos (Pro)

If the printer runs [moonraker-timelapse](https://github.com/mainsail-crew/moonraker-timelapse) and your 3D Print Log account has Pro, the video it renders after a print is attached to that print. The bridge waits up to 10 minutes after the print ends for the render (a render that `TIMELAPSE_RENDER` in your end macro finished just before the end counts too), downloads the video from Moonraker and uploads it straight to 3D Print Log's file storage. The print itself is completed right away; the video follows. Without Pro the bridge only asks whether you have it, and never downloads or uploads the video.

Set `timelapse = false` in the printer's section to never attach videos. A video over `max_attachment_mb` in `[bridge]` (100 by default, at most 200, 3D Print Log's limit) is skipped, as is one that would leave the state directory's disk with less than 50 MB free. Videos wait in `attachments/` in the state directory until they're uploaded, and while one uploads, other prints' starts and ends go first. A print holds 5 files; a video 3D Print Log refuses is skipped with a line in the log. A render that finishes while the bridge isn't running isn't attached.

### Printers found on your network, and connected in the app

A paired bridge checks in with 3D Print Log every few minutes (`PUT /api/bridges/me`, [privacy](docs/privacy.md#discovery-and-check-in)). It says which printers it recognizes on your network, and 3D Print Log lists them under "Found on your network" on the bridge's page. **It never watches a printer just because it found it**: you connect one in the app (to an existing printer of yours or a new one), and the bridge starts watching it within a minute, with no restart and no config. Disconnect it there and the bridge lets it go. On a shared network (a dorm, a makerspace) a neighbor's printer is never logged to your account.

How it looks, while it runs:

- **Always, and cheaply:** an mDNS query every 5 minutes for Moonraker (`[zeroconf]`), PrusaLink and OctoPrint, and listening for Bambu Lab printers' announcements on UDP port 2021.
- **Only when you tap "Scan now" in the app:** the per-address scan `printlog-bridge discover` does, on this computer's /24 and those of the printers it knows. A scan looks like a port scan to network security tools, so it never runs on its own.

It reports only printers an adapter recognizes (Moonraker, Bambu Lab, PrusaLink, OctoPrint), never any other host. Printers in the config file are reported too, as "managed by config": they stay read-only in the app, and the config file wins when both name the same printer. `discovery = false` in `[bridge]` turns the looking off; printers you connect in the app by address are still watched.

**A password or access code** (a Bambu Lab printer's access code, a PrusaLink password, an OctoPrint API key, the API key of a Moonraker with `[authorization]`) is entered in the app, next to the printer on the bridge's page. The app encrypts it in your browser to this bridge's own key, so 3D Print Log stores and forwards only something it can't read, and only this bridge can open it ([privacy](docs/privacy.md#printer-credentials-entered-in-the-app)). The bridge starts watching within a minute. If the printer refuses it, or the bridge can't open it, the app says so next to the printer, and so do `status` and `doctor`.

**The bridge's key.** The bridge makes its key the first time it runs paired (or when you `pair` it), in `credential-key.pem` in its state directory, readable only by the bridge's user. It never leaves the bridge. `printlog-bridge status`, `doctor` and `pair` show its fingerprint, such as `Encryption key: aed0 3e9a 8282 33b2`. A key the bridge reports after it was first paired (an update from a bridge without one, a lost state directory, `printlog-bridge rotate-key`) is held by 3D Print Log until you confirm it on the Bridges page: check that the app shows the same characters as `status`, then confirm. Until then `status` says `waiting for confirmation in the app`. Pairing again keeps the key. `printlog-bridge rotate-key` makes a new one (confirm it in the app, then enter the printers' credentials there again, since nothing sealed to the old key opens with the new one). The key needs the `cryptography` package, which the Docker image and the Home Assistant app include, and which installs with the bridge on 64-bit PCs and 64-bit Arm. On 32-bit Arm `install.sh` installs it when it can (on Raspberry Pi OS it can); where it can't (a Pi Zero or Pi 1, say) the bridge runs without it, `doctor` says so and how to add it, and credentials go in the config file as below.

**In the config file instead** (the way before the app took credentials, and still supported): add a section with the number the bridge's log and `printlog-bridge status` give the printer (`App printer 7 ... needs credentials`):

```ini
[app_printer 7]
access_code = 12345678
```

Save it and the bridge starts watching within a few seconds. **A value in the config file wins** over one entered in the app for the same field: the config file is the bridge's own, it's the rule for printers too, and setups made before the app took credentials keep working unchanged. `status` shows where each credential came from (`access_code: from [app_printer 7] in the config file, which overrides the app's`); remove the line to use the app's.

**Networking.** Multicast and broadcast don't cross Docker's default (bridge) network, so finding printers needs the host's network: `network_mode: host` (the one-line install, the Compose file and the examples above use it), Unraid's host networking (the template's default), or the Home Assistant app (always on the host's network). On Docker Desktop (Windows, macOS) the container can't see your LAN this way; connect printers in the app by address instead. Besides its [local page](#the-local-page) (`127.0.0.1:7180` unless you set otherwise), the bridge opens no TCP port; it listens on UDP 2021 only for Bambu's announcements.

A bridge paired before this release lacks the permission to check in (`bridge:sync`): it pairs again once by itself, with the QR code in its log; if that isn't approved, it says so in `status` and `doctor` and keeps logging the printers in its config. `printlog-bridge pair` gives it the permission at any time.

### The local page

The bridge serves a small page with what `printlog-bridge status` says (whether it runs, its pairing and key fingerprint, the last check-in, each printer's adapter and connection), its version, the code and QR code to approve while it pairs, and, when you click **Run the checks**, `printlog-bridge doctor`'s checks (run in the background, at most every 30 seconds; the page refreshes until they're done). It refreshes itself. It isn't a second place to manage the bridge: printers are added, changed and given credentials in 3D Print Log (or the config file), and the page has no form and changes nothing; it answers `GET` only.

```ini
[bridge]
status_page = 127.0.0.1:7180          ; the default: this computer only. off turns it off
; status_page = 0.0.0.0:7180          ; every address of this computer: your whole network
; status_page_clients = 192.168.1.10  ; only these addresses may open it
```

`PRINTLOG_BRIDGE_STATUS_PAGE` and `PRINTLOG_BRIDGE_STATUS_PAGE_CLIENTS` set the same and win over the file. A port alone (`status_page = 8080`) keeps it on this computer. On a printer's Pi, open it over SSH (`ssh -L 7180:127.0.0.1:7180 pi@voron.local`, then <http://127.0.0.1:7180/>) or set an address. The Home Assistant app shows it as its panel (Home Assistant's ingress), for Home Assistant's own proxy only. The address is read when the bridge starts; change it, then restart.

**Before you open it to your network**: anyone who can reach the page sees the bridge's status, your printers' names and addresses, and, while the bridge pairs, the code to approve it. Whoever approves that code first gets the bridge in their account, so on a shared network (a dorm, a makerspace, an office) keep the page on `127.0.0.1` or limit it with `status_page_clients`. `127.0.0.1` is only private on a computer that's yours alone: on a shared host (a shared server, a NAS other people log in to), any user of it can open the page and read the code while the bridge pairs. Pair from a host you control, or set `status_page = off` there. It never shows a key, token, password, access code or the bridge's private key: only the key's fingerprint, as `status` does. It answers only a request for its IP address, `localhost`, a name without dots or a local name (`.local`, `.lan`, `.home`, `.internal`, `.home.arpa`), so a web page can't reach it by pointing a name of its own at it (DNS rebinding). It refuses a request another site's page makes (`Sec-Fetch-Site` other than `same-origin` or `none`, or a `Sec-Fetch-Dest` other than a page or frame), so a link or image elsewhere can't make it run its checks. Every answer forbids scripts, framing by another site, caching and content sniffing. A connection gets 10 seconds to send its request, and one from an address `status_page_clients` doesn't list is closed at once. A page that fails, or a port that's taken, never stops the bridge: it logs a warning and carries on.

### When a printer's address changes

Most printers get their IP address from the router (DHCP), and it can change, after a power cut say. In remote mode the bridge finds the printer again without you editing the config. The first time it connects, it remembers who the printer is: the MAC addresses Moonraker reports, in the printer's state file. When the printer can't be reached for 3 minutes, the bridge looks for that same printer on its last known network (the /24 around its last address): first over zeroconf, when Moonraker has `[zeroconf]` set up, then by asking each address on Moonraker's port for `/server/info`, at most once every 10 minutes. When it answers somewhere new, the bridge carries on there and remembers the new address in its state; `status` and the log say where it moved (`moved from 192.168.1.87 to 192.168.1.91`). Your config is never changed, and changing `url` in it starts afresh.

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

### Moving from Moonraker's notifier

If you set up 3D Print Log's Moonraker notifier before the bridge (a `[notifier ...]` section in `moonraker.conf` whose `url` posts to `api.3dprintlog.com`), both would log each print. The bridge reads Moonraker's config each time it connects, and while such a section is there it warns in its log, in `status` and in `doctor`, naming the section and its file:

```
  Warnings:
    - Moonraker's [notifier 3d_print_log] in moonraker.conf also logs prints to 3D Print Log, so each print can be logged twice. The bridge logs them itself: remove that section (its [notifier ...] line and every line up to the next section) and restart Moonraker.
```

The bridge never edits `moonraker.conf` itself. Once you remove the section and restart Moonraker, the bridge reconnects and the warning goes away. It never shows the notifier's URL, which holds your API key.

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

### Probing an unsupported printer

Got a printer the bridge doesn't support yet (a Creality K1, K2 or Hi on stock firmware, an Anycubic Kobra or Photon, an Elegoo or other resin printer, a HeyGears)? `printlog-bridge probe` records what it offers on your network, so an adapter can be written for it. It's strictly read-only: it never sends anything that changes the printer's state (no print, pause, cancel, move, heat, upload, delete or firmware command), and never contacts 3D Print Log. Leave the printer on, ideally printing, and run it from a computer on the same network:

```sh
printlog-bridge probe 192.168.1.50 --redact             # the printer's IP address
printlog-bridge probe 192.168.1.50 --seconds 120 --redact   # listen longer, during a print
```

From a checkout of this repository, with [uv](https://docs.astral.sh/uv/) (Windows Git Bash, macOS or Linux): `uv run printlog-bridge probe 192.168.1.50 --redact`. The first run creates `.venv` and installs the bridge into it; on Windows, set `export UV_LINK_MODE=copy` first if the checkout is on another drive than uv's cache. Without uv: `python -m venv .venv`, `.venv/Scripts/pip install -e .` (`.venv/bin/pip` on macOS and Linux), then `.venv/Scripts/printlog-bridge probe ...`. In Docker, the host's network is needed to hear announcements: `mkdir probe && docker run --rm --network host -v "$PWD/probe:/data" ghcr.io/hoffmanengineering/printlog-bridge:edge probe 192.168.1.50 --redact` writes the file into `probe/`.

For `--seconds` (30 by default) it:

- listens for what the printer announces: mDNS (and asks it which services it has), SSDP, Bambu Lab's NOTIFY, and the answer to the Chitu/SDCP discovery broadcast (`M99999` on UDP 3000);
- tries a short list of ports that printers are known to use (80, 443, 1883, 3000, 3030, 4408, 4409, 6000, 7125, 8000, 8080, 8883, 9883, 9999, 18910 and a few more; `--help` lists each with what it's suspected to be, and `--port` adds others). It's not a port scan;
- on each open one, asks only read-only status questions: `GET /` and documented status endpoints (Moonraker, PrusaLink, OctoPrint, Anycubic's `/info`), Creality's websocket on 9999 (its status `get` requests, never the current file's), SDCP's status and attributes, an MQTT broker that needs no login (subscribed to everything, read-only; one that wants credentials is left alone), and, only with `--photon` for an Anycubic Photon, its `sysinfo` and `getstatus` on port 6000. It never follows a redirect.

Everything it may send is on an allow-list that `printlog-bridge probe --help` prints, and that it prints again before it starts; each message is checked against it before it goes out (one that isn't on it is recorded as refused and never sent), and tests check that nothing else ever reaches a fake printer. Ctrl+C stops it early and still writes what it recorded.

It writes one JSON file in the current folder, `probe-<address>-<time>.json` (`probe-redacted-<time>.json` with `--redact`; or `--out FILE`), with every request, answer and announcement and when it happened (bodies and messages are cut at 64 KB and 16 KB), and prints a summary. The summary always hides credentials, serial numbers, MAC addresses, Wi-Fi, host and file names, and leaves mDNS records and raw answers to the file. The file holds everything unless you add `--redact`: then credentials are removed; serial numbers, MAC and IP addresses, Wi-Fi and host names and file names are replaced with placeholders that stay the same throughout the file (`<serial-1>`, `<mac-1>`, `<ip-1>`, `<ssid-1>`, `<host-1>`, `<file-1>`); and binary payloads are dropped. The summary says which you got. Please [open an issue](https://github.com/HoffmanEngineering/printlog-bridge/issues/new) for your printer and attach the redacted file, after a look through it: a printer's name, for one, can still be in it.

On Windows, the firewall drops unsolicited UDP for Python unless it's allowed on private networks (Windows asks the first time; say yes for private networks), and the SSDP Discovery service holds UDP port 1900, so NOTIFY announcements there may not be heard. The probe carries on and says so in its notes. A Docker container needs `--network host` for the same reason.

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

CI runs both on Python 3.9 and 3.12. It also runs `shellcheck` on the shell scripts, and `tests/install/smoke.sh`, which runs `install.sh` and `uninstall.sh` in a throwaway home inside a `python:3.9-slim` container, against a fake `systemctl`, and checks that Moonraker's files end up byte for byte as they started. The file edits themselves are in `scripts/install_helper.py`, tested by `tests/test_install_helper.py`. `tests/install/docker_install_smoke.sh` runs the one-line install, `scripts/docker-install.sh`, against a fake `docker` (with `/bin/sh` as dash, and the runner's `docker compose` checking the file it writes): the Compose project, running it again, pairing, `--uninstall`, and each setup it refuses. A `v*` tag attaches it to the GitHub release with that version's image tag set in it (`docker.yml`'s `release-script` job).

The Docker workflow builds the image for each platform and runs `tests/docker/smoke.sh` against it: `--version`, `store-key`, `doctor`, `run`, `status` and `pair` inside the container run with `--user` (as installs from before the image handed over its folder itself), then a new install with no config and a root-owned folder, and `PUID`/`PGID`, against three fake Moonrakers, a fake 3D Print Log API and a fake Auth0 on the runner (`tests/docker/serve_fakes.py`), so it never calls the real API. amd64 and arm64 run natively (arm64 on GitHub's Arm runner), arm/v7 under QEMU. It then pushes all three to GHCR from `main` (`edge`) and from `v*` tags, and `tests/docker/check_published.sh` checks the result without logging in: the tag pulls, has every platform, and each one starts. Run the smoke test yourself with `docker build -t printlog-bridge:dev . && tests/docker/smoke.sh printlog-bridge:dev` (add `--platform linux/arm/v7` to the build, and `linux/arm/v7` after the image, to try another platform under QEMU).

`tests/fixtures/moonraker/` holds REAL answers from a Snapmaker U1 and an Infimech EX, which `tests/test_real_captures.py` runs the adapter against. `tests/trace_replay.py` replays a trace as Moonraker, so a recorded print becomes a regression test: see `tests/test_trace_replay.py`, which replays a SYNTHETIC U1 print from `tests/fixtures/traces/`.

`pytest -m integration` runs the bridge against a real Moonraker, built from source in Docker (set `MOONRAKER_REF` to pick a tag or commit; the default is `v0.9.3`). The first run builds the image, which takes a minute or two. CI runs it against the last release and a pinned recent commit. To support another kind of printer, see [docs/adapters.md](docs/adapters.md).

## License

[MIT](LICENSE)
