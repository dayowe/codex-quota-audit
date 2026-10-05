#!/usr/bin/env python3
"""Sync the self-contained plugin runtime from the canonical ``src/cqa`` tree.

``src/cqa`` is the only editable implementation.  The plugin keeps a generated
runtime mirror so marketplace installs remain self-contained without requiring
an additional pip install.
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "cqa"
TARGET = ROOT / "plugins" / "codex-quota-audit" / "runtime" / "cqa"


def _files(root: Path) -> set[Path]:
    return {
        p.relative_to(root)
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }


def is_synced() -> bool:
    if not TARGET.is_dir():
        return False
    source_files = _files(SOURCE)
    target_files = _files(TARGET)
    if source_files != target_files:
        return False
    return all(filecmp.cmp(SOURCE / rel, TARGET / rel, shallow=False) for rel in source_files)


def sync() -> None:
    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        SOURCE,
        TARGET,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the plugin mirror differs from src/cqa")
    args = parser.parse_args(argv)
    if args.check:
        if not is_synced():
            raise SystemExit("plugin runtime is out of sync; run tools/sync_plugin_runtime.py")
        print("plugin runtime matches src/cqa")
        return 0
    sync()
    print(f"{SOURCE.relative_to(ROOT)} -> {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
