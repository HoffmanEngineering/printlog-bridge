"""Shared test setup. Being here also puts tests/ on sys.path, so tests in subfolders can import
fake_moonraker and session_helpers."""

import os

# `run` serves the local page on 127.0.0.1:7180 by default; tests (and the bridges they start in
# subprocesses, which inherit this) don't, unless a test turns it on with a port of its own.
os.environ["PRINTLOG_BRIDGE_STATUS_PAGE"] = "off"
