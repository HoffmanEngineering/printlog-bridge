# 3D Print Log Bridge

This app runs [printlog-bridge](https://github.com/HoffmanEngineering/printlog-bridge) on your Home Assistant. It watches your printers over your network and logs every print to [3D Print Log](https://www.3dprintlog.com): its outcome, duration, filament used and a picture, with nothing to type in.

It watches:

- **Klipper printers** through Moonraker (Voron, Snapmaker U1, Creality K1/K2 with Moonraker, and any Mainsail or Fluidd printer);
- **Bambu Lab printers** (X1, P1, A1, H2) over their LAN connection;
- **Prusa printers with PrusaLink** (MK4/MK4S, MK3.5/3.9, Core One, MINI, XL).

Home Assistant must be on the same network as the printers. The app needs a 64-bit system (amd64 or aarch64, such as a Raspberry Pi 4 or 5, or a Home Assistant Green).

## Install

1. In Home Assistant, open **Settings > Apps** (formerly Add-ons), then the app store, and in its menu choose **Repositories**. Add `https://github.com/HoffmanEngineering/printlog-bridge`.
2. Find **3D Print Log Bridge** in the store and install it.
3. On the app's **Configuration** tab, add your printers (below), and save.
4. Turn on **Watchdog**, then **Start** the app.
5. Open the **Log** tab and pair the bridge (below).

## Printers found on your network

Once paired, the bridge looks for printers on your network (Moonraker and PrusaLink over mDNS, Bambu Lab printers' announcements) and 3D Print Log lists them under "Found on your network" on the bridge's page. It never watches one until you connect it there; then it starts within a minute, with no restart. The app runs on Home Assistant's own network, so this works as is. A printer connected in the app that needs an access code or password gets it from 3D Print Log: enter it next to the printer on the bridge's page, and your browser encrypts it to this bridge's own key, so 3D Print Log can't read it. The bridge's key is in the app's private `/data` folder and never leaves it. When 3D Print Log asks you to confirm the bridge's key (a bridge updated from a version without one does, once), compare the fingerprint it shows with the one the bridge logs when it starts (the **Log** tab: `encryption key (aed0 3e9a 8282 33b2)`), then confirm. A `printlog-bridge.cfg` in the app's config folder can still set a credential under `[app_printer <number>]` (the number is in the **Log** tab), and it wins over the app's; see the bridge's README.

## Signing in (pairing)

On its first start the bridge pairs with your 3D Print Log account. The app's panel (**3D Print Log Bridge** in the sidebar, or **Open web UI** on the app's page) and the **Log** tab show a QR code, an address and a code:

1. Scan the QR code with your phone (or open the address and enter the code).
2. Sign in to 3D Print Log if asked, and approve the bridge.

The bridge then carries on by itself. A code works for 15 minutes; when one expires, the log shows a new one. You can remove the bridge in 3D Print Log at any time; its log then says "re-pair needed", and restarting the app pairs it again.

If the log says pairing is off on 3D Print Log (it mentions `store-key`), use an API key instead (below).

If a printer has no **Printer ID** yet, the log lists your printers in 3D Print Log with their numbers after pairing, and the app stops: set the printer's **Printer ID** on the Configuration tab and start the app again.

Rather use an API key? Create one at <https://www.3dprintlog.com/api-keys>, put it in **3D Print Log API key** and restart the app. A paired bridge uses its pairing, not the key.

## Printers

Add one entry per printer under **Printers**. Each needs a **Name** (yours, for the log), an **Adapter** and its **Printer ID** in 3D Print Log: the number at the end of the printer's page, such as `42` in `https://www.3dprintlog.com/printers/42`. The other fields depend on the adapter. In YAML mode (the Configuration tab's menu, **Edit in YAML**) the list looks like this:

```yaml
printers:
  - name: voron
    adapter: moonraker
    printer_id: 42
    url: http://192.168.1.20:7125
  - name: x1c
    adapter: bambu
    printer_id: 44
    host: 192.168.1.50
    serial: 01S00C123456789
    access_code: "12345678"
  - name: mk4s
    adapter: prusalink
    printer_id: 45
    host: 192.168.1.60
    password: your-prusalink-password
  - name: ender
    adapter: octoprint
    printer_id: 46
    url: http://octopi.local
    api_key: your-octoprint-key
  - name: centauri
    adapter: sdcp
    printer_id: 47
    host: 192.168.1.77
  - name: railcore
    adapter: duet
    printer_id: 48
    host: 192.168.1.88
  - name: kobra
    adapter: anycubic
    printer_id: 49
    host: 192.168.1.89
  - name: ad5x
    adapter: flashforge
    printer_id: 50
    host: 192.168.1.60
    serial: SNMOMC9900728
    check_code: 8f3a2c71
```

| Adapter | Fields |
|---|---|
| `moonraker` | **url**: Moonraker's address, such as `http://192.168.1.20:7125`. **api_key** only when Moonraker's `[authorization]` doesn't trust your Home Assistant's address. |
| `bambu` | **host**, **serial** and **access_code**. On firmware with Bambu's Authorization Control, turn on LAN Only Mode and Developer Mode on the printer first. The access code is on the printer's screen: X1/H2 Settings > LAN Only, P1 Settings > WLAN, A1 Settings > LAN Only Mode. |
| `prusalink` | **host** and **password** (the printer's Settings > Network > PrusaLink shows them). Older firmware: **api_key** instead of the password. **username** only when it isn't `maker`. |
| `octoprint` | **url**: OctoPrint's address, such as `http://octopi.local` or `http://192.168.1.70:5000`. **api_key**: a key from OctoPrint's Settings > Application Keys (or your user's Settings > API Key). |
| `sdcp` | **host**: the Elegoo Centauri Carbon's IP address, shown in its network settings. Nothing else: the printer's local protocol (SDCP) has no password, and the bridge only reads its status. Not yet checked on a real printer. |
| `duet` | **host**: the Duet board's IP address or host name (Duet Web Control's address). **password** only when the board's `config.g` sets one with `M551`. Works with or without an SBC. Not yet checked on a real board. |
| `anycubic` | **host**: the IP address of an Anycubic Kobra S1, Kobra 3 or Photon P1 / P1 Max, with LAN mode turned on on its screen. Nothing else: in LAN mode the printer hands the bridge its MQTT login, and the bridge only reads its status. Checked on a Kobra S1 (idle); prints not yet recorded. A Photon accepts one LAN client at a time, so the bridge and Photon Workshop push each other off: when Photon Workshop takes the connection, the bridge waits 5 minutes before it tries again. |
| `flashforge` | **host**: the IP address of a FlashForge Adventurer 5M, 5M Pro or AD5X. **serial**: its serial number (it starts with `SN`). **check_code**: shown on its screen once LAN-only mode is on (Settings > Network). The bridge only reads its status. Not yet checked on a real printer. |

A field an adapter doesn't use must stay empty: the app says which one when it refuses to start. Give each printer a fixed address (a DHCP reservation in your router). A Moonraker printer that moves to a new address is found again on your network.

Secrets (access codes, passwords, API keys) are entered as password fields. A value can't contain line breaks, start with `#` or `;`, or have `#` or `;` after a space (the bridge's config would read the rest as a comment), so change such a password on the printer.

## Options

| Option | |
|---|---|
| **Device name** | The name 3D Print Log shows for this bridge. Default: `printlog-bridge on <host name>`. |
| **3D Print Log API key** | Leave empty to pair. |
| **3D Print Log API** | Leave empty. Only for testing against another 3D Print Log server. |
| **Trace** | Records every message to and from each Moonraker printer, secrets removed, in the app's data, to report a problem. Leave it off otherwise. |

## Advanced: your own config file

For the bridge's other settings (where to look for a printer that moved, finish-only mode for uploader users, snapshots, timelapse, slicer settings and more; see the [README's Config section](https://github.com/HoffmanEngineering/printlog-bridge#config)), put a complete `printlog-bridge.cfg` in the app's config folder. It is `addon_configs/<id>_printlog_bridge/` (or `app_configs/`) through the Samba share or a file editor app.

When that file exists, the app uses it instead of the **Printers** option. Device name, API key and Trace still apply. Leave out `state_dir`: the app keeps its state in its own data.

## The status panel

The app's panel (**3D Print Log Bridge** in Home Assistant's sidebar; turn on **Show in sidebar** on the app's page) shows whether the bridge runs, its version, whether it's paired and its encryption key's fingerprint, the last check-in with 3D Print Log, each printer and its connection, and the code to approve while it pairs. **Run the checks** runs the bridge's `doctor`. The panel only shows: printers are managed in 3D Print Log and on the Configuration tab. Home Assistant's ingress serves it, signed in as you; the app answers only Home Assistant's own proxy on its port (7180), even though it's on the host's network: any other address is closed as it connects. It never shows a key, token, password or access code.

## Logs, health and updates

- The **Log** tab shows the bridge's log: pairing, each print it logs, and problems with a printer.
- With **Watchdog** on, Home Assistant restarts the app when the bridge stops or stops checking in (its health check is the same as `printlog-bridge status`).
- Updates appear in Home Assistant like any app's. The app's version is the bridge's.

## What is stored, and backups

The app keeps the bridge's config (written from your options at every start), the device token from pairing, prints waiting to be sent, and the bridge's log in its own data folder, which only the app can read. A Home Assistant backup includes them, except the log, traces and support bundle, so a restored backup keeps the bridge paired. Home Assistant encrypts backups by default since 2025.1. See the bridge's [privacy notes](https://github.com/HoffmanEngineering/printlog-bridge/blob/main/docs/privacy.md).

## Not yet

- 32-bit systems (armv7): Home Assistant no longer supports them. Run the Docker image instead.

## Support

Report a problem at <https://github.com/HoffmanEngineering/printlog-bridge/issues>, with the app's log.
