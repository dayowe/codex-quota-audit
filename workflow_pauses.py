"""Quiet intervals and user-confirmed pauses, separate from primary accounting.

Only timestamps and aggregate usage are needed. Silence is never classified as a
pause automatically. Normal profiling only reads annotations; review writes them.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import sys
import tempfile
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCHEMA = "workflow-pause-analysis-v1"
STORE_SCHEMA = "workflow-pause-annotations-v1"


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError()
        return dt.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Use an ISO timestamp with a timezone, e.g. 2026-09-21T22:56:00+02:00") from None


def interval(start, end):
    start, end = timestamp(start), timestamp(end)
    if end <= start:
        raise ValueError("The interval end must be later than its start")
    return start, end


def default_store():
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    if not base.is_absolute():
        base = Path.home() / ".local/state"
    return base / "codex-quota-audit" / "pauses"


def store_path(directory, root):
    # A stable analysis-root key, never the growing family ID or a display label.
    return Path(directory).expanduser() / (hashlib.sha256(root.encode()).hexdigest() + ".json")


def validate_entries(entries):
    if not isinstance(entries, list):
        raise ValueError("Pause annotations must contain a list of decisions")
    seen = set()
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError("Invalid pause annotation")
        interval(row.get("candidate_start"), row.get("candidate_end"))
        interval(row.get("start"), row.get("end"))
        key = (row["candidate_start"], row["candidate_end"])
        if key in seen or row.get("status") not in {"confirmed", "rejected"}:
            raise ValueError("Duplicate or invalid pause decision")
        if row.get("boundary_source") not in {"inferred", "user-supplied"}:
            raise ValueError("Invalid pause boundary source")
        seen.add(key)
    return entries


def load_annotations(directory, root):
    path = store_path(directory, root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return [], None
    try:
        obj = json.loads(raw)
        if obj.get("schema") != STORE_SCHEMA or obj.get("analysis_root") != root:
            raise ValueError("Pause annotation schema or root does not match")
        entries = validate_entries(obj.get("decisions"))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Cannot use pause annotations at {path}: {exc}") from None
    return entries, hashlib.sha256(raw).hexdigest()


def save_annotations(directory, root, entries, expected_digest):
    """Atomic, private write; concurrent/stale reviews must not overwrite decisions."""
    validate_entries(entries)
    path = store_path(directory, root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = path.with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError(f"Another pause review may be saving. Check {lock} before retrying") from None
    os.close(fd)
    temporary = None
    try:
        _, digest = load_annotations(directory, root)
        if digest != expected_digest:
            raise ValueError("Pause decisions changed during review; rerun review instead of overwriting them")
        obj = {"schema": STORE_SCHEMA, "analysis_root": root, "decisions": entries}
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            json.dump(obj, f, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink()
    return path


def make_evidence(root, start, end, requests):
    """Aggregate simultaneous calls; keep enough precision for offline editing."""
    grouped = defaultdict(lambda: [0, 0])
    for request in requests:
        counts = grouped[request.ts.astimezone(timezone.utc)]
        counts[0] += 1
        counts[1] += request.total_tokens
    return {
        "schema": SCHEMA,
        "analysis_root": root,
        "analysis_start": start.isoformat() if start is not None else None,
        "analysis_end": end.isoformat() if end is not None else None,
        "timeline_columns": ["timestamp", "requests", "raw_tokens"],
        "usage_timeline": [[ts.isoformat(), *counts] for ts, counts in sorted(grouped.items())],
    }


def unpack_evidence(evidence):
    if not isinstance(evidence, dict) or evidence.get("schema") != SCHEMA:
        raise ValueError("Report lacks pause-review data; regenerate it with profiler v6.1+ and --export-json")
    root = evidence.get("analysis_root")
    if not isinstance(root, str) or not root:
        raise ValueError("Pause-review data lacks a stable analysis-root key")
    start, end = timestamp(evidence.get("analysis_start")), timestamp(evidence.get("analysis_end"))
    if end < start:
        raise ValueError("Invalid pause-review analysis window")
    rows = evidence.get("usage_timeline")
    if (not isinstance(rows, list)
            or evidence.get("timeline_columns") != ["timestamp", "requests", "raw_tokens"]):
        raise ValueError("Pause-review data lacks a usage timeline")
    parsed = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("Invalid pause-review usage row")
        ts = timestamp(row[0])
        if (type(row[1]) is not int or row[1] <= 0 or type(row[2]) is not int or row[2] < 0
                or not start <= ts < end or (parsed and ts <= parsed[-1][0])):
            raise ValueError("Invalid, unordered or out-of-window pause-review usage")
        parsed.append((ts, row[1], row[2]))
    return root, start, end, parsed


def union_intervals(intervals, start, end):
    merged = []
    for left, right in sorted((max(a, start), min(b, end)) for a, b in intervals):
        if right <= left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def measure(rows, start, end, excluded=()):
    merged = union_intervals(excluded, start, end)
    times = [r[0] for r in rows]
    excluded_requests = excluded_tokens = 0
    for left, right in merged:
        selected = rows[bisect_left(times, left):bisect_left(times, right)]
        excluded_requests += sum(r[1] for r in selected)
        excluded_tokens += sum(r[2] for r in selected)
    excluded_seconds = sum((b-a).total_seconds() for a, b in merged)
    seconds = (end-start).total_seconds() - excluded_seconds
    requests = sum(r[1] for r in rows) - excluded_requests
    tokens = sum(r[2] for r in rows) - excluded_tokens
    return {
        "seconds": seconds, "requests": requests, "raw_tokens": tokens,
        "raw_million_tokens_per_hour": tokens / seconds * 3600 / 1e6 if seconds > 0 else None,
        "requests_per_hour": requests / seconds * 3600 if seconds > 0 else None,
        "excluded_seconds": excluded_seconds, "excluded_requests": excluded_requests,
        "excluded_raw_tokens": excluded_tokens,
    }


def analyze(evidence, entries, gap_minutes=60):
    if type(gap_minutes) not in {int, float} or not math.isfinite(gap_minutes) or gap_minutes <= 0:
        raise ValueError("--quiet-gap-minutes must be a positive finite number")
    root, start, end, rows = unpack_evidence(evidence)
    validate_entries(entries)
    confirmed = union_intervals([interval(r["start"], r["end"]) for r in entries
                                 if r["status"] == "confirmed"], start, end)
    decisions = {(timestamp(r["candidate_start"]), timestamp(r["candidate_end"])): r for r in entries}
    gaps = []
    for previous, following in zip(rows, rows[1:]):
        if (following[0]-previous[0]).total_seconds() < gap_minutes * 60:
            continue
        # Keep both bounding requests outside the inferred half-open interval.
        left, right = previous[0] + timedelta(microseconds=1), following[0]
        decision = decisions.get((left, right))
        covered = any(a <= left and right <= b for a, b in confirmed)
        gaps.append({"id": f"G{len(gaps)+1}", "start": left.isoformat(), "end": right.isoformat(),
                     "seconds": (right-left).total_seconds(),
                     "status": decision["status"] if decision else "covered" if covered else "unclassified"})
    unknown = [interval(g["start"], g["end"]) for g in gaps if g["status"] == "unclassified"]
    visible = [r for r in entries if timestamp(r["end"]) > start and timestamp(r["start"]) < end]
    return {
        **evidence, "quiet_gap_minutes": gap_minutes, "quiet_intervals": gaps,
        "decisions": visible,
        "elapsed": measure(rows, start, end),
        "excluding_confirmed_pauses": measure(rows, start, end, confirmed),
        "if_unclassified_gaps_were_pauses": measure(rows, start, end, [*confirmed, *unknown]) if unknown else None,
        "notes": [
            "Silence does not prove a pause. Gap boundaries lie between recorded calls and are approximate.",
            "Only gaps bounded by observed requests are suggested; leading/trailing silence is not inferred.",
            "Intervals are [start, end); overlapping pauses are counted once and clipped to the selected window.",
            "Requests are assigned by their recorded timestamp, not measured execution duration.",
            "Unpaused time includes ordinary waits; it is not CPU time or a measure of productive work.",
            "Primary totals, concurrency, compaction and other existing report metrics remain unchanged.",
        ],
    }


def local_time(value):
    return timestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def print_summary(result, report_path=None, directory=None, review_hint=True):
    def rate(view):
        n = view["raw_million_tokens_per_hour"]
        return f"{n:.2f}M" if n is not None else "unavailable (zero remaining duration)"
    raw, adjusted = result["elapsed"], result["excluding_confirmed_pauses"]
    print("\nElapsed time and pauses")
    print("-----------------------")
    print(f"Elapsed: {raw['seconds']/3600:.2f}h; confirmed pauses: {adjusted['excluded_seconds']/3600:.2f}h; "
          f"excluding pauses: {adjusted['seconds']/3600:.2f}h")
    print(f"All observed usage: {raw['raw_tokens']:,} raw tokens; {raw['requests']:,} requests")
    print(f"Raw tokens/elapsed hour:          {rate(raw)}")
    print(f"Raw tokens/hour excluding pauses: {rate(adjusted)}")
    if adjusted["excluded_requests"]:
        print(f"Usage DURING confirmed pauses: {adjusted['excluded_raw_tokens']:,} tokens in "
              f"{adjusted['excluded_requests']:,} requests; retained in overall totals, excluded from adjusted rate.")
    for row in result["decisions"]:
        if row["status"] == "confirmed":
            print(f"Confirmed pause: {local_time(row['start'])} -> {local_time(row['end'])} "
                  f"[{row['boundary_source']} boundaries]")
    for gap in result["quiet_intervals"]:
        print(f"{gap['id']}  {local_time(gap['start'])} -> {local_time(gap['end'])}  "
              f"{gap['seconds']/3600:.2f}h  [{gap['status']}]")
    hypothetical = result["if_unclassified_gaps_were_pauses"]
    if hypothetical:
        print(f"If unclassified gaps were also pauses: {rate(hypothetical)} tokens/hour (hypothesis, not confirmed)")
    if any(r["boundary_source"] == "inferred" and r["status"] == "confirmed" for r in result["decisions"]):
        print("Some confirmed pauses use approximate, call-inferred boundaries.")
    print("Silence can include tests or external waits. Unpaused hours are not active-compute hours.")
    if review_hint and report_path:
        command = ["python3", "profile_workflow_cost.py", "--review-pauses", str(report_path)]
        if directory is not None:
            command += ["--pause-store", str(directory)]
        print("Review/edit saved decisions without rescanning logs: " + shlex.join(command))
    elif review_hint:
        print("Use --export-json REPORT.json to enable offline pause review.")


def review_report(path, directory, output=None):
    """Interactive only when explicitly requested; no access to Codex rollouts."""
    if not sys.stdin.isatty():
        raise ValueError("--review-pauses requires an interactive terminal; ordinary profiling never prompts")
    path = Path(path).expanduser()
    if output and Path(output).expanduser().resolve() == path.resolve():
        raise ValueError("Use a different --export-json path to preserve the original report")
    if output and Path(output).expanduser().exists():
        raise ValueError("Reviewed output already exists; choose a new --export-json path")
    obj = json.loads(path.read_text())
    if not isinstance(obj, dict):
        raise ValueError("Expected a workflow report JSON object")
    evidence = obj.get("pause_analysis")
    root, start, end, rows = unpack_evidence(evidence)
    comparison = obj.get("comparison_metrics")
    if not isinstance(comparison, dict) or sum(r[2] for r in rows) != comparison.get("workflow_raw_tokens"):
        raise ValueError("Saved pause timeline does not reconcile with the report's raw token total")
    entries, digest = load_annotations(directory, root)
    original = list(entries)
    minutes = evidence.get("quiet_gap_minutes", 60)
    result = analyze(evidence, entries, minutes)
    print_summary(result, review_hint=False)
    print("\nReview uses this saved snapshot, not current logs. Times include your local UTC offset.")

    def edited_interval():
        a, b = interval(input("Start (ISO timestamp + timezone): ").strip(),
                        input("End (ISO timestamp + timezone): ").strip())
        if a < start or b > end:
            raise ValueError("Edited boundaries must lie inside this report's analysis window")
        return a.isoformat(), b.isoformat()

    # Include existing decisions even when their candidate no longer appears.
    targets = {(r["candidate_start"], r["candidate_end"]) for r in result["decisions"]}
    targets.update((g["start"], g["end"]) for g in result["quiet_intervals"] if g["status"] != "covered")
    add_more = True
    for index, key in enumerate(sorted(targets), 1):
        old = next((r for r in entries if (r["candidate_start"], r["candidate_end"]) == key), None)
        left, right = (old["start"], old["end"]) if old else key
        print(f"\nInterval {index}: {local_time(left)} -> {local_time(right)}")
        print(f"Current: {old['status'] + ' / ' + old['boundary_source'] if old else 'unclassified'}")
        print("1. Confirm these boundaries  2. Confirm with edited times  3. Ordinary elapsed time")
        print("4. Leave unclassified / remove decision  Enter: keep current decision  q: finish review")
        while True:
            choice = input("Choice: ").strip().lower()
            if choice in {"", "q", "1", "2", "3", "4"}:
                if choice == "2":
                    try:
                        left, right = edited_interval()
                    except ValueError as exc:
                        print(f"Unchanged: {exc}")
                        continue
                break
            print("Choose 1, 2, 3, 4, Enter or q.")
        if choice == "q":
            add_more = False
            break
        if not choice:
            continue
        boundary = "user-supplied" if choice == "2" else old["boundary_source"] if old else "inferred"
        replacement = [r for r in entries if (r["candidate_start"], r["candidate_end"]) != key]
        if choice != "4":
            replacement.append({"candidate_start": key[0], "candidate_end": key[1], "start": left, "end": right,
                                "status": "rejected" if choice == "3" else "confirmed", "boundary_source": boundary,
                                "updated_at": datetime.now(timezone.utc).isoformat()})
        entries = replacement
    while add_more:
        choice = input("\nAdd a pause not suggested above? a: enter times; Enter: finish and save: ").strip().lower()
        if not choice:
            break
        if choice != "a":
            print("Enter a to add a pause, or press Enter to finish.")
            continue
        try:
            left, right = edited_interval()
        except ValueError as exc:
            print(f"Not added: {exc}")
            continue
        entries = [r for r in entries if (r["candidate_start"], r["candidate_end"]) != (left, right)]
        entries.append({"candidate_start": left, "candidate_end": right, "start": left, "end": right,
                        "status": "confirmed", "boundary_source": "user-supplied",
                        "updated_at": datetime.now(timezone.utc).isoformat()})
    if entries != original:
        saved = save_annotations(directory, root, entries, digest)
        print(f"\nSaved decisions to {saved}. Future profiles of this analysis root apply them automatically.")
    else:
        print("\nNo pause decisions changed.")
    obj["pause_analysis"] = analyze(evidence, entries, minutes)
    print_summary(obj["pause_analysis"], path, directory)
    if output:
        # Exclusive creation protects historical reports and accidental collisions.
        with Path(output).expanduser().open("x", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, allow_nan=False)
            f.write("\n")
        print(f"Wrote reviewed report: {output}")
    print("Original report and Codex logs unchanged.")
    return 0
