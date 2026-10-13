# Writing an adapter

An adapter watches one kind of printer and reports its jobs to the bridge. Moonraker (Klipper) is the first; PrusaLink, OctoPrint or anything else with an API can follow without a change to `core/`.

## Where things live

```
src/printlog_bridge/
  core/            printer-agnostic: config, events, API client, outbox, doctor
  adapters/
    __init__.py    ADAPTERS: the registry, by config name
    moonraker/     the Klipper adapter
  cli/             the printlog-bridge command
```

`core/` never imports from `adapters/`. The CLI hands the registry to `core` instead, and `tests/test_layout.py` fails if anything in `core/` imports an adapter.

## Adding one

1. Create `src/printlog_bridge/adapters/<name>/` with a subclass of `printlog_bridge.core.adapter.Adapter`.
2. Add it to `ADAPTERS` in `src/printlog_bridge/adapters/__init__.py`, keyed by its `name`.
3. Users select it per printer:

   ```ini
   [printer mk4]
   adapter = prusalink
   url = http://mk4.local
   ```

## The interface

| Member | What it does |
| --- | --- |
| `name` | The value of `adapter = ...` in the config. Must match its key in `ADAPTERS`. |
| `from_config(printer, options)` | Builds the adapter from its `[printer <name>]` section, minus `adapter`. Raise `ConfigError` for anything missing or invalid, naming the section and option. Do no I/O: `doctor` loads the config before checking anything, so a printer that's off must not stop the config from loading. Reject unknown options, so a typo surfaces instead of being ignored. |
| `describe()` | One line for logs and `doctor`, such as `moonraker, remote http://u1.local:7125`. |
| `use_spools(spools)` | Optional. Called before `run` with a `SpoolSink`, for a printer that can tell which spool is in a slot: await `spools.spool_command(SpoolCommand(...))` for a user's command (it returns the answer to show them, and never raises) and `spools.spool_reading(SpoolReading(...))` for what a reader in a slot sees. Don't wait on either in your connection's read loop. |
| `use_trace(trace)` | Optional. Called before `run` when `trace = true`, with the printer's `Trace` (`core/trace.py`): call `trace.record(SEND, raw)` and `trace.record(RECEIVE, raw)` with each message's JSON text as it goes out and comes in, and `trace.mark("open")` / `trace.mark("close")` around each connection. Neither waits or raises; the trace scrubs secrets and writes in a thread of its own. |
| `use_state(state)` | Optional. Called before `run` with the printer's `PrinterState` (`core/state.py`), for an adapter that keeps something of its own between runs: put it in `state.discovery` (plain JSON, never a password) and call `state.save()`. The Moonraker adapter keeps the printer's identity and last found address there (`adapters/moonraker/locator.py`). |
| `check()` | Diagnostics for `doctor`: a list of `CheckResult`s named `Printer <name>`. Return `fail` when the bridge can't work (unreachable, wrong credentials), `warn` when it can but something needs attention (Klipper shut down). Don't raise; `doctor` reports an exception as a failed check, but your message will be better. |
| `run(sink, report)` | Watches the printer until cancelled, awaiting `sink.emit(event)` for each job event, in order. Reconnect on your own after a dropped connection, and after each connect replay every job that started at or after `await sink.resume_after(printer)` (or, when that is `None`, only a job running now). Tell `report` (a `PrinterReport`) what `printlog-bridge status` should show: `connected()`, `disconnected(reason)`, `printer_state(state)` for the printer's own state, and `warn(topic, message)` for something the user must fix, cleared with `warn(topic, None)` once it's fixed. Return only when cancelled. |

## Events

Adapters report jobs with the types in `printlog_bridge.core.events`, never with their printer's own vocabulary.

- `JobStarted(printer, job_id, file_name, started_at, estimated_duration_s, estimated_filament_mm, estimated_filament_g, estimated_filament_g_by_slot)`, the estimates from the slicer when the file has them. `estimated_filament_g_by_slot` maps slot to grams, only for a file with a weight per tool; leave it empty otherwise and the totals make one row.
- `JobFinished(printer, job_id, outcome, finished_at, duration_s, filament_mm, note)`, where `outcome` is a `JobOutcome` (`COMPLETED`, `CANCELLED` or `FAILED`), `filament_mm` maps slot index (tool 0, 1, ...) to millimetres fed (one entry, slot 0, on a single-tool printer; a slot that fed nothing can be left out), and `note` says why a job failed in words for the print's notes ("Klipper shutdown").
- `JobImage(printer, job_id, kind, fetch)`, a picture of the job: `ImageKind.THUMBNAIL` (the slicer's preview) after the job's start, `ImageKind.SNAPSHOT` (a webcam frame) after its finish. `fetch()` returns the image's bytes, or `None` when there is none; the sink calls it only when it still wants that picture, so a replay costs nothing. Give up within 10 s and raise `ImageUnavailable` (`core/images.py`) with a message that is safe to log; `fetch_image` and `read_image_file` there already bound size and time and mask passwords in URLs. The sink stops waiting after 15 s regardless, and a picture never holds up its print: one that can't be had is skipped, and one the API refuses is dropped.

- `SpoolCommand(printer, slot, filament_id, tag, slot_count)`, the user assigning a spool to a slot by its id in 3D Print Log or a tag's UID, or clearing the slot (neither). `slot_count` is how many tools the printer has, when you know; a slot beyond what 3D Print Log has makes it add slots, up to that.
- `SpoolReading(printer, slot, tag, material, color_hex, slot_count)`, what a reader in a slot sees: a tag's UID as hex, the material it names (`PLA`), its color as `#RRGGBB`. Report a slot when its reading changes, and an empty reading when the spool is gone. A tag one of the user's spools has assigns that spool; otherwise a material or color unlike the assigned spool's adds a note to the print.

`job_id` must be stable for the job's lifetime and unique on that printer, such as Moonraker's history `job_id`. The sink (`core/lifecycle.py`) writes each event to the outbox (`core/outbox.py`, SQLite in the state directory) and returns without waiting on the API, then sends it from there; so `emit` is quick even while 3D Print Log is unreachable. It logs a job under `<instance>:<job_id>`, where the instance id is a UUID kept in the state directory per printer, and remembers each job it has logged, so a replayed event is recognised instead of logged twice. Replays are expected: emit a job's start again before its finish whenever you aren't sure the start was seen.

Use timezone-aware `datetime`s.

## Tests

Test against a real local server rather than mocking your HTTP client, and make that server behave like the real printer, checked against one. `tests/fake_moonraker.py` speaks Moonraker's JSON-RPC over a WebSocket and over its Unix socket, including its authorization and its one-agent-per-name rule. `tests/test_moonraker_adapter.py` covers config parsing and each `check()` outcome; `tests/test_moonraker_session.py` covers the connection, reconnects and Klipper restarts; `tests/test_moonraker_jobs.py` covers turning history into events. `tests/fake_printlog_api.py` stands in for the 3D Print Log API with its rules for connectors (idempotent create, complete, patch), and `tests/test_job_lifecycle.py` runs the whole bridge between the two fakes, including a print made with the API unreachable (`api.offline = True` cuts every request off). `tests/test_outbox.py` covers the outbox on its own, and `tests/test_job_logger.py` covers sending from it: order per print, backoff, `Retry-After`, a power cut in the middle of a send, the 30-day limit, and pictures; `tests/test_moonraker_images.py` covers finding a job's thumbnail and webcam snapshot, against `tests/fake_webcam.py`. `tests/test_spools.py` covers assigning spools and slot readings against the fake API's slot and tag rules, `tests/test_moonraker_spools.py` the console commands and the U1's reader, `tests/test_macros.py` renders `klipper/printlog.cfg` with jinja2 as Klipper does, and `tests/test_spool_assignment.py` runs it all through the bridge.

A fake is only as good as its likeness, so `tests/integration/` checks it. `test_fake_matches_moonraker.py` sends the same raw JSON-RPC exchanges to the fake and to a real Moonraker in Docker and expects the same answers, and `test_real_moonraker.py` runs the bridge itself against the real one: auth, the extensions list, a second bridge, a Moonraker restart, and local mode inside the container. When you learn something new about Moonraker, add it to both the fake and the contract test.

## The Moonraker adapter

`adapters/moonraker/session.py` keeps one JSON-RPC connection open: the Unix socket in local mode (each message ends with an ETX byte, `0x03`), `ws://<printer>/websocket` in remote mode. On each connect it identifies as an agent, registers its remote methods (none yet), and subscribes to `print_stats`, `toolhead.extruder`, `virtual_sdcard`, `webhooks` and, when the printer has it, `filament_detect`. It subscribes again after every `notify_klippy_ready`, and reconnects with a backoff that doubles from 1 s to 60 s and resets after a good connection. `check()` asks `server.info` over that same connection, without identifying, so `doctor` tests the path `run` uses.

`adapters/moonraker/jobs.py` turns job history into events. `notify_history_changed` reports a job when it is added and when it finishes, and on every connect `server.history.list` is read from the resume point, a page at a time, so a job that started or ended while the bridge was away is still reported. A job Moonraker found in progress when it started is `interrupted`, and is only ever seen this way. For an `error`, the note carries `print_stats.message` when the bridge saw it.

`adapters/moonraker/tools.py` splits a job's filament by tool. Klipper counts one total per print (`print_stats.filament_used`, across every extruder), and neither its extruder objects nor Moonraker's history entry (`filament_used`, `auxiliary_data`) say how much each tool fed. So `ToolUsage` gives each increase of the total to the extruder named in `toolhead.extruder` at that status update (`extruder` is slot 0, `extruder<N>` slot N), and restarts when Klipper loads a new file. At the job's end, `per_slot` scales what it saw to the history entry's `filament_used`, which stays the source of truth; with nothing seen (the job ran while the bridge was away), it splits by the slicer's grams per tool (`filament_weights` in the file's metadata, one per filament in tool order, from PrusaSlicer, OrcaSlicer and Snapmaker Orca), and with neither it's all slot 0. An MMU print on one extruder (`mmu_print`) has no per-tool estimate: its filaments are gates, not tools. Without `[history]` in moonraker.conf the history methods don't exist, and the bridge logs how to turn it on.

`adapters/moonraker/spools.py` takes spool commands from the console. The `PRINTLOG_SET_SPOOL` and `PRINTLOG_CLEAR_SPOOL` macros (`klipper/printlog.cfg`) print a line such as `// printlog: SET_SPOOL TOOL=2 TAG=04A23F11` with `action_respond_info`, Moonraker sends every console line to every client as `notify_gcode_response`, and `SpoolWatcher` passes the command on and shows the answer with the file's `_PRINTLOG_RESPOND` macro (through `printer.gcode.script`). Not `action_call_remote_method`: Klipper raises an error when no client has registered the method, so with the bridge restarting or offline the macro would fail, and in a print's G-code that ends the print. A console line can't fail; the cost is that one printed while the bridge is away is missed, which the macro tells the user to look out for.

`adapters/moonraker/u1_rfid.py` reads the Snapmaker U1's RFID reader, Klipper's `filament_detect` object, written from Snapmaker's published firmware (`u1-klipper`'s `filament_detect.py` and `filament_protocol.py`) and checked against a real U1's answers (`tests/fixtures/moonraker/u1`, `tests/test_real_captures.py`). `info` has one entry per channel, channel `n` feeding `extruder<n>`; an entry is filled in only for a Snapmaker tag whose signature checked out (`VENDOR` is `NONE` otherwise), with `CARD_UID` as a list of byte values, `MAIN_TYPE` the material and `RGB_1` the color. Confirmed on a U1 (Klipper 2.0.0.205, Moonraker 2.0.0): `filament_detect` is in `printer.objects.list`; `CARD_UID` is a list of four bytes (`[160, 93, 28, 63]`), and the number `0` in an empty slot, whose `VENDOR` is `NONE`; `RGB_1` is the color the slicer has for the same spool. Still to check, with a trace (`trace = true`): that the status arrives through a subscription while printing, that the byte order is the one a phone's NFC app shows, and how an entry changes when a spool is pulled out or runs out.
