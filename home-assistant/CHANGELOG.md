# Changelog

The app's version is printlog-bridge's. The bridge's own changes are in its [releases](https://github.com/HoffmanEngineering/printlog-bridge/releases).

## 0.3.0

- Klipper prints are no longer lost or merged into an older print when Moonraker reuses a job number (after its history is cleared, or the Pi is set up again).
- A print fed from one AMS tray or slot other than the first puts its material on that slot, instead of also charging the spool in slot 1.
- On Bambu Lab and PrusaLink printers, a print whose end the bridge missed while it was disconnected is charged for how far it got, instead of nothing or the full estimate.
- A printer connected on the Bridges page is watched again after an error, instead of only after the app restarts.
- Prints waiting to be sent are kept for 30 days of the bridge running, so a Raspberry Pi whose clock jumps forward at startup no longer drops them.
- The stored API key is sent only to the 3D Print Log address it was saved for. If you point the bridge at your own or a local API with a key saved by 0.2.1 or earlier, run `printlog-bridge store-key` once after updating.
- The bridge limits how much it reads from printers and print files, and never sends a printer's credentials to another address.

## 0.2.1

- On the Bridges page, printers from your configuration keep their own names instead of the name the printer advertises on the network, and show which 3D Print Log printer they log to.

## 0.2.0

- New printers in the app's options: OctoPrint, Elegoo (SDCP), Duet, FlashForge and Anycubic, beside Moonraker, Bambu Lab and PrusaLink.
- The bridge finds printers on your network and checks in with 3D Print Log, so you can connect printers and enter their passwords on the new Bridges page instead of in the options.
- Resin prints are logged in mL.
- A bridge paired with an earlier version pairs again once by itself to get the new Bridges permission. Approve it with the code in the log, then confirm the bridge's encryption key on the Bridges page.

## 0.1.1

- First version of the Home Assistant app: printers in the app's options (Moonraker, Bambu Lab, PrusaLink), pairing by a QR code in the log, or an API key; an optional `printlog-bridge.cfg` in the app's config folder for every other setting.
