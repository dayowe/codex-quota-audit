#!/usr/bin/env python3
"""Build a release ZIP from a clean Git clone of the current commit.

Only committed files can enter the archive. The ZIP intentionally includes the
clone's ``.git`` directory so maintainers can inspect the exact release history.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="destination ZIP path")
    args = parser.parse_args()
    if not (ROOT / ".git").exists():
        raise SystemExit("release packaging requires a Git checkout")
    version = json.loads((ROOT / "plugins/codex-quota-audit/.codex-plugin/plugin.json").read_text())["version"]
    output = (args.output or (ROOT.parent / f"CodexQuotaAudit_{version}_with_git.zip")).resolve()
    commit = _run("git", "rev-parse", "HEAD")
    if _run("git", "status", "--porcelain"):
        raise SystemExit("working tree is not clean; commit changes before building a release archive")

    with tempfile.TemporaryDirectory(prefix="cqa-release-") as d:
        clone = Path(d) / f"CodexQuotaAudit_{version}"
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(ROOT), str(clone)], check=True)
        subprocess.run(["git", "checkout", "--quiet", commit], cwd=clone, check=True)
        # The archive intentionally keeps Git history, but not a machine-local
        # origin URL pointing back at the maintainer's checkout.
        subprocess.run(["git", "remote", "remove", "origin"], cwd=clone, check=True)
        status = _run("git", "status", "--porcelain", cwd=clone)
        if status:
            raise SystemExit(f"clean release clone unexpectedly dirty:\n{status}")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(clone.rglob("*")):
                if path.is_file():
                    zf.write(path, Path(clone.name) / path.relative_to(clone))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
