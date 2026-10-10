"""Printer-agnostic parts of the bridge.

Nothing in this package may import from `printlog_bridge.adapters`; adapters are handed in by the
CLI. `tests/test_layout.py` enforces that.
"""
