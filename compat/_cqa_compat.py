"""Source-checkout compatibility helpers for historical helper scripts.

The canonical implementation lives in ``src/cqa``.  Scripts in ``compat/`` are
kept only for maintainers who still need the pre-0.8 direct Python entry points;
normal users should use the installed ``cqa`` console command.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def load(module_name: str) -> ModuleType:
    src = str(SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    return importlib.import_module(module_name)


def reexport(namespace: dict, module_name: str) -> ModuleType:
    module = load(module_name)
    for name, value in vars(module).items():
        if name.startswith("__") and name not in {"__version__"}:
            continue
        namespace[name] = value
    namespace["__version__"] = getattr(module, "__version__", None)
    return module
