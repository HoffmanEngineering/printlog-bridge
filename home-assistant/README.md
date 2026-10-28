# 3D Print Log Bridge

Logs every print from your printers to [3D Print Log](https://www.3dprintlog.com) automatically: outcome, duration, filament used and a picture, with nothing to type in.

- Klipper printers through Moonraker, Bambu Lab printers (X1, P1, A1, H2), Prusa printers with PrusaLink and printers run by OctoPrint, from Home Assistant on the same network.
- Pairs with your 3D Print Log account by a QR code in the app's log; no API key to copy.
- Keeps what it can't send yet until 3D Print Log is reachable again.

Runs [printlog-bridge](https://github.com/HoffmanEngineering/printlog-bridge) (MIT licensed). Needs a 64-bit Home Assistant system (amd64 or aarch64). The Documentation tab has the setup.
