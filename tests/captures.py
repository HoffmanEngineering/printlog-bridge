"""The REAL Moonraker captures in tests/fixtures/moonraker (see its README), for tests."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fake_moonraker import FakeMoonraker

CAPTURES = Path(__file__).resolve().parent / "fixtures" / "moonraker"
U1, INFIMECH = "u1", "infimech"


@lru_cache(maxsize=None)
def _load(printer: str, name: str) -> Any:
    text = (CAPTURES / printer / f"{name}.json").read_text(encoding="utf-8")
    return json.loads(text)["result"]


def capture(printer: str, name: str) -> Any:
    """What the printer answered to one call (`status`, `history`, `objects_list`...)."""
    return copy.deepcopy(_load(printer, name))


def job(printer: str, job_id: str) -> dict[str, Any]:
    [found] = [j for j in capture(printer, "history")["jobs"] if j["job_id"] == job_id]
    return found


def moonraker_like(printer: str, **options: Any) -> FakeMoonraker:
    """The fake Moonraker, with the printer's real objects and their real status."""
    moonraker = FakeMoonraker(objects=tuple(capture(printer, "objects_list")["objects"]), **options)
    moonraker.status = capture(printer, "status")["status"]
    return moonraker
