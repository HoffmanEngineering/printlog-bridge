# REAL Moonraker captures

Captured 2026-10-12 from the maintainer's own printers, idle, one request each. Unlike the
SYNTHETIC fixtures elsewhere in `tests/`, these are what the printers answered, unchanged except
where noted.

| Folder | Printer | Firmware |
| --- | --- | --- |
| `u1/` | Snapmaker U1 | Klipper `2.0.0.205_20260914173503`, Moonraker `2.0.0` (Snapmaker's forks) |
| `infimech/` | Infimech EX | the vendor's Klipper and Moonraker, both reporting version `9.9.9` |
| `k3/` | Creality K3 (CFS-Nano) | DWIN 4.0.3.1.25, Klipper `09faed31` |

Each file is one call's JSON-RPC answer (`{"result": ...}`):

| File | Call |
| --- | --- |
| `server_info.json` | `server.info` |
| `printer_info.json` | `printer.info` |
| `objects_list.json` | `printer.objects.list` |
| `status.json` | `printer.objects.query` for the objects the bridge subscribes to, plus the vendor objects (`filament_detect`, `filament_feed`, `filament_manager`...) |
| `history.json` | `server.history.list` (the last 5 jobs) |
| `webcams.json` | `server.webcams.list` |
| `server_config.json` | `server.config` |

Added 2026-10-31, idle after a print, one HTTP `GET /printer/objects/query` each:

| File | Query |
| --- | --- |
| `u1/print_task_config.json` | `print_task_config`, whole: where the screen sent each G-code tool (`extruder_map_table`) and what each head has loaded |
| `k3/box.json` | `box=same_material,T1` and `nozzle_manager=selected`: the CFS's loaded slots (its `sn` is all `0xFF`, no serial) |

Scrubbed before committing:

- `infimech/server_config.json`: the `[notifier 3d_print_log]` URL's `X-Api-Key` is `<redacted>`,
  and its `printerId` is `0`.
- `infimech/printer_info.json`: the hostname's device suffix is `0000`.

Nothing else in them is secret: the IP ranges are Moonraker's default `trusted_clients`, and the
U1's hostname (`lava`) is the same on every U1. Before adding a capture, check it for API keys,
tokens, passwords, hostnames, IP addresses and serial numbers.
