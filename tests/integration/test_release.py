"""Release-gate tests: packaging shape, all-sections fixture, and privacy canaries."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cqa import __version__
from cqa.report.core import render_dashboard_html, write_cqa_report_json
from tools.release_fixture import CANARIES, build_release_fixture, write_raw_telemetry_fixture


class ReleaseEngineeringTests(unittest.TestCase):
    def test_repository_root_is_project_metadata_only(self):
        required = {
            ".gitignore", "LICENSE", "README.md", "pyproject.toml",
        }
        files = {p.name for p in ROOT.iterdir() if p.is_file()}
        local_notes = {"CHANGELOG.md", "ROADMAP.md"}
        self.assertEqual(files - local_notes, required)

    def test_package_and_plugin_versions_are_aligned(self):
        manifest = json.loads((ROOT / "plugins/codex-quota-audit/.codex-plugin/plugin.json").read_text())
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertEqual(__version__, "0.8.0")
        self.assertEqual(manifest["version"], __version__)
        self.assertIn(f'version = "{__version__}"', pyproject)

    def test_canonical_assets_and_generated_mirrors_match(self):
        canonical = ROOT / "src" / "cqa" / "assets"
        public_schema = ROOT / "schema" / "cqa-report-v1.schema.json"
        plugin = ROOT / "plugins" / "codex-quota-audit" / "runtime" / "cqa" / "assets"
        self.assertEqual(public_schema.read_bytes(), (canonical / "schema" / public_schema.name).read_bytes())
        self.assertEqual((plugin / "schema" / public_schema.name).read_bytes(), public_schema.read_bytes())
        self.assertEqual(
            (plugin / "dashboard" / "cqa-dashboard-v1.template.html").read_bytes(),
            (canonical / "dashboard" / "cqa-dashboard-v1.template.html").read_bytes(),
        )

    def test_all_sections_release_fixture_and_secret_canaries(self):
        report = build_release_fixture()
        self.assertEqual(report["quota"]["status"], "complete")
        self.assertIn(report["guardian"]["status"], {"complete", "partial"})
        self.assertEqual(report["banked_resets"]["status"], "complete")
        self.assertIn(report["workflow"]["status"], {"complete", "partial"})
        self.assertTrue(report["workflow"]["profiles"])
        with tempfile.TemporaryDirectory() as d:
            jp, hp = Path(d)/"report.json", Path(d)/"report.html"
            write_cqa_report_json(str(jp), report)
            render_dashboard_html(report, str(hp))
            combined = jp.read_text(encoding="utf-8") + hp.read_text(encoding="utf-8")
            for label, canary in CANARIES.items():
                self.assertNotIn(canary, combined, f"privacy canary leaked: {label}")
            self.assertNotIn("https://", hp.read_text(encoding="utf-8"))
            self.assertNotIn("http://", hp.read_text(encoding="utf-8"))

    def test_raw_telemetry_drives_quota_and_workflow_without_leaking_content(self):
        from cqa import cli
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            write_raw_telemetry_fixture(home)
            rc = cli.dashboard_main(["--home", str(home), "--workflow", "latest", "--report-json", "--no-open", "--quiet"])
            self.assertEqual(rc, 0)
            jp = home / "codex-quota-audit" / "latest" / "combined.json"
            hp = home / "codex-quota-audit" / "latest" / "combined.html"
            report = json.loads(jp.read_text(encoding="utf-8"))
            self.assertIn(report["quota"]["status"], {"complete", "partial", "insufficient_data"})
            self.assertIn(report["workflow"]["status"], {"complete", "partial"})
            self.assertTrue(report["workflow"]["profiles"])
            workflow = report["workflow"]["profiles"][0]
            self.assertEqual(workflow["throughput"]["turns"], 9)
            self.assertAlmostEqual(workflow["throughput"]["turns_per_observed_second"], 1 / 60)
            self.assertEqual(workflow["throughput_by_model"][0]["model"], "gpt-6-astra")
            self.assertTrue(all(role.get("throughput") is not None for role in workflow["roles"]))
            self.assertGreaterEqual(workflow["performance"]["generation_timed_turns"], 4)
            self.assertGreater(workflow["performance"]["visible_output_tokens_per_second"], 0)
            self.assertGreater(workflow["performance"]["ttft_p50_seconds"], 0)
            self.assertGreater(workflow["performance_by_model"][0]["performance"]["visible_generation_coverage"], 0)
            self.assertIn("response_efficiency", workflow["extensions"])
            self.assertIn("response_efficiency", workflow["performance_by_model"][0]["extensions"])
            pricing = workflow["extensions"]["pricing"]
            self.assertTrue(pricing["by_model"])
            self.assertIsNotNone(pricing["priced_requests"])
            self.assertIsNotNone(pricing["long_context_priced_requests"])
            priced_rows = [row for row in pricing["by_model"] if row["api_list_equivalent_usd"] is not None]
            self.assertAlmostEqual(
                sum(row["api_list_equivalent_usd"] for row in priced_rows),
                workflow["summary"]["api_list_equivalent_usd"],
                places=5,
            )
            combined = jp.read_text(encoding="utf-8") + hp.read_text(encoding="utf-8")
            self.assertNotIn(CANARIES["prompt"], combined)
            self.assertNotIn(CANARIES["tool"], combined)
            self.assertNotIn("root-session-123456789", combined)
            self.assertNotIn("child-session-123456789", combined)


if __name__ == "__main__":
    unittest.main()
