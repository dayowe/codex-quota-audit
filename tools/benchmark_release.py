#!/usr/bin/env python3
"""Small repeatable release benchmark for report construction/rendering.

This is not a competitive benchmark. It records a local baseline so regressions in
report size, wall time, or peak Python memory can be noticed before a release.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cqa.report.core import render_dashboard_html, write_cqa_report_json
from release_fixture import build_release_fixture


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--json", dest="json_path", help="write benchmark result JSON")
    args = ap.parse_args()
    if args.repeat < 1:
        ap.error("--repeat must be >= 1")

    samples = []
    for _ in range(args.repeat):
        with tempfile.TemporaryDirectory(prefix="cqa-bench-") as d:
            jp, hp = Path(d)/"report.json", Path(d)/"report.html"
            tracemalloc.start()
            start = time.perf_counter()
            report = build_release_fixture()
            built = time.perf_counter()
            write_cqa_report_json(str(jp), report)
            render_dashboard_html(report, str(hp))
            end = time.perf_counter()
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            samples.append({
                "build_seconds": built-start,
                "total_seconds": end-start,
                "peak_python_bytes": peak,
                "report_json_bytes": jp.stat().st_size,
                "report_html_bytes": hp.stat().st_size,
            })
    def avg(k): return sum(x[k] for x in samples)/len(samples)
    result = {
        "fixture": "all-sections-release-fixture-v1",
        "repeat": args.repeat,
        "average": {k: avg(k) for k in samples[0]},
        "samples": samples,
        "note": "Synthetic report-pipeline baseline; live-history scanning is machine/data dependent.",
    }
    print(json.dumps(result, indent=2))
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
