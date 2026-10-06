"""Policy attribution must not erase work or invent server billing evidence."""
import contextlib
import csv
import io
import json
import math
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cqa import auto_review_policy as policy, cli
from cqa.quota import audit
from cqa.report.core import build_cqa_report_v1, build_workflow_only_report_v1
from cqa.workflow import lifecycle, profile, records, telemetry
from tests.regression import test_workflow_cache as cache_tests


OLD = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
FREE = policy.ANNOUNCED_AT + timedelta(seconds=1)


def event(ts, used=1, model="codex-auto-review", mode="chatgpt", tokens=1000):
    reset = (OLD + timedelta(days=7)).timestamp()
    e = audit._fake_event(ts.isoformat(), used, reset, model=model, unc=tokens, cached=0, out=0)
    e.source = "review" if model == "codex-auto-review" else "parent"
    e.auth_mode, e.auth_source = mode, "rollout_auth_mode" if mode != "unknown" else "unknown"
    e.rate_windows = {10080: audit.RateWindow(used, reset)}
    return e


def approval(ts, tokens=1_000_000, mode="chatgpt"):
    return {"start": ts, "end": ts, "guardian_tokens": tokens, "guardian_ratecard_usd": 1.5,
            "guardian_ratecard_base_usd": 1.5, "pair_confidence": "high",
            "quota_policy": policy.summarize([(policy.classify(ts, mode, "rollout_auth_mode"), tokens)])}


def period_rows(approvals):
    args = audit.build_parser().parse_args([])
    primary = audit.analyze_events([event(OLD, 10, "gpt-6.1-sol"), event(FREE, 15)], args)
    fit = {"status": "supported", "guardian_coef": 2, "guardian_lo": 1, "guardian_hi": 3}
    return audit.guardian_period_cost_rows(primary, approvals, fit, args)


def report(approvals, rows):
    args = audit.build_parser().parse_args([])
    return build_cqa_report_v1(generator_version="0.9.1", args=args,
        coverage={"log_start": OLD, "log_end": FREE}, chart_rows=[], regimes=[],
        approval_episodes=approvals, period_cost_rows=rows,
        guardian_summary_data=audit.guardian_summary(rows, approvals),
        banked_rows=[], banked_summary={}, banked_slice_rows=[], banked_slice_summary={})


class AutoReviewPolicyTests(unittest.TestCase):
    def test_reference_boundaries_and_authentication_scope(self):
        self.assertEqual(policy.classify(OLD)["status"], "historical")
        self.assertEqual(policy.classify(policy.TRANSITION_START, "chatgpt")["status"], "transition")
        self.assertEqual(policy.classify(policy.ANNOUNCED_AT - timedelta(microseconds=1), "chatgpt")["status"], "transition")
        self.assertEqual(policy.classify(policy.ANNOUNCED_AT, "chatgpt")["policy_quota_points"], 0)
        self.assertEqual(policy.classify(FREE)["status"], "unknown")
        self.assertEqual(policy.classify(FREE, "api")["status"], "outside_scope")
        self.assertEqual(policy.classify(OLD, "api")["status"], "outside_scope")
        self.assertEqual(policy.classify(None, "chatgpt")["status"], "unknown")
        self.assertEqual(policy.classify(FREE.replace(tzinfo=None), "chatgpt")["status"], "unknown")

    def test_declaration_is_an_assumption_and_does_not_override_facts(self):
        q = policy.classify(FREE, declaration="chatgpt")
        self.assertEqual((q["status"], q["auth_source"]), ("free", "declared"))
        self.assertEqual(policy.classify(FREE, "api", "rollout_auth_mode", "chatgpt")["status"], "outside_scope")
        self.assertEqual(policy.classify(FREE, "unknown", "conflicting_metadata", "chatgpt")["status"], "unknown")
        self.assertEqual(policy.classify(OLD, declaration="chatgpt")["status"], "historical")

    def test_auth_evidence_is_narrow_and_explicit_evidence_wins(self):
        self.assertIsNone(policy.auth_evidence({"auth_mode": "chatgpt", "type": "message"}, "response_item"))
        self.assertIsNone(policy.auth_evidence({"model_provider": "openai", "source": "cli"}, "session_meta"))
        self.assertEqual(policy.auth_evidence({"type": "token_count", "rate_limits": {"plan_type": "pro"}}, "event_msg"),
                         ("chatgpt", "rate_limit_plan_type"))
        explicit = policy.auth_evidence({"auth_mode": "apikey", "rate_limits": {"plan_type": "pro"}}, "turn_context")
        self.assertEqual(explicit, ("api", "rollout_auth_mode"))
        self.assertEqual(policy.update_auth(*explicit, ("chatgpt", "rate_limit_plan_type")), explicit)
        self.assertEqual(policy.update_auth(*explicit, ("chatgpt", "rollout_auth_mode")), ("chatgpt", "rollout_auth_mode"))
        self.assertEqual(policy.auth_evidence({"auth_mode": "api", "authentication_mode": "chatgpt"}, "session_meta"),
                         ("unknown", "conflicting_metadata"))

    def test_quota_and_workflow_extract_same_historical_auth_facts(self):
        def record(ts, payload, kind):
            return json.dumps({"timestamp": ts.isoformat(), "type": kind, "payload": payload})
        usage = {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 10}},
                 "rate_limits": {"limit_id": "codex", "plan_type": "pro", "primary": {
                     "used_percent": 5, "window_minutes": 10080, "resets_at": (FREE + timedelta(days=7)).timestamp()}}}
        rows = [record(OLD, {"model": "codex-auto-review", "auth_mode": "api"}, "session_meta"),
                record(OLD, usage, "event_msg"),
                record(FREE, {"authentication_mode": "chatgpt"}, "turn_context"),
                record(FREE, usage, "event_msg")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("\n".join(rows))
            quota = audit.parse_file(str(path), audit.DEFAULT_PRICES, 10080, audit.ParseStats())
            workflow = records.read_consumer(str(path), lifecycle.session_consumer("safe", []), None).usage
        self.assertEqual([q.auth_mode for q in quota], ["api", "chatgpt"])
        self.assertEqual([q.auth_mode for q in workflow], ["api", "chatgpt"])
        self.assertEqual([policy.observation(q)["status"] for q in quota], ["outside_scope", "free"])
        self.assertEqual([policy.observation(q)["status"] for q in workflow], ["outside_scope", "free"])

    def test_burst_segments_split_at_policy_and_reset_boundaries(self):
        args = audit.build_parser().parse_args([])
        reset_boundary = FREE + timedelta(seconds=1)
        primary = SimpleNamespace(episodes=[SimpleNamespace(activation_at=OLD), SimpleNamespace(activation_at=reset_boundary)])
        before = policy.ANNOUNCED_AT - timedelta(seconds=1)
        events = [event(before), event(FREE), event(reset_boundary)]
        groups = list(audit._review_policy_groups(events, args, primary))
        self.assertEqual([len(g) for g in groups], [1, 1, 1])
        self.assertEqual(sum(e.tokens for g in groups for e in g), sum(e.tokens for e in events))
        midnight = [event(policy.TRANSITION_START - timedelta(seconds=1)), event(policy.TRANSITION_START)]
        self.assertEqual([len(g) for g in audit._review_policy_groups(midnight, args, primary)], [1, 1])

    def test_fit_excludes_free_transition_unknown_and_api_episodes(self):
        rng = random.Random(103)
        historical = []
        for i in range(80):
            parent, guardian = rng.uniform(.05, 3), rng.uniform(.05, 3)
            row = approval(OLD - timedelta(days=i), int(guardian * 1e6))
            row.update(parent_tokens=int(parent * 1e6), parent_source=str(i % 10),
                       quota_10080m_status="ok", quota_10080m_points=.2 * parent + .8 * guardian)
            historical.append(row)
        fit = audit.guardian_incremental_fit(historical, 10080, 100)
        self.assertEqual(fit["status"], "supported")
        extras = [approval(FREE), approval(policy.TRANSITION_START), approval(FREE, mode="unknown"), approval(OLD, mode="api")]
        for row in extras:
            row.update(parent_tokens=100000, parent_source="new", quota_10080m_status="ok", quota_10080m_points=100)
        self.assertEqual(audit.guardian_incremental_fit(historical + extras, 10080, 100), fit)
        self.assertEqual(audit.guardian_incremental_fit(extras, 10080, 100)["episodes"], 0)

    def test_historical_context_and_snapshot_cannot_cross_transition_day(self):
        args = audit.build_parser().parse_args([])
        args.guardian_context_seconds = 5
        review_ts = policy.TRANSITION_START - timedelta(seconds=1)
        events = [event(review_ts - timedelta(seconds=2), 1, "gpt-6.1-sol"), event(review_ts, 2)]
        episodes = audit.build_approval_episodes(events, audit.ParseStats(), args)
        self.assertEqual(episodes[0]["quota_policy"]["status"], "historical")
        self.assertFalse(episodes[0]["historical_fit_eligible"])
        self.assertEqual(audit.guardian_incremental_fit(episodes, 10080, 100)["episodes"], 0)
        # Parent context can be historical while a particular window's next
        # usable account snapshot comes from the transition day.
        args.guardian_context_seconds = 60
        args.guardian_quota_snapshot_seconds = 900
        review = event(policy.TRANSITION_START - timedelta(minutes=10), 2)
        review.rate_windows = {300: audit.RateWindow(2, review.reset_at)}
        events = [event(review.ts - timedelta(seconds=1), 1, "gpt-6.1-sol"), review,
                  event(policy.TRANSITION_START, 3, "gpt-6.1-sol")]
        row = audit.build_approval_episodes(events, audit.ParseStats(), args)[0]
        self.assertTrue(row["historical_fit_eligible"])
        self.assertEqual(row["quota_10080m_status"], "ok")
        self.assertFalse(row["quota_10080m_historical_fit_eligible"])
        self.assertEqual(audit.guardian_incremental_fit([row], 10080, 100)["episodes"], 0)

    def test_free_period_zero_is_policy_based_and_global_meter_is_preserved(self):
        approvals = [approval(FREE)]
        rows = period_rows(approvals)
        self.assertEqual(rows[0]["period_used_points"], 15)
        self.assertEqual(rows[0]["quota_policy"]["policy_quota_points"], 0)
        self.assertTrue(math.isnan(rows[0]["estimated_guardian_points"]))
        model = report(approvals, rows)
        self.assertEqual(model["guardian"]["status"], "complete")
        p = model["guardian"]["periods"][0]
        self.assertIsNone(p["estimated_quota_overhead"])
        self.assertEqual(p["quota_points_used"], 15)
        self.assertGreater(p["ratecard"]["api_list_equivalent_usd"], 0)
        self.assertEqual(p["extensions"]["auto_review_quota_policy"]["status"], "free")

    def test_mixed_period_keeps_subtotals_and_unknown_portion(self):
        approvals = [approval(OLD + timedelta(seconds=1)), approval(FREE), approval(FREE + timedelta(seconds=1), mode="unknown")]
        rows = period_rows(approvals)
        q = rows[0]["quota_policy"]
        self.assertEqual(q["status"], "mixed")
        self.assertEqual(q["tokens_by_status"]["historical"], 1_000_000)
        self.assertEqual(q["tokens_by_status"]["free"], 1_000_000)
        self.assertEqual(q["tokens_by_status"]["unknown"], 1_000_000)
        self.assertEqual(q["auth_modes"], ["chatgpt", "unknown"])
        self.assertEqual(q["historical_estimate"], {"value": 2, "lo": 1, "hi": 3})
        self.assertIsNone(q["policy_quota_points"])
        self.assertTrue(math.isnan(rows[0]["estimated_guardian_points"]))
        model = report(approvals, rows)
        self.assertEqual(model["guardian"]["status"], "partial")
        self.assertIsNone(model["guardian"]["summary"]["estimated_quota_overhead"])
        self.assertEqual(model["guardian"]["extensions"]["auto_review_quota_policy"]["historical_estimate"]["value"], 2)

    def test_api_and_unknown_periods_do_not_get_zero_or_historical_coefficient(self):
        for mode in ("api", "unknown"):
            with self.subTest(mode=mode):
                rows = period_rows([approval(FREE, mode=mode)])
                self.assertIsNone(rows[0]["quota_policy"]["policy_quota_points"])
                self.assertNotIn("historical_estimate", rows[0]["quota_policy"])
                self.assertTrue(math.isnan(rows[0]["estimated_guardian_points"]))

    def test_historical_period_estimate_is_unchanged(self):
        approvals = [approval(OLD + timedelta(seconds=1))]
        row = period_rows(approvals)[0]
        self.assertEqual((row["estimated_guardian_points"], row["estimated_guardian_points_lo"], row["estimated_guardian_points_hi"]), (2, 1, 3))
        self.assertIsNotNone(report(approvals, [row])["guardian"]["periods"][0]["estimated_quota_overhead"])

    def test_quota_value_fit_exclusion_keeps_concurrent_work_and_meter(self):
        review, coding = event(FREE, 11), event(FREE + timedelta(seconds=1), 15, "gpt-6.1-sol")
        bucket = audit.make_bucket(1, coding.reset_at, OLD, 10, coding, [review, coding], 5)
        self.assertEqual((bucket.points, bucket.total_tokens), (5, review.tokens + coding.tokens))
        self.assertGreater(bucket.usage.api_usd, 0)
        self.assertFalse(audit.usable_bucket(bucket, .95))
        self.assertEqual(audit._dominant_weight_rows([bucket], 0), [])
        self.assertTrue(audit._quota_slice_from_buckets([bucket], 5, from_end=False, min_price_coverage=.95)["auto_review_policy_excluded"])
        historical = event(OLD)
        bucket = audit.make_bucket(1, historical.reset_at, OLD, 0, historical, [historical], 1)
        self.assertTrue(audit.usable_bucket(bucket, .95))

    def test_workflow_pricing_retains_nonzero_api_equivalent(self):
        req = lifecycle.UsageRequest(FREE, "safe", 100000, 50000, 10000, 0, "codex-auto-review", "unknown", auth_mode="chatgpt", auth_source="rollout_auth_mode")
        pricing = profile.build_pricing_breakdown([req], audit.DEFAULT_PRICES)
        self.assertGreater(pricing["by_model"][0]["api_list_equivalent_usd"], 0)
        self.assertEqual(pricing["auto_review_quota_policy"]["policy_quota_points"], 0)
        fixture = ROOT / "tests" / "fixtures" / "reference" / "workflow_cost_profile_latest_20260922T122034Z.json"
        source = json.loads(fixture.read_text())
        source["auto_review_quota_policy"] = pricing["auto_review_quota_policy"]
        source.setdefault("pricing", {}).update(pricing)
        normalized = build_workflow_only_report_v1(source, generator_version="0.9.1")["workflow"]["profiles"][0]
        self.assertEqual(normalized["extensions"]["auto_review_quota_policy"]["policy_quota_points"], 0)
        self.assertGreater(normalized["extensions"]["pricing"]["by_model"][0]["api_list_equivalent_usd"], 0)

    def test_declared_api_history_is_also_excluded_from_quota_value_fits(self):
        events = [event(OLD, 0, "gpt-6.1-sol"), event(OLD + timedelta(seconds=1), 1, mode="unknown")]
        args = audit.build_parser().parse_args(["--auto-review-auth-mode", "api"])
        analysis = audit.analyze_events(events, args)
        self.assertTrue(analysis.buckets[0].quota_fit_excluded)
        self.assertEqual(analysis.events[1].auth_source, "declared")
        self.assertEqual(events[1].auth_mode, "unknown")
        self.assertEqual(analysis.buckets[0].points, 1)

    def test_csv_and_cli_distinguish_policy_zero_from_estimate(self):
        approvals = [approval(FREE)]
        rows = period_rows(approvals)
        with tempfile.TemporaryDirectory() as directory:
            for exporter, values in ((audit.export_guardian_period_cost_csv, rows), (audit.export_approval_episodes_csv, approvals)):
                path = Path(directory) / "export.csv"
                exporter(str(path), values)
                with path.open() as stream:
                    row = next(csv.DictReader(stream))
                self.assertEqual(row["auto_review_policy_status"], "free")
                self.assertEqual(float(row["policy_quota_points"]), 0)
                self.assertEqual(int(row["free_guardian_tokens"]), 1_000_000)
            mixed = period_rows([approval(OLD), approval(FREE)])
            audit.export_guardian_period_cost_csv(str(path), mixed)
            with path.open() as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["auto_review_policy_status"], "mixed")
            self.assertEqual(row["policy_quota_points"], "")
            self.assertEqual(float(row["historical_estimated_guardian_points"]), 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            audit.print_guardian_period_cost(rows, {"status": "not identifiable"}, 10080)
        self.assertIn("0 quota points under announced policy", output.getvalue())
        self.assertNotIn("historical quota cost is not identifiable", output.getvalue())

    def test_policy_declaration_is_applied_after_cache_load(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = cache_tests.WorkflowCacheTests()
            home, ids = helper.fixture(directory)
            path = home / "sessions" / "root.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            for row in rows:
                row["timestamp"] = FREE.isoformat()
                if row["type"] == "session_meta":
                    row["payload"]["model"] = "codex-auto-review"
            path.write_text("\n".join(json.dumps(row) for row in rows))
            unknown = helper.run_profile(home, ids["root"])
            with mock.patch.object(telemetry, "extract", side_effect=AssertionError("cache should be reused")):
                declared = helper.run_profile(home, ids["root"], "--auto-review-auth-mode", "chatgpt")
            self.assertEqual(unknown["auto_review_quota_policy"]["status"], "unknown")
            self.assertEqual(declared["auto_review_quota_policy"]["status"], "free")
            self.assertEqual(declared["auto_review_quota_policy"]["auth_bases"], ["declared"])
            self.assertEqual(unknown["nested_attribution"]["total"], declared["nested_attribution"]["total"])
            for before, after in zip(unknown["pricing"]["by_model"], declared["pricing"]["by_model"]):
                self.assertEqual({k: v for k, v in before.items() if k != "quota_policy"},
                                 {k: v for k, v in after.items() if k != "quota_policy"})

    def test_combined_dashboard_applies_declaration_to_both_analyzers(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = cache_tests.WorkflowCacheTests()
            home, ids = helper.fixture(directory)
            for path in (home / "sessions").glob("*.jsonl"):
                i = {"root": 0, "alpha": 1, "beta": 2}[path.stem]
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                for n, row in enumerate(rows):
                    row["timestamp"] = (FREE + timedelta(seconds=10 * i + n)).isoformat()
                    if row["type"] == "session_meta":
                        row["payload"]["model"] = "codex-auto-review" if path.stem == "alpha" else "gpt-6.1-sol"
                    if row["payload"].get("type") == "token_count":
                        row["payload"]["rate_limits"] = {"limit_id": "codex", "primary": {
                            "used_percent": 10 + i, "window_minutes": 10080,
                            "resets_at": (FREE + timedelta(days=7)).timestamp()}}
                path.write_text("\n".join(json.dumps(row) for row in rows))
            output = home / "combined.json"
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cli.dashboard_main(["--home", str(home), "--workflow", ids["root"], "--auto-review-auth-mode", "chatgpt",
                                         "--output", str(home / "combined.html"), "--report-json", str(output), "--no-open", "--quiet"])
            self.assertEqual(rc, 0)
            model = json.loads(output.read_text())
            guardian = model["guardian"]["extensions"]["auto_review_quota_policy"]
            workflow = model["workflow"]["profiles"][0]["extensions"]["auto_review_quota_policy"]
            for q in (guardian, workflow):
                self.assertEqual((q["status"], q["auth_declaration"], q["auth_bases"]), ("free", "chatgpt", ["declared"]))
                self.assertEqual(q["policy_quota_points"], 0)

    def test_normalized_extension_drops_private_unrecognized_values(self):
        q = policy.summarize([(policy.classify(FREE, "chatgpt"), 1)])
        q.update(auth_bases=["/home/private", "declared"], access_token="secret", source_ref="private account")
        safe = policy.normalize(q)
        self.assertEqual(safe["auth_bases"], ["declared"])
        self.assertEqual(safe["source_ref"], policy.SOURCE_REF)
        self.assertNotIn("secret", json.dumps(safe))


if __name__ == "__main__":
    unittest.main()
