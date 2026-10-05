import importlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
PLUGIN = ROOT / "plugins" / "codex-quota-audit"


class PluginPackagingTests(unittest.TestCase):
    def test_manifest_and_marketplace_are_consistent(self):
        manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
        marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
        self.assertEqual(manifest["name"], "codex-quota-audit")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(marketplace["name"], "dayowe")
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], manifest["name"])
        self.assertEqual(entry["source"], {"source": "local", "path": "./plugins/codex-quota-audit"})
        self.assertTrue((ROOT / entry["source"]["path"]).resolve().is_dir())

    def test_exactly_two_valid_skill_surfaces_exist(self):
        skills = sorted(p.name for p in (PLUGIN / "skills").iterdir() if p.is_dir())
        self.assertEqual(skills, ["quota-audit", "workflow-profile"])
        for name in skills:
            content = (PLUGIN / "skills" / name / "SKILL.md").read_text()
            self.assertTrue(content.startswith("---\n"))
            front = content.split("---\n", 2)[1]
            self.assertIn(f"name: {name}\n", front)
            self.assertIn("description:", front)
            self.assertNotIn("[TODO:", content)
            self.assertIn("--quiet", content)

    def test_plugin_runtime_is_generated_from_canonical_src_package(self):
        runtime = PLUGIN / "runtime"
        package = runtime / "cqa"
        self.assertTrue((package / "cli.py").is_file())
        self.assertTrue((package / "quota" / "audit.py").is_file())
        self.assertTrue((package / "workflow" / "profile.py").is_file())
        self.assertTrue((package / "report" / "core.py").is_file())
        for legacy in ("cqa.py", "codex_quota_audit.py", "cqa_report.py",
                       "profile_workflow_cost.py", "find_workflow_candidates.py"):
            self.assertFalse((runtime / legacy).exists(), legacy)
        self.assertTrue((ROOT / "tools" / "sync_plugin_runtime.py").is_file())
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "sync_plugin_runtime.py"), "--check"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_workflow_skill_launcher_normalizes_relative_output(self):
        launcher = PLUGIN / "skills" / "workflow-profile" / "scripts" / "run.py"
        spec = importlib.util.spec_from_file_location("cqa_workflow_skill_launcher", launcher)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            args = module._normalize_workflow_output([
                "workflow", "profile", "latest", "--home", d,
                "--output", "workflow-report.html", "--quiet",
            ])
            i = args.index("--output")
            self.assertEqual(args[i + 1], str(Path(d).resolve() / "codex-quota-audit" / "exports" / "workflow-report.html"))

    def test_workflow_skill_launcher_preserves_absolute_custom_output(self):
        launcher = PLUGIN / "skills" / "workflow-profile" / "scripts" / "run.py"
        spec = importlib.util.spec_from_file_location("cqa_workflow_skill_launcher_absolute", launcher)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as d:
            custom = str(Path(d).resolve() / "custom.html")
            args = module._normalize_workflow_output(["workflow", "profile", "W-test", "--output", custom])
            self.assertEqual(args[args.index("--output") + 1], custom)

    def test_skill_launchers_find_bundled_runtime(self):
        for skill in ("quota-audit", "workflow-profile"):
            launcher = PLUGIN / "skills" / skill / "scripts" / "run.py"
            result = subprocess.run(
                [sys.executable, str(launcher), "--version"],
                cwd=launcher.parent.parent,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cqa 0.8.0", result.stdout)

    def test_plugin_skill_commands_cover_quota_workflow_and_combined_modes(self):
        quota = (PLUGIN / "skills" / "quota-audit" / "SKILL.md").read_text()
        workflow = (PLUGIN / "skills" / "workflow-profile" / "SKILL.md").read_text()
        self.assertIn("dashboard --quiet", quota)
        self.assertIn("dashboard --workflow latest --workflow-multi-agent-only --quiet", quota)
        self.assertIn("--multi-agent-only", workflow)
        self.assertIn("workflow profile latest", workflow)
        self.assertIn("local CQA report library", workflow)
        self.assertNotIn("--output ~/.codex/codex-quota-audit/workflow-report.html", workflow)

    def _load_bundled_cqa(self):
        runtime = PLUGIN / "runtime"
        sys.path.insert(0, str(runtime))
        try:
            for key in [k for k in list(sys.modules) if k == "cqa" or k.startswith("cqa.")]:
                sys.modules.pop(key, None)
            return importlib.import_module("cqa.cli")
        finally:
            sys.path.pop(0)

    def _fake_call(self, cqa):
        def call(func, argv, *, quiet, call_kwargs=None):
            args = list(argv)
            if "--export-json" in args:
                path = Path(args[args.index("--export-json") + 1])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")
            if "--dashboard" in args:
                path = Path(args[args.index("--dashboard") + 1])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("<!doctype html><title>plugin smoke</title>", encoding="utf-8")
            if "--report-json" in args:
                path = Path(args[args.index("--report-json") + 1])
                path.parent.mkdir(parents=True, exist_ok=True)
                workflow = (func is cqa.profiler.main) or "--workflow-profile-json" in args
                report = {
                    "schema": "cqa-report", "schema_version": "1.0.0",
                    "report": {"generated_at": "2026-10-03T12:00:00Z", "observed_range": {
                        "start": "2026-09-01T00:00:00Z", "end": "2026-10-03T12:00:00Z"}},
                    "quota": {"status": "complete" if func is cqa.audit.main else "not_requested", "cohorts": []},
                    "guardian": {"status": "not_requested"}, "banked_resets": {"status": "not_requested"},
                    "workflow": {"status": "complete" if workflow else "not_requested", "profiles": ([{
                        "analysis_window": {"start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z"},
                        "summary": {"sessions": 2, "requests": 3, "raw_tokens": 1000},
                    }] if workflow else [])},
                }
                path.write_text(json.dumps(report), encoding="utf-8")
            return 0
        return call

    def test_plugin_quota_only_smoke_path(self):
        cqa = self._load_bundled_cqa()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call(cqa)) as call:
            rc = cqa.dashboard_main(["--home", d, "--no-open", "--quiet"])
            self.assertEqual(rc, 0)
            self.assertEqual(call.call_count, 1)
            self.assertIs(call.call_args.args[0], cqa.audit.main)

    def test_plugin_workflow_only_smoke_path(self):
        cqa = self._load_bundled_cqa()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call(cqa)) as call:
            rc = cqa.workflow_profile_main(["W-test", "--home", d, "--no-open", "--quiet"])
            self.assertEqual(rc, 0)
            self.assertEqual(call.call_count, 1)
            self.assertIs(call.call_args.args[0], cqa.profiler.main)

    def test_plugin_combined_dashboard_smoke_path(self):
        cqa = self._load_bundled_cqa()
        family = SimpleNamespace(family_key="W-latest")
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(cqa, "_discover_latest", return_value=(family, object())), \
             mock.patch.object(cqa, "_call_main", side_effect=self._fake_call(cqa)) as call:
            rc = cqa.dashboard_main(["--home", d, "--workflow", "latest", "--no-open", "--quiet"])
            self.assertEqual(rc, 0)
            self.assertEqual(call.call_count, 2)
            self.assertIs(call.call_args_list[0].args[0], cqa.profiler.main)
            self.assertIs(call.call_args_list[1].args[0], cqa.audit.main)


if __name__ == "__main__":
    unittest.main()
