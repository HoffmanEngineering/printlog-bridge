"""The adapter boundary: a new adapter must never need a change to `core/`."""

from __future__ import annotations

import ast
from pathlib import Path

import printlog_bridge.core
from printlog_bridge.adapters import ADAPTERS
from printlog_bridge.core.adapter import Adapter

CORE = Path(printlog_bridge.core.__file__).parent


def imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_core_never_imports_an_adapter():
    offenders = [
        f"{path.name} imports {module}"
        for path in sorted(CORE.rglob("*.py"))
        for module in imported_modules(path)
        if module == "printlog_bridge.adapters" or module.startswith("printlog_bridge.adapters.")
    ]

    assert offenders == []


def test_every_registered_adapter_implements_the_interface_under_its_own_name():
    assert "moonraker" in ADAPTERS
    for name, adapter_type in ADAPTERS.items():
        assert issubclass(adapter_type, Adapter), name
        assert adapter_type.name == name
