# Changelog

The app's version is printlog-bridge's. The bridge's own changes are in its [releases](https://github.com/HoffmanEngineering/printlog-bridge/releases).

## 0.2.1

- On the Bridges page, printers from your configuration keep their own names instead of the name the printer advertises on the network, and show which 3D Print Log printer they log to.

## 0.2.0

- New printers in the app's options: OctoPrint, Elegoo (SDCP), Duet, FlashForge and Anycubic, beside Moonraker, Bambu Lab and PrusaLink.
- The bridge finds printers on your network and checks in with 3D Print Log, so you can connect printers and enter their passwords on the new Bridges page instead of in the options.
- Resin prints are logged in mL.
- A bridge paired with an earlier version pairs again once by itself to get the new Bridges permission. Approve it with the code in the log, then confirm the bridge's encryption key on the Bridges page.

## 0.1.1

- First version of the Home Assistant app: printers in the app's options (Moonraker, Bambu Lab, PrusaLink), pairing by a QR code in the log, or an API key; an optional `printlog-bridge.cfg` in the app's config folder for every other setting.
