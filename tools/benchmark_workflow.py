#!/usr/bin/env python3
"""Reproducible cold/warm workflow-index benchmark using temporary synthetic logs."""
import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from cqa.workflow import candidates as finder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=256)
    parser.add_argument("--payload-kib", type=int, default=64)
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=1, metavar="N")
    args = parser.parse_args()
    if args.files < 1 or args.payload_kib < 0:
        parser.error("--files must be positive and --payload-kib must be nonnegative")
    with tempfile.TemporaryDirectory(prefix="cqa-workflow-benchmark-") as scratch:
        home = Path(scratch)
        directory = home / "sessions"
        directory.mkdir()
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        for n in range(args.files):
            timestamp = (now + timedelta(seconds=n)).isoformat()
            payload = {"session_id": f"benchmark-session-{n:08d}", "source": "cli", "model": "gpt-6-astra"}
            rows = [
                {"timestamp": timestamp, "type": "session_meta", "payload": payload},
                {"timestamp": timestamp, "type": "event_msg", "payload": {"type": "user_message", "message": "x" * args.payload_kib * 1024}},
                {"timestamp": timestamp, "type": "event_msg", "payload": {"type": "token_count", "info": {
                    "last_token_usage": {"input_tokens": 1000, "output_tokens": 100},
                    "total_token_usage": {"input_tokens": 1000, "output_tokens": 100}}}},
            ]
            (directory / f"{n:08d}.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        measurements = {}
        for label, options in (("uncached", {"use_cache": False}), ("cold", {}), ("warm", {})):
            start = time.perf_counter()
            discovery = finder.discover_workflow_families(str(home), workers=args.workers, **options)
            measurements[label] = {**discovery.stats, "seconds": round(time.perf_counter() - start, 4)}
        print(json.dumps({"files": args.files, "payload_kib": args.payload_kib,
                          "workers": args.workers, "measurements": measurements}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
