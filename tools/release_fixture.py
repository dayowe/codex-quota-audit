#!/usr/bin/env python3
"""Build a deterministic all-sections release fixture for local tests/benchmarks.

This fixture is intentionally synthetic at the report-input boundary. It exercises
quota cohorts, Guardian, banked resets, workflow normalization, privacy filtering,
and the self-contained renderer without depending on a maintainer's live ~/.codex.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cqa.quota import audit
from cqa.report.core import attach_workflow_profile_sources, build_cqa_report_v1

CANARIES = {
    "prompt": "CQA_SECRET_PROMPT_CANARY_9E81F1",
    "tool": "CQA_SECRET_TOOL_OUTPUT_CANARY_7D22A4",
    "path": "/home/private-user/.codex/sessions/CQA_PRIVATE_PATH_CANARY_4C90.jsonl",
    "session": "123e4567-e89b-42d3-a456-426614174999",
}


def _chart_rows():
    with (ROOT / "tests" / "fixtures" / "reference" / "quota_chart_data.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _regimes(rows):
    seen = set(); result = []
    for row in rows:
        key = (row["model"], row["regime"], row["regime_start"], row["regime_end"])
        if key in seen:
            continue
        seen.add(key)
        result.append(SimpleNamespace(
            model=row["model"], index=int(row["regime"][1:]), label=row["regime"],
            start_ts=datetime.fromisoformat(row["regime_start"]),
            end_ts=datetime.fromisoformat(row["regime_end"]),
            detection_basis=row["regime_detection_basis"],
        ))
    return result


def build_release_fixture():
    chart = _chart_rows()
    ns = argparse.Namespace(
        no_guardian_audit=False,
        chart_interval=.80, chart_bootstraps=1000,
        guardian_fit_bootstraps=300,
        banked_reset=["2026-09-05T23:11"],
        banked_capacity_interval=.80, banked_capacity_bootstraps=1000,
        banked_slice_interval=.80, banked_slice_bootstraps=1000,
        banked_slice_points=[14.0], window_minutes=10080,
        chart_model_purity=.95, chart_effort_purity=.95,
    )
    coverage = {
        "log_start": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "log_end": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "files": 12, "records": 2400,
        # Deliberate ignored input: report builders must never surface source paths.
        "source_path": CANARIES["path"],
    }
    period_start = datetime(2026, 9, 5, tzinfo=timezone.utc)
    period_end = datetime(2026, 9, 12, tzinfo=timezone.utc)
    guardian_periods = [{
        "period_start": period_start, "period_end": period_end,
        "period_used_points": 100.0, "approvals": 1, "paired_approvals": 1,
        "guardian_tokens": 1_250_000,
        "estimated_guardian_points": 0.75,
        "estimated_guardian_points_lo": 0.45,
        "estimated_guardian_points_hi": 1.05,
        "estimated_share_of_used_percent": 0.75,
        "estimated_share_of_used_percent_lo": 0.45,
        "estimated_share_of_used_percent_hi": 1.05,
        "guardian_ratecard_model": "gpt-5.6-luna", "guardian_ratecard_usd": 1.2,
        "guardian_ratecard_priced_approvals": 1, "guardian_long_context_events": 0,
        "estimate_status": "supported",
    }]
    approvals = [{
        "start": datetime(2026, 9, 6, tzinfo=timezone.utc),
        "end": datetime(2026, 9, 6, 0, 0, 5, tzinfo=timezone.utc),
        "duration_seconds": 5.0, "pair_confidence": "high", "pair_method": "synthetic",
        "pair_ambiguous": False, "guardian_events": 1, "guardian_tokens": 1_250_000,
        "guardian_cached": 900_000, "guardian_uncached": 300_000,
        "guardian_output": 50_000, "guardian_reasoning_output": 10_000,
        "parent_model": "gpt-6-astra", "parent_effort": "high", "policy_regime": "R1",
        "guardian_ratecard_model": "gpt-5.6-luna", "guardian_ratecard_usd": 1.2,
        "guardian_ratecard_base_usd": 1.2, "guardian_long_context_events": 0,
        "prompt": CANARIES["prompt"], "tool_output": CANARIES["tool"],
        "session_id": CANARIES["session"], "source_path": CANARIES["path"],
    }]
    guardian_summary = {
        "guardian_tokens": 1_250_000, "estimated_points": .75,
        "estimated_points_lo": .45, "estimated_points_hi": 1.05,
    }

    common = dict(model="gpt-6-astra", model_share=.99, effort="high", effort_share=.99,
                  regime="R1", complete_period=True, eligible=True, guardian_tokens=100_000)
    banked_rows = [
        dict(common, period_start=datetime(2026,9,5,tzinfo=timezone.utc), period_end=datetime(2026,9,12,tzinfo=timezone.utc),
             reset_kind="confirmed", confirmed_banked=True, banked_marker="2026-09-05T23:11",
             banked_marker_precision="minute", banked_match_offset_minutes=0.0, quota_points=100.0,
             all_tokens=900_000_000, all_mtokens_per_point=9.0, all_api_usd_per_point=12.0,
             core_tokens=890_000_000, core_mtokens_per_point=8.9, core_api_usd_per_point=11.9),
        dict(common, period_start=datetime(2026,9,12,tzinfo=timezone.utc), period_end=datetime(2026,9,19,tzinfo=timezone.utc),
             reset_kind="comparison", confirmed_banked=False, banked_marker=None,
             banked_marker_precision=None, banked_match_offset_minutes=None, quota_points=100.0,
             all_tokens=850_000_000, all_mtokens_per_point=8.5, all_api_usd_per_point=11.2,
             core_tokens=840_000_000, core_mtokens_per_point=8.4, core_api_usd_per_point=11.1),
    ]
    metrics = {}
    for key,label,ratio in [
        ("all_raw","Raw tokens, all work",1.06), ("all_api","API $eq, all work",1.07),
        ("core_raw","Raw tokens, excluding Guardian",1.06), ("core_api","API $eq, excluding Guardian",1.07),
    ]:
        metrics[key] = {"label": label, "ratio": ratio, "lo": ratio-.08, "hi": ratio+.08, "bootstrap_n": 1000}
    banked_summary = {
        "matched_markers": 1, "matched_strata": [["gpt-6-astra","high","R1"]],
        "matched_banked_periods": 1, "matched_comparison_periods": 1,
        "metrics": metrics, "interpretation": "synthetic release fixture",
    }
    slice_metrics = {}
    for key,label,ratio in [
        ("all_raw","Raw tokens, all work",.98), ("all_api","API $eq, all work",.97),
        ("core_raw","Raw tokens, excluding Guardian",.99), ("core_api","API $eq, excluding Guardian",.98),
    ]:
        slice_metrics[key] = {"label": label, "ratio": ratio, "lo": ratio-.05, "hi": ratio+.05, "bootstrap_n": 1000}
    banked_slice_summary = {
        "preferred_points": 14.0, "strongest_metric": "core_api",
        "interpretation": "synthetic release fixture",
        "slices": {"14.0": {"pairs": 1, "metrics": slice_metrics}},
    }
    banked_slice_rows = [{
        "eligible": True, "reset_time": datetime(2026,9,5,23,11,tzinfo=timezone.utc),
        "banked_marker": "2026-09-05T23:11", "banked_marker_precision": "minute",
        "slice_points": 14.0, "model": "gpt-6-astra", "effort": "high", "regime": "R1",
        "same_model": True, "same_effort": True, "same_regime": True,
        "before_period_start": datetime(2026,9,5,tzinfo=timezone.utc),
        "after_period_start": datetime(2026,9,12,tzinfo=timezone.utc),
        "before_meter_start": 86.0, "before_meter_end": 100.0, "before_interpolated": False,
        "after_meter_start": 0.0, "after_meter_end": 14.0, "after_interpolated": False,
        "before_model_share": .99, "after_model_share": .99,
        "before_effort_share": .99, "after_effort_share": .99,
        "before_points": 14.0, "after_points": 14.0,
        "before_all_tokens": 126_000_000, "before_all_api_usd": 168.0,
        "before_core_tokens": 124_000_000, "before_core_api_usd": 166.0,
        "before_guardian_tokens": 2_000_000,
        "after_all_tokens": 123_000_000, "after_all_api_usd": 163.0,
        "after_core_tokens": 122_000_000, "after_core_api_usd": 162.0,
        "after_guardian_tokens": 1_000_000,
        "ratio_all_raw": .98, "ratio_all_api": .97, "ratio_core_raw": .99, "ratio_core_api": .98,
    }]

    report = build_cqa_report_v1(
        generator_version=audit.__version__, args=ns, coverage=coverage,
        chart_rows=chart, regimes=_regimes(chart), approval_episodes=approvals,
        period_cost_rows=guardian_periods, guardian_summary_data=guardian_summary,
        banked_rows=banked_rows, banked_summary=banked_summary,
        banked_slice_rows=banked_slice_rows, banked_slice_summary=banked_slice_summary,
        report_kind="dashboard",
    )

    workflow = copy.deepcopy(json.loads((ROOT / "tests" / "fixtures" / "reference" / "workflow_cost_profile_5x_pauses_reviewed.json").read_text(encoding="utf-8")))
    # Deliberately poison raw/private fields and unknown fields. The normalized report must drop/remap all of them.
    workflow["family"] = CANARIES["session"]
    workflow["prompt"] = CANARIES["prompt"]
    workflow["tool_output"] = CANARIES["tool"]
    workflow["source_path"] = CANARIES["path"]
    sessions = workflow.get("nested_attribution", {}).get("sessions", [])
    if sessions:
        sessions[0]["session"] = CANARIES["session"]
        sessions[0]["source_path"] = CANARIES["path"]
    return attach_workflow_profile_sources(report, [workflow])


def write_raw_telemetry_fixture(home: Path) -> None:
    """Write a tiny linked root/worker rollout tree understood by both analyzers."""
    from datetime import timedelta
    sessions = Path(home) / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    reset = int((start + timedelta(days=7)).timestamp())
    root_id = "root-session-123456789"
    child_id = "child-session-123456789"

    def rec(ts, kind, payload):
        return {"timestamp": ts.isoformat().replace("+00:00", "Z"), "type": kind, "payload": payload}

    def timed_turn(base, turn_id, inp, cached, out, reasoning, *, ttft_ms=1800, visible_ms=1200):
        """Client-observed timing records matching modern Codex rollout telemetry."""
        epoch_ms = int(base.timestamp() * 1000)
        reasoning_start = epoch_ms + 300
        reasoning_end = epoch_ms + max(400, ttft_ms - 50)
        visible_start = epoch_ms + ttft_ms
        visible_end = visible_start + visible_ms
        return [
            rec(base + timedelta(milliseconds=300), "event_msg", {
                "type": "item_completed", "turn_id": turn_id, "item": {"type": "Reasoning"},
                "started_at_ms": reasoning_start, "completed_at_ms": reasoning_end,
            }),
            rec(base + timedelta(milliseconds=ttft_ms), "event_msg", {
                "type": "item_completed", "turn_id": turn_id, "item": {"type": "AgentMessage"},
                "started_at_ms": visible_start, "completed_at_ms": visible_end,
            }),
            rec(base + timedelta(milliseconds=ttft_ms + visible_ms + 10), "token_usage_record", {
                "turn_id": turn_id, "response_id": f"resp-{turn_id}",
                "usage": {
                    "input_tokens": inp, "cached_input_tokens": cached,
                    "output_tokens": out, "reasoning_output_tokens": reasoning,
                    "total_tokens": inp + out,
                },
                "turn_token_usage": {
                    "input_tokens": inp, "cached_input_tokens": cached,
                    "output_tokens": out, "reasoning_output_tokens": reasoning,
                    "total_tokens": inp + out,
                },
            }),
            rec(base + timedelta(milliseconds=ttft_ms + visible_ms + 20), "event_msg", {
                "type": "task_complete", "turn_id": turn_id,
                "duration_ms": ttft_ms + visible_ms + 20,
                "time_to_first_token_ms": ttft_ms,
            }),
        ]

    root = [
        rec(start, "session_meta", {"session_id": root_id, "source": "cli", "model": "gpt-6-astra"}),
        rec(start, "response_item", {"type": "message", "content": CANARIES["prompt"]}),
        rec(start + timedelta(seconds=.1), "event_msg", {"type": "thread_settings", "model": "gpt-6-astra", "effort": "high", "approval_policy": "on-request", "approvals_reviewer": "auto_review"}),
        rec(start + timedelta(seconds=1), "response_item", {"type": "function_call", "name": "collaboration.spawn_agent", "call_id": "spawn-1", "arguments": json.dumps({"task_name": "worker"})}),
        rec(start + timedelta(seconds=1.1), "response_item", {"type": "function_call_output", "call_id": "spawn-1", "output": json.dumps({"agent_id": child_id})}),
    ]
    total_in = total_cached = total_out = 0
    for i, pct in enumerate([0, 1, 2, 3, 4, 5]):
        inp, cached, out = 1_000_000, 800_000, 10_000
        total_in += inp; total_cached += cached; total_out += out
        if i < 2:
            root.extend(timed_turn(
                start + timedelta(minutes=10+i) - timedelta(seconds=4),
                f"root-turn-{i}-123456789", inp, cached, out, 2_000,
                ttft_ms=1800 + i * 200, visible_ms=1000 + i * 500,
            ))
        root.append(rec(start + timedelta(minutes=10+i), "event_msg", {
            "type": "token_count",
            "info": {
                "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out},
                "total_token_usage": {"input_tokens": total_in, "cached_input_tokens": total_cached, "output_tokens": total_out, "total_tokens": total_in + total_out},
            },
            "rate_limits": {"limit_id": "codex", "primary": {"used_percent": pct, "window_minutes": 10080, "resets_at": reset}},
        }))

    child = [
        rec(start + timedelta(seconds=2), "session_meta", {"session_id": child_id, "source": "subagent", "parent_session_id": root_id, "model": "gpt-6-astra", "role": "worker"}),
        rec(start + timedelta(seconds=2.1), "event_msg", {"type": "thread_settings", "model": "gpt-6-astra", "effort": "high"}),
        rec(start + timedelta(seconds=2.2), "response_item", {"type": "message", "content": CANARIES["tool"]}),
    ]
    total_in = total_cached = total_out = 0
    for i, pct in enumerate([0, 1, 2]):
        inp, cached, out = 500_000, 400_000, 5_000
        total_in += inp; total_cached += cached; total_out += out
        if i < 2:
            child.extend(timed_turn(
                start + timedelta(minutes=12+i) - timedelta(seconds=3),
                f"child-turn-{i}-123456789", inp, cached, out, 1_000,
                ttft_ms=1400 + i * 100, visible_ms=800 + i * 400,
            ))
        child.append(rec(start + timedelta(minutes=12+i), "event_msg", {
            "type": "token_count",
            "info": {
                "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out},
                "total_token_usage": {"input_tokens": total_in, "cached_input_tokens": total_cached, "output_tokens": total_out, "total_tokens": total_in + total_out},
            },
            "rate_limits": {"limit_id": "codex", "primary": {"used_percent": pct, "window_minutes": 10080, "resets_at": reset}},
        }))

    (sessions / "root-canary.jsonl").write_text("\n".join(json.dumps(x) for x in root) + "\n", encoding="utf-8")
    (sessions / "child-canary.jsonl").write_text("\n".join(json.dumps(x) for x in child) + "\n", encoding="utf-8")


if __name__ == "__main__":
    print(json.dumps(build_release_fixture(), indent=2, ensure_ascii=False, allow_nan=False))
