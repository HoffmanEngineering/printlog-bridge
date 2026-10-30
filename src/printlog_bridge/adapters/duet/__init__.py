"""Duet boards running RepRapFirmware, over their HTTP API. See docs/adapters.md."""

from .adapter import DuetAdapter

__all__ = ["DuetAdapter"]
