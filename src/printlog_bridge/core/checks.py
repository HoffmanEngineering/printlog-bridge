"""Results reported by `printlog-bridge doctor`."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Status(Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "FAIL"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str

    @classmethod
    def ok(cls, name: str, detail: str) -> CheckResult:
        return cls(name, Status.OK, detail)

    @classmethod
    def warn(cls, name: str, detail: str) -> CheckResult:
        return cls(name, Status.WARN, detail)

    @classmethod
    def fail(cls, name: str, detail: str) -> CheckResult:
        return cls(name, Status.FAIL, detail)
