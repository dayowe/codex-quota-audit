"""cqa-report-v1 contract, privacy and renderer regression tests."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
FIXTURES = ROOT / "tests" / "fixtures" / "reference"
SCHEMA = ROOT / "schema" / "cqa-report-v1.schema.json"

from cqa.quota import audit
from cqa.report.core import (
    attach_workflow_profile_sources, build_cqa_report_v1, build_workflow_only_report_v1,
    render_dashboard_html, write_cqa_report_json,
)

FORBIDDEN_KEYS = {
    "prompt", "response", "access_token", "refresh_token", "auth_token",
    "email", "account_id", "source_path", "session_uuid", "session_id", "thread_id",
}
FORBIDDEN_PATTERNS = [
    re.compile(r"(?:^|[\\/])Users[\\/]"),
    re.compile(r"(?:^|[\\/])home[\\/]"),
    re.compile(r"~[\\/]\.codex[\\/]sessions"),
    re.compile(r"\.codex[\\/]sessions[\\/]"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I),
]


def args(**overrides):
    base = dict(
        no_guardian_audit=True,
        chart_interval=.80,
        chart_bootstraps=1000,
        guardian_fit_bootstraps=300,
        banked_reset=[],
        banked_capacity_interval=.80,
        banked_capacity_bootstraps=1000,
        banked_slice_interval=.80,
        banked_slice_bootstraps=1000,
        banked_slice_points=[5.0, 10.0, 14.0, 20.0],
        window_minutes=10080,
        chart_model_purity=.95,
        chart_effort_purity=.95,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def coverage():
    return {
        "log_start": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "log_end": datetime(2026, 2, 1, tzinfo=timezone.utc),
        "files": 3,
        "records": 100,
    }


def read_csv(name):
    with (FIXTURES / name).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def regimes_from_chart(rows):
    out, seen = [], set()
    for row in rows:
        key = (row["model"], row["regime"], row["regime_start"], row["regime_end"])
        if key in seen:
            continue
        seen.add(key)
        out.append(SimpleNamespace(
            model=row["model"], index=int(row["regime"][1:]), label=row["regime"],
            start_ts=datetime.fromisoformat(row["regime_start"]),
            end_ts=datetime.fromisoformat(row["regime_end"]),
            detection_basis=row["regime_detection_basis"],
        ))
    return out


def empty_banked():
    return ({"matched_markers": 0, "matched_strata": [], "metrics": {}, "interpretation": "not tested"},
            {"slices": {}, "preferred_points": None, "strongest_metric": None, "interpretation": "not tested"})


def build_quota_fixture():
    chart = read_csv("quota_chart_data.csv")
    banked, slices = empty_banked()
    return build_cqa_report_v1(
        generator_version=audit.__version__, args=args(), coverage=coverage(),
        chart_rows=chart, regimes=regimes_from_chart(chart), approval_episodes=[],
        period_cost_rows=[], guardian_summary_data={}, banked_rows=[], banked_summary=banked,
        banked_slice_rows=[], banked_slice_summary=slices, report_kind="export",
    )


def walk(value, path="$" ):
    if isinstance(value, dict):
        for key, child in value.items():
            yield path, key, child
            yield from walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from walk(child, f"{path}[{i}]")


class ReportContractTests(unittest.TestCase):
    def test_existing_quota_chart_regression_anchor(self):
        report = build_quota_fixture()
        cohorts = {(c["model"], c["effort"]): c for c in report["quota"]["cohorts"]}
        sol = cohorts[("gpt-5.6-sol", "high")]
        self.assertEqual(sol["evidence"]["quota_points"], 67.0)
        self.assertAlmostEqual(sol["efficiency"]["raw_tokens_per_quota_point"]["estimate"] / 1_000_000,
                               39.78670331343284)
        self.assertEqual(report["guardian"]["status"], "not_requested")
        self.assertEqual(report["workflow"]["status"], "not_requested")

    def test_dashboard_safe_privacy_contract(self):
        report = build_quota_fixture()
        self.assertEqual(report["privacy"], {
            "profile": "dashboard-safe-v1",
            "contains_prompt_text": False,
            "contains_response_text": False,
            "contains_tool_output": False,
            "contains_file_contents": False,
            "contains_auth_data": False,
            "contains_account_identity": False,
            "contains_source_paths": False,
            "contains_raw_session_ids": False,
        })
        for path, key, value in walk(report):
            self.assertNotIn(key.lower(), FORBIDDEN_KEYS, f"forbidden key at {path}: {key}")
            if isinstance(value, str):
                for pattern in FORBIDDEN_PATTERNS:
                    self.assertIsNone(pattern.search(value), f"suspicious string at {path}.{key}: {value!r}")

    def test_schema_validation_when_jsonschema_is_available(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is an optional development dependency")
        report = build_quota_fixture()
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        ).validate(report)

    def test_renderer_is_self_contained_and_json_round_trips(self):
        report = build_quota_fixture()
        with tempfile.TemporaryDirectory() as d:
            json_path = Path(d) / "report.json"
            html_path = Path(d) / "report.html"
            write_cqa_report_json(str(json_path), report)
            render_dashboard_html(report, str(html_path))
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["schema_version"], "1.0.0")
            html = html_path.read_text(encoding="utf-8")
            self.assertIn('id="cqa-report"', html)
            self.assertNotIn("__CQA_REPORT_JSON__", html)
            self.assertNotRegex(html, r'<script[^>]+src=["\']https?://')
            self.assertNotRegex(html, r'<link[^>]+href=["\']https?://')


    def test_workflow_profile_maps_into_shared_contract(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8"))
        report = build_workflow_only_report_v1(
            source, generator_version=audit.__version__, report_kind="dashboard"
        )
        self.assertEqual(report["quota"]["status"], "not_requested")
        self.assertEqual(report["workflow"]["status"], "partial")
        profile = report["workflow"]["profiles"][0]
        self.assertEqual(profile["summary"]["raw_tokens"], 230143796)
        self.assertAlmostEqual(profile["summary"]["api_list_equivalent_usd"], 325.0676165)
        self.assertEqual(profile["summary"]["sessions"], 29)
        self.assertEqual(profile["summary"]["compactions"], 16)
        self.assertIsNone(profile["extensions"]["window_selection"]["excluded_carry_in_sessions"])
        self.assertIsNone(profile["extensions"]["window_selection"]["excluded_carry_in_usage"])
        self.assertEqual(len(profile["agents"]), 29)
        self.assertEqual(len(profile["compactions"]), 16)
        self.assertEqual(len(profile["signals"]), 4)
        concurrency_signal = next(s for s in profile["signals"] if s["code"] == "EXTRA_CONCURRENCY_HOURS")
        self.assertEqual(concurrency_signal["label"], "Extra concurrent agent-hours")
        self.assertIn("can exceed workflow wall-clock duration", concurrency_signal["summary"])
        self.assertTrue(all(a["id"].startswith("agent-001-") for a in profile["agents"]))
        self.assertFalse(any(str(a["id"]).startswith("A") for a in profile["agents"]))

        try:
            import jsonschema
        except ImportError:
            return
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        ).validate(report)


    def test_workflow_throughput_maps_by_role_model_and_agent(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8"))
        first_row = next(r for r in source["nested_attribution"]["sessions"] if r["role"] in source["role_costs"])
        first_agent = first_row["agent"]
        first_role = first_row["role"]
        source["turn_throughput"] = {
            "method": {
                "turn_definition": "one deduplicated token_count/token_usage_record carrying last_token_usage",
                "cadence_basis": "consecutive observed turns in the same session stream",
                "idle_gap_seconds": 120.0,
                "caveat": "Observed cadence is not server inference latency.",
            },
            "workflow_elapsed_seconds": 600.0,
            "overall": {
                "turns": 20, "contributors": 2, "observed_turn_transitions": 18,
                "observed_turn_interval_seconds": 90.0, "turns_per_observed_second": 0.2,
                "turns_per_workflow_second": 20/600, "raw_tokens": 2_000_000, "output_tokens": 200_000, "tokens_per_turn": 100_000.0,
                "output_tokens_per_workflow_second": 200_000/600, "raw_tokens_per_observed_second": 2_000_000/90,
            },
            "by_role": {first_role: {
                "turns": 12, "contributors": 1, "observed_turn_transitions": 11,
                "observed_turn_interval_seconds": 55.0, "turns_per_observed_second": 0.2,
                "turns_per_workflow_second": 12/600, "raw_tokens": 1_200_000, "output_tokens": 120_000, "tokens_per_turn": 100_000.0,
                "output_tokens_per_workflow_second": 120_000/600, "raw_tokens_per_observed_second": 1_200_000/55,
            }},
            "by_model": {"model-x": {
                "turns": 20, "contributors": 2, "observed_turn_transitions": 18,
                "observed_turn_interval_seconds": 90.0, "turns_per_observed_second": 0.2,
                "turns_per_workflow_second": 20/600, "raw_tokens": 2_000_000, "output_tokens": 200_000, "tokens_per_turn": 100_000.0,
                "output_tokens_per_workflow_second": 200_000/600, "raw_tokens_per_observed_second": 2_000_000/90,
            }},
            "by_agent": {first_agent: {
                "turns": 12, "contributors": 1, "observed_turn_transitions": 11,
                "observed_turn_interval_seconds": 55.0, "turns_per_observed_second": 0.2,
                "turns_per_workflow_second": 12/600, "raw_tokens": 1_200_000, "output_tokens": 120_000, "tokens_per_turn": 100_000.0,
                "output_tokens_per_workflow_second": 120_000/600, "raw_tokens_per_observed_second": 1_200_000/55,
            }},
        }
        report = build_workflow_only_report_v1(source, generator_version=audit.__version__)
        profile = report["workflow"]["profiles"][0]
        self.assertEqual(profile["throughput"]["turns"], 20)
        self.assertEqual(profile["throughput"]["output_tokens"], 200_000)
        self.assertAlmostEqual(profile["throughput"]["output_tokens_per_workflow_second"], 200_000/600)
        self.assertEqual(profile["throughput_method"]["idle_gap_seconds"], 120.0)
        self.assertEqual(profile["throughput_by_model"][0]["model"], "model-x")
        self.assertAlmostEqual(profile["throughput_by_model"][0]["throughput"]["turns_per_observed_second"], 0.2)
        role = next(r for r in profile["roles"] if r["role"] == first_role)
        self.assertEqual(role["throughput"]["turns"], 12)
        agent = next(a for a in profile["agents"] if a["role"] == first_role and a["throughput"] is not None)
        self.assertEqual(agent["throughput"]["turns"], 12)

        try:
            import jsonschema
        except ImportError:
            return
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(report)
        with tempfile.TemporaryDirectory() as d:
            hp = Path(d) / "throughput.html"
            render_dashboard_html(report, str(hp))
            html = hp.read_text(encoding="utf-8")
            self.assertIn("Workflow pace", html)
            self.assertIn("Observed turns/min", html)
            self.assertIn("Model output / elapsed min", html)
            self.assertIn("Raw tokens/min · observed intervals", html)
            self.assertIn("Canonical performance terms with plain-language definitions", html)
            self.assertIn('href="#model-performance"', html)
            self.assertIn('href="#workflow-timeline"', html)
            self.assertIn('id="toTop"', html)
            self.assertIn("function throughputTable(w)", html)

    def test_workflow_zero_price_coverage_is_unavailable_not_zero(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8"))
        row = next(r for r in source["nested_attribution"]["sessions"] if r.get("direct", {}).get("total_tokens", 0) > 0)
        row["direct"]["api_list_equivalent_usd"] = 0.0
        row["direct"]["price_coverage"] = 0.0
        report = build_workflow_only_report_v1(source, generator_version=audit.__version__)
        profile = report["workflow"]["profiles"][0]
        matching = [a for a in profile["agents"] if a["usage"]["total_tokens"] == row["direct"]["total_tokens"]]
        self.assertTrue(matching)
        self.assertIsNone(matching[0]["usage"]["api_list_equivalent_usd"])
        self.assertEqual(matching[0]["usage"]["price_coverage"], 0.0)

    def test_workflow_role_aggregation_separates_root_topology(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8"))
        root = next(r for r in source["nested_attribution"]["sessions"] if r["agent"] == "ROOT")
        root["role"] = "coordinator/root"
        source["role_costs"]["coordinator/root"] = {
            "requests": 2, "input_tokens": 100, "cached_input_tokens": 60,
            "uncached_input_tokens": 40, "output_tokens": 20,
            "reasoning_output_tokens": 5, "api_list_equivalent_usd": None,
            "price_coverage": None,
        }
        source["role_costs"]["coordinator"] = {
            "requests": 3, "input_tokens": 200, "cached_input_tokens": 120,
            "uncached_input_tokens": 80, "output_tokens": 30,
            "reasoning_output_tokens": 7, "api_list_equivalent_usd": None,
            "price_coverage": None,
        }
        source["turn_throughput"] = {
            "method": {"turn_definition": "test", "cadence_basis": "test", "idle_gap_seconds": 120.0},
            "workflow_elapsed_seconds": 600.0,
            "overall": {
                "turns": 5, "contributors": 2, "observed_turn_transitions": 3,
                "observed_turn_interval_seconds": 60.0, "turns_per_observed_second": 3/60,
                "turns_per_workflow_second": 5/600, "raw_tokens": 350,
                "output_tokens": 50, "tokens_per_turn": 70.0,
                "output_tokens_per_workflow_second": 50/600,
                "observed_raw_tokens": 300, "raw_tokens_per_observed_second": 5.0,
            },
            "by_role": {
                "coordinator/root": {
                    "turns": 2, "contributors": 1, "observed_turn_transitions": 1,
                    "observed_turn_interval_seconds": 20.0, "turns_per_observed_second": 1/20,
                    "turns_per_workflow_second": 2/600, "raw_tokens": 120,
                    "output_tokens": 20, "tokens_per_turn": 60.0,
                    "output_tokens_per_workflow_second": 20/600,
                    "observed_raw_tokens": 100, "raw_tokens_per_observed_second": 5.0,
                },
                "coordinator": {
                    "turns": 3, "contributors": 1, "observed_turn_transitions": 2,
                    "observed_turn_interval_seconds": 40.0, "turns_per_observed_second": 2/40,
                    "turns_per_workflow_second": 3/600, "raw_tokens": 230,
                    "output_tokens": 30, "tokens_per_turn": 230/3,
                    "output_tokens_per_workflow_second": 30/600,
                    "observed_raw_tokens": 200, "raw_tokens_per_observed_second": 5.0,
                },
            },
            "by_model": {},
            "by_agent": {},
        }

        report = build_workflow_only_report_v1(source, generator_version=audit.__version__)
        profile = report["workflow"]["profiles"][0]
        coordinator = [r for r in profile["roles"] if r["role"] == "coordinator"]
        self.assertEqual(len(coordinator), 1)
        coordinator = coordinator[0]
        self.assertEqual(coordinator["usage"]["requests"], 5)
        self.assertEqual(coordinator["usage"]["total_tokens"], 350)
        self.assertIsNone(coordinator["usage"]["api_list_equivalent_usd"])
        self.assertIsNone(coordinator["usage"]["price_coverage"])
        self.assertEqual(coordinator["throughput"]["turns"], 5)
        self.assertAlmostEqual(coordinator["throughput"]["turns_per_workflow_second"], 5/600)
        self.assertCountEqual(
            coordinator["extensions"]["topology_merged_from"],
            ["coordinator", "coordinator/root"],
        )
        self.assertFalse(any(r["role"].endswith("/root") for r in profile["roles"]))
        root_agent = next(a for a in profile["agents"] if a["extensions"].get("is_root"))
        self.assertEqual(root_agent["role"], "coordinator")
        self.assertIsNone(root_agent["parent_id"])
        self.assertTrue(root_agent["extensions"]["is_root"])

    def test_workflow_model_performance_maps_and_renders_timing_coverage(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8"))
        first_row = next(r for r in source["nested_attribution"]["sessions"] if r["role"] in source["role_costs"])
        agent = first_row["agent"]
        role = first_row["role"]
        stats = {
            "turns": 10, "contributors": 1,
            "generation_timed_turns": 8, "qualified_visible_responses": 8,
            "visible_responses": 10, "visible_timed_responses": 10,
            "generation_coverage": 0.8, "visible_generation_coverage": 0.8,
            "visible_timing_coverage": 1.0,
            "visible_output_tokens": 800, "visible_generation_seconds": 10.0,
            "visible_output_tokens_per_second": 80.0,
            "visible_output_tokens_per_turn": 100.0,
            "reasoning_tokens_per_turn": 40.0,
            "reasoning_timed_turns": 8, "reasoning_seconds": 4.0,
            "reasoning_tokens_per_second": 100.0,
            "ttft_samples": 9, "ttft_coverage": 0.9,
            "ttft_p50_seconds": 2.1, "ttft_p90_seconds": 4.8,
            "turn_duration_samples": 9, "turn_duration_coverage": 0.9,
            "turn_duration_p50_seconds": 5.2, "turn_duration_p90_seconds": 9.4,
            "qualification_reasons": {"single_timed_agent_message": 8, "multiple_agent_messages_in_turn": 2},
        }
        source["turn_performance"] = {
            "method": {
                "turn_definition": "one turn-level token_usage_record joined by turn_id to task/item timing telemetry",
                "visible_output_definition": "max(output_tokens - reasoning_output_tokens, 0)",
                "generation_qualification": "response-level usage plus timed visible AgentMessage items with no mixed tool/function-call output",
                "aggregation": "sum visible tokens / sum qualified AgentMessage seconds",
                "caveat": "client-observed timing",
            },
            "overall": stats,
            "by_role": {role: stats},
            "by_model": {"gpt-test": stats},
            "by_model_effort": [{"model": "gpt-test", "effort": "high", "stats": stats}],
            "by_agent": {agent: stats},
        }
        response_stats = {
            "tasks_seen": 10, "complete_tasks": 10, "qualified_tasks": 9,
            "task_coverage": 0.9, "evidence_quality": "partial",
            "nonreasoning_output_tokens": 1800, "output_tokens_per_second": 30.0,
            "task_elapsed_seconds": 120.0, "tool_wait_seconds": 60.0,
            "tool_excluded_seconds": 60.0, "timed_reasoning_seconds": 15.0,
            "timed_visible_generation_seconds": 10.0, "other_tool_excluded_seconds": 35.0,
            "reasoning_share": 0.25, "tool_wait_share": 0.5,
            "tool_pairing_coverage": 0.99, "reasoning_timing_coverage": 0.98,
            "agent_message_timing_coverage": 1.0,
            "ttft_samples": 9, "ttft_p50_seconds": 2.4, "ttft_p90_seconds": 5.0,
            "qualification_reasons": {"qualified_task": 9, "incomplete_tool_pairing": 1},
        }
        source["response_efficiency"] = {
            "method": {
                "status": "production-extension",
                "task_interval": "task_started -> task_complete",
                "token_basis": "non-reasoning model output",
                "tool_exclusion": "paired tool call -> result spans",
                "reasoning_semantics": "reasoning stays in denominator",
                "residual_semantics": "residual is not server compute",
                "qualification": "complete tasks with valid tool pairing",
            },
            "overall": response_stats,
            "by_model": {"gpt-test": response_stats},
            "by_model_effort": [{"model": "gpt-test", "effort": "high", "stats": response_stats}],
            "by_agent": {agent: response_stats},
        }
        report = build_workflow_only_report_v1(source, generator_version=audit.__version__)
        profile = report["workflow"]["profiles"][0]
        self.assertEqual(profile["performance"]["generation_timed_turns"], 8)
        self.assertEqual(profile["performance_by_model"][0]["model"], "gpt-test")
        self.assertEqual(profile["performance_by_model"][0]["effort"], "high")
        self.assertEqual(profile["performance_by_model"][0]["performance"]["visible_output_tokens_per_second"], 80.0)
        efficiency = profile["performance_by_model"][0]["extensions"]["response_efficiency"]
        self.assertEqual(efficiency["output_tokens_per_second"], 30.0)
        self.assertEqual(efficiency["evidence_quality"], "partial")
        self.assertEqual(profile["extensions"]["response_efficiency"]["overall"]["reasoning_share"], 0.25)
        self.assertEqual(next(r for r in profile["roles"] if r["role"] == role)["performance"]["ttft_p50_seconds"], 2.1)
        self.assertEqual(next(a for a in profile["agents"] if a["role"] == role and a.get("performance"))["performance"]["turn_duration_p90_seconds"], 9.4)

        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        try:
            import jsonschema
        except ImportError:
            return
        jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(report)
        with tempfile.TemporaryDirectory() as d:
            hp = Path(d) / "performance.html"
            render_dashboard_html(report, str(hp))
            html = hp.read_text(encoding="utf-8")
            self.assertIn("Model performance", html)
            self.assertIn("See where the workflow spent time and tokens.", html)
            self.assertIn("API list-equivalent cost", html)
            self.assertIn("TTFT P50", html)
            self.assertIn("TTFT P90", html)
            self.assertIn("Workflow pace", html)
            self.assertIn("Agent activity", html)
            self.assertIn("Tool-excluded output rate", html)
            self.assertIn("Visible generation rate", html)
            self.assertIn("Reasoning share", html)
            self.assertIn("Response-time decomposition", html)
            self.assertIn("Other retained time", html)
            self.assertIn("Canonical performance terms with plain-language definitions", html)
            self.assertIn("Attribution quality", html)
            self.assertIn("Measured visible responses", html)
            self.assertIn("Visible responses timed", html)
            self.assertIn("Response timing", html)
            self.assertIn("Time to First Token", html)
            self.assertIn("Task duration P50", html)
            self.assertIn("Task duration P90", html)
            self.assertIn("Observed turns/min", html)
            self.assertIn("Model output / elapsed min", html)
            self.assertIn("Raw tokens/min · observed intervals", html)
            self.assertIn("Review / Guardian models", html)
            self.assertIn("Visible responses timed", html)
            self.assertIn("Too small for stable comparison", html)
            self.assertIn("performanceUnavailableReason", html)
            self.assertIn("Role source: trusted spawn label", html)
            self.assertIn("Workflow-average turns/min", html)
            self.assertIn("no parent link in report", html)
            self.assertNotIn("child / peer", html)
            self.assertNotIn("not enough evidence", html)

    def test_window_selection_preserves_scoped_counts_usage_and_policy(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8"))
        window = source["analysis_window"]
        window.update({
            "after": "2026-10-09T06:00:00+02:00", "before": "2026-10-09T12:00:00+02:00",
            "carry_in_policy": "exclude", "included_carry_in_sessions": 0, "excluded_carry_in_sessions": 6,
            "excluded_carry_in_activity_in_window": {"requests": 10, "total_tokens": 1100,
                "input_tokens": 1000, "output_tokens": 100, "api_list_equivalent_usd": .02, "price_coverage": 1},
            "root_candidates": [{"session_key": "PRIVATE_SELECTOR_DO_NOT_EXPORT"}],
        })
        report = build_workflow_only_report_v1(source, generator_version=audit.__version__)
        selection = report["workflow"]["profiles"][0]["extensions"]["window_selection"]
        self.assertEqual(selection["requested_after"], "2026-10-09T04:00:00Z")
        self.assertEqual(selection["carry_in_policy"], "exclude")
        self.assertEqual(selection["excluded_carry_in_sessions"], 6)
        self.assertEqual(selection["excluded_carry_in_usage"]["total_tokens"], 1100)
        self.assertNotIn("PRIVATE_SELECTOR_DO_NOT_EXPORT", json.dumps(report))
        self.assertIn("WORKFLOW_CARRY_IN_EXCLUDED", {w["code"] for w in report["data_quality"]["warnings"]})
        window.update({"carry_in_policy": "include", "included_carry_in_sessions": 6, "excluded_carry_in_sessions": 0,
                       "included_carry_in_activity_in_window": window["excluded_carry_in_activity_in_window"],
                       "excluded_carry_in_activity_in_window": {}})
        included = build_workflow_only_report_v1(source, generator_version=audit.__version__)
        self.assertEqual(included["workflow"]["profiles"][0]["extensions"]["window_selection"]["included_carry_in_sessions"], 6)
        self.assertNotIn("WORKFLOW_CARRY_IN_EXCLUDED", {w["code"] for w in included["data_quality"]["warnings"]})

    def test_missing_worker_lifetimes_and_model_task_timing_are_cautions(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8"))
        source["workflow_analysis"] = {"lifetime_windows_status": "unavailable"}
        source["active_windows"] = []
        source["turn_performance"] = {"by_model_effort": [
            {"model": "model-a", "effort": "high", "stats": {"turns": 2}}]}
        stats = {"tasks_seen": 2, "complete_tasks": 0, "qualified_tasks": 0, "evidence_quality": "unavailable",
                 "qualification_reasons": {"started_before_window": 1, "missing_task_complete": 1}}
        source["response_efficiency"] = {"overall": stats, "by_model_effort": [
            {"model": "model-a", "effort": "high", "stats": stats}]}
        report = build_workflow_only_report_v1(source, generator_version=audit.__version__)
        self.assertEqual(report["data_quality"]["overall"], "caution")
        codes = {w["code"] for w in report["data_quality"]["warnings"]}
        self.assertIn("WORKFLOW_LIFETIME_WINDOWS_UNAVAILABLE", codes)
        self.assertIn("WORKFLOW_RESPONSE_TIMING_UNAVAILABLE", codes)
        efficiency = report["workflow"]["profiles"][0]["performance_by_model"][0]["extensions"]["response_efficiency"]
        self.assertEqual(efficiency["tasks_seen"], 2)
        self.assertIsNone(efficiency["output_tokens_per_second"])
        self.assertEqual(efficiency["qualification_reasons"]["started_before_window"], 1)

    def test_workflow_timeline_uses_privacy_safe_lifecycle_objects(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8"))
        report = build_workflow_only_report_v1(source, generator_version=audit.__version__)
        profile = report["workflow"]["profiles"][0]
        timeline = profile["timeline"]
        self.assertGreater(len(timeline["agent_windows"]), 0)
        self.assertGreater(len(timeline["guardian_activity"]), 0)
        self.assertGreater(len(timeline["concurrency_windows"]), 0)
        self.assertTrue(all(x["agent_id"].startswith("agent-001-") for x in timeline["agent_windows"]))
        self.assertTrue(all(x["active_children"] >= 2 for x in timeline["concurrency_windows"]))
        source_refill_targets = [e.get("refill_target_tokens") for e in source.get("compaction_audit", {}).get("events", []) if e.get("refill_target_tokens") is not None]
        report_refill_targets = [c.get("extensions", {}).get("refill_target_tokens") for c in profile["compactions"] if c.get("extensions", {}).get("refill_target_tokens") is not None]
        self.assertEqual(report_refill_targets, source_refill_targets)
        with tempfile.TemporaryDirectory() as d:
            hp = Path(d) / "timeline.html"
            render_dashboard_html(report, str(hp))
            html = hp.read_text(encoding="utf-8")
            self.assertIn('id="timelineRoleLegend"', html)
            self.assertIn("function roleColor(role)", html)
            self.assertIn("Agent roles", html)
            self.assertIn("2+ child overlap interval", html)
            self.assertIn("Compaction point event", html)
            self.assertIn("Guardian activity interval", html)
            self.assertIn("Point events", html)
            self.assertIn("marker width is not duration", html)
            self.assertIn("Context refill time", html)
            self.assertIn("Configured refill threshold reached", html)
            self.assertIn("Context compaction marker", html)
            self.assertIn("Refill target", html)
        self.assertTrue(all((x["agent_id"] is None or x["agent_id"].startswith("agent-001-"))
                            for x in timeline["guardian_activity"]))

    def test_workflow_timeline_maps_real_handoffs_to_report_local_ids(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_latest_20260922T122034Z.json").read_text(encoding="utf-8"))
        report = build_workflow_only_report_v1(source, generator_version=audit.__version__)
        handoffs = report["workflow"]["profiles"][0]["timeline"]["handoffs"]
        self.assertGreater(len(handoffs), 0)
        self.assertTrue(all(x["from_agent_id"].startswith("agent-001-") for x in handoffs))
        self.assertTrue(all(x["to_agent_id"].startswith("agent-001-") for x in handoffs))
        self.assertFalse(any(str(x["from_agent_id"]).startswith("A") for x in handoffs))
        self.assertFalse(any(str(x["to_agent_id"]).startswith("A") for x in handoffs))

    def test_multiple_workflows_render_with_navigation_and_unique_local_ids(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8"))
        report = attach_workflow_profile_sources(build_quota_fixture(), [source, source])
        self.assertEqual(len(report["workflow"]["profiles"]), 2)
        self.assertEqual([x["id"] for x in report["workflow"]["profiles"]], ["workflow-001", "workflow-002"])
        first_ids = {x["id"] for x in report["workflow"]["profiles"][0]["agents"]}
        second_ids = {x["id"] for x in report["workflow"]["profiles"][1]["agents"]}
        self.assertTrue(first_ids.isdisjoint(second_ids))
        with tempfile.TemporaryDirectory() as d:
            html_path = Path(d) / "multi.html"
            render_dashboard_html(report, str(html_path))
            html = html_path.read_text(encoding="utf-8")
            self.assertIn('id="workflowSelect"', html)
            self.assertIn('id="workflowTimeline"', html)
            self.assertIn('id="history"', html)
            self.assertIn('function renderHistory()', html)
            self.assertIn('function renderWorkflowTimeline(w)', html)

    def test_workflow_profile_can_be_attached_to_quota_report(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8"))
        report = attach_workflow_profile_sources(build_quota_fixture(), [source])
        self.assertEqual(report["workflow"]["status"], "partial")
        self.assertEqual(len(report["workflow"]["profiles"]), 1)
        self.assertEqual(report["quota"]["status"], "complete")
        self.assertEqual(report["data_quality"]["metrics"]["workflow_sessions"], 29)
        self.assertIn("codex-workflow-cost-profile", report["analysis"]["methods"]["workflow_attribution"])
        self.assertTrue(any(c["name"] == "codex-workflow-cost-profile" for c in report["generator"]["components"]))

    def test_workflow_report_preserves_dashboard_safe_privacy(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8"))
        report = build_workflow_only_report_v1(source, generator_version=audit.__version__)
        serialized = json.dumps(report, allow_nan=False)
        self.assertNotRegex(serialized, r'"(?:family|session|analysis_root)"\\s*:')
        self.assertNotRegex(serialized, r'\\bS-[0-9a-f]{6,}\\b')
        self.assertNotRegex(serialized, r'\\bW-[0-9a-f]{6,}\\b')
        for path, key, value in walk(report):
            self.assertNotIn(key.lower(), FORBIDDEN_KEYS, f"forbidden key at {path}: {key}")
            if isinstance(value, str):
                for pattern in FORBIDDEN_PATTERNS:
                    self.assertIsNone(pattern.search(value), f"suspicious string at {path}.{key}: {value!r}")

    def test_unavailable_guardian_and_banked_estimates_use_null_not_fake_zero(self):
        chart = read_csv("quota_chart_data.csv")
        period = {
            "period_start": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "period_end": datetime(2026, 1, 8, tzinfo=timezone.utc),
            "period_used_points": 50.0, "approvals": 1, "paired_approvals": 0,
            "guardian_tokens": 1_000_000, "guardian_ratecard_model": "gpt-5.4",
            "guardian_ratecard_usd": 1.0, "guardian_ratecard_priced_approvals": 1,
            "guardian_long_context_events": 0, "estimate_status": "not-identifiable",
            "estimated_guardian_points": float("nan"), "estimated_guardian_points_lo": float("nan"),
            "estimated_guardian_points_hi": float("nan"), "estimated_share_of_used_percent": float("nan"),
            "estimated_share_of_used_percent_lo": float("nan"), "estimated_share_of_used_percent_hi": float("nan"),
        }
        approval = {
            "start": datetime(2026, 1, 2, tzinfo=timezone.utc), "end": datetime(2026, 1, 2, 0, 1, tzinfo=timezone.utc),
            "duration_seconds": 60, "pair_confidence": "low", "pair_method": "nearest", "pair_ambiguous": True,
            "guardian_events": 1, "guardian_tokens": 1_000_000, "guardian_cached": 900_000,
            "guardian_uncached": 90_000, "guardian_output": 10_000, "guardian_reasoning_output": 5_000,
            "guardian_ratecard_model": "gpt-5.4", "guardian_ratecard_usd": 1.0,
            "guardian_ratecard_base_usd": 1.0, "guardian_long_context_events": 0,
            "parent_model": "gpt-5.4", "parent_effort": "high", "policy_regime": "R1",
        }
        banked = {"matched_markers": 1, "matched_strata": [("x","high","R1")], "matched_banked_periods": 1,
                  "matched_comparison_periods": 1, "strongest_metric": "core_raw", "interpretation": "test",
                  "metrics": {"core_raw": {"label": "Raw tokens, excluding Guardian", "ratio": 1.0,
                                               "lo": float("nan"), "hi": float("nan"), "bootstrap_n": 0}}}
        slice_summary = {"preferred_points": 5.0, "strongest_metric": "core_raw", "interpretation": "test",
                         "slices": {5.0: {"pairs": 1, "metrics": {
                             "all_raw": {"label":"Raw tokens, all work","ratio":1.0,"lo":float('nan'),"hi":float('nan'),"bootstrap_n":0},
                             "all_api": {"label":"API $eq, all work","ratio":float('nan'),"lo":float('nan'),"hi":float('nan'),"bootstrap_n":0},
                             "core_raw": {"label":"Raw tokens, excluding Guardian","ratio":1.0,"lo":float('nan'),"hi":float('nan'),"bootstrap_n":0},
                             "core_api": {"label":"API $eq, excluding Guardian","ratio":float('nan'),"lo":float('nan'),"hi":float('nan'),"bootstrap_n":0},
                         }}}}
        report = build_cqa_report_v1(
            generator_version=audit.__version__, args=args(no_guardian_audit=False, banked_reset=['2026-01-08T00:00']),
            coverage=coverage(), chart_rows=chart, regimes=regimes_from_chart(chart), approval_episodes=[approval],
            period_cost_rows=[period], guardian_summary_data={"guardian_tokens": 0}, banked_rows=[], banked_summary=banked,
            banked_slice_rows=[], banked_slice_summary=slice_summary, report_kind="dashboard",
        )
        self.assertIsNone(report["guardian"]["periods"][0]["estimated_quota_overhead"])
        self.assertIsNone(report["banked_resets"]["boundary_slices"][0]["metrics"]["core_api"]["after_before_ratio"])
        json.dumps(report, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
