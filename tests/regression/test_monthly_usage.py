"""Calendar, counting, provenance, export and cache boundaries for cqa usage."""
import contextlib
import csv
import io
import json
import math
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from cqa import cli
from cqa import reports as reportlib
from cqa.report.core import build_usage_only_report_v1
from cqa.quota import audit
from cqa.usage import analysis, loader
from cqa.usage import cli as usage_cli
from cqa.workflow import candidates as finder, cache


SEPTEMBER = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
CANARY = "private-prompt-email-session-01a10d1a-ddc5-71f0-b4c4-f0b2426d00a6"


def record(ts, payload, kind="event_msg"):
    return {"timestamp": ts.isoformat(), "type": kind, "payload": payload}


def usage(ts, inp=100, cached=50, output=10, *, total=None, meter=False):
    payload = {"type": "token_count", "info": {"last_token_usage": {
        "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": output,
        "reasoning_output_tokens": 2}}}
    if total is not None:
        payload["info"]["total_token_usage"] = {"input_tokens": total, "cached_input_tokens": 0, "output_tokens": 0}
    if meter:
        payload["rate_limits"] = {"limit_id": "codex", "primary": {"window_minutes": 10080, "used_percent": 1, "resets_at": 1800000000}}
    return record(ts, payload)


def write(home, name, rows):
    path = Path(home) / "sessions" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def metadata(ts=SEPTEMBER, model="gpt-6.1-sol", auth=None):
    payload = {"model": model, "email": CANARY, "id": CANARY}
    if auth is not None:
        payload["auth_mode"] = auth
    return record(ts, payload, "session_meta")


def load(home, prices=None, **kwargs):
    return loader.load_events(str(home), prices or audit.DEFAULT_PRICES, 10080, 2, 20, 5, 2, 20,
                              include_unmetered=True, **kwargs)


def result(home, *, prices=None, month="2026-09", models=(), **kwargs):
    events, stats = load(home, prices, **kwargs)
    bounds = analysis.period(month, timezone_name="Europe/Berlin")[2]
    return analysis.analyze(events, stats, prices or audit.DEFAULT_PRICES, bounds, models=models)


class MonthlyUsageTests(unittest.TestCase):
    def test_calendar_bounds_include_start_exclude_end_and_handle_dst(self):
        start, end, bounds = analysis.period("2026-09", timezone_name="Europe/Berlin")
        self.assertEqual(bounds["start_inclusive"], "2026-08-31T22:00:00Z")
        self.assertEqual(bounds["end_exclusive"], "2026-09-30T22:00:00Z")
        a, b, _ = analysis.period("2026-10", timezone_name="Europe/Berlin")
        self.assertEqual((b - a).total_seconds(), (31 * 24 + 1) * 3600)
        a, b, _ = analysis.period("2026-03", timezone_name="Europe/Berlin")
        self.assertEqual((b - a).total_seconds(), (31 * 24 - 1) * 3600)
        with tempfile.TemporaryDirectory() as home:
            write(home, "range", [metadata(), usage(start - timedelta(microseconds=1)), usage(start),
                                  usage(end - timedelta(microseconds=1)), usage(end)])
            self.assertEqual(result(home)["summary"]["requests"], 2)

    def test_current_month_uses_selected_timezone_and_december_rolls_over(self):
        now = datetime(2026, 9, 30, 23, tzinfo=timezone.utc)
        self.assertEqual(analysis.period(timezone_name="Europe/Berlin", now=now)[2]["label"], "2026-10")
        self.assertEqual(analysis.period("2026-12")[2]["end_exclusive"], "2027-01-01T00:00:00Z")
        with mock.patch.object(analysis, "ZoneInfo", side_effect=AssertionError("UTC needs no tzdata")):
            self.assertEqual(analysis.period("2026-09")[2]["timezone"], "UTC")

    def test_invalid_and_ambiguous_selection_rejected(self):
        for kwargs in ({"month": "2026-13"}, {"month": "26-09"}, {"month": "9999-12"},
                       {"month": "2026-09", "after": SEPTEMBER.isoformat()},
                       {"after": SEPTEMBER.isoformat()},
                       {"after": "2026-09-01T00:00:00", "before": "2026-10-01T00:00:00Z"},
                       {"after": SEPTEMBER.isoformat(), "before": SEPTEMBER.isoformat()},
                       {"timezone_name": "Nonexistent/Nowhere"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                analysis.period(**kwargs)
        start, end, _ = analysis.period(after="2026-09-01T00:00:00+02:00", before="2026-10-01T00:00:00+02:00")
        self.assertEqual((end - start).days, 30)

    def test_usage_includes_unmetered_records_and_quota_behavior_is_preserved(self):
        with tempfile.TemporaryDirectory() as home:
            path = write(home, "mixed", [metadata(auth="chatgpt"), usage(SEPTEMBER),
                                          usage(SEPTEMBER + timedelta(seconds=1), meter=True)])
            rows = result(home)
            self.assertEqual(rows["summary"]["requests"], 2)
            self.assertEqual(rows["coverage"]["without_quota_snapshot_requests"], 1)
            self.assertEqual(rows["summary"]["total_tokens"], 220)  # Reasoning is already output.
            legacy = audit.parse_file(str(path), audit.DEFAULT_PRICES, 10080, audit.ParseStats())
            self.assertEqual(len(legacy), 1)
            events, _ = audit.load_events(home, audit.DEFAULT_PRICES, 10080, 2, 20, 5, 2, 20)
            self.assertEqual(len(events), 1)

    def test_invalid_usage_does_not_invent_a_dollar_value(self):
        with tempfile.TemporaryDirectory() as home:
            write(home, "invalid", [metadata(), usage(SEPTEMBER, inp="invalid"),
                                    usage(SEPTEMBER, inp=-1), usage(SEPTEMBER, cached=101),
                                    record(SEPTEMBER, {"type": "token_count", "info": {}}),
                                    usage(SEPTEMBER + timedelta(seconds=1))])
            rows = result(home)
            self.assertEqual(rows["summary"]["requests"], 1)
            self.assertEqual(rows["coverage"]["history"]["missing_or_invalid_usage_records"], 4)
            self.assertEqual(rows["status"], "partial")

    def test_global_and_immediate_duplicates_are_removed_once(self):
        with tempfile.TemporaryDirectory() as home:
            rows = [metadata(), usage(SEPTEMBER, total=100), usage(SEPTEMBER + timedelta(seconds=1), total=100),
                    usage(SEPTEMBER + timedelta(seconds=2), total=200)]
            write(home, "first", rows)
            write(home, "copy", rows)
            data = result(home)
            self.assertEqual(data["summary"]["requests"], 2)
            self.assertEqual(data["coverage"]["history"]["global_duplicates_removed"], 2)
            self.assertEqual(data["coverage"]["history"]["immediate_duplicates_removed"], 2)

    def test_replay_classification_precedes_calendar_filtering(self):
        with tempfile.TemporaryDirectory() as home:
            first = datetime(2026, 8, 31, 23, 59, 59, 800000, tzinfo=timezone.utc)
            rows = [metadata(first)]
            rows += [usage(first + timedelta(milliseconds=10 * i), inp=1000000, cached=0, total=(i + 1) * 1000000)
                     for i in range(30)]
            rows += [usage(first + timedelta(seconds=10), total=30000100)]
            write(home, "replay", rows)
            events, stats = load(home)
            data = analysis.analyze(events, stats, audit.DEFAULT_PRICES, analysis.period("2026-09")[2])
            self.assertEqual(data["summary"]["requests"], 1)
            self.assertEqual(data["coverage"]["excluded_probable_replay_requests"], 10)

    def test_sign_in_evidence_and_auto_review_remain_separate(self):
        with tempfile.TemporaryDirectory() as home:
            write(home, "chatgpt", [metadata(auth="chatgpt"), usage(SEPTEMBER)])
            write(home, "api", [metadata(auth="api"), usage(SEPTEMBER + timedelta(seconds=1))])
            write(home, "unknown", [metadata(model="codex-auto-review"), usage(SEPTEMBER + timedelta(seconds=2))])
            data = result(home)
            modes = {r["auth_mode"]: r for r in data["by_auth_mode"]}
            self.assertEqual({k: r["requests"] for k, r in modes.items()}, {"unknown": 1, "chatgpt": 1, "api": 1})
            self.assertEqual(data["by_activity"][1]["requests"], 1)
            self.assertGreater(data["by_activity"][1]["api_list_equivalent_usd"], 0)

    def test_unknown_model_retains_tokens_and_exposes_only_a_priced_subtotal(self):
        with tempfile.TemporaryDirectory() as home:
            write(home, "known", [metadata(), usage(SEPTEMBER)])
            write(home, "unknown", [metadata(model="future-model"), usage(SEPTEMBER + timedelta(seconds=1))])
            data = result(home)
            self.assertEqual(data["summary"]["total_tokens"], 220)
            self.assertEqual(data["summary"]["price_coverage"], .5)
            self.assertIsNone(data["summary"]["api_list_equivalent_usd"])
            self.assertGreater(data["summary"]["priced_subtotal_usd"], 0)
            self.assertEqual(data["status"], "partial")
            unknown = next(row for row in data["by_model"] if row["model"] == "future-model")
            self.assertEqual(unknown["rate_models"], [])

    def test_pricing_uses_per_request_long_context_and_historical_review_mapping(self):
        with tempfile.TemporaryDirectory() as home:
            write(home, "long", [metadata(), usage(SEPTEMBER, inp=300000, cached=250000, output=10000)])
            data = result(home)
            self.assertAlmostEqual(data["summary"]["api_list_equivalent_usd"], .4)
            self.assertAlmostEqual(data["summary"]["long_context_uplift_usd"], .175)
            self.assertEqual(data["summary"]["long_context_requests"], 1)
            write(home, "astra", [metadata(model="gpt-6-astra"), usage(SEPTEMBER + timedelta(seconds=1), inp=300000, cached=250000, output=10000)])
            self.assertEqual(result(home)["summary"]["long_context_requests"], 1)
            early = datetime(2026, 7, 29, tzinfo=timezone.utc)
            write(home, "old-review", [metadata(early, "codex-auto-review"), usage(early)])
            review = result(home, month="2026-07", models=["codex-auto-review"])
            self.assertIn("gpt-5.4", review["pricing"]["rates_per_million"])
            self.assertEqual(review["by_model"][0]["rate_models"], ["gpt-5.4"])
            self.assertAlmostEqual(review["summary"]["api_list_equivalent_usd"], .0002875)

    def test_cache_reuses_extraction_when_period_filter_or_prices_change(self):
        with tempfile.TemporaryDirectory() as home:
            write(home, "warm", [metadata(), usage(SEPTEMBER)])
            cold = result(home)
            with mock.patch.object(audit, "parse_file", side_effect=AssertionError("warm run reparsed logs")):
                warm = result(home)
                empty = result(home, month="2026-08")
                changed = result(home, prices={"gpt-6.1-sol": (4, .2, 20)})
                filtered = result(home, models=["other-model"])
            self.assertEqual(warm["summary"], cold["summary"])
            self.assertAlmostEqual(changed["summary"]["api_list_equivalent_usd"], cold["summary"]["api_list_equivalent_usd"] * 2)
            self.assertEqual(warm["coverage"]["cache"]["processed"], 0)
            self.assertEqual(empty["status"], "not_available")
            self.assertIsNone(filtered["summary"]["api_list_equivalent_usd"])
            direct = result(home, use_cache=False)
            self.assertEqual(direct["summary"], cold["summary"])

    def test_usage_format_and_rebuild_preserve_legacy_discovery_and_quota_keys(self):
        with tempfile.TemporaryDirectory() as home:
            path = write(home, "cache", [metadata(), usage(SEPTEMBER, meter=True)])
            finder.discover_workflow_families(home)
            result(home)
            with mock.patch.dict(cache.EXTRACTION_VERSIONS, usage=99):
                self.assertEqual(result(home)["coverage"]["cache"]["processed"], 1)
                self.assertEqual(finder.discover_workflow_families(home).stats["processed"], 0)
            result(home, rebuild_cache=True)
            self.assertEqual(finder.discover_workflow_families(home).stats["processed"], 0)
            with cache.Index(home) as index:
                value, _ = index.get(str(path), "quota", 10080)
                self.assertIsNotNone(value)
                self.assertEqual(index.config(10080, "quota"), "[3,10080]")

    def test_cli_json_and_csv_reconcile_without_graph_fits_or_private_content(self):
        with tempfile.TemporaryDirectory() as home:
            write(home, "a", [metadata(auth="chatgpt"), usage(SEPTEMBER)])
            write(home, "b", [metadata(model="codex-auto-review", auth="api"), usage(SEPTEMBER + timedelta(seconds=1))])
            write(home, "c", [metadata(model="future-model"), usage(SEPTEMBER + timedelta(seconds=2))])
            write(home, "private", [record(SEPTEMBER, {"type": "message", "content": CANARY}, "response_item")])
            jp, cp = Path(home) / "usage.json", Path(home) / "usage.csv"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                 mock.patch.object(finder, "discover_workflow_families", side_effect=AssertionError("usage built a graph")), \
                 mock.patch.object(audit, "analyze_events", side_effect=AssertionError("usage ran quota inference")):
                self.assertEqual(cli.main(["usage", "--home", home, "--month", "2026-09", "--quiet",
                                           "--report-json", str(jp), "--export-usage", str(cp)]), 0)
            report = json.loads(jp.read_text())
            data = report["report"]["extensions"]["usage"]
            with cp.open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(sum(int(r["total_tokens"]) for r in rows), data["summary"]["total_tokens"])
            self.assertAlmostEqual(sum(float(r["priced_subtotal_usd"]) for r in rows), data["summary"]["priced_subtotal_usd"])
            self.assertIn("subtotal", stdout.getvalue())
            for section in ("quota", "guardian", "banked_resets", "workflow"):
                self.assertEqual(report[section]["status"], "not_requested")
            for content in (jp.read_text(), cp.read_text(), stdout.getvalue()):
                self.assertNotIn(CANARY, content)
                self.assertNotIn(str(Path(home).resolve() / "sessions"), content)
            try:
                import jsonschema
            except ImportError:
                self.skipTest("jsonschema is not installed")
            schema = json.loads((ROOT / "schema/cqa-report-v1.schema.json").read_text())
            jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(report)

    def test_cli_model_filter_and_explicit_range_and_no_cache(self):
        with tempfile.TemporaryDirectory() as home:
            write(home, "selected", [metadata(), usage(SEPTEMBER)])
            write(home, "ignored", [metadata(model="gpt-6-astra"), usage(SEPTEMBER + timedelta(seconds=1))])
            jp = Path(home) / "usage.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["usage", "--home", home, "--after", "2026-09-01T00:00:00+02:00",
                                           "--before", "2026-10-01T00:00:00+02:00", "--model", "gpt-6.1-sol",
                                           "--report-json", str(jp), "--no-cache", "--quiet"]), 0)
            data = json.loads(jp.read_text())["report"]["extensions"]["usage"]
            self.assertEqual(data["summary"]["requests"], 1)
            self.assertEqual(data["filters"]["models"], ["gpt-6.1-sol"])
            self.assertFalse((Path(home) / "codex-quota-audit/cache/workflow.sqlite3").exists())

    def test_invalid_override_and_overlapping_output_paths_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            prices = Path(home) / "prices.json"
            for value in (float("nan"), float("inf"), -1):
                prices.write_text(json.dumps({"gpt-6.1-sol": [value, .1, 10]}))
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    usage_cli.main(["--home", home, "--prices", str(prices), "--quiet"])
                self.assertEqual(raised.exception.code, 2)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                usage_cli.main(["--home", home, "--report-json", str(prices), "--export-usage", str(prices)])

    def test_terminal_preserves_tiny_positive_cost_and_incomplete_price_coverage(self):
        with tempfile.TemporaryDirectory() as home:
            write(home, "large", [metadata(), usage(SEPTEMBER, inp=10000000, cached=9000000, output=500000)])
            write(home, "unpriced", [metadata(model="future-model"), usage(SEPTEMBER + timedelta(seconds=1))])
            write(home, "tiny-review", [metadata(model="codex-auto-review"), usage(SEPTEMBER + timedelta(seconds=2))])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                usage_cli.print_result(result(home))
            output = stdout.getvalue()
            self.assertIn("Price coverage: >99.99% of tokens", output)
            self.assertIn("2/3 records priced (1 unpriced)", output)
            self.assertIn("Auto-review portion: <$0.01", output)
            self.assertNotIn("$0.00", output)

    def test_daily_buckets_use_requested_timezone_and_reconcile_partial_prices(self):
        with tempfile.TemporaryDirectory() as home:
            first = SEPTEMBER.replace(hour=21, minute=30)
            second = SEPTEMBER.replace(hour=22, minute=30)
            write(home, "daily", [metadata(first), usage(first), usage(second)])
            write(home, "unknown", [metadata(second, "future-model"), usage(second + timedelta(seconds=1))])
            data = result(home)
            days = data["by_day"]
            self.assertEqual([row["date"] for row in days], ["2026-09-12", "2026-09-13"])
            self.assertEqual([row["requests"] for row in days], [1, 2])
            self.assertEqual(days[1]["pricing_status"], "partial")
            self.assertIsNone(days[1]["api_list_equivalent_usd"])
            for measure in ("requests", "total_tokens", "priced_tokens", "unpriced_tokens", "unpriced_requests"):
                self.assertEqual(sum(row[measure] for row in days), data["summary"][measure])
            self.assertAlmostEqual(sum(row["priced_subtotal_usd"] for row in days), data["summary"]["priced_subtotal_usd"])
            events, stats = load(home)
            utc = analysis.analyze(events, stats, audit.DEFAULT_PRICES, analysis.period("2026-09")[2])
            self.assertEqual([row["date"] for row in utc["by_day"]], ["2026-09-12"])

    def test_daily_buckets_keep_both_dst_hours_on_the_same_calendar_day(self):
        with tempfile.TemporaryDirectory() as home:
            first = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
            write(home, "dst", [metadata(first), usage(first), usage(first + timedelta(hours=1))])
            data = result(home, month="2026-10")
            self.assertEqual(len(data["by_day"]), 1)
            self.assertEqual(data["by_day"][0]["date"], "2026-10-25")
            self.assertEqual(data["by_day"][0]["requests"], 2)

    def test_dashboard_archives_shared_result_without_a_second_scan_or_cache_invalidation(self):
        with tempfile.TemporaryDirectory() as home:
            write(home, "known", [metadata(auth="chatgpt"), usage(SEPTEMBER)])
            write(home, "unpriced", [metadata(model="future-model"), usage(SEPTEMBER + timedelta(seconds=1))])
            original = result(home)
            jp, cp = Path(home) / "chosen.json", Path(home) / "chosen.csv"
            with contextlib.redirect_stdout(io.StringIO()), \
                 mock.patch.object(loader, "load_events", wraps=loader.load_events) as scan, \
                 mock.patch.object(audit, "parse_file", side_effect=AssertionError("warm dashboard reparsed logs")), \
                 mock.patch.object(audit, "analyze_events", side_effect=AssertionError("dashboard ran quota fits")), \
                 mock.patch.object(finder, "discover_workflow_families", side_effect=AssertionError("dashboard reconstructed a graph")), \
                 mock.patch.object(cli, "open_report") as browser:
                self.assertEqual(cli.main(["usage", "--home", home, "--month", "2026-09", "--timezone", "Europe/Berlin",
                                           "--dashboard", "--report-json", str(jp), "--export-usage", str(cp),
                                           "--name", "September work", "--no-open", "--quiet"]), 0)
            scan.assert_called_once()
            browser.assert_not_called()
            entries = reportlib.list_entries(home, report_type_filter="usage")
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            hp = reportlib.root(home) / entry["html"]
            html = hp.read_text()
            embedded = json.loads(re.search(r'<script type="application/json" id="cqa-report">(.*?)</script>', html, re.S).group(1))
            exported = json.loads(jp.read_text())
            self.assertEqual(embedded, exported)
            self.assertEqual(embedded["report"]["kind"], "dashboard")
            data = embedded["report"]["extensions"]["usage"]
            self.assertEqual(data["summary"], original["summary"])
            self.assertEqual(data["by_day"], original["by_day"])
            self.assertEqual(data["coverage"]["cache"]["processed"], 0)
            self.assertEqual((reportlib.root(home) / entry["json"]).read_text(), jp.read_text())
            self.assertEqual((reportlib.latest_dir(home) / "usage.html").read_text(), html)
            self.assertEqual((reportlib.latest_dir(home) / "usage.json").read_text(), jp.read_text())
            self.assertEqual(reportlib.resolve_entry(home, "latest-usage")["id"], entry["id"])
            self.assertEqual(entry["analysis_start"], original["period"]["start_inclusive"])
            self.assertEqual(entry["analysis_end"], original["period"]["end_exclusive"])
            self.assertIn("2026-09", hp.name)
            self.assertNotIn(CANARY, html)
            self.assertNotIn(str(Path(home) / "sessions"), html)
            with cp.open() as stream:
                self.assertEqual(sum(int(row["total_tokens"]) for row in csv.DictReader(stream)), data["summary"]["total_tokens"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.reports_main(["--home", home, "list", "--type", "usage"]), 0)
            self.assertIn("Europe/Berlin", out.getvalue())
            self.assertIn("usage records", out.getvalue())
            self.assertIn("2026-09 · Europe/Berlin", reportlib.index_path(home).read_text())

    def test_custom_dashboard_and_optional_json_bypass_library_and_open_correct_file(self):
        with tempfile.TemporaryDirectory() as home:
            write(home, "sample", [metadata(), usage(SEPTEMBER)])
            hp = Path(home) / "export" / "september.html"
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli, "open_report", return_value=True) as browser:
                self.assertEqual(usage_cli.main(["--home", home, "--month", "2026-09", "--dashboard", str(hp),
                                                "--report-json", "--quiet"]), 0)
            browser.assert_called_once_with(hp)
            self.assertTrue(hp.exists())
            self.assertTrue(hp.with_suffix(".json").exists())
            self.assertEqual(reportlib.list_entries(home), [])

    def test_usage_alias_does_not_replace_existing_quota_latest_and_json_is_opt_in(self):
        with tempfile.TemporaryDirectory() as home:
            write(home, "sample", [metadata(), usage(SEPTEMBER)])
            data = result(home)
            quota = build_usage_only_report_v1(data, generator_version="test")
            quota["quota"]["status"] = "complete"
            source = Path(home) / "quota.html"
            source.write_text("<html>existing quota</html>")
            old = reportlib.register_report(home, source, quota)
            with contextlib.redirect_stdout(io.StringIO()):
                usage_cli.main(["--home", home, "--month", "2026-09", "--dashboard", "--report-json", "--no-open", "--quiet"])
                usage_cli.main(["--home", home, "--month", "2026-09", "--dashboard", "--no-open", "--quiet"])
            self.assertEqual(reportlib.resolve_entry(home, "latest-quota")["id"], old["id"])
            self.assertEqual((reportlib.latest_dir(home) / "quota.html").read_text(), "<html>existing quota</html>")
            self.assertIsNone(reportlib.resolve_entry(home, "latest-usage")["json"])
            self.assertFalse((reportlib.latest_dir(home) / "usage.json").exists())
            self.assertEqual(len(reportlib.list_entries(home, report_type_filter="usage")), 2)

    def test_dashboard_output_collisions_and_inapplicable_flags_fail_before_scanning(self):
        with tempfile.TemporaryDirectory() as home:
            hp = str(Path(home) / "usage.html")
            for extra in (["--dashboard", hp, "--report-json", hp],
                          ["--dashboard", hp, "--export-usage", hp],
                          ["--dashboard", hp, "--report-json", "--export-usage", str(Path(hp).with_suffix(".json"))],
                          ["--report-json"], ["--name", "September"],
                          ["--dashboard", hp, "--name", "September"]):
                with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), \
                     mock.patch.object(loader, "load_events") as scan, self.assertRaises(SystemExit) as error:
                    usage_cli.main(["--home", home, *extra])
                self.assertEqual(error.exception.code, 2)
                scan.assert_not_called()

    def test_empty_dashboard_keeps_equivalent_unavailable_and_html_escapes_embedded_script(self):
        with tempfile.TemporaryDirectory() as home:
            hp, jp = Path(home) / "empty.html", Path(home) / "empty.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(usage_cli.main(["--home", home, "--month", "2026-09", "--dashboard", str(hp),
                                                "--report-json", str(jp), "--no-open", "--quiet"]), 0)
            data = json.loads(jp.read_text())["report"]["extensions"]["usage"]
            self.assertEqual(data["by_day"], [])
            self.assertEqual(data["status"], "not_available")
            self.assertIsNone(data["summary"]["api_list_equivalent_usd"])
            label = 'model</script><img src=x onerror="window.unsafe=true">'
            write(home, "hostile-label", [metadata(model=label), usage(SEPTEMBER)])
            with contextlib.redirect_stdout(io.StringIO()):
                usage_cli.main(["--home", home, "--month", "2026-09", "--dashboard", str(hp), "--no-open", "--quiet"])
            self.assertNotIn(label, hp.read_text())
            self.assertIn(r'model<\/script>', hp.read_text())


if __name__ == "__main__":
    unittest.main()
