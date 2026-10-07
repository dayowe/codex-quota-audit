#!/usr/bin/env python3
"""Unified user-facing CLI for Codex Quota Audit.

This is intentionally a thin orchestration layer. Quota inference remains in
``cqa.quota.audit`` and workflow attribution remains in ``cqa.workflow.profile``.
The unified CLI chooses which analyzer to run,
selects privacy-safe workflow candidates when requested, standardizes report
locations, and opens generated HTML reports locally.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import sys
import tempfile
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from .quota import audit
from .workflow import candidates as finder
from .workflow import profile as profiler
from . import reports as reportlib
from . import auto_review_policy as review_policy

__version__ = "0.10.1"


def report_dir(home: str) -> Path:
    return Path(os.path.expanduser(home)).resolve() / "codex-quota-audit"


def default_dashboard_path(home: str) -> Path:
    return report_dir(home) / "latest" / "quota.html"


def default_report_json_path(home: str) -> Path:
    return report_dir(home) / "latest" / "quota.json"


def default_workflow_dashboard_path(home: str) -> Path:
    return report_dir(home) / "latest" / "workflow.html"


def default_workflow_report_json_path(home: str) -> Path:
    return report_dir(home) / "latest" / "workflow.json"


def open_report(path: str | Path) -> bool:
    """Open a generated local report using the platform browser handler."""
    target = Path(path).expanduser().resolve()
    if not target.exists():
        return False
    try:
        return bool(webbrowser.open(target.as_uri(), new=2))
    except Exception:
        return False


def _call_main(func: Callable[[Optional[Sequence[str]]], int], argv: Sequence[str], *, quiet: bool,
               call_kwargs: Optional[dict] = None) -> int:
    """Call a compatibility CLI main() while optionally suppressing normal chatter."""
    kwargs = call_kwargs or {}
    if not quiet:
        try:
            return int(func(argv, **kwargs) or 0)
        except SystemExit as exc:
            return int(exc.code or 0)

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = int(func(argv, **kwargs) or 0)
    except SystemExit as exc:
        rc = int(exc.code or 0)
    captured_err = stderr.getvalue().strip()
    if captured_err:
        print(captured_err, file=sys.stderr)
    if rc:
        captured_out = stdout.getvalue().strip()
        if captured_out:
            print(captured_out, file=sys.stderr)
    return rc


class _ProgressReporter:
    """Small terminal progress surface for long workflow profiling runs."""

    def __init__(self, stream=None, *, tty: Optional[bool] = None) -> None:
        self.stream = stream or sys.stdout
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)()) if tty is None else bool(tty)
        self.started = time.monotonic()
        self.stage_started = self.started
        self.label: Optional[str] = None
        self.detail = ""

    def header(self, title: str) -> None:
        print(title, file=self.stream, flush=True)
        print(file=self.stream, flush=True)

    def _text(self, label: str, detail: str = "") -> str:
        return label + (f" · {detail}" if detail else "")

    def begin(self, label: str, detail: str = "") -> None:
        self.finish()
        self.label = label
        self.detail = detail
        self.stage_started = time.monotonic()
        text = self._text(label, detail)
        if self.tty:
            self.stream.write(f"\r\x1b[2K◐ {text}")
            self.stream.flush()
        else:
            print(f"[cqa] {text}", file=self.stream, flush=True)

    def update(self, detail: str) -> None:
        if not self.label or detail == self.detail:
            return
        self.detail = detail
        text = self._text(self.label, detail)
        if self.tty:
            self.stream.write(f"\r\x1b[2K◐ {text}")
            self.stream.flush()
        else:
            print(f"[cqa] {text}", file=self.stream, flush=True)

    def finish(self) -> None:
        if not self.label:
            return
        elapsed = time.monotonic() - self.stage_started
        if self.tty:
            self.stream.write(f"\r\x1b[2K✓ {self._text(self.label, self.detail)} · {elapsed:.1f}s\n")
            self.stream.flush()
        self.label = None
        self.detail = ""

    def complete(self, label: str = "Report ready") -> None:
        self.finish()
        elapsed = time.monotonic() - self.started
        prefix = "✓ " if self.tty else "[cqa] "
        print(f"{prefix}{label} · {elapsed:.1f}s", file=self.stream, flush=True)

    def interrupted(self) -> None:
        if self.tty and self.label:
            self.stream.write("\r\x1b[2K")
            self.stream.flush()
        label = self.label or "workflow profiling"
        self.label = None
        print(f"Interrupted while {label.lower()}.", file=self.stream, flush=True)

    def event(self, name: str, **meta: object) -> None:
        current = int(meta.get("current") or 0)
        total = int(meta.get("total") or 0)
        counters = ""
        if "cached" in meta:
            counters = f" · {int(meta.get('cached') or 0):,} cached, {int(meta.get('processed') or 0):,} processed, {int(meta.get('bytes_read') or 0)/1048576:.1f} MiB read"
        if name == "timings_done":
            self.finish()
            stages = meta.get("stages") or {}
            print("[cqa] Timings · " + ", ".join(f"{k}={float(v):.3f}s" for k, v in stages.items()), file=self.stream)
        elif name == "discovery_done":
            self.update(f"{total:,} log files{counters}" + (" · discovery reused" if meta.get("reused") else ""))
            self.finish()
        elif name == "scan_start":
            self.begin("Resolving workflow", f"{total:,} log files")
        elif name == "scan_progress":
            self.update(f"{current:,}/{total:,} log files{counters}")
        elif name == "parse_start":
            self.begin("Reading workflow telemetry", f"{total:,} sessions")
        elif name == "parse_progress":
            self.update(f"{current:,}/{total:,} sessions{counters}")
        elif name == "structure_start":
            self.begin("Building workflow structure")
        elif name == "usage_start":
            self.begin("Analyzing usage & pricing")
        elif name == "performance_start":
            self.begin("Analyzing model performance")
        elif name == "compaction_start":
            self.begin("Analyzing compactions & timeline")
        elif name == "render_start":
            self.begin("Rendering dashboard")


def _discovery_options(extra, root_role=None, role_map=None, reporter=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--rebuild-cache", action="store_true")
    parser.add_argument("--cache-dir")
    parser.add_argument("--workers", type=int, choices=range(1,17), default=1, metavar="N")
    parser.add_argument("--roles", nargs="+", default=list(finder.DEFAULT_ROLES))
    args, _ = parser.parse_known_args(extra)
    options = {}
    if args.no_cache:
        options["use_cache"] = False
    if args.rebuild_cache:
        options["rebuild_cache"] = True
    if args.cache_dir:
        options["cache_dir"] = args.cache_dir
    if args.workers != 1:
        options["workers"] = args.workers
    roles = [r.strip().lower() for r in args.roles if r.strip()]
    if root_role:
        roles.append(profiler.attribution._normalize_declared_role(root_role))
    if role_map:
        roles.extend(sorted(profiler.attribution.role_map_roles(role_map)))
    roles = tuple(dict.fromkeys(roles))
    if roles != finder.DEFAULT_ROLES:
        options["roles"] = roles
    if reporter is not None:
        options["events"] = reporter.event
    return options


def _cache_arguments(extra):
    """Forward shared cache controls without forwarding quota-only switches."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--rebuild-cache", action="store_true")
    parser.add_argument("--cache-dir")
    args, _ = parser.parse_known_args(extra)
    result = []
    if args.no_cache:
        result.append("--no-cache")
    if args.rebuild_cache:
        result.append("--rebuild-cache")
    if args.cache_dir:
        result.extend(("--cache-dir", args.cache_dir))
    return result


def _discover_latest(home: str, recent_days: float, *, multi_agent_only: bool = False, **options) -> tuple[Optional[finder.Family], finder.Discovery]:
    discovery = finder.discover_workflow_families(home, options.pop("roles", finder.DEFAULT_ROLES), recent_days=recent_days, **options)
    family = finder.latest_workflow_family(
        discovery.families, discovery.sessions, recent_days=recent_days,
        allow_standalone_fallback=not multi_agent_only,
        require_delegated_worker=multi_agent_only,
        exclude_session_identifiers=(
            finder.current_codex_session_identifiers() if multi_agent_only else ()
        ),
    )
    return family, discovery


def _format_latest_summary(family: finder.Family, discovery: finder.Discovery) -> str:
    last = family.last_ts(discovery.sessions)
    last_text = last.astimezone().strftime("%Y-%m-%d %H:%M") if last else "unknown time"
    kind = "linked workflow" if len(family.members) > 1 else "standalone session"
    return f"{kind}, {len(family.members)} session(s), ended {last_text}"


def _reject_forwarded_output_controls(extra: Sequence[str], parser: argparse.ArgumentParser) -> None:
    forbidden = {
        "--dashboard", "--report-json", "--workflow-profile-json", "--home", "--prices",
    }
    for token in extra:
        key = token.split("=", 1)[0]
        if key in forbidden:
            parser.error(f"{key} is controlled by the unified dashboard command")


def _load_json(path: Path) -> dict:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return obj


def _copy_requested_json(temp_json: Path, requested: Path | None) -> None:
    if requested is None:
        return
    requested.parent.mkdir(parents=True, exist_ok=True)
    requested.write_bytes(temp_json.read_bytes())


def dashboard_main(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(
        prog="cqa dashboard",
        description="Build the local quota dashboard, optionally including a workflow profile.",
        epilog=(
            "Default runs are archived in the local CQA report library. "
            "Unrecognized quota-analyzer options are forwarded to the quota analyzer."
        ),
    )
    p.add_argument("--home", default="~/.codex", help="Codex data directory")
    p.add_argument("--prices", help="price normalization override accepted by the quota/workflow analyzers")
    review_policy.add_arguments(p)
    p.add_argument("--output", metavar="PATH", help="explicit HTML export path; bypasses the report library")
    p.add_argument("--name", metavar="LABEL", help="optional friendly label stored with this report")
    p.add_argument("--report-json", nargs="?", const="__DEFAULT__", metavar="PATH",
                   help="also preserve cqa-report-v1 JSON; omit PATH to keep it beside the archived report")
    p.add_argument("--workflow", default="none", metavar="none|latest|ID",
                   help="attach no workflow, the latest inspectable workflow, or a W-/S-/raw session selector")
    p.add_argument("--workflow-profile-json", action="append", default=[], metavar="PATH",
                   help="attach an existing privacy-safe workflow-profiler export; repeatable")
    p.add_argument("--workflow-profile", choices=("generic", "staged"), default="generic",
                   help="interpretation profile used when --workflow triggers a fresh workflow analysis")
    p.add_argument("--workflow-root-role", metavar="ROLE", help="explicit role for the attached workflow root; workflow-agnostic")
    p.add_argument("--workflow-role-map", metavar="JSON", help="optional local workflow-role-map-v1 overrides for the attached workflow")
    p.add_argument("--workflow-multi-agent-only", action="store_true",
                   help="when --workflow latest is used, require a delegated non-Guardian worker and exclude the active Codex session")
    p.add_argument("--recent-days", type=float, default=90.0,
                   help="lookback used to resolve --workflow latest")
    p.add_argument("--banked-reset", action="append", default=[], metavar="TIMESTAMP",
                   help="user-confirmed banked reset; repeatable")
    p.add_argument("--history", action="store_true", help="include historical tables in analyzer output")
    p.add_argument("--diagnostics", action="store_true", help="run detailed analyzer diagnostics")
    p.add_argument("--no-guardian-audit", action="store_true", help="skip Guardian analysis")
    p.add_argument("--include-replays", action="store_true", help="include probable replay prefixes")
    p.add_argument("--no-open", action="store_true", help="write the dashboard without opening a browser")
    p.add_argument("--quiet", action="store_true", help="suppress progress chatter; still print the final report path")
    p.add_argument("--show-analysis-output", action="store_true",
                   help="show verbose output from the underlying analyzers instead of the concise front-end summary")
    args, extra = p.parse_known_args(argv)
    _reject_forwarded_output_controls(extra, p)
    if not math.isfinite(args.recent_days) or args.recent_days <= 0:
        p.error("--recent-days must be positive")

    home = os.path.expanduser(args.home)
    reportlib.ensure_dirs(home)
    custom_output = Path(args.output).expanduser() if args.output else None
    if custom_output is not None:
        custom_output.parent.mkdir(parents=True, exist_ok=True)
    requested_json = None
    if args.report_json and args.report_json != "__DEFAULT__":
        requested_json = Path(args.report_json).expanduser()

    profiles = [str(Path(x).expanduser()) for x in args.workflow_profile_json]
    workflow_selector = args.workflow.strip()
    selected_source = None
    discovery = None
    reporter = None if args.quiet or args.show_analysis_output else _ProgressReporter(sys.stdout)
    if reporter is not None:
        reporter.header("Codex Quota Audit · dashboard")

    with tempfile.TemporaryDirectory(prefix="cqa-run-") as tmp:
        tmpdir = Path(tmp)
        if workflow_selector.lower() != "none":
            if workflow_selector.lower() == "latest":
                if not args.quiet:
                    print("Discovering latest workflow…")
                try:
                    family, discovery = _discover_latest(home, args.recent_days, multi_agent_only=args.workflow_multi_agent_only, **_discovery_options(extra, args.workflow_root_role, args.workflow_role_map, reporter))
                except ValueError as exc:
                    p.error(str(exc))
                except KeyboardInterrupt:
                    if reporter is not None:
                        reporter.interrupted()
                    return 130
                if family is None:
                    if args.workflow_multi_agent_only:
                        print("No recent multi-agent workflow with delegated non-Guardian worker usage was found in the selected Codex history.", file=sys.stderr)
                    else:
                        print("No workflow with observed usage was found in the selected Codex history.", file=sys.stderr)
                    return 1
                workflow_selector = family.family_key
                if not args.quiet:
                    print(f"Selected latest {_format_latest_summary(family, discovery)}.")
            selected_source = workflow_selector
            profile_path = tmpdir / "workflow-profile.json"
            profile_args = [
                "--family", workflow_selector,
                "--home", home,
                "--workflow-profile", args.workflow_profile,
                "--export-json", str(profile_path),
            ]
            profile_args += _cache_arguments(extra)
            profile_args += ["--auto-review-auth-mode", args.auto_review_auth_mode]
            if args.prices:
                profile_args += ["--prices", args.prices]
            if args.workflow_root_role:
                profile_args += ["--root-role", args.workflow_root_role]
            if args.workflow_role_map:
                profile_args += ["--role-map", args.workflow_role_map]
            if reporter is None and not args.quiet:
                print("Profiling workflow…")
            try:
                rc = _call_main(
                    profiler.main, profile_args, quiet=not args.show_analysis_output,
                    call_kwargs=({**({"progress": reporter.event} if reporter is not None else {}), **({"discovery": discovery} if discovery is not None else {})} or None),
                )
            except KeyboardInterrupt:
                if reporter is not None:
                    reporter.interrupted()
                return 130
            if rc:
                return rc
            if reporter is not None:
                reporter.finish()
            profiles.append(str(profile_path))

        output = custom_output or (tmpdir / "report.html")
        temp_json = tmpdir / "report.json"
        quota_args: list[str] = ["--home", home, "--dashboard", str(output), "--report-json", str(temp_json)]
        quota_args += ["--auto-review-auth-mode", args.auto_review_auth_mode]
        if args.prices:
            quota_args += ["--prices", args.prices]
        for marker in args.banked_reset:
            quota_args += ["--banked-reset", marker]
        if args.history:
            quota_args.append("--history")
        if args.diagnostics:
            quota_args.append("--diagnostics")
        if args.no_guardian_audit:
            quota_args.append("--no-guardian-audit")
        if args.include_replays:
            quota_args.append("--include-replays")
        for path in profiles:
            quota_args += ["--workflow-profile-json", path]
        # A fresh workflow already rebuilt and seeded the shared cache.
        quota_args += [token for token in extra if token != "--rebuild-cache" or workflow_selector.lower() == "none"]

        if reporter is not None:
            reporter.begin("Building quota dashboard")
        elif not args.quiet:
            print("Building quota dashboard…")
        try:
            rc = _call_main(audit.main, quota_args, quiet=not args.show_analysis_output)
        except KeyboardInterrupt:
            if reporter is not None:
                reporter.interrupted()
            return 130
        if rc:
            return rc
        if reporter is not None:
            reporter.finish()

        report = _load_json(temp_json)
        if custom_output is None:
            keep_json = temp_json if args.report_json else None
            entry = reportlib.register_report(
                home, output, report, json_source=keep_json,
                friendly_name=args.name, source_selector=selected_source,
            )
            output = reportlib.root(home) / str(entry["html"])
            archive_json = reportlib.root(home) / str(entry["json"]) if entry.get("json") else None
            if requested_json is not None:
                _copy_requested_json(temp_json, requested_json)
                json_print = requested_json
            else:
                json_print = archive_json
        else:
            custom_output.parent.mkdir(parents=True, exist_ok=True)
            if requested_json is not None:
                _copy_requested_json(temp_json, requested_json)
                json_print = requested_json
            elif args.report_json:
                default_json = default_report_json_path(home)
                _copy_requested_json(temp_json, default_json)
                json_print = default_json
            else:
                json_print = None

    if reporter is not None:
        reporter.complete()
    print(f"Dashboard: {output.expanduser()}")
    if json_print is not None:
        print(f"Report JSON: {json_print.expanduser()}")
    if args.no_open:
        return 0
    if open_report(output):
        if not args.quiet:
            print("Opened dashboard in your browser.")
    elif not args.quiet:
        print("Dashboard was generated, but no browser handler accepted the open request.")
    return 0

def audit_main(argv: Sequence[str]) -> int:
    return _call_main(audit.main, argv, quiet=False)


def workflow_candidates_main(argv: Sequence[str]) -> int:
    return _call_main(finder.main, argv, quiet=False)


def workflow_profile_main(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(
        prog="cqa workflow profile",
        description="Profile a workflow and build the workflow dashboard. Defaults to the latest inspectable workflow.",
    )
    p.add_argument("selector", nargs="?", default="latest", help="W-/S-/raw session selector, or 'latest'")
    p.add_argument("--home", default="~/.codex", help="Codex data directory")
    p.add_argument("--output", metavar="PATH", help="explicit HTML export path; bypasses the report library")
    p.add_argument("--name", metavar="LABEL", help="optional friendly label stored with this workflow report")
    p.add_argument("--report-json", nargs="?", const="__DEFAULT__", metavar="PATH",
                   help="also preserve cqa-report-v1 JSON; omit PATH to keep it beside the archived report")
    p.add_argument("--export-json", metavar="PATH", help="also write the detailed privacy-safe workflow profile")
    p.add_argument("--recent-days", type=float, default=90.0, help="lookback for the 'latest' selector")
    p.add_argument("--multi-agent-only", action="store_true",
                   help="for 'latest', require a delegated non-Guardian worker and exclude the active Codex session")
    p.add_argument("--root-role", metavar="ROLE", help="explicit role for the selected workflow root; workflow-agnostic")
    p.add_argument("--role-map", metavar="JSON", help="optional local workflow-role-map-v1 overrides")
    p.add_argument("--no-open", action="store_true", help="write the dashboard without opening it")
    p.add_argument("--quiet", action="store_true", help="suppress progress chatter; still print the final report path")
    p.add_argument("--show-analysis-output", action="store_true", help="show detailed profiler output")
    review_policy.add_arguments(p)
    args, extra = p.parse_known_args(argv)
    if not math.isfinite(args.recent_days) or args.recent_days <= 0:
        p.error("--recent-days must be positive")
    forbidden = {"--family", "--session", "--session-id", "--dashboard", "--report-json", "--home"}
    for token in extra:
        if token.split("=", 1)[0] in forbidden:
            p.error(f"{token.split('=', 1)[0]} is controlled by the unified workflow profile command")

    home = os.path.expanduser(args.home)
    reportlib.ensure_dirs(home)
    reporter = None if args.quiet or args.show_analysis_output else _ProgressReporter(sys.stdout)
    if reporter is not None:
        reporter.header("Codex Quota Audit · workflow profile")
    selector = args.selector
    discovery = None
    if selector.lower() == "latest":
        if not args.quiet:
            print("Discovering latest workflow…")
        try:
            family, discovery = _discover_latest(home, args.recent_days, multi_agent_only=args.multi_agent_only, **_discovery_options(extra, args.root_role, args.role_map, reporter))
        except ValueError as exc:
            p.error(str(exc))
        except KeyboardInterrupt:
            if reporter is not None:
                reporter.interrupted()
            return 130
        if family is None:
            if args.multi_agent_only:
                print("No recent multi-agent workflow with delegated non-Guardian worker usage was found in the selected Codex history.", file=sys.stderr)
            else:
                print("No workflow with observed usage was found in the selected Codex history.", file=sys.stderr)
            return 1
        selector = family.family_key
        if not args.quiet:
            print(f"Selected latest {_format_latest_summary(family, discovery)}.")

    custom_output = Path(args.output).expanduser() if args.output else None
    if custom_output is not None:
        custom_output.parent.mkdir(parents=True, exist_ok=True)
    requested_json = Path(args.report_json).expanduser() if args.report_json and args.report_json != "__DEFAULT__" else None
    with tempfile.TemporaryDirectory(prefix="cqa-workflow-report-") as tmp:
        tmpdir = Path(tmp)
        output = custom_output or (tmpdir / "workflow.html")
        temp_json = tmpdir / "workflow-report.json"
        profile_args: list[str] = ["--family", selector, "--home", home, "--dashboard", str(output), "--report-json", str(temp_json)]
        profile_args += ["--auto-review-auth-mode", args.auto_review_auth_mode]
        if args.export_json:
            profile_args += ["--export-json", args.export_json]
        if args.root_role:
            profile_args += ["--root-role", args.root_role]
        if args.role_map:
            profile_args += ["--role-map", args.role_map]
        profile_args += list(extra)
        if reporter is None and not args.quiet:
            print("Profiling workflow…")
        try:
            rc = _call_main(
                profiler.main, profile_args, quiet=not args.show_analysis_output,
                call_kwargs=({**({"progress": reporter.event} if reporter is not None else {}), **({"discovery": discovery} if discovery is not None else {})} or None),
            )
        except KeyboardInterrupt:
            if reporter is not None:
                reporter.interrupted()
            return 130
        if rc:
            return rc
        if reporter is not None:
            reporter.finish()
        report = _load_json(temp_json)
        if custom_output is None:
            entry = reportlib.register_report(
                home, output, report, json_source=(temp_json if args.report_json else None),
                friendly_name=args.name, source_selector=selector,
            )
            output = reportlib.root(home) / str(entry["html"])
            archive_json = reportlib.root(home) / str(entry["json"]) if entry.get("json") else None
            if requested_json is not None:
                _copy_requested_json(temp_json, requested_json)
                json_print = requested_json
            else:
                json_print = archive_json
        else:
            custom_output.parent.mkdir(parents=True, exist_ok=True)
            if requested_json is not None:
                _copy_requested_json(temp_json, requested_json)
                json_print = requested_json
            elif args.report_json:
                default_json = default_workflow_report_json_path(home)
                _copy_requested_json(temp_json, default_json)
                json_print = default_json
            else:
                json_print = None

    if reporter is not None:
        reporter.complete()
    print(f"Workflow dashboard: {output.expanduser()}")
    if json_print is not None:
        print(f"Report JSON: {json_print.expanduser()}")
    if args.no_open:
        return 0
    if open_report(output):
        if not args.quiet:
            print("Opened workflow dashboard in your browser.")
    elif not args.quiet:
        print("Workflow dashboard was generated, but no browser handler accepted the open request.")
    return 0

def reports_main(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(
        prog="cqa reports",
        description="Browse the local Codex Quota Audit report library.",
    )
    p.add_argument("--home", default="~/.codex", help="Codex data directory")
    sub = p.add_subparsers(dest="command")
    lp = sub.add_parser("list", help="list recent reports in the terminal")
    lp.add_argument("--type", choices=("quota", "workflow", "combined", "usage"))
    op = sub.add_parser("open", help="open a report by ID or latest alias")
    op.add_argument("selector", nargs="?", default="latest", help="report ID, unique friendly name, latest, latest-workflow, latest-quota, latest-combined, or latest-usage")
    args = p.parse_args(argv)
    home = os.path.expanduser(args.home)
    reportlib.ensure_dirs(home)
    if args.command == "list":
        rows = reportlib.list_entries(home, report_type_filter=args.type)
        if not rows:
            print("No CQA reports found.")
            return 0
        print(f"{'ID':<28} {'TYPE':<9} {'ANALYZED':<20} NAME / SUMMARY")
        print("-" * 100)
        for e in rows:
            start = e.get("analysis_start")
            end = e.get("analysis_end")
            if start and end:
                try:
                    a = datetime.fromisoformat(str(start).replace("Z", "+00:00")).astimezone()
                    b = datetime.fromisoformat(str(end).replace("Z", "+00:00")).astimezone()
                    analyzed = f"{a.strftime('%b %d')}→{b.strftime('%b %d')}"
                except ValueError:
                    analyzed = "—"
            else:
                analyzed = "—"
            name = e.get("name") or e.get("workflow_ref") or ""
            summary = e.get("summary") if isinstance(e.get("summary"), dict) else {}
            if e.get("type") == "usage":
                analyzed = str(summary.get("period_label") or analyzed)
            if not name:
                name = "Quota audit" if e.get("type") == "quota" else "Usage report" if e.get("type") == "usage" else "CQA report"
            extra = []
            if summary.get("sessions") is not None:
                extra.append(f"{int(summary['sessions']):,} sessions")
            if summary.get("requests") is not None:
                extra.append(f"{int(summary['requests']):,} {'usage records' if e.get('type') == 'usage' else 'turns'}")
            if e.get("type") == "usage":
                extra.append(str(summary.get("timezone") or "UTC"))
            print(f"{str(e.get('id') or ''):<28} {str(e.get('type') or ''):<9} {analyzed:<20} {name}{(' · ' + ' · '.join(extra)) if extra else ''}")
        return 0
    if args.command == "open":
        entry = reportlib.resolve_entry(home, args.selector)
        if entry is None:
            print(f"No unique report matches: {args.selector}", file=sys.stderr)
            return 1
        path = reportlib.root(home) / str(entry["html"])
        print(f"Report: {path}")
        return 0 if open_report(path) else 1

    # Default human UX: open a locally generated picker/library page.
    path = reportlib.write_index(home)
    print(f"Report library: {path}")
    if open_report(path):
        return 0
    print("Report library was generated, but no browser handler accepted the open request.")
    return 0


def report_validate_main(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="cqa report validate", description="Validate a cqa-report-v1 JSON document.")
    p.add_argument("path")
    args = p.parse_args(argv)
    try:
        import jsonschema
    except ImportError:
        print("Report validation requires the optional development dependency 'jsonschema'.", file=sys.stderr)
        return 2
    try:
        report = json.loads(Path(args.path).expanduser().read_text(encoding="utf-8"))
        schema_path = Path(__file__).resolve().parent / "assets" / "schema" / "cqa-report-v1.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(report)
    except (OSError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
        print(f"Invalid report: {exc}", file=sys.stderr)
        return 1
    print(f"Valid cqa-report-v1: {Path(args.path).expanduser()}")
    return 0


def print_help() -> None:
    print(f"""Codex Quota Audit unified CLI v{__version__}

Usage:
  cqa dashboard [options]                 Build the quota dashboard
  cqa audit [quota-analyzer options]      Run the existing quota CLI
  cqa usage [options]                    Count monthly or timestamp-range usage
  cqa workflow candidates [options]       Find privacy-safe workflow candidates
  cqa workflow profile [ID|latest]        Profile a workflow and build its dashboard
  cqa reports [list|open]                 Browse the local report library
  cqa report validate REPORT.json         Validate cqa-report-v1
  cqa research throughput-compare [ID]    Compare CQA and Tokscale-style throughput

Common examples:
  cqa dashboard
  cqa dashboard --workflow latest
  cqa dashboard --workflow latest --report-json
  cqa dashboard --banked-reset 2026-09-22T23:33 --no-open
  cqa workflow profile latest
  cqa workflow profile latest --multi-agent-only
  cqa reports
  cqa reports list
  cqa usage --month 2026-09 --timezone Europe/Berlin --dashboard
  cqa research throughput-compare latest

Historical direct-script entry points live under compat/; the cqa command is the supported interface.
""")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print_help()
        return 0
    if args[0] in {"--version", "-V"}:
        print(f"cqa {__version__} (quota {audit.__version__}, workflow {profiler.__version__})")
        return 0

    command = args.pop(0)
    if command == "dashboard":
        return dashboard_main(args)
    if command == "audit":
        return audit_main(args)
    if command == "usage":
        from .usage import cli as usage_cli
        return usage_cli.main(args)
    if command == "workflow":
        if not args or args[0] in {"-h", "--help", "help"}:
            print("Usage: cqa workflow candidates [options]\n       cqa workflow profile [ID|latest] [options]")
            return 0
        sub = args.pop(0)
        if sub == "candidates":
            return workflow_candidates_main(args)
        if sub == "profile":
            return workflow_profile_main(args)
        print(f"Unknown workflow command: {sub}", file=sys.stderr)
        return 2
    if command == "reports":
        return reports_main(args)
    if command == "report":
        if not args or args[0] in {"-h", "--help", "help"}:
            print("Usage: cqa report validate REPORT.json")
            return 0
        sub = args.pop(0)
        if sub == "validate":
            return report_validate_main(args)
        print(f"Unknown report command: {sub}", file=sys.stderr)
        return 2

    if command == "research":
        if not args or args[0] in {"-h", "--help", "help"}:
            print("Usage: cqa research throughput-compare [ID|latest] [options]")
            return 0
        sub = args.pop(0)
        if sub == "throughput-compare":
            from .research import throughput_compare
            return throughput_compare.main(args)
        print(f"Unknown research command: {sub}", file=sys.stderr)
        return 2

    print(f"Unknown command: {command}", file=sys.stderr)
    print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
