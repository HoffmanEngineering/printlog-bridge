# Writing an adapter

An adapter watches one kind of printer and reports its jobs to the bridge. Moonraker (Klipper) is the first; PrusaLink, OctoPrint or anything else with an API can follow without a change to `core/`.

## Where things live

```
src/printlog_bridge/
  core/            printer-agnostic: config, events, API client, doctor
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
| `check()` | Diagnostics for `doctor`: a list of `CheckResult`s named `Printer <name>`. Return `fail` when the bridge can't work (unreachable, wrong credentials), `warn` when it can but something needs attention (Klipper shut down). Don't raise; `doctor` reports an exception as a failed check, but your message will be better. |
| `run(sink)` | Watches the printer until cancelled, awaiting `sink.emit(event)` for each job event, in order. Reconnect on your own after a dropped connection. Return only when cancelled. |

## Events

Adapters report jobs with the types in `printlog_bridge.core.events`, never with their printer's own vocabulary.

- `JobStarted(printer, job_id, file_name, started_at)`
- `JobFinished(printer, job_id, outcome, finished_at, duration_s, filament_mm)`, where `outcome` is a `JobOutcome` (`COMPLETED`, `CANCELLED` or `FAILED`) and `filament_mm` maps slot index to millimetres fed.

`job_id` must be stable for the job's lifetime and unique on that printer, such as Moonraker's history `job_id`. The job lifecycle (#4) derives the API's external ID from it, so a replayed event after a restart is recognised instead of logged twice.

Use timezone-aware `datetime`s.

## Tests

Test against a real local server rather than mocking your HTTP client, and make that server behave like the real printer, checked against one. `tests/fake_moonraker.py` speaks Moonraker's JSON-RPC over a WebSocket and over its Unix socket, including its authorization and its one-agent-per-name rule. `tests/test_moonraker_adapter.py` covers config parsing and each `check()` outcome; `tests/test_moonraker_session.py` covers the connection, reconnects and Klipper restarts.

A fake is only as good as its likeness, so `tests/integration/` checks it. `test_fake_matches_moonraker.py` sends the same raw JSON-RPC exchanges to the fake and to a real Moonraker in Docker and expects the same answers, and `test_real_moonraker.py` runs the bridge itself against the real one: auth, the extensions list, a second bridge, a Moonraker restart, and local mode inside the container. When you learn something new about Moonraker, add it to both the fake and the contract test.

## The Moonraker adapter

`adapters/moonraker/session.py` keeps one JSON-RPC connection open: the Unix socket in local mode (each message ends with an ETX byte, `0x03`), `ws://<printer>/websocket` in remote mode. On each connect it identifies as an agent, registers its remote methods, and subscribes to `print_stats`, `toolhead.extruder`, `virtual_sdcard`, `webhooks` and, when the printer has it, `filament_detect`. It subscribes again after every `notify_klippy_ready`, and reconnects with a backoff that doubles from 1 s to 60 s and resets after a good connection. `check()` asks `server.info` over that same connection, without identifying, so `doctor` tests the path `run` uses.
