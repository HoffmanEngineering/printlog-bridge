# Privacy and security

What printlog-bridge sends, where it connects, and how it keeps your keys. Each guarantee below is what the code does today, and a test fails if that changes: most are in [`tests/test_privacy.py`](../tests/test_privacy.py), and the others are named where they come up.

## What leaves the printer

The bridge sends 3D Print Log exactly these calls, and nothing else:

| Call | When | What it carries |
| --- | --- | --- |
| `GET /api/Users/me` | `doctor` | nothing; checks the key |
| `GET /api/Printers/{id}` | `doctor`; at start and every 5 minutes; when a print starts and ends | nothing; checks `printer_id`, and reads which spool is loaded in each slot |
| `GET /api/Users/me/user-settings` | first print after a start | nothing; reads your default visibility and comment settings |
| `PUT /api/Connections/{instance}` | at start, then every 5 minutes | `kind` (`moonraker`), `displayName` (`printlog-bridge (<printer name from your config>)`), `agentVersion`, `printerId` |
| `POST /api/Prints` | a print starts | `printerId`, `title` (made from the file name), `fileName` (the file's name, without its folder), `startDate`, `status`, `viewStatus` and `allowComments` (your account defaults), `externalSource` (`moonraker`), `externalId` and `connectionInstanceId` (a random id this bridge made for the printer, plus Moonraker's job id), the slicer's estimated print time and filament (length and weight, and the weight per tool on a multi-tool file), when the file has them, each tool's row linked to the spool loaded in its slot, and `notes` when the printer's RFID reader disagrees with a spool's material or color |
| `GET /api/Prints/{id}` | a print ends | nothing; reads what you changed in the app |
| `POST /api/Prints/{id}/complete` | a print ends | `status` (the outcome), `endedAt`, `printTimeInSeconds`, and per tool (`slot`) the filament length Klipper fed (linked to the spool you chose in the app) |
| `PATCH /api/Prints/{id}` | a print fails or is cut short | `notes`: a line saying why, with Klipper's error message when there is one |
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

## Where it connects

The bridge only makes outbound connections, to:

1. **Moonraker**: its Unix socket in local mode, or the `url` you set in remote mode (HTTP and WebSocket, on your network). In remote mode it also downloads the thumbnail from Moonraker's file API.
2. **The webcam's `snapshot_url`**, as Mainsail or Fluidd have it set in Moonraker (usually the printer itself), when a print ends.
3. **3D Print Log**, at `api_url` (`https://api.3dprintlog.com` by default), **over HTTPS only.** The config refuses a plain `http://` `api_url`, except to this computer itself (`localhost`, `127.0.0.1`, `::1`), for a test server or a TLS proxy on the same host.

It **never listens on a port**: nothing can connect to the bridge. Pairing (printlog-bridge#2) will add 3D Print Log's sign-in site while pairing, and nothing else.

Tests: `test_connects_only_to_moonraker_its_webcam_and_3d_print_log` records every connection during a print. `TestOnlyOutboundConnections` fails if any code in the bridge starts a server or listens on a socket, if a module other than the API client, the image fetcher and the Moonraker session opens a connection, or if `api_url` accepts plain http to another host.

## Your keys and passwords

The bridge holds up to three secrets: your 3D Print Log API key, Moonraker's API key (`moonraker_api_key`, remote mode only), and any password or token in a Moonraker or webcam URL (`http://user:password@host`, `?token=...`).

**The API key is kept out of the folder Moonraker serves.** In local mode, `printlog-bridge.cfg` sits in Klipper's config folder, and Moonraker lets anyone who can open Mainsail or Fluidd read and download every file in it. So:

- `install.sh` stores the key in `~/.local/share/printlog-bridge/api_key` (the bridge's state directory), mode 600 so only you can read it, and never writes it into `printlog-bridge.cfg`.
- `printlog-bridge store-key` stores or replaces the key there. Run it on an older install to move `api_key` out of `printlog-bridge.cfg`: it writes the key file and empties the line in the config.
- In the Docker image, `store-key` keeps it in `/data/state/api_key` in the mounted folder, mode 600, owned by the user the container runs as. The image itself never holds a key; `PRINTLOG_API_KEY` can be set on the container instead, where anyone who can manage the host's containers can read it.
- The bridge looks for the key in this order: the `PRINTLOG_API_KEY` environment variable, the key file, then `api_key` in `[bridge]`. A key in the config still works, so existing installs keep running.
- `doctor` warns when `api_key` is in a config file Moonraker serves (even an unused one), when the key file is in a folder Moonraker serves, and when other users on the computer can read the key file.

`moonraker_api_key` stays in the config. It is only for remote mode, where the bridge usually runs on another computer, away from any folder Moonraker serves; `doctor` doesn't check where it is.

**Secrets never reach the log, the status file, `status`, `doctor`, a trace or the support bundle.** URLs are shown with their user, password and secret-looking query values (`token`, `key`, `pass`, `auth`, `sig`, `user`, ...) replaced by `***`, in `bridge.log`, `status.json`, `status`, `doctor`, and every error message. The support bundle `doctor` writes removes every key, token and password it knows of (from the config, the key file and the environment) from every file in it, as well as passwords in URLs and secret-looking query values, and never includes the key file.

Tests: `test_no_secret_reaches_the_log`, `..._the_state_directory` and `..._status` run a print, traced, with every kind of secret set and look for each in everything the bridge wrote, the trace included. `TestMoonrakerUrlsAreMasked` covers `doctor`'s messages, `TestSupportBundle` here and in `tests/test_doctor.py` the bundle, `tests/test_install_helper.py` and `tests/install/smoke.sh` where install.sh puts the key, and `TestApiKeyStorage` in `tests/test_doctor.py` the warnings.

## What it keeps on the printer

In the state directory (`~/.local/share/printlog-bridge` by default): the key file, which job it logged as which print (`printers/`), the outbox of events and pictures not yet sent (`outbox.sqlite3`, `images/`), `status.json` and `bridge.log`, and with `trace = true`, the traces (`traces/`). An event still unsent after 30 days is dropped. `uninstall.sh --purge` removes all of it.

### Traces

Tracing is off unless you set `trace = true` in `[bridge]` (or `PRINTLOG_BRIDGE_TRACE=1`). Then the bridge records every message it sends to and receives from each printer's Moonraker in `traces/<printer>-<YYYYMMDD>.jsonl`. **Traces stay on this computer**: the bridge never sends them anywhere. The only way one leaves is in the support bundle `doctor` writes (the latest 20 MB), which you choose to attach.

What a trace holds is what Moonraker tells the bridge, and what the bridge asks it:

- the printer's state as it changes: print progress, the active tool, filament used, Klipper's state, and on a U1 its RFID reader's tags;
- Moonraker's job history: file names (with their folders), times, outcomes and the slicer's metadata;
- the printer's list of objects, its webcams (their URLs, scrubbed), and lines printed to the console;
- the bridge's own requests, such as which objects it subscribes to.

It holds no G-code. Before a line is written, every secret the support bundle removes is removed from it: your 3D Print Log API key, Moonraker's API key, passwords in URLs and secret-looking query values (a webcam's `?token=`), any field named like a key, token, secret or password, and Moonraker's answer to a request for a oneshot token or API key. The support bundle scrubs traces a second time.

Each printer's traces stay under 50 MB, the oldest deleted first. Recording never slows the bridge, and a disk error never stops it. Turn tracing off and delete `traces/` whenever you like.

Tests: `tests/test_trace.py` (each kind of secret, the size cap, a stuck or failing disk), `TestWhatIsSent` (a traced print, checked for every secret), and `TestSupportBundle` in `tests/test_doctor.py`.

## Not covered yet

- **Pairing** (printlog-bridge#2) will replace the personal API key with a device token that can only log prints, kept in the state directory. Until then the bridge uses an API key with your account's full API access, so keep it to yourself and revoke it at [3dprintlog.com/api-keys](https://www.3dprintlog.com/api-keys) if it leaks.
- **Traffic on your network is as secure as Moonraker's.** Remote mode talks to Moonraker over the URL you give it, usually plain HTTP on your LAN, as Mainsail and Fluidd do.
