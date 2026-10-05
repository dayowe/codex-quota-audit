#!/usr/bin/env python3
"""Local release gate for Codex Quota Audit.

The gate runs from the repository root, validates canonical/generated mirrors,
builds and installs an offline wheel, keeps every subprocess bounded, and
cleans local build metadata so a release check does not litter the checkout.
"""
from __future__ import annotations

import atexit
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT_SECONDS = 180.0


class Runner:
    def __init__(self, total: int) -> None:
        self.total = total
        self.index = 0

    @staticmethod
    def _text(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    def run(self, label: str, args: list[str], *, env: dict[str, str] | None = None,
            timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.index += 1
        merged = os.environ.copy()
        merged.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        if env:
            merged.update(env)
        prefix = f"[{self.index:02d}/{self.total:02d}] {label}"
        print(f"{prefix:<52} ... ", end="", flush=True)
        started = time.monotonic()
        try:
            proc = subprocess.run(args, cwd=ROOT, env=merged, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            print(f"TIMEOUT ({elapsed:.1f}s)", flush=True)
            self._diagnostic(args, self._text(exc.stdout), self._text(exc.stderr), timeout=timeout)
            raise SystemExit(124)
        elapsed = time.monotonic() - started
        if proc.returncode:
            print(f"FAIL rc={proc.returncode} ({elapsed:.1f}s)", flush=True)
            self._diagnostic(args, proc.stdout, proc.stderr)
            raise SystemExit(proc.returncode)
        print(f"OK ({elapsed:.1f}s)", flush=True)

    @staticmethod
    def _diagnostic(args: list[str], stdout: str, stderr: str, *, timeout: float | None = None) -> None:
        print("\nCommand:", file=sys.stderr)
        print(f"  {shlex.join(args)}", file=sys.stderr)
        if timeout is not None:
            print(f"Timeout: {timeout:g}s", file=sys.stderr)
        if stdout.strip():
            print("\nstdout:", file=sys.stderr)
            print(stdout.rstrip(), file=sys.stderr)
        if stderr.strip():
            print("\nstderr:", file=sys.stderr)
            print(stderr.rstrip(), file=sys.stderr)


def _cleanup_checkout_artifacts() -> None:
    """Remove build metadata created by local wheel validation."""
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    for egg_info in (ROOT / "src").glob("*.egg-info"):
        shutil.rmtree(egg_info, ignore_errors=True)


def main() -> int:
    atexit.register(_cleanup_checkout_artifacts)
    py = sys.executable
    source_env = {"PYTHONPATH": str(ROOT / "src")}
    have_git_check = bool(shutil.which("git") and (ROOT / ".git").exists())
    total = 15 if have_git_check else 14
    runner = Runner(total=total)

    with tempfile.TemporaryDirectory(prefix="cqa-release-check-") as scratch:
        scratch_path = Path(scratch)
        pycache = scratch_path / "pycache"
        runner.run("unit + integration tests", [py, "-m", "unittest", "discover", "-s", "tests", "-v"], timeout=300)
        runner.run("plugin runtime sync check", [py, "tools/sync_plugin_runtime.py", "--check"])
        runner.run("public asset sync check", [py, "tools/sync_public_assets.py", "--check"])
        runner.run("quota self-test", [py, "-m", "cqa.quota.audit", "--self-test"], env=source_env)
        runner.run("workflow self-test", [py, "-m", "cqa.workflow.profile", "--self-test"], env=source_env)
        runner.run("candidate finder self-test", [py, "-m", "cqa.workflow.candidates", "--self-test"], env=source_env)
        runner.run(
            "source compile",
            [py, "-m", "compileall", "-q", "src/cqa", "compat", "tools", "plugins/codex-quota-audit/skills"],
            env={"PYTHONPYCACHEPREFIX": str(pycache)},
        )

        for skill in ("quota-audit", "workflow-profile"):
            launcher = ROOT / "plugins" / "codex-quota-audit" / "skills" / skill / "scripts" / "run.py"
            runner.run(f"plugin launcher: {skill}", [py, str(launcher), "--version"])

        out = scratch_path / "wheel"
        out.mkdir()
        runner.run("offline wheel build", [py, "-m", "pip", "wheel", ".", "--no-build-isolation",
                                           "--no-deps", "-w", str(out)],
                   env={"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
                   timeout=60)
        wheels = list(out.glob("codex_quota_audit-*.whl"))
        if len(wheels) != 1:
            raise SystemExit(f"expected one wheel, found {len(wheels)}")
        wheel = wheels[0]
        unpack = out / "unpacked"
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(unpack)
            names = set(zf.namelist())
        required = {
            "cqa/cli.py",
            "cqa/quota/audit.py",
            "cqa/workflow/profile.py",
            "cqa/workflow/response_efficiency.py",
            "cqa/report/core.py",
            "cqa/research/throughput_compare.py",
            "cqa/assets/dashboard/cqa-dashboard-v1.template.html",
            "cqa/assets/schema/cqa-report-v1.schema.json",
        }
        missing = sorted(required - names)
        if missing:
            raise SystemExit(f"wheel is missing package data: {missing}")
        runner.run("wheel import smoke", [py, "-c",
            "import sys; sys.path.insert(0, r'%s'); from cqa.cli import main; raise SystemExit(main(['--version']))" % unpack], timeout=30)

        prefix = out / "prefix"
        runner.run("wheel install smoke", [py, "-m", "pip", "install", "--no-deps", "--prefix",
                                           str(prefix), str(wheel)],
                   env={"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}, timeout=180)
        scripts = prefix / ("Scripts" if os.name == "nt" else "bin")
        exe = scripts / ("cqa.exe" if os.name == "nt" else "cqa")
        site_candidates = list(prefix.rglob("site-packages"))
        if not exe.exists() or not site_candidates:
            raise SystemExit("installed wheel did not create the cqa console entry point/package")
        installed_env = {"PYTHONPATH": os.pathsep.join(str(x) for x in site_candidates)}
        runner.run("installed cqa console smoke", [str(exe), "--version"], env=installed_env, timeout=30)
        runner.run("installed research help smoke", [str(exe), "research", "help"], env=installed_env, timeout=30)

    if have_git_check:
        runner.run("git diff --check", ["git", "diff", "--check"], timeout=30)

    print("\nRelease check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
