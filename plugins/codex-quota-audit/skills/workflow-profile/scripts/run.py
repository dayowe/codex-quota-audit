#!/usr/bin/env python3
"""Thin launcher for the bundled canonical CQA package.

The workflow skill keeps normal plugin output out of the caller's project tree:
normal runs omit ``--output`` so the unified CLI archives them in the local
report library. Explicit relative outputs are moved under the selected Codex
home instead of leaking into the caller project; absolute paths are preserved.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _arg_value(args: list[str], name: str, default: str | None = None) -> str | None:
    for i, token in enumerate(args):
        if token == name and i + 1 < len(args):
            return args[i + 1]
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return default


def _normalize_workflow_output(args: list[str]) -> list[str]:
    if len(args) < 2 or args[0:2] != ["workflow", "profile"]:
        return args
    out = list(args)
    home = Path(_arg_value(out, "--home", "~/.codex") or "~/.codex").expanduser().resolve()
    exports = home / "codex-quota-audit" / "exports"
    output = _arg_value(out, "--output")
    if output is None:
        return out
    expanded = Path(output).expanduser()
    if not expanded.is_absolute():
        # A relative plugin output is almost always an accidental CWD leak. Keep
        # normal skill artifacts in the standard report directory instead.
        for i, token in enumerate(out):
            if token == "--output" and i + 1 < len(out):
                out[i + 1] = str(exports / expanded.name)
                break
            if token.startswith("--output="):
                out[i] = "--output=" + str(exports / expanded.name)
                break
    return out


def main() -> int:
    plugin_root = Path(__file__).resolve().parents[3]
    runtime = plugin_root / "runtime"
    sys.path.insert(0, str(runtime))
    from cqa.cli import main as cqa_main
    return int(cqa_main(_normalize_workflow_output(sys.argv[1:])) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
