"""The dashboard talks to the backend only over HTTP (spec 2, 15.2): it never imports it."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[2] / "dashboard"
BACKEND = "rfq_agent"


def _modules() -> list[Path]:
    return sorted(DASHBOARD.rglob("*.py"))


def _imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_dashboard_module_imports_the_backend() -> None:
    modules = _modules()
    assert DASHBOARD / "app.py" in modules
    offenders = {
        str(path.relative_to(DASHBOARD)): sorted(
            n for n in _imported_names(path) if n.split(".")[0] == BACKEND
        )
        for path in modules
    }
    assert {k: v for k, v in offenders.items() if v} == {}


BLOCK_BACKEND = f"""
import importlib, importlib.abc, pathlib, sys

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == {BACKEND!r}:
            raise ImportError("dashboard imported the backend: " + name)
        return None

sys.meta_path.insert(0, Block())
sys.path[:] = [p for p in sys.path if not p.endswith("src")]
root = pathlib.Path({str(DASHBOARD.parent)!r})
sys.path.insert(0, str(root))
for path in sorted((root / "dashboard").rglob("*.py")):
    parts = path.relative_to(root).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    importlib.import_module(".".join(parts))
assert "dashboard.app" in sys.modules
print("ok")
"""


def test_every_dashboard_module_imports_without_the_backend() -> None:
    result = subprocess.run(
        [sys.executable, "-c", BLOCK_BACKEND], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
