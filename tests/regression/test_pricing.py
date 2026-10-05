"""Regression tests for Work/Codex rate-card normalization."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file())
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
FIXTURES = ROOT / "tests" / "fixtures" / "reference"

from cqa.quota import audit
from cqa.report.core import build_workflow_only_report_v1, render_dashboard_html
from cqa.workflow import lifecycle, profile


class PricingRegressionTests(unittest.TestCase):
    def test_gpt_6_1_sol_standard_rates_are_configured(self):
        self.assertEqual(audit.DEFAULT_PRICES["gpt-6.1-sol"], (2.0, 0.1, 10.0))
        ts = datetime(2026, 10, 4, tzinfo=timezone.utc)
        # 50k uncached + 250k cached + 10k output => >272k input, so 2x/2x/1.5x.
        base, adjusted, long_context, resolved = audit.ratecard_event_cost(
            "gpt-6.1-sol", 50_000, 250_000, 10_000, audit.DEFAULT_PRICES, ts=ts
        )
        self.assertEqual(resolved, "gpt-6.1-sol")
        self.assertTrue(long_context)
        self.assertAlmostEqual(base, 0.225)
        self.assertAlmostEqual(adjusted, 0.4)

    def test_astra_codex_long_context_exception_is_preserved(self):
        ts = datetime(2026, 10, 4, tzinfo=timezone.utc)
        base, adjusted, long_context, resolved = audit.ratecard_event_cost(
            "gpt-6-astra", 50_000, 250_000, 10_000, audit.DEFAULT_PRICES, ts=ts
        )
        self.assertEqual(resolved, "gpt-6-astra")
        self.assertFalse(long_context)
        self.assertAlmostEqual(adjusted, base)

    def test_auto_review_mapping_is_historical(self):
        before = datetime(2026, 7, 29, 23, 59, tzinfo=timezone.utc)
        after = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(audit.auto_review_ratecard_model_at(before), "gpt-5.4")
        self.assertEqual(audit.auto_review_ratecard_model_at(after), "gpt-5.6-luna")

        pre_base, pre_cost, _pre_lc, pre_model = audit.ratecard_event_cost(
            "codex-auto-review", 100_000, 100_000, 10_000, audit.DEFAULT_PRICES, ts=before
        )
        post_base, post_cost, _post_lc, post_model = audit.ratecard_event_cost(
            "codex-auto-review", 100_000, 100_000, 10_000, audit.DEFAULT_PRICES, ts=after
        )
        self.assertEqual(pre_model, "gpt-5.4")
        self.assertEqual(post_model, "gpt-5.6-luna")
        self.assertAlmostEqual(pre_base, pre_cost)
        self.assertAlmostEqual(post_base, post_cost)
        self.assertGreater(pre_cost, post_cost)

    def test_workflow_request_pricing_uses_sol_and_long_context(self):
        ts = datetime(2026, 10, 4, tzinfo=timezone.utc)
        req = lifecycle.UsageRequest(
            ts, "S", 300_000, 250_000, 10_000, 2_000, "gpt-6.1-sol", "xhigh"
        )
        self.assertAlmostEqual(profile.request_api_eq(req, audit.DEFAULT_PRICES), 0.4)
        totals = profile.TokenTotals()
        totals.add_request(req, audit.DEFAULT_PRICES)
        self.assertAlmostEqual(totals.api_eq, 0.4)
        self.assertAlmostEqual(totals.price_coverage, 1.0)

    def test_all_sol_workflow_totals_are_fully_priced(self):
        ts = datetime(2026, 10, 4, tzinfo=timezone.utc)
        requests = [
            lifecycle.UsageRequest(ts, "S", 100_000, 50_000, 10_000, 0, "gpt-6.1-sol", "xhigh"),
            lifecycle.UsageRequest(ts, "S", 300_000, 250_000, 10_000, 0, "gpt-6.1-sol", "xhigh"),
        ]
        totals = profile.TokenTotals()
        for req in requests:
            totals.add_request(req, audit.DEFAULT_PRICES)
        # First request: .1 uncached + .005 cached + .1 output = .205.
        # Second request is >272K input and receives 2x/2x/1.5x = .4.
        self.assertAlmostEqual(totals.api_eq, 0.605)
        self.assertAlmostEqual(totals.price_coverage, 1.0)
        self.assertEqual(totals.priced_tokens, totals.total_tokens_for_coverage)

    def test_mixed_priced_unpriced_usage_reports_partial_coverage(self):
        ts = datetime(2026, 10, 4, tzinfo=timezone.utc)
        priced = lifecycle.UsageRequest(ts, "S", 100_000, 50_000, 10_000, 0, "gpt-6.1-sol", "xhigh")
        unknown = lifecycle.UsageRequest(ts, "S", 100_000, 50_000, 10_000, 0, "future-model", "xhigh")
        totals = profile.TokenTotals()
        totals.add_request(priced, audit.DEFAULT_PRICES)
        totals.add_request(unknown, audit.DEFAULT_PRICES)
        self.assertAlmostEqual(totals.price_coverage, 0.5)
        self.assertGreater(totals.api_eq, 0)

    def test_pricing_breakdown_is_auditable_by_rate_row_and_long_context_uplift(self):
        ts = datetime(2026, 10, 4, tzinfo=timezone.utc)
        requests = [
            lifecycle.UsageRequest(ts, "S", 100_000, 50_000, 10_000, 0, "gpt-6.1-sol", "xhigh"),
            lifecycle.UsageRequest(ts, "S", 300_000, 250_000, 10_000, 0, "gpt-6.1-sol", "xhigh"),
            lifecycle.UsageRequest(ts, "S", 100_000, 50_000, 10_000, 0, "codex-auto-review", "low"),
            lifecycle.UsageRequest(ts, "S", 100_000, 50_000, 10_000, 0, "future-model", "xhigh"),
        ]
        breakdown = profile.build_pricing_breakdown(requests, audit.DEFAULT_PRICES)
        self.assertEqual(breakdown["priced_requests"], 3)
        self.assertEqual(breakdown["unpriced_requests"], 1)
        self.assertEqual(breakdown["long_context_priced_requests"], 1)
        self.assertAlmostEqual(breakdown["long_context_price_uplift_usd"], 0.175)
        self.assertGreater(breakdown["price_coverage"], 0)
        self.assertLess(breakdown["price_coverage"], 1)

        rows = {(r["observed_model"], r["ratecard_model"]): r for r in breakdown["by_model"]}
        sol = rows[("gpt-6.1-sol", "gpt-6.1-sol")]
        self.assertEqual(sol["requests"], 2)
        self.assertEqual(sol["long_context_priced_requests"], 1)
        self.assertAlmostEqual(sol["api_list_equivalent_usd"], 0.605)
        self.assertAlmostEqual(sol["long_context_price_uplift_usd"], 0.175)
        self.assertEqual(sol["rates_per_million"], {
            "uncached_input": 2.0, "cached_input": 0.1, "output": 10.0,
        })

        review = rows[("codex-auto-review", "gpt-5.6-luna")]
        self.assertEqual(review["requests"], 1)
        self.assertAlmostEqual(review["api_list_equivalent_usd"], 0.023)
        unknown = rows[("future-model", "future-model")]
        self.assertIsNone(unknown["api_list_equivalent_usd"])
        self.assertEqual(unknown["price_coverage"], 0.0)

    def test_report_extension_carries_pricing_coverage_and_dashboard_warns_on_partial(self):
        source = json.loads((FIXTURES / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8"))
        total = source["nested_attribution"]["total"]
        total["price_coverage"] = 0.5
        total["api_list_equivalent_usd"] = 12.34
        source.setdefault("comparison", {})["workflow_api_eq"] = 12.34
        source["pricing"] = {
            "basis": "ChatGPT Work/Codex Standard token rates",
            "source": audit.RATECARD_SOURCE,
            "source_ref": audit.RATECARD_SOURCE_REF,
            "as_of": audit.RATECARD_AS_OF,
            "auto_review_transition_date": "2026-07-30",
            "auto_review_before": "gpt-5.4",
            "auto_review_on_or_after": "gpt-5.6-luna",
            "long_context_threshold_input_tokens": 272000,
            "long_context_input_multiplier": 2.0,
            "long_context_cached_multiplier": 2.0,
            "long_context_output_multiplier": 1.5,
            "long_context_exempt_models": ["gpt-6-astra"],
            "by_model": [{
                "observed_model": "gpt-6.1-sol",
                "ratecard_model": "gpt-6.1-sol",
                "requests": 2,
                "priced_requests": 2,
                "input_tokens": 400000,
                "cached_input_tokens": 300000,
                "uncached_input_tokens": 100000,
                "output_tokens": 20000,
                "total_tokens": 420000,
                "priced_tokens": 420000,
                "api_list_equivalent_usd": 0.605,
                "base_api_list_equivalent_usd": 0.430,
                "price_coverage": 1.0,
                "long_context_priced_requests": 1,
                "long_context_price_uplift_usd": 0.175,
                "rates_per_million": {"uncached_input": 2.0, "cached_input": 0.1, "output": 10.0},
            }],
            "priced_requests": 2,
            "unpriced_requests": 1,
            "long_context_priced_requests": 1,
            "long_context_price_uplift_usd": 0.175,
            "custom_price_override": False,
        }
        report = build_workflow_only_report_v1(source, generator_version="test")
        pricing = report["workflow"]["profiles"][0]["extensions"]["pricing"]
        self.assertEqual(pricing["status"], "partial")
        self.assertAlmostEqual(pricing["price_coverage"], 0.5)
        self.assertEqual(pricing["as_of"], audit.RATECARD_AS_OF)
        self.assertEqual(pricing["priced_requests"], 2)
        self.assertEqual(pricing["unpriced_requests"], 1)
        self.assertEqual(pricing["long_context_priced_requests"], 1)
        self.assertAlmostEqual(pricing["long_context_price_uplift_usd"], 0.175)
        self.assertEqual(pricing["by_model"][0]["ratecard_model"], "gpt-6.1-sol")
        with tempfile.TemporaryDirectory() as d:
            hp = Path(d) / "report.html"
            render_dashboard_html(report, str(hp))
            html = hp.read_text(encoding="utf-8")
            self.assertIn("Priced subtotal", html)
            self.assertIn("incomplete workflow cost estimate", html)
            self.assertIn("price coverage", html)
            self.assertIn("Pricing details", html)
            self.assertIn("Cost by model / rate-card row", html)
            self.assertIn("Long-context pricing", html)
            self.assertIn("inspect pricing details", html)


if __name__ == "__main__":
    unittest.main()
