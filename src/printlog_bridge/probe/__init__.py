"""`printlog-bridge probe`: a read-only look at a printer the bridge doesn't support yet.

It records what a future adapter needs (what the printer announces on the LAN, which of a short
list of ports are open, and what each answers to read-only status requests) into one JSON trace,
and prints a short summary. Everything it sends is on `allowlist.RULES`, and every send is checked
against it before the bytes go out (`record.Recorder.send`).
"""
