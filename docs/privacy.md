# Privacy and security

What printlog-bridge sends, where it connects, and how it keeps your keys. Each guarantee below is what the code does today, and a test fails if that changes: most are in [`tests/test_privacy.py`](../tests/test_privacy.py), and the others are named where they come up.

## What leaves the printer

The bridge sends 3D Print Log exactly these calls, and nothing else:

| Call | When | What it carries |
| --- | --- | --- |
| `GET /api/Users/me` | `doctor` | nothing; checks the key |
| `GET /api/Printers/{id}` | `doctor`; at start and every 5 minutes; when a print starts and ends | nothing; checks `printer_id`, and reads which spool is loaded in each slot |
| `GET /api/Users/me/user-settings` | first print after a start | nothing; reads your default visibility and comment settings |
| `PUT /api/Connections/{instance}` | at start, then every 5 minutes (the heartbeat) | `kind` (`moonraker`), `displayName` (`printlog-bridge (<printer name from your config>)`), `agentVersion`, `printerId`, and the printer's health: see [The heartbeat](#the-heartbeat) |
| `GET /api/Prints/claimable` | a print starts (and when a print ends that the bridge saw no start for), unless `adopt_prints = false` | `printerId` and `createdAfter` (the start time less `adopt_window`): looks for a print you made yourself, with the Slic3r uploader, the Cura plugin or by hand, to log the job to instead of a new one. The file name is compared on the bridge, not sent |
| `POST /api/Prints/{id}/claim` | a print starts and one of yours matches it | `externalSource`, `externalId`, `connectionInstanceId` and `startDate`, as for `POST /api/Prints`; your print keeps everything else |
| `POST /api/Prints` | a print starts and there's no print of yours to adopt (never with `create_prints = false`) | `printerId`, `title` (made from the file name), `fileName` (the file's name, without its folder), `startDate`, `status`, `viewStatus` and `allowComments` (your account defaults), `externalSource` (`moonraker`), `externalId` and `connectionInstanceId` (a random id this bridge made for the printer, plus Moonraker's job id), the slicer's estimated print time and filament (length and weight, and the weight per tool on a multi-tool file), when the file has them, each tool's row linked to the spool loaded in its slot, and `notes` when the printer's RFID reader disagrees with a spool's material or color |
| `GET /api/Prints/{id}` | a print ends | nothing; reads what you changed in the app |
| `POST /api/Prints/{id}/complete` | a print ends | `status` (the outcome), `endedAt`, `printTimeInSeconds`, and per tool (`slot`) the filament length Klipper fed (linked to the spool you chose in the app), split into a row per spool when a tool's spool changed mid-print |
| `PATCH /api/Prints/{id}` | a print fails or is cut short, a spool changed mid-print, or a print was paused for a filament change with no spool assigned | `notes`: a line saying why, with Klipper's error message when there is one; which spools a tool changed between, where; how to split a filament change's usage |
| `POST /api/Prints/{id}/image` | a print starts, and when it ends | the slicer's thumbnail (at start), one webcam frame (at end, unless `snapshots = false`) |
| `GET /api/Filaments/by-tag/{uid}` | `PRINTLOG_SET_SPOOL TAG=...`, or a Snapmaker U1 reads a spool's RFID tag | the tag's UID |
| `PUT /api/Printers/{id}/slots/{slot}` | `PRINTLOG_SET_SPOOL`, or a U1 tag that matches one of your spools | `filamentId` (the spool), `slotLabel` (`T0`, `T1`...) |
| `DELETE /api/Printers/{id}/slots/{slot}` | `PRINTLOG_CLEAR_SPOOL` | nothing |
| `PUT /api/Printers/{id}` | a spool is assigned to a tool beyond the printer's slot count in 3D Print Log | the printer as 3D Print Log returned it, with only `slotCount` raised to the printer's tool count |

Every request also carries the API key (`X-Api-Key`) and `User-Agent: printlog-bridge/<version>`.

That is all. In particular:

- **No G-code leaves the printer.** The bridge never reads, downloads or uploads a G-code file. The thumbnail is read from the picture Moonraker extracted into `.thumbs/`, and only from a file named as a picture (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.bmp`); anything else is skipped before it is opened. A file that isn't a picture is never sent, whatever its name. Uploading the G-code is a planned opt-in (printlog-bridge#15); until it exists, nothing uploads it.
- **No folder names or paths.** `fileName` is the file's own name; the folders it sits in on the printer are dropped.
- **No other printer data.** Temperatures, positions, macros, the rest of `printer.cfg` and the rest of Moonraker's history stay on the printer.
- **The webcam is asked only when a print ends**, for one frame, and never when `snapshots = false` is set for the printer.

Tests: `TestSpoolCalls` assigns spools by tag and by id and checks each call and field, and that the printer is sent back as read. `TestWhatIsSent` runs a whole print through the bridge and checks every call against this table and every field against these lists, that the only images uploaded are the thumbnail and the webcam frame, and that Moonraker was never asked for the G-code file. `TestGcodeIsNeverRead` points a thumbnail at the G-code file itself, and `test_with_snapshots_off_the_webcam_is_never_asked` does what it says.

### The heartbeat

Each heartbeat also tells 3D Print Log how the bridge is doing, so the app can show a printer whose prints aren't getting through, and so 3D Print Log can see which Klipper and Moonraker versions cause trouble. It goes only to 3D Print Log, in the same request as the heartbeat. **There is no third-party telemetry**: no analytics service, no crash reporter, nothing else is contacted. These are all of its fields, sent with every heartbeat (`null` when not known yet):

| Field | What it is |
| --- | --- |
| `mode` | `local` (Moonraker's socket on this host) or `remote` (Moonraker over the network). Never the address. |
| `klipperVersion` | Klipper's version, as Moonraker reports it (`software_version` in `printer.info`), at most 50 characters |
| `moonrakerVersion` | Moonraker's version (`moonraker_version` in `server.info`), at most 50 characters |
| `printerModel` | the printer model the slicer wrote in the last job's file (`printer_model`, such as `InfiMech EX`), when it wrote one, at most 100 characters |
| `outboxDepth` | how many events and pictures for this printer are waiting in the outbox to be sent |
| `outboxOldestAgeSeconds` | how long the oldest of them has waited, in seconds |
| `lastError` | the last warning or error the bridge logged for this printer, scrubbed (below), at most 300 characters |
| `lastErrorAt` | when that was, in UTC |
| `errorCount` | how many warnings and errors it logged for this printer since the last heartbeat that got through |
| `snapshots` | whether a webcam frame is added to each print (`snapshots` in the config) |
| `slicerSettings` | whether each print's slicer settings are sent; always `false` for now (printlog-bridge#36) |

`kind` is the adapter (`moonraker`). A problem that names no printer (the state directory is full, say) counts for every printer.

**No host names, IP addresses, file names or keys are in a heartbeat.** The bridge asks Moonraker for `printer.info` only for Klipper's version, and drops the host name beside it. Before `lastError` leaves the printer, the bridge removes from it every key and password it knows of, every URL, path, IP address, `host:port`, dotted name (`u1.local`, `benchy.gcode`), quoted text (which is how file names turn up in messages) and the names of your printers, leaving only what went wrong, such as `couldn't send the heartbeat: can't reach 3D Print Log at <url>: Cannot connect to host <ip> ...`. The bridge's own log keeps the full message.

Tests: `TestWhatIsSent` runs a print with a failing heartbeat and checks that every heartbeat carries exactly these fields, what each one holds, and that none holds the API or Moonraker key, a password, Moonraker's address or host name, the file name or the state directory. `tests/test_health.py` checks the scrubbing of each kind of host, address, path and file name, and `tests/test_heartbeat.py` each field.

## Where it connects

The bridge only makes outbound connections, to:

1. **Moonraker**: its Unix socket in local mode, or the `url` you set in remote mode (HTTP and WebSocket, on your network). In remote mode it also downloads the thumbnail from Moonraker's file API.
2. **The webcam's `snapshot_url`**, as Mainsail or Fluidd have it set in Moonraker (usually the printer itself), when a print ends.
3. **3D Print Log**, at `api_url` (`https://api.3dprintlog.com` by default), **over HTTPS only.** The config refuses a plain `http://` `api_url`, except to this computer itself (`localhost`, `127.0.0.1`, `::1`), for a test server or a TLS proxy on the same host. With `allow_http_on_lan = true` (or `PRINTLOG_ALLOW_HTTP_ON_LAN=1`) it also accepts plain http to a private-network IP address (RFC 1918 `10/8`, `172.16/12` and `192.168/16`, link-local `169.254/16` and `fe80::/10`, IPv6 unique local `fc00::/7`), for a development API on your own network; the API key then travels unencrypted on that network, so `run` logs a warning every time it starts and `doctor` shows one. A public address, a host name, or any other address Python calls private (Teredo, documentation and reserved ranges) is refused either way.
4. **Moonraker's port on your local network, to find a printer again** (remote mode, unless `discovery = false`). When a printer can't be reached for 3 minutes (`discovery_after`), or a different printer answers at its address, the bridge looks for it, at most once every 10 minutes (`discovery_interval`, never more often than every 30 seconds). It sends one mDNS query for `_moonraker._tcp` to the local multicast group (224.0.0.251), then asks each address of the printer's last known /24 for `GET /server/info` on Moonraker's port, and a Moonraker that answers for `GET /machine/system_info`, to compare its MAC addresses and instance id with the printer's. `discovery_zeroconf = false` skips the mDNS query, and `discovery_networks` names the networks to scan instead. It only ever looks on private IPv4 networks (RFC 1918), each a /24 or smaller (the config refuses anything else), and reads nothing else. `moonraker_api_key`, when set, goes only to an address that has already answered as Moonraker, or asked for authorization, on that port. It never connects its WebSocket, or logs anything from, a printer whose identity doesn't match. What it remembers (the MAC addresses, Moonraker's instance id and the printer's IP addresses) stays in the printer's state file; never a password from the `url`.

`printlog-bridge discover`, which you run yourself, does the same over the networks you name with `--subnet` (or the configured printers' and this computer's own), and also reads `/printer/info` and `/printer/objects/list` from each Moonraker that answers, to show its host name, Klipper version and tool count. It sends no keys.

It **never listens on a port**: nothing can connect to the bridge. Pairing (printlog-bridge#2) will add 3D Print Log's sign-in site while pairing, and nothing else.

Tests: `test_connects_only_to_moonraker_its_webcam_and_3d_print_log` records every connection during a print. In `tests/test_discovery.py`, `test_a_different_printer_at_the_old_address_is_never_used` puts another printer at the old address, `test_with_discovery_off_the_printer_isnt_identified_or_looked_for` checks the off switch, `test_the_moonraker_key_goes_only_to_an_address_that_answers_as_moonraker` and `test_no_password_in_the_url_reaches_the_state_file` what they say, and `TestAddresses` that only private networks of a /24 or smaller are searched. `TestOnlyOutboundConnections` fails if any code in the bridge starts a server or listens on a socket, if a module other than the API client, the image fetcher, the Moonraker session and discovery opens a connection, or if `api_url` accepts plain http to another host (beyond a private-network address with `allow_http_on_lan`, which `TestPlainHttpOnTheLanIsWarnedAbout` checks is warned about).

## Your keys and passwords

The bridge holds up to three secrets: your 3D Print Log API key, Moonraker's API key (`moonraker_api_key`, remote mode only), and any password or token in a Moonraker or webcam URL (`http://user:password@host`, `?token=...`).

**The API key is kept out of the folder Moonraker serves.** In local mode, `printlog-bridge.cfg` sits in Klipper's config folder, and Moonraker lets anyone who can open Mainsail or Fluidd read and download every file in it. So:

- `install.sh` stores the key in `~/.local/share/printlog-bridge/api_key` (the bridge's state directory), mode 600 so only you can read it, and never writes it into `printlog-bridge.cfg`.
- `printlog-bridge store-key` stores or replaces the key there. Run it on an older install to move `api_key` out of `printlog-bridge.cfg`: it writes the key file and empties the line in the config. Run it as the user the bridge runs as, never with `sudo`. When it has an `api_key` to move out of the config, it refuses, before writing anything, unless the config belongs to the user running it: otherwise the key file would be another user's and the rewritten config root's or out of reach. A config an earlier `sudo store-key` left to root gets a `sudo chown` to give it back. A config with no `api_key` isn't rewritten, so a service user can store its key next to a root-owned `/etc` config. Root may only store a key for root's own config, in a folder only root can write, from a root login rather than through `sudo`, since the key file goes where that config says. The config is read, checked and rewritten through its folder held open, so renaming files in between can't redirect it, and a symlinked config stays a symlink.
- In the Docker image, `store-key` keeps it in `/data/state/api_key` in the mounted folder, mode 600, owned by the user the container runs as. The image itself never holds a key; `PRINTLOG_API_KEY` can be set on the container instead, where anyone who can manage the host's containers can read it.
- The bridge looks for the key in this order: the `PRINTLOG_API_KEY` environment variable, the key file, then `api_key` in `[bridge]`. A key in the config still works, so existing installs keep running.
- `doctor` warns when `api_key` is in a config file Moonraker serves (even an unused one), when the key file is in a folder Moonraker serves, and when other users on the computer can read the key file.

`moonraker_api_key` stays in the config. It is only for remote mode, where the bridge usually runs on another computer, away from any folder Moonraker serves; `doctor` doesn't check where it is.

**Secrets never reach the log, the status file, `status`, `doctor`, a trace or the support bundle.** URLs are shown with their user, password and secret-looking query values (`token`, `key`, `pass`, `auth`, `sig`, `user`, ...) replaced by `***`, in `bridge.log`, `status.json`, `status`, `doctor`, and every error message. An error that repeats a URL has the same parts hidden in any URL it writes, and each such secret hidden wherever else it appears, as configured, decoded or re-encoded (aiohttp writes `%2f` as `%2F`, for one). A secret under 3 characters, such as the user `pi`, is hidden only inside a URL, so a host name such as `pi.local` stays readable. The support bundle `doctor` writes removes every key, token and password it knows of (from the config, the key file and the environment) from every file in it, as well as passwords in URLs and secret-looking query values, and never includes the key file.

Tests: `test_no_secret_reaches_the_log`, `..._the_state_directory` and `..._status` run a print, traced, with every kind of secret set and look for each in everything the bridge wrote, the trace included. `TestMoonrakerUrlsAreMasked` covers `doctor`'s messages, `TestSupportBundle` here and in `tests/test_doctor.py` the bundle, `tests/test_install_helper.py` and `tests/install/smoke.sh` where install.sh puts the key, and `TestApiKeyStorage` in `tests/test_doctor.py` the warnings.

## What it keeps on the printer

In the state directory (`~/.local/share/printlog-bridge` by default): the key file, which job it logged as which print, and each remote printer's identity and last found address (`printers/`), the outbox of events and pictures not yet sent (`outbox.sqlite3`, `images/`), `status.json` and `bridge.log`, and with `trace = true`, the traces (`traces/`). An event still unsent after 30 days is dropped. `uninstall.sh --purge` removes all of it.

### Traces

Tracing is off unless you set `trace = true` in `[bridge]` (or `PRINTLOG_BRIDGE_TRACE=1`). Then the bridge records every message it sends to and receives from each printer's Moonraker in `traces/<printer>-<YYYYMMDD>.jsonl`. **Traces stay on this computer**: the bridge never sends them anywhere. The only way one leaves is in the support bundle `doctor` writes (the latest 20 MB), which you choose to attach.

What a trace holds is what Moonraker tells the bridge, and what the bridge asks it:

- the printer's state as it changes: print progress, the active tool, filament used, Klipper's state, and on a U1 its RFID reader's tags;
- Moonraker's job history: file names (with their folders), times, outcomes and the slicer's metadata;
- the printer's list of objects, its webcams (their URLs, scrubbed), and lines printed to the console;
- Moonraker's and Klipper's versions, and the host name Klipper reports beside its version (`printer.info`);
- the bridge's own requests, such as which objects it subscribes to.

It holds no G-code. Before a line is written, every secret the support bundle removes is removed from it: your 3D Print Log API key, Moonraker's API key, passwords in URLs and secret-looking query values (a webcam's `?token=`), any field named like a key, token, secret or password, and Moonraker's answer to a request for a oneshot token or API key. The support bundle scrubs traces a second time.

Each printer's traces stay under 50 MB, the oldest deleted first. Recording never slows the bridge, and a disk error never stops it. Turn tracing off and delete `traces/` whenever you like.

Tests: `tests/test_trace.py` (each kind of secret, the size cap, a stuck or failing disk), `TestWhatIsSent` (a traced print, checked for every secret), and `TestSupportBundle` in `tests/test_doctor.py`.

## Not covered yet

- **Pairing** (printlog-bridge#2) will replace the personal API key with a device token that can only log prints, kept in the state directory. Until then the bridge uses an API key with your account's full API access, so keep it to yourself and revoke it at [3dprintlog.com/api-keys](https://www.3dprintlog.com/api-keys) if it leaks.
- **Traffic on your network is as secure as Moonraker's.** Remote mode talks to Moonraker over the URL you give it, usually plain HTTP on your LAN, as Mainsail and Fluidd do.
