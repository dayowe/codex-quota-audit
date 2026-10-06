#!/usr/bin/env python3
"""Privacy-safe tool-excluded model-response efficiency analysis.

The metric in this module measures response-level non-reasoning model output
against observed task time after subtracting only exactly paired model tool-call
-> tool-result/output wall-clock spans. Reasoning and request/model-side latency
remain in the denominator. It deliberately does not claim server-internal
inference timing.
"""
from __future__ import annotations

import json
import contextlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import candidates as finder
from .records import response_timing_hint


@dataclass
class ToolExcludedTask:
    """Content-free task timing needed for tool-excluded output throughput."""

    turn_id: str
    model: str = "unknown"
    effort: str = "unknown"
    start_ts: Optional[datetime] = None
    end_ts: Optional[datetime] = None
    ttft_seconds: Optional[float] = None
    response_records: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    nonreasoning_output_tokens: int = 0
    visible_responses: int = 0
    exact_visible_responses: int = 0
    mixed_visible_tool_responses: int = 0
    missing_visible_timing_responses: int = 0
    exact_visible_output_tokens: int = 0
    reasoning_items: int = 0
    timed_reasoning_items: int = 0
    invalid_reasoning_items: int = 0
    agent_message_items: int = 0
    timed_agent_message_items: int = 0
    invalid_agent_message_items: int = 0
    reasoning_intervals: List[Tuple[float, float]] = field(default_factory=list)
    agent_message_intervals: List[Tuple[float, float]] = field(default_factory=list)
    tool_calls: Dict[str, float] = field(default_factory=dict)
    tool_results: Dict[str, float] = field(default_factory=dict)


def _interval_union(intervals: Iterable[Tuple[float, float]], *,
                    clip_start: Optional[float] = None,
                    clip_end: Optional[float] = None) -> List[Tuple[float, float]]:
    clipped: List[Tuple[float, float]] = []
    for start, end in intervals:
        if not (math.isfinite(start) and math.isfinite(end)):
            continue
        if clip_start is not None:
            start = max(start, clip_start)
        if clip_end is not None:
            end = min(end, clip_end)
        if end > start:
            clipped.append((start, end))
    clipped.sort()
    merged: List[Tuple[float, float]] = []
    for start, end in clipped:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _interval_seconds(intervals: Iterable[Tuple[float, float]]) -> float:
    return sum(max(0.0, end - start) for start, end in intervals)


def _subtract_intervals(intervals: Iterable[Tuple[float, float]],
                        excluded: Iterable[Tuple[float, float]]) -> List[Tuple[float, float]]:
    base = _interval_union(intervals)
    cuts = _interval_union(excluded)
    if not cuts:
        return base
    out: List[Tuple[float, float]] = []
    for start, end in base:
        cursor = start
        for cut_start, cut_end in cuts:
            if cut_end <= cursor:
                continue
            if cut_start >= end:
                break
            if cut_start > cursor:
                out.append((cursor, min(cut_start, end)))
            cursor = max(cursor, cut_end)
            if cursor >= end:
                break
        if cursor < end:
            out.append((cursor, end))
    return out


def _response_call_type(payload_type: str) -> bool:
    ptype = payload_type.lower()
    return (
        ptype in {"function_call", "custom_tool_call", "tool_call"}
        or (ptype.endswith("_call") and not ptype.endswith(("_call_output", "_call_result")))
    )


def _response_result_type(payload_type: str) -> bool:
    ptype = payload_type.lower()
    return (
        ptype in {"function_call_output", "custom_tool_call_output", "tool_call_output"}
        or ptype.endswith(("_call_output", "_call_result", "_output", "_result"))
    )


def parse_tool_excluded_tasks(path: str, *, activation: Optional[datetime] = None,
                              after: Optional[datetime] = None,
                              before: Optional[datetime] = None, records=None) -> List[ToolExcludedTask]:
    """Extract task/tool response timing without retaining content.

    Model/effort context is allowed to flow through pre-boundary records, but
    task/tool events outside the requested observation window do not contribute.
    Tasks crossing a boundary therefore remain incomplete rather than being
    clipped and guessed.
    """
    tasks: Dict[str, ToolExcludedTask] = {}
    pending: Dict[str, dict] = {}
    call_owner: Dict[str, str] = {}
    active_turn_id: Optional[str] = None
    current_model = "unknown"
    current_effort = "unknown"

    lower = activation
    if after is not None and (lower is None or after > lower):
        lower = after

    def task_for(turn_id: str) -> ToolExcludedTask:
        task = tasks.get(turn_id)
        if task is None:
            task = ToolExcludedTask(turn_id=turn_id, model=current_model, effort=current_effort)
            tasks[turn_id] = task
        if current_model and current_model != "unknown":
            task.model = current_model
        if current_effort and current_effort != "unknown":
            task.effort = current_effort
        return task

    def pending_for(turn_id: str) -> dict:
        return pending.setdefault(turn_id, {
            "agent_message_count": 0,
            "agent_timed_count": 0,
            "has_nonvisible_model_output": False,
        })

    try:
        fh = open(path, "rb") if records is None else contextlib.nullcontext(records)
    except OSError:
        return []

    with fh as stream:
        for raw in stream:
            # Cached records were selected using this same predicate before
            # content was stripped; do not re-filter their sanitized payloads.
            if records is None and not response_timing_hint(raw):
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            ts = finder.parse_ts(obj.get("timestamp"))
            if ts is None:
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            top_type = str(obj.get("type", ""))
            ptype = str(payload.get("type", ""))

            model = finder.model_from_payload(payload)
            if model:
                current_model = model
            effort = finder.effort_from_payload(payload)
            if effort:
                current_effort = effort

            # Keep context from earlier records, but do not create analysis
            # events before activation/window start or after window end.
            if lower is not None and ts < lower:
                continue
            if before is not None and ts > before:
                continue

            raw_turn_id = payload.get("turn_id")
            meta = payload.get("internal_chat_message_metadata_passthrough")
            if not isinstance(raw_turn_id, str) and isinstance(meta, dict):
                raw_turn_id = meta.get("turn_id")
            if isinstance(raw_turn_id, str) and raw_turn_id:
                active_turn_id = raw_turn_id

            if ptype == "task_started" and isinstance(raw_turn_id, str) and raw_turn_id:
                task_for(raw_turn_id).start_ts = ts
                continue

            if top_type == "turn_context" and isinstance(raw_turn_id, str) and raw_turn_id:
                task_for(raw_turn_id)
                continue

            if ptype == "item_completed" and isinstance(raw_turn_id, str) and raw_turn_id:
                task = task_for(raw_turn_id)
                item = payload.get("item")
                if isinstance(item, dict):
                    item_type = str(item.get("type", ""))
                    start_ms = payload.get("started_at_ms")
                    end_ms = payload.get("completed_at_ms")
                    valid_span = (
                        isinstance(start_ms, (int, float))
                        and isinstance(end_ms, (int, float))
                        and end_ms > start_ms
                    )
                    if item_type == "Reasoning":
                        task.reasoning_items += 1
                        if valid_span:
                            task.timed_reasoning_items += 1
                            task.reasoning_intervals.append((float(start_ms) / 1000.0, float(end_ms) / 1000.0))
                        else:
                            task.invalid_reasoning_items += 1
                    elif item_type == "AgentMessage":
                        task.agent_message_items += 1
                        cand = pending_for(raw_turn_id)
                        cand["agent_message_count"] += 1
                        if valid_span:
                            task.timed_agent_message_items += 1
                            cand["agent_timed_count"] += 1
                            task.agent_message_intervals.append((float(start_ms) / 1000.0, float(end_ms) / 1000.0))
                        else:
                            task.invalid_agent_message_items += 1

            if top_type == "response_item":
                rid = raw_turn_id if isinstance(raw_turn_id, str) and raw_turn_id else active_turn_id
                if rid:
                    if _response_call_type(ptype):
                        pending_for(rid)["has_nonvisible_model_output"] = True
                        call_id = payload.get("call_id")
                        if isinstance(call_id, str) and call_id:
                            task_for(rid).tool_calls.setdefault(call_id, ts.timestamp())
                            call_owner[call_id] = rid
                    elif _response_result_type(ptype):
                        call_id = payload.get("call_id")
                        owner = call_owner.get(call_id) if isinstance(call_id, str) else None
                        owner = owner or rid
                        if owner and isinstance(call_id, str) and call_id:
                            task_for(owner).tool_results.setdefault(call_id, ts.timestamp())

            if top_type == "token_usage_record":
                rid = payload.get("turn_id")
                usage = payload.get("usage")
                if isinstance(rid, str) and rid and isinstance(usage, dict):
                    task = task_for(rid)
                    task.response_records += 1
                    out_tokens = max(0, int(usage.get("output_tokens", 0) or 0))
                    reasoning_tokens = max(0, int(usage.get("reasoning_output_tokens", 0) or 0))
                    nonreasoning = max(0, out_tokens - reasoning_tokens)
                    task.output_tokens += out_tokens
                    task.reasoning_tokens += reasoning_tokens
                    task.nonreasoning_output_tokens += nonreasoning
                    cand = pending_for(rid)
                    if cand["agent_message_count"] > 0:
                        task.visible_responses += 1
                        if cand["agent_timed_count"] > 0 and not cand["has_nonvisible_model_output"]:
                            task.exact_visible_responses += 1
                            task.exact_visible_output_tokens += nonreasoning
                        elif cand["has_nonvisible_model_output"]:
                            task.mixed_visible_tool_responses += 1
                        else:
                            task.missing_visible_timing_responses += 1
                    pending[rid] = {
                        "agent_message_count": 0,
                        "agent_timed_count": 0,
                        "has_nonvisible_model_output": False,
                    }

            if ptype == "task_complete" and isinstance(raw_turn_id, str) and raw_turn_id:
                task = task_for(raw_turn_id)
                task.end_ts = ts
                ttft_ms = payload.get("time_to_first_token_ms")
                if isinstance(ttft_ms, (int, float)) and ttft_ms >= 0:
                    task.ttft_seconds = float(ttft_ms) / 1000.0

    return list(tasks.values())


def _percentile(values: Sequence[float], q: float) -> Optional[float]:
    vals = sorted(float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v)))
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = max(0.0, min(1.0, q)) * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def aggregate_tool_excluded(tasks: Iterable[ToolExcludedTask]) -> dict:
    task_list = list(tasks)
    complete_tasks = 0
    qualified_tasks = 0
    output_tokens = 0
    reasoning_tokens = 0
    exact_visible_tokens = 0
    task_elapsed = 0.0
    tool_seconds = 0.0
    tool_excluded = 0.0
    reasoning_seconds = 0.0
    visible_seconds = 0.0
    direct_activity_seconds = 0.0
    response_records = 0
    visible_responses = 0
    exact_visible_responses = 0
    mixed_visible = 0
    missing_visible = 0
    tool_calls = 0
    paired_calls = 0
    unpaired_calls = 0
    unpaired_results = 0
    invalid_tool_pairs = 0
    reasoning_items = 0
    timed_reasoning_items = 0
    invalid_reasoning_items = 0
    agent_items = 0
    timed_agent_items = 0
    invalid_agent_items = 0
    ttft: List[float] = []
    reasons = Counter()

    for task in task_list:
        if task.start_ts is None:
            reasons["missing_task_start"] += 1
            continue
        if task.end_ts is None:
            reasons["missing_task_complete"] += 1
            continue
        start = task.start_ts.timestamp()
        end = task.end_ts.timestamp()
        if end <= start:
            reasons["nonpositive_task_interval"] += 1
            continue
        complete_tasks += 1
        call_ids = set(task.tool_calls)
        result_ids = set(task.tool_results)
        valid_tool_spans: List[Tuple[float, float]] = []
        bad_pairs = 0
        for call_id in call_ids & result_ids:
            a = task.tool_calls[call_id]
            b = task.tool_results[call_id]
            if b > a:
                valid_tool_spans.append((a, b))
            else:
                bad_pairs += 1
        task_unpaired_calls = len(call_ids - result_ids)
        task_unpaired_results = len(result_ids - call_ids)
        task_tool_union = _interval_union(valid_tool_spans, clip_start=start, clip_end=end)
        elapsed = end - start
        excluded = _interval_seconds(task_tool_union)
        active = elapsed - excluded

        tool_calls += len(call_ids)
        paired_calls += len(valid_tool_spans)
        unpaired_calls += task_unpaired_calls
        unpaired_results += task_unpaired_results
        invalid_tool_pairs += bad_pairs

        # Coverage diagnostics describe every complete task, even when that task
        # is later excluded from the headline because a tool span is ambiguous.
        response_records += task.response_records
        visible_responses += task.visible_responses
        exact_visible_responses += task.exact_visible_responses
        mixed_visible += task.mixed_visible_tool_responses
        missing_visible += task.missing_visible_timing_responses
        reasoning_items += task.reasoning_items
        timed_reasoning_items += task.timed_reasoning_items
        invalid_reasoning_items += task.invalid_reasoning_items
        agent_items += task.agent_message_items
        timed_agent_items += task.timed_agent_message_items
        invalid_agent_items += task.invalid_agent_message_items

        if task_unpaired_calls or task_unpaired_results or bad_pairs:
            reasons["incomplete_tool_pairing"] += 1
            continue
        if task.response_records <= 0:
            reasons["missing_response_usage"] += 1
            continue
        if active <= 0:
            reasons["nonpositive_tool_excluded_interval"] += 1
            continue

        qualified_tasks += 1
        output_tokens += task.nonreasoning_output_tokens
        reasoning_tokens += task.reasoning_tokens
        exact_visible_tokens += task.exact_visible_output_tokens
        task_elapsed += elapsed
        tool_seconds += excluded
        tool_excluded += active
        if task.ttft_seconds is not None and task.ttft_seconds >= 0:
            ttft.append(task.ttft_seconds)

        reasoning_active = _subtract_intervals(
            _interval_union(task.reasoning_intervals, clip_start=start, clip_end=end), task_tool_union
        )
        visible_active = _subtract_intervals(
            _interval_union(task.agent_message_intervals, clip_start=start, clip_end=end), task_tool_union
        )
        direct_active = _interval_union(reasoning_active + visible_active)
        reasoning_seconds += _interval_seconds(reasoning_active)
        visible_seconds += _interval_seconds(visible_active)
        direct_activity_seconds += _interval_seconds(direct_active)
        reasons["qualified_task"] += 1

    task_coverage = qualified_tasks / complete_tasks if complete_tasks else None
    pairing_coverage = paired_calls / tool_calls if tool_calls else 1.0
    visible_coverage = exact_visible_responses / visible_responses if visible_responses else None
    reasoning_coverage = timed_reasoning_items / reasoning_items if reasoning_items else None
    agent_coverage = timed_agent_items / agent_items if agent_items else None
    if qualified_tasks == 0:
        evidence_quality = "unavailable"
    elif complete_tasks == qualified_tasks and pairing_coverage == 1.0:
        evidence_quality = "exact"
    else:
        evidence_quality = "partial"
    residual = max(0.0, tool_excluded - direct_activity_seconds)
    reasoning_share = reasoning_seconds / tool_excluded if tool_excluded > 0 else None
    tool_share = tool_seconds / task_elapsed if task_elapsed > 0 else None
    return {
        "tasks_seen": len(task_list),
        "complete_tasks": complete_tasks,
        "qualified_tasks": qualified_tasks,
        "task_coverage": task_coverage,
        "evidence_quality": evidence_quality,
        "nonreasoning_output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        "output_tokens_per_second": output_tokens / tool_excluded if tool_excluded > 0 else None,
        "exact_visible_output_tokens": exact_visible_tokens,
        "exact_visible_output_tokens_per_second": (
            exact_visible_tokens / tool_excluded if tool_excluded > 0 else None
        ),
        "task_elapsed_seconds": task_elapsed,
        "tool_wait_seconds": tool_seconds,
        "tool_excluded_seconds": tool_excluded,
        "timed_reasoning_seconds": reasoning_seconds,
        "timed_visible_generation_seconds": visible_seconds,
        "other_tool_excluded_seconds": residual,
        "reasoning_share": reasoning_share,
        "tool_wait_share": tool_share,
        "response_records": response_records,
        "visible_responses": visible_responses,
        "exact_visible_responses": exact_visible_responses,
        "mixed_visible_tool_responses": mixed_visible,
        "missing_visible_timing_responses": missing_visible,
        "visible_attribution_coverage": visible_coverage,
        "tool_calls": tool_calls,
        "paired_tool_calls": paired_calls,
        "unpaired_tool_calls": unpaired_calls,
        "unpaired_tool_results": unpaired_results,
        "invalid_tool_pairs": invalid_tool_pairs,
        "tool_pairing_coverage": pairing_coverage,
        "reasoning_items": reasoning_items,
        "timed_reasoning_items": timed_reasoning_items,
        "invalid_reasoning_items": invalid_reasoning_items,
        "reasoning_timing_coverage": reasoning_coverage,
        "agent_message_items": agent_items,
        "timed_agent_message_items": timed_agent_items,
        "invalid_agent_message_items": invalid_agent_items,
        "agent_message_timing_coverage": agent_coverage,
        "ttft_samples": len(ttft),
        "ttft_p50_seconds": _percentile(ttft, 0.50),
        "ttft_p90_seconds": _percentile(ttft, 0.90),
        "qualification_reasons": dict(sorted(reasons.items())),
    }


def method_description() -> dict:
    return {
        "status": "production-extension",
        "task_interval": "observed task_started event timestamp -> matching task_complete event timestamp",
        "token_basis": "response-level non-reasoning model output tokens (output_tokens - reasoning_output_tokens); model-generated tool-call tokens remain model output and are included",
        "tool_exclusion": "subtract the union of model-emitted response-item tool call -> matching response-item tool result/output wall-clock spans, paired by call_id",
        "reasoning_semantics": "reasoning time remains in the denominator; positive timed Reasoning and AgentMessage spans are reported as direct diagnostics but are not required to infer the denominator",
        "residual_semantics": "other_tool_excluded_seconds is remaining observed client-side time after directly timed Reasoning/AgentMessage spans; it can include TTFT/request latency, inter-item/model-resume latency, tool-call generation/serialization, and small client overhead and is not server-internal compute time",
        "qualification": "headline throughput includes complete tasks with response-level usage and complete valid tool-call/result pairing; incomplete tool pairing is excluded rather than guessed",
    }


def build_response_efficiency(family, sessions: Mapping[str, object], labels: Mapping[str, str],
                              identities: Mapping[str, Mapping[str, object]], *,
                              after: Optional[datetime] = None,
                              before: Optional[datetime] = None, observations=None) -> dict:
    """Aggregate tool-excluded response efficiency for one workflow family."""
    all_tasks: List[ToolExcludedTask] = []
    by_model: Dict[str, List[ToolExcludedTask]] = defaultdict(list)
    by_model_effort: Dict[Tuple[str, str], List[ToolExcludedTask]] = defaultdict(list)
    by_agent: Dict[str, List[ToolExcludedTask]] = defaultdict(list)

    for key in family.members:
        session = sessions[key]
        activation = identities.get(key, {}).get("activation") if isinstance(identities.get(key), Mapping) else None
        tasks = parse_tool_excluded_tasks(
            session.path, activation=activation if isinstance(activation, datetime) else None,
            after=after, before=before,
            records=((json.dumps(r).encode() for r in observations[key].response_records) if observations is not None else None)
        )
        label = labels[key]
        all_tasks.extend(tasks)
        by_agent[label].extend(tasks)
        for task in tasks:
            model = task.model or "unknown"
            effort = task.effort or "unknown"
            by_model[model].append(task)
            by_model_effort[(model, effort)].append(task)

    return {
        "method": method_description(),
        "overall": aggregate_tool_excluded(all_tasks),
        "by_model": {model: aggregate_tool_excluded(tasks) for model, tasks in sorted(by_model.items())},
        "by_model_effort": [
            {"model": model, "effort": effort, "stats": aggregate_tool_excluded(tasks)}
            for (model, effort), tasks in sorted(by_model_effort.items())
        ],
        "by_agent": {label: aggregate_tool_excluded(tasks) for label, tasks in sorted(by_agent.items())},
    }
