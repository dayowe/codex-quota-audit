#!/usr/bin/env python3
"""Local release gate for Codex Quota Audit.

The gate runs from the repository root, validates canonical/generated mirrors,
builds and installs an offline wheel, keeps every subprocess bounded, and
cleans local build metadata so a release check does not litter the checkout.
"""
from __future__ import annotations

import atexit
import json
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
    total = 16 if have_git_check else 15
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
            "cqa/usage/__init__.py",
            "cqa/usage/analysis.py",
            "cqa/usage/cli.py",
            "cqa/usage/loader.py",
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
        usage_home = scratch_path / "usage-home"
        (usage_home / "sessions").mkdir(parents=True)
        usage_rows = [
            {"timestamp": "2026-09-12T12:00:00Z", "type": "session_meta",
             "payload": {"model": "gpt-6.1-sol", "auth_mode": "chatgpt"}},
            {"timestamp": "2026-09-12T12:00:01Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {"last_token_usage": {
                 "input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10}}}},
        ]
        (usage_home / "sessions" / "sample.jsonl").write_text(
            "\n".join(json.dumps(row) for row in usage_rows) + "\n", encoding="utf-8")
        usage_json = scratch_path / "usage.json"
        usage_html = scratch_path / "usage.html"
        runner.run("installed usage totals smoke", [str(exe), "usage", "--home", str(usage_home),
                   "--month", "2026-09", "--quiet", "--report-json", str(usage_json),
                   "--dashboard", str(usage_html), "--no-open"],
                   env=installed_env, timeout=30)
        usage_result = json.loads(usage_json.read_text(encoding="utf-8"))["report"]["extensions"]["usage"]
        if (usage_result["summary"]["requests"] != 1 or usage_result["summary"]["total_tokens"] != 110
                or usage_result["coverage"]["without_quota_snapshot_requests"] != 1
                or abs(usage_result["summary"]["api_list_equivalent_usd"] - .000205) > 1e-12):
            raise SystemExit("installed usage command did not preserve unmetered tokens and pricing")
        if (usage_result["by_day"][0]["total_tokens"] != 110
                or 'id="usageDailyChart"' not in usage_html.read_text(encoding="utf-8")):
            raise SystemExit("installed usage dashboard did not preserve daily work or include its renderer")

    if have_git_check:
        runner.run("git diff --check", ["git", "diff", "--check"], timeout=30)

    print("\nRelease check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
