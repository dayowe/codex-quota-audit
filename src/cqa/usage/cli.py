"""Terminal, HTML and export consumers of the shared calendar usage result."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import tempfile
from pathlib import Path

from .. import __version__
from ..quota import audit
from .. import reports as reportlib
from ..report.core import build_usage_only_report_v1, render_dashboard_html, write_cqa_report_json
from . import analysis, loader


def _currency(value):
    return "<$0.01" if 0 < value < .01 else f"${value:,.2f}"


def _dollars(row, *, show_subtotal=True):
    if row["pricing_status"] == "complete":
        return _currency(row["api_list_equivalent_usd"])
    if row["pricing_status"] == "partial":
        return _currency(row["priced_subtotal_usd"]) + (" subtotal" if show_subtotal else "")
    return "unavailable"


def print_result(result):
    period = result["period"]
    summary = result["summary"]
    print(f"Usage: {period['label']} · {period['timezone']}")
    print(f"Range: {period['calendar_start']} ≤ timestamp < {period['calendar_end']}")
    if result["filters"]["models"]:
        print("Models: " + ", ".join(result["filters"]["models"]))
    title = "API equivalent" if summary["pricing_status"] == "complete" else "Priced subtotal"
    print(f"{title}: {_dollars(summary, show_subtotal=False)} · {summary['requests']:,} usage records · {summary['total_tokens']:,} tokens")
    print(f"Tokens: {summary['uncached_input_tokens']:,} uncached input · "
          f"{summary['cached_input_tokens']:,} cached input · {summary['output_tokens']:,} output")
    coverage = summary["price_coverage"]
    if coverage is None:
        print("Price coverage: unavailable")
    else:
        text = f"{coverage:.2%}"
        if coverage < 1 and text == "100.00%":
            text = ">99.99%"
        print(f"Price coverage: {text} of tokens · {summary['priced_requests']:,}/{summary['requests']:,} records priced "
              f"({summary['unpriced_requests']:,} unpriced)")
    print()
    print(f"{'MODEL':<24} {'RECORDS':>9} {'TOKENS':>16}  API EQUIVALENT / SUBTOTAL")
    for row in result["by_model"]:
        print(f"{row['model']:<24} {row['requests']:>9,} {row['total_tokens']:>16,}  {_dollars(row)}")
    print()
    print("Sign-in evidence (payment source is not inferred):")
    for row in result["by_auth_mode"]:
        if row["requests"]:
            print(f"  {row['auth_mode']:<10} {_dollars(row)} · {row['requests']:,} records")
    for row in result["by_activity"]:
        if row["activity"] == "auto_review" and row["requests"]:
            print(f"Auto-review portion: {_dollars(row)} · {row['total_tokens']:,} tokens (included above)")
    cov = result["coverage"]
    print(f"Observed activity: {cov['observed_days']} calendar {'day' if cov['observed_days'] == 1 else 'days'} · "
          f"{cov['without_quota_snapshot_requests']:,} records without quota snapshots")
    print("Coverage: available local logs; absence of records does not establish inactivity.")
    print(f"Pricing: {result['pricing']['basis']} · rate card {result['pricing']['as_of']}" +
          (" · custom overrides applied" if result["pricing"]["custom_price_override"] else ""))
    print("API equivalent measures token work, not a bill or maximum plan capacity.")
    print("Cache-write/tool charges and fast-mode/regional multipliers are unaccounted for.")
    for warning in result["warnings"]:
        print("Coverage note: " + warning["message"])


def export_csv(path, result):
    """Disjoint model/sign-in/activity rows with numeric coverage and provenance."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "start_inclusive": result["period"]["start_inclusive"],
        "end_exclusive": result["period"]["end_exclusive"],
        "timezone": result["period"]["timezone"],
        "pricing_basis": result["pricing"]["basis"],
        "ratecard_as_of": result["pricing"]["as_of"],
        "ratecard_source": result["pricing"]["source"],
        "ratecard_source_ref": result["pricing"]["source_ref"],
        "rates_per_million": json.dumps(result["pricing"]["rates_per_million"], sort_keys=True),
        "custom_price_override": result["pricing"]["custom_price_override"],
    }
    measures = list(analysis.aggregate([], {}))
    fields = list(metadata) + ["model", "auth_mode", "activity"] + measures
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in result["rows"]:
            cooked = dict(metadata, **row)
            cooked["auth_evidence_sources"] = json.dumps(cooked["auth_evidence_sources"], sort_keys=True)
            writer.writerow(cooked)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cqa usage", description="Count calendar usage and token-rate-equivalent work from available local Codex logs.")
    parser.add_argument("--month", metavar="YYYY-MM", help="calendar month (default: current month in --timezone)")
    parser.add_argument("--after", metavar="ISO8601", help="inclusive start with an explicit timezone offset; requires --before")
    parser.add_argument("--before", metavar="ISO8601", help="exclusive end with an explicit timezone offset; requires --after")
    parser.add_argument("--timezone", default="UTC", help="calendar timezone (default: UTC; e.g. Europe/Berlin)")
    parser.add_argument("--model", action="append", default=[], help="exact observed model name; repeat to select multiple models")
    parser.add_argument("--home", default="~/.codex", help="Codex data directory")
    parser.add_argument("--prices", help="local token-price overrides in dollars per million tokens")
    parser.add_argument("--dashboard", nargs="?", const="__DEFAULT__", metavar="PATH",
                        help="build and open HTML; omit PATH to archive it in the report library")
    parser.add_argument("--no-open", action="store_true", help="write HTML without opening a browser")
    parser.add_argument("--name", metavar="LABEL", help="friendly label for an archived usage dashboard")
    parser.add_argument("--report-json", nargs="?", const="__DEFAULT__", metavar="PATH",
                        help="export cqa-report-v1; omit PATH to keep JSON beside the dashboard (requires --dashboard)")
    parser.add_argument("--export-usage", metavar="CSV", help="export disjoint model/sign-in/activity aggregates")
    parser.add_argument("--no-cache", action="store_true", help="bypass cache reads and writes")
    parser.add_argument("--rebuild-cache", action="store_true", help="refresh only usage cache entries for the selected home")
    parser.add_argument("--cache-dir", metavar="DIRECTORY", help="override the local telemetry cache directory")
    parser.add_argument("--quiet", action="store_true", help="hide extraction progress; retain the usage summary")
    args = parser.parse_args(argv)
    if args.report_json == "__DEFAULT__" and not args.dashboard:
        parser.error("--report-json without PATH requires --dashboard")
    if args.name and args.dashboard != "__DEFAULT__":
        parser.error("--name requires an archived dashboard: use --dashboard without PATH")
    home = os.path.expanduser(args.home)
    custom_html = Path(args.dashboard).expanduser() if args.dashboard and args.dashboard != "__DEFAULT__" else None
    json_output = (custom_html.with_suffix(".json") if custom_html and args.report_json == "__DEFAULT__"
                   else Path(args.report_json).expanduser() if args.report_json and args.report_json != "__DEFAULT__" else None)
    csv_output = Path(args.export_usage).expanduser() if args.export_usage else None
    destinations = [p.resolve() for p in (custom_html, json_output, csv_output) if p is not None]
    if len(destinations) != len(set(destinations)):
        parser.error("HTML, JSON and CSV output paths must be different")
    html_output = None
    try:
        _, _, selected_period = analysis.period(args.month, args.after, args.before, args.timezone)
        prices = audit.load_prices(args.prices)
        if any(not math.isfinite(value) or value < 0 for rates in prices.values() for value in rates):
            raise ValueError("Prices must be finite, nonnegative dollars per million tokens")
        from ..cli import _ProgressReporter, open_report
        reporter = None if args.quiet else _ProgressReporter()
        if reporter:
            reporter.header("Codex Quota Audit · usage")
        def progress(name, **meta):
            if not reporter:
                return
            if name == "scan_start":
                reporter.begin("Reading usage telemetry", f"{meta['total']:,} log files")
            else:
                reporter.update(f"{meta['current']:,}/{meta['total']:,} log files · "
                                f"{meta['cached']:,} cached, {meta['processed']:,} processed, "
                                f"{meta['bytes_read'] / 1048576:.1f} MiB read")
        defaults = audit.build_parser().parse_args([])
        events, stats = loader.load_events(
            home, prices, audit.DEFAULT_WINDOW_MINUTES,
            defaults.replay_scan_seconds, defaults.replay_min_events,
            defaults.replay_min_growth_mtokens, defaults.replay_start_max_mtokens,
            defaults.dense_threshold, include_unmetered=True,
            use_cache=not args.no_cache, cache_dir=args.cache_dir, rebuild_cache=args.rebuild_cache,
            progress=progress,
        )
        if reporter:
            reporter.finish()
        result = analysis.analyze(events, stats, prices, selected_period, models=args.model,
                                  custom_prices=bool(args.prices))
        if args.dashboard or args.report_json:
            report = build_usage_only_report_v1(result, generator_version=__version__,
                                                report_kind="dashboard" if args.dashboard else "export")
            if args.dashboard:
                if reporter:
                    reporter.begin("Rendering usage dashboard")
                if custom_html:
                    html_output = Path(render_dashboard_html(report, str(custom_html)))
                else:
                    with tempfile.TemporaryDirectory(prefix="cqa-usage-") as temporary:
                        temp_html = Path(temporary) / "usage.html"
                        temp_json = Path(temporary) / "usage.json"
                        render_dashboard_html(report, str(temp_html))
                        if args.report_json:
                            write_cqa_report_json(str(temp_json), report)
                        entry = reportlib.register_report(home, temp_html, report,
                                                          json_source=temp_json if args.report_json else None,
                                                          friendly_name=args.name)
                        html_output = reportlib.root(home) / entry["html"]
                        if args.report_json == "__DEFAULT__":
                            json_output = reportlib.root(home) / entry["json"]
                if reporter:
                    reporter.complete()
            if json_output:
                write_cqa_report_json(str(json_output), report)
        if args.export_usage:
            export_csv(args.export_usage, result)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.error(str(exc))
    print_result(result)
    if html_output:
        print(f"Usage dashboard: {html_output}")
    if json_output:
        print(f"Report JSON: {json_output}")
    if args.export_usage:
        print(f"Usage CSV: {Path(args.export_usage).expanduser()}")
    if html_output and not args.no_open:
        if open_report(html_output):
            if not args.quiet:
                print("Opened usage dashboard in your browser.")
        elif not args.quiet:
            print("Usage dashboard was generated, but no browser handler accepted the open request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
