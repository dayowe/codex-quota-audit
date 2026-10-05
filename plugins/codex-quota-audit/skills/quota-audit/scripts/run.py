#!/usr/bin/env python3
"""Thin launcher for the bundled canonical CQA package."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    plugin_root = Path(__file__).resolve().parents[3]
    runtime = plugin_root / "runtime"
    sys.path.insert(0, str(runtime))
    from cqa.cli import main as cqa_main
    return int(cqa_main(sys.argv[1:]) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
