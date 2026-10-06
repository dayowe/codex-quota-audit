import tempfile
import sys
import unittest
import json
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from cqa import cli as cqa
from cqa import reports as reportlib
from cqa.workflow import candidates as finder


class UnifiedCliTests(unittest.TestCase):
    def test_combined_rebuild_does_not_discard_fresh_workflow_cache(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call_main) as call:
            self.assertEqual(cqa.dashboard_main(["--home", directory, "--workflow", "W-test",
                                                  "--rebuild-cache", "--no-open", "--quiet"]), 0)
            self.assertIn("--rebuild-cache", call.call_args_list[0].args[1])
            self.assertNotIn("--rebuild-cache", call.call_args_list[1].args[1])

    def _session(self, key, when, tokens=100, *, source_kind="unknown", model=None):
        session = finder.Session(
            path=f"/{key}.jsonl", session_key=key,
            first_ts=when, last_ts=when + timedelta(minutes=5),
            input_tokens=tokens, output_tokens=1, source_kind=source_kind,
        )
        if model:
            session.models[model] += 1
        return session

    def _fake_report(self, *, workflow=False):
        report = {
            "schema": "cqa-report",
            "schema_version": "1.0.0",
            "report": {
                "generated_at": "2026-10-03T12:00:00Z",
                "observed_range": {"start": "2026-09-01T00:00:00Z", "end": "2026-10-03T12:00:00Z"},
            },
            "quota": {"status": "not_requested" if workflow else "complete", "cohorts": []},
            "guardian": {"status": "not_requested"},
            "banked_resets": {"status": "not_requested"},
            "workflow": {"status": "not_requested", "profiles": []},
        }
        if workflow:
            report["workflow"] = {
                "status": "complete",
                "profiles": [{
                    "analysis_window": {"start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z"},
                    "summary": {"sessions": 2, "requests": 3, "raw_tokens": 1000},
                }],
            }
        return report

    def _fake_call_main(self, func, argv, *, quiet, call_kwargs=None):
        args = list(argv)
        if "--export-json" in args:
            path = Path(args[args.index("--export-json") + 1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
        if "--dashboard" in args:
            path = Path(args[args.index("--dashboard") + 1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<!doctype html><title>CQA test</title>\n", encoding="utf-8")
        if "--report-json" in args:
            path = Path(args[args.index("--report-json") + 1])
            path.parent.mkdir(parents=True, exist_ok=True)
            workflow = (func is cqa.profiler.main) or "--workflow-profile-json" in args
            report = self._fake_report(workflow=workflow)
            if func is cqa.audit.main:
                report["quota"] = {"status": "complete", "cohorts": []}
            path.write_text(json.dumps(report), encoding="utf-8")
        return 0

    def test_standard_paths_follow_selected_codex_home(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d).resolve() / "codex-quota-audit"
            self.assertEqual(cqa.default_dashboard_path(d), base / "latest" / "quota.html")
            self.assertEqual(cqa.default_report_json_path(d), base / "latest" / "quota.json")
            self.assertEqual(cqa.default_workflow_dashboard_path(d), base / "latest" / "workflow.html")

    def test_latest_workflow_prefers_latest_linked_family(self):
        t = datetime(2026, 9, 1, tzinfo=timezone.utc)
        sessions = {
            "old-root": self._session("old-root", t),
            "old-child": self._session("old-child", t + timedelta(minutes=1)),
            "new-root": self._session("new-root", t + timedelta(days=2)),
            "new-child": self._session("new-child", t + timedelta(days=2, minutes=1)),
            "solo": self._session("solo", t + timedelta(days=3)),
        }
        old = finder.Family(["old-root", "old-child"], "old-root",
                            [finder.Edge("old-root", "old-child", "high", "explicit-parent-id")],
                            "W-old", score=100, sample_quality="linked, complete usage coverage")
        new = finder.Family(["new-root", "new-child"], "new-root",
                            [finder.Edge("new-root", "new-child", "high", "explicit-parent-id")],
                            "W-new", score=50, sample_quality="linked, complete usage coverage")
        solo = finder.Family(["solo"], "solo", [], "W-solo", score=999, sample_quality="standalone session")
        chosen = finder.latest_workflow_family([old, new, solo], sessions, recent_days=90)
        self.assertIs(chosen, new)

    def test_latest_workflow_falls_back_to_standalone(self):
        t = datetime(2026, 9, 1, tzinfo=timezone.utc)
        sessions = {"solo": self._session("solo", t)}
        solo = finder.Family(["solo"], "solo", [], "W-solo", score=10, sample_quality="standalone session")
        self.assertIs(finder.latest_workflow_family([solo], sessions), solo)
        self.assertIsNone(finder.latest_workflow_family([solo], sessions, allow_standalone_fallback=False))


    def test_multi_agent_latest_skips_newer_guardian_only_family(self):
        t = datetime(2026, 9, 1, tzinfo=timezone.utc)
        sessions = {
            "worker-root": self._session("worker-root", t),
            "worker-child": self._session("worker-child", t + timedelta(minutes=1), source_kind="subagent", model="gpt-5.6-sol"),
            "dash-root": self._session("dash-root", t + timedelta(days=2)),
            "guardian": self._session("guardian", t + timedelta(days=2, minutes=1), source_kind="guardian", model="codex-auto-review"),
        }
        worker = finder.Family(["worker-root", "worker-child"], "worker-root",
                               [finder.Edge("worker-root", "worker-child", "high", "explicit-parent-id")],
                               "W-worker", score=50, sample_quality="linked, complete usage coverage")
        dashboard = finder.Family(["dash-root", "guardian"], "dash-root",
                                  [finder.Edge("dash-root", "guardian", "high", "explicit-parent-id")],
                                  "W-dashboard", score=100, sample_quality="linked, complete usage coverage")
        chosen = finder.latest_workflow_family(
            [worker, dashboard], sessions, require_delegated_worker=True, allow_standalone_fallback=False
        )
        self.assertIs(chosen, worker)

    def test_multi_agent_latest_excludes_active_codex_session(self):
        t = datetime(2026, 9, 1, tzinfo=timezone.utc)
        raw_current = "019dba93-8214-7d50-a089-9690b4ce6b9e"
        current_fp = finder.fp(raw_current)
        current_key = finder.short_key("S", current_fp)
        current_root = self._session(current_key, t + timedelta(days=3))
        current_root.links.own_ids.add(current_fp)
        sessions = {
            current_key: current_root,
            "current-worker": self._session("current-worker", t + timedelta(days=3, minutes=1), source_kind="subagent", model="gpt-5.6-sol"),
            "old-root": self._session("old-root", t),
            "old-worker": self._session("old-worker", t + timedelta(minutes=1), source_kind="subagent", model="gpt-5.6-sol"),
        }
        current = finder.Family([current_key, "current-worker"], current_key,
                                [finder.Edge(current_key, "current-worker", "high", "explicit-parent-id")],
                                "W-current", score=100, sample_quality="linked, complete usage coverage")
        old = finder.Family(["old-root", "old-worker"], "old-root",
                            [finder.Edge("old-root", "old-worker", "high", "explicit-parent-id")],
                            "W-old", score=50, sample_quality="linked, complete usage coverage")
        chosen = finder.latest_workflow_family(
            [old, current], sessions, require_delegated_worker=True,
            exclude_session_identifiers=[raw_current]
        )
        self.assertIs(chosen, old)

    def test_current_codex_session_identifiers_read_supported_env(self):
        env = {"CODEX_THREAD_ID": "thread-123456", "CODEX_SESSION_ID": "session-123456"}
        self.assertEqual(
            finder.current_codex_session_identifiers(env),
            ("thread-123456", "session-123456"),
        )

    def test_dashboard_latest_orchestrates_profile_then_quota(self):
        t = datetime(2026, 9, 1, tzinfo=timezone.utc)
        sessions = {"r": self._session("r", t), "c": self._session("c", t + timedelta(minutes=1))}
        family = finder.Family(["r", "c"], "r", [finder.Edge("r", "c", "high", "explicit-parent-id")],
                               "W-latest", score=50, sample_quality="linked, complete usage coverage")
        discovery = finder.Discovery([], sessions, family.edges, [family], finder.Counter())
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_discover_latest", return_value=(family, discovery)), \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call_main) as call, \
             mock.patch.object(cqa, "open_report") as opened:
            rc = cqa.dashboard_main(["--home", d, "--workflow", "latest", "--no-open"])
            self.assertEqual(rc, 0)
            self.assertEqual(call.call_count, 2)
            profile_argv = call.call_args_list[0].args[1]
            quota_argv = call.call_args_list[1].args[1]
            self.assertIn("W-latest", profile_argv)
            self.assertIn("--export-json", profile_argv)
            self.assertIn("--workflow-profile-json", quota_argv)
            self.assertIn("--dashboard", quota_argv)
            rows = reportlib.list_entries(d)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["type"], "combined")
            self.assertEqual(rows[0]["workflow_ref"], "wf-latest")
            opened.assert_not_called()


    def test_workflow_profile_multi_agent_only_uses_strict_discovery_and_standard_output(self):
        t = datetime(2026, 9, 1, tzinfo=timezone.utc)
        sessions = {"r": self._session("r", t), "c": self._session("c", t + timedelta(minutes=1), source_kind="subagent", model="gpt-5.6-sol")}
        family = finder.Family(["r", "c"], "r", [finder.Edge("r", "c", "high", "explicit-parent-id")],
                               "W-latest", score=50, sample_quality="linked, complete usage coverage")
        discovery = finder.Discovery([], sessions, family.edges, [family], finder.Counter())
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_discover_latest", return_value=(family, discovery)) as discover, \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call_main) as call, \
             mock.patch.object(cqa, "open_report"):
            rc = cqa.workflow_profile_main(["latest", "--home", d, "--multi-agent-only", "--no-open", "--quiet"])
            self.assertEqual(rc, 0)
            discover.assert_called_once_with(str(Path(d)), 90.0, multi_agent_only=True)
            profile_argv = call.call_args.args[1]
            self.assertIn("--dashboard", profile_argv)
            rows = reportlib.list_entries(d)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["type"], "workflow")
            self.assertEqual(rows[0]["workflow_ref"], "wf-latest")

    def test_dashboard_forwards_workflow_role_overrides(self):
        t = datetime(2026, 9, 1, tzinfo=timezone.utc)
        sessions = {"r": self._session("r", t), "c": self._session("c", t + timedelta(minutes=1))}
        family = finder.Family(["r", "c"], "r", [finder.Edge("r", "c", "high", "explicit-parent-id")],
                               "W-latest", score=50, sample_quality="linked, complete usage coverage")
        discovery = finder.Discovery([], sessions, family.edges, [family], finder.Counter())
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_discover_latest", return_value=(family, discovery)), \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call_main) as call, \
             mock.patch.object(cqa, "open_report"):
            role_map = str(Path(d) / "roles.json")
            Path(role_map).write_text(json.dumps({"schema": "workflow-role-map-v1", "sessions": []}))
            rc = cqa.dashboard_main(["--home", d, "--workflow", "latest",
                                     "--workflow-root-role", "manager",
                                     "--workflow-role-map", role_map,
                                     "--no-open", "--quiet"])
            self.assertEqual(rc, 0)
            profile_argv = call.call_args_list[0].args[1]
            self.assertEqual(profile_argv[profile_argv.index("--root-role") + 1], "manager")
            self.assertEqual(profile_argv[profile_argv.index("--role-map") + 1], role_map)

    def test_workflow_profile_forwards_role_overrides(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call_main) as call, \
             mock.patch.object(cqa, "open_report"):
            role_map = str(Path(d) / "roles.json")
            rc = cqa.workflow_profile_main(["W-explicit", "--home", d,
                                            "--root-role", "planner",
                                            "--role-map", role_map,
                                            "--no-open", "--quiet"])
            self.assertEqual(rc, 0)
            profile_argv = call.call_args.args[1]
            self.assertEqual(profile_argv[profile_argv.index("--root-role") + 1], "planner")
            self.assertEqual(profile_argv[profile_argv.index("--role-map") + 1], role_map)

    def test_dashboard_multi_agent_only_passes_strict_discovery(self):
        t = datetime(2026, 9, 1, tzinfo=timezone.utc)
        sessions = {"r": self._session("r", t), "c": self._session("c", t + timedelta(minutes=1), source_kind="subagent", model="gpt-5.6-sol")}
        family = finder.Family(["r", "c"], "r", [finder.Edge("r", "c", "high", "explicit-parent-id")],
                               "W-latest", score=50, sample_quality="linked, complete usage coverage")
        discovery = finder.Discovery([], sessions, family.edges, [family], finder.Counter())
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_discover_latest", return_value=(family, discovery)) as discover, \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call_main), \
             mock.patch.object(cqa, "open_report"):
            rc = cqa.dashboard_main(["--home", d, "--workflow", "latest", "--workflow-multi-agent-only", "--no-open", "--quiet"])
            self.assertEqual(rc, 0)
            discover.assert_called_once_with(str(Path(d)), 90.0, multi_agent_only=True)

    def test_dashboard_opens_by_default(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call_main), \
             mock.patch.object(cqa, "open_report", return_value=True) as opened:
            rc = cqa.dashboard_main(["--home", d])
            self.assertEqual(rc, 0)
            rows = reportlib.list_entries(d)
            archived = reportlib.root(d) / rows[0]["html"]
            opened.assert_called_once_with(archived)

    def test_dashboard_quiet_prints_only_final_path(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call_main), \
             mock.patch.object(cqa, "open_report", return_value=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cqa.dashboard_main(["--home", d, "--quiet"])
            self.assertEqual(rc, 0)
            lines = [line for line in out.getvalue().splitlines() if line.strip()]
            rows = reportlib.list_entries(d)
            self.assertEqual(lines, [f"Dashboard: {reportlib.root(d) / rows[0]['html']}"])

    def test_workflow_profile_quiet_prints_only_final_path(self):
        import contextlib, io
        t = datetime(2026, 9, 1, tzinfo=timezone.utc)
        sessions = {"r": self._session("r", t), "c": self._session("c", t + timedelta(minutes=1))}
        family = finder.Family(["r", "c"], "r", [finder.Edge("r", "c", "high", "explicit-parent-id")],
                               "W-latest", score=50, sample_quality="linked, complete usage coverage")
        discovery = finder.Discovery([], sessions, family.edges, [family], finder.Counter())
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_discover_latest", return_value=(family, discovery)), \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call_main), \
             mock.patch.object(cqa, "open_report", return_value=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cqa.workflow_profile_main(["latest", "--home", d, "--quiet"])
            self.assertEqual(rc, 0)
            lines = [line for line in out.getvalue().splitlines() if line.strip()]
            rows = reportlib.list_entries(d)
            self.assertEqual(lines, [f"Workflow dashboard: {reportlib.root(d) / rows[0]['html']}"])

    def test_report_library_archives_without_overwriting_and_supports_picker_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "source.html"
            src.write_text("<html>one</html>", encoding="utf-8")
            report = self._fake_report(workflow=True)
            a = reportlib.register_report(d, src, report, friendly_name="Big frontend migration", source_selector="W-7c2a1234")
            src.write_text("<html>two</html>", encoding="utf-8")
            b = reportlib.register_report(d, src, report, friendly_name="Big frontend migration", source_selector="W-7c2a1234")
            self.assertNotEqual(a["html"], b["html"])
            self.assertIn("2026-09-01_big-frontend-migration_wf-7c2a1234", a["html"])
            self.assertEqual(len(reportlib.list_entries(d)), 2)
            self.assertEqual(reportlib.resolve_entry(d, "latest")["id"], b["id"])
            self.assertEqual((reportlib.latest_dir(d) / "workflow.html").read_text(), "<html>two</html>")
            index = reportlib.write_index(d).read_text(encoding="utf-8")
            self.assertIn("Big frontend migration", index)
            self.assertIn("wf-7c2a1234", index)

    def test_report_library_removes_stale_latest_json_when_newest_report_has_no_json(self):
        with tempfile.TemporaryDirectory() as d:
            html_src = Path(d) / "source.html"
            json_src = Path(d) / "source.json"
            html_src.write_text("<html>with json</html>", encoding="utf-8")
            json_src.write_text("{}\n", encoding="utf-8")
            report = self._fake_report(workflow=True)
            reportlib.register_report(d, html_src, report, json_source=json_src, source_selector="W-abcd1234")
            latest_json = reportlib.latest_dir(d) / "workflow.json"
            self.assertTrue(latest_json.exists())

            html_src.write_text("<html>without json</html>", encoding="utf-8")
            reportlib.register_report(d, html_src, report, source_selector="W-abcd1234")
            self.assertFalse(latest_json.exists())

    def test_reports_list_is_human_browseable_without_memorizing_names(self):
        import contextlib, io
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "source.html"
            src.write_text("<html></html>", encoding="utf-8")
            reportlib.register_report(d, src, self._fake_report(workflow=True), source_selector="W-abcd1234")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cqa.reports_main(["--home", d, "list"])
            self.assertEqual(rc, 0)
            text = buf.getvalue()
            self.assertIn("WORKFLOW", text.upper())
            self.assertIn("wf-abcd1234", text)
            self.assertIn("2 sessions", text)


    def test_progress_reporter_plain_output_shows_real_stages_and_counts(self):
        stream = io.StringIO()
        progress = cqa._ProgressReporter(stream, tty=False)
        progress.header("Codex Quota Audit · workflow profile")
        progress.event("scan_start", total=184)
        progress.event("scan_progress", current=100, total=184)
        progress.event("parse_start", total=11)
        progress.event("parse_progress", current=11, total=11)
        progress.event("usage_start")
        progress.event("performance_start")
        progress.event("render_start")
        progress.complete()
        out = stream.getvalue()
        self.assertIn("Codex Quota Audit · workflow profile", out)
        self.assertIn("Resolving workflow · 184 log files", out)
        self.assertIn("100/184 log files", out)
        self.assertIn("Reading workflow telemetry · 11 sessions", out)
        self.assertIn("11/11 sessions", out)
        self.assertIn("Analyzing usage & pricing", out)
        self.assertIn("Analyzing model performance", out)
        self.assertIn("Rendering dashboard", out)
        self.assertIn("Report ready", out)
        self.assertNotIn("%", out)

    def test_progress_reporter_tty_uses_in_place_status_and_clean_interrupt(self):
        stream = io.StringIO()
        progress = cqa._ProgressReporter(stream, tty=True)
        progress.event("parse_start", total=3)
        progress.event("parse_progress", current=1, total=3)
        progress.interrupted()
        out = stream.getvalue()
        self.assertIn("◐ Reading workflow telemetry", out)
        self.assertIn("1/3 sessions", out)
        self.assertIn("Interrupted while reading workflow telemetry.", out)

    def test_workflow_profile_passes_progress_callback_when_not_quiet(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call_main) as call, \
             mock.patch.object(cqa, "open_report"):
            rc = cqa.workflow_profile_main(["W-test", "--home", d, "--no-open"])
            self.assertEqual(rc, 0)
            kwargs = call.call_args.kwargs
            self.assertIn("call_kwargs", kwargs)
            self.assertTrue(callable(kwargs["call_kwargs"]["progress"]))

    def test_workflow_profile_quiet_does_not_install_progress_callback(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call_main) as call:
            rc = cqa.workflow_profile_main(["W-test", "--home", d, "--no-open", "--quiet"])
            self.assertEqual(rc, 0)
            self.assertIsNone(call.call_args.kwargs.get("call_kwargs"))


if __name__ == "__main__":
    unittest.main()
