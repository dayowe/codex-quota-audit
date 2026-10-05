#!/usr/bin/env python3
"""Refresh public contract copies from canonical package assets."""
from __future__ import annotations

import argparse
import filecmp
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ASSETS = ROOT / "src" / "cqa" / "assets"
PAIRS = [
    (PACKAGE_ASSETS / "schema" / "cqa-report-v1.schema.json", ROOT / "schema" / "cqa-report-v1.schema.json"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    for src, dst in PAIRS:
        if args.check:
            if not dst.is_file() or not filecmp.cmp(src, dst, shallow=False):
                raise SystemExit(f"public asset out of sync: {dst.relative_to(ROOT)}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"{src.relative_to(ROOT)} -> {dst.relative_to(ROOT)}")
    if args.check:
        print("public assets match canonical package assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
