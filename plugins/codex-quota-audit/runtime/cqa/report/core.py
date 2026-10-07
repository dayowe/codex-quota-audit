#!/usr/bin/env python3
"""Privacy-safe cqa-report-v1 builder and self-contained HTML renderer.

This module intentionally contains no quota statistics. It only translates
already-computed analyzer results into the stable cqa-report-v1 presentation
contract and renders that contract into a dashboard.
"""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Optional, Sequence
from .. import auto_review_policy as review_policy


SCHEMA_NAME = "cqa-report"
SCHEMA_VERSION = "1.0.0"
PRIVACY_PROFILE = "dashboard-safe-v1"
DEFAULT_DASHBOARD_PATH = "~/.codex/codex-quota-audit/report.html"
DASHBOARD_PLACEHOLDER = "__CQA_REPORT_JSON__"


BANKED_METRICS = (
    ("all_raw", "Raw tokens, all work"),
    ("all_api", "API $eq, all work"),
    ("core_raw", "Raw tokens, excluding Guardian"),
    ("core_api", "API $eq, excluding Guardian"),
)


def _finite(value: object) -> Optional[float]:
    try:
        x = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _number(value: object, default: float = 0.0) -> float:
    x = _finite(value)
    return default if x is None else x


def _integer(value: object, default: int = 0) -> int:
    x = _finite(value)
    return default if x is None else int(round(x))


def _boolean(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes"}


def _iso_utc(value: object) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if dt.tzinfo is None:
        # Analyzer timestamps are normally timezone-aware. Preserve an unexpected
        # naive timestamp rather than inventing the user's local timezone.
        return dt.isoformat()
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")




def _parse_iso_datetime(value: object) -> Optional[datetime]:
    text = _iso_utc(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _workflow_concurrency_windows(agent_windows: Sequence[Mapping[str, object]], profile_index: int) -> list[Dict[str, object]]:
    """Convert trusted lifecycle windows into display-safe 2+ child overlap intervals.

    This is a deterministic normalization of already-authoritative lifecycle windows;
    it does not infer new agent lifetimes or workflow attribution.
    """
    events: list[tuple[datetime, int, str, str]] = []
    for row in agent_windows:
        start = _parse_iso_datetime(row.get("start"))
        end = _parse_iso_datetime(row.get("end"))
        agent_id = str(row.get("agent_id") or "")
        role = str(row.get("role") or "unknown")
        if start is None or end is None or end <= start or not agent_id:
            continue
        # End events sort before start events at the same instant to avoid a
        # zero-duration overlap when one child hands off exactly to another.
        events.append((start, 1, agent_id, role))
        events.append((end, -1, agent_id, role))
    events.sort(key=lambda x: (x[0], x[1]))
    active: dict[str, str] = {}
    out: list[Dict[str, object]] = []
    prev: Optional[datetime] = None
    idx = 0
    for when, delta, agent_id, role in events:
        if prev is not None and when > prev and len(active) >= 2:
            idx += 1
            out.append({
                "id": f"concurrency-{profile_index:03d}-{idx:03d}",
                "start": _iso_utc(prev),
                "end": _iso_utc(when),
                "active_children": len(active),
                "agent_ids": sorted(active),
                "roles": sorted(set(active.values())),
            })
        if delta < 0:
            active.pop(agent_id, None)
        else:
            active[agent_id] = role
        prev = when
    return out

def _interval(method: str, level: Optional[float], low: object, high: object,
              resamples: object = None) -> Optional[Dict[str, object]]:
    lo, hi = _finite(low), _finite(high)
    if lo is None or hi is None:
        return None
    out: Dict[str, object] = {
        "method": method,
        "level": level,
        "low": lo,
        "high": hi,
    }
    n = _finite(resamples)
    if n is not None:
        out["resamples"] = int(round(n))
    return out


def _estimate(value: object, interval: Optional[Dict[str, object]] = None) -> Optional[Dict[str, object]]:
    x = _finite(value)
    if x is None:
        return None
    return {"estimate": x, "interval": interval}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"


def _regime_key(model: object, label: object, start: object, end: object) -> tuple[str, str, Optional[str], Optional[str]]:
    return str(model or "unknown"), str(label or "unknown"), _iso_utc(start), _iso_utc(end)


def _banked_work(row: Mapping[str, object], prefix: str) -> Dict[str, object]:
    points = _number(row.get("quota_points"))
    tokens = _integer(row.get(f"{prefix}_tokens"))
    return {
        "tokens": max(0, tokens),
        "tokens_per_quota_point": (tokens / points if points > 0 else None),
        "api_list_equivalent_usd": _finite(row.get(f"{prefix}_api_usd")),
        "api_list_equivalent_usd_per_quota_point": _finite(row.get(f"{prefix}_api_usd_per_point")),
        "price_coverage": _finite(row.get(f"{prefix}_price_coverage")),
    }


def _boundary_side(row: Mapping[str, object], prefix: str) -> Dict[str, object]:
    start = _iso_utc(row.get(f"{prefix}_period_start"))
    if start is None:
        raise ValueError(f"eligible banked boundary row is missing {prefix}_period_start")
    return {
        "period_start": start,
        "meter_start": _number(row.get(f"{prefix}_meter_start")),
        "meter_end": _number(row.get(f"{prefix}_meter_end")),
        "interpolated": _boolean(row.get(f"{prefix}_interpolated")),
        "quota_points": max(0.0, _number(row.get(f"{prefix}_points"))),
        "all_tokens": max(0, _integer(row.get(f"{prefix}_all_tokens"))),
        "core_tokens": max(0, _integer(row.get(f"{prefix}_core_tokens"))),
        "guardian_tokens": max(0, _integer(row.get(f"{prefix}_guardian_tokens"))),
        "all_api_list_equivalent_usd": _finite(row.get(f"{prefix}_all_api_usd")),
        "core_api_list_equivalent_usd": _finite(row.get(f"{prefix}_core_api_usd")),
        "model_token_share": min(1.0, max(0.0, _number(row.get(f"{prefix}_model_share")))),
        "effort_token_share": min(1.0, max(0.0, _number(row.get(f"{prefix}_effort_share")))),
    }


def build_cqa_report_v1(*, generator_version: str, args: Any,
                        coverage: Mapping[str, object],
                        chart_rows: Sequence[Mapping[str, object]],
                        regimes: Sequence[Any],
                        approval_episodes: Sequence[Mapping[str, object]],
                        period_cost_rows: Sequence[Mapping[str, object]],
                        guardian_summary_data: Mapping[str, object],
                        banked_rows: Sequence[Mapping[str, object]],
                        banked_summary: Mapping[str, object],
                        banked_slice_rows: Sequence[Mapping[str, object]],
                        banked_slice_summary: Mapping[str, object],
                        report_kind: str = "export") -> Dict[str, object]:
    """Translate already-computed analyzer results into cqa-report-v1.

    This function must remain presentation-only: it may normalize units,
    timestamps and references, but must not redo quota inference, regression,
    bootstrapping, regime detection or workflow attribution.
    """
    # ----- policy regimes -----
    regime_records: list[Dict[str, object]] = []
    regime_ids: dict[tuple[str, str, Optional[str], Optional[str]], str] = {}
    sorted_regimes = sorted(
        regimes,
        key=lambda rg: (str(getattr(rg, "model", "")), getattr(rg, "start_ts", datetime.min.replace(tzinfo=timezone.utc)),
                        int(getattr(rg, "index", 0))),
    )
    for i, rg in enumerate(sorted_regimes, 1):
        model = str(getattr(rg, "model", "unknown"))
        label = str(getattr(rg, "label", f"R{getattr(rg, 'index', i)}"))
        start = _iso_utc(getattr(rg, "start_ts", None))
        end = _iso_utc(getattr(rg, "end_ts", None))
        if start is None:
            continue
        rid = f"regime-{i:03d}"
        key = (model, label, start, end)
        regime_ids[key] = rid
        regime_records.append({
            "id": rid,
            "label": label,
            "model": model,
            "start": start,
            "end": end,
            "detection": {
                "method": f"codex-quota-audit-v{generator_version}",
                "basis": str(getattr(rg, "detection_basis", "unknown")),
            },
            "extensions": {},
        })

    # ----- quota cohorts -----
    cohorts: list[Dict[str, object]] = []
    for i, row in enumerate(chart_rows, 1):
        key = _regime_key(row.get("model"), row.get("regime"), row.get("regime_start"), row.get("regime_end"))
        rid = regime_ids.get(key)
        if rid is None:
            # Chart aggregation can intentionally use different purity settings
            # from the history detector. A row-derived regime keeps the cohort
            # reference valid without changing either analysis.
            rid = f"regime-{len(regime_records) + 1:03d}"
            regime_ids[key] = rid
            start = key[2] or _iso_utc(coverage.get("log_start"))
            if start is None:
                raise ValueError("quota cohort is missing both regime_start and coverage.log_start")
            regime_records.append({
                "id": rid,
                "label": key[1],
                "model": key[0],
                "start": start,
                "end": key[3],
                "detection": {
                    "method": f"codex-quota-audit-v{generator_version}",
                    "basis": str(row.get("regime_detection_basis") or "unknown"),
                },
                "extensions": {"source": "chart_cohort"},
            })

        raw_interval = _interval(
            "whole_episode_bootstrap_percentile",
            _finite(getattr(args, "chart_interval", 0.80)),
            _number(row.get("tokens_p10_m")) * 1_000_000 if _finite(row.get("tokens_p10_m")) is not None else None,
            _number(row.get("tokens_p90_m")) * 1_000_000 if _finite(row.get("tokens_p90_m")) is not None else None,
            getattr(args, "chart_bootstraps", None),
        )
        raw = _estimate(
            _number(row.get("tokens_m_per_point")) * 1_000_000,
            raw_interval,
        )
        if raw is None:
            continue
        api_value = _finite(row.get("api_usd_per_point"))
        api = None
        if api_value is not None:
            api = _estimate(
                api_value,
                _interval(
                    "whole_episode_bootstrap_percentile",
                    _finite(getattr(args, "chart_interval", 0.80)),
                    row.get("api_p10"), row.get("api_p90"),
                    getattr(args, "chart_bootstraps", None),
                ),
            )
        cohorts.append({
            "id": f"cohort-{i:03d}",
            "model": str(row.get("model") or "unknown"),
            "effort": str(row.get("effort") or "unknown"),
            "regime_id": rid,
            "evidence": {
                "quota_points": max(0.0, _number(row.get("quota_points"))),
                "episodes": max(0, _integer(row.get("episodes"))),
                "buckets": max(0, _integer(row.get("buckets"))),
                "model_token_share": min(1.0, max(0.0, _number(row.get("model_token_share")))),
                "effort_token_share": min(1.0, max(0.0, _number(row.get("effort_token_share")))),
            },
            "efficiency": {
                "raw_tokens_per_quota_point": raw,
                "api_equivalent_usd_per_quota_point": api,
            },
            "token_composition_per_quota_point": {
                "cached_input": max(0.0, _number(row.get("cached_m_per_point")) * 1_000_000),
                "uncached_input": max(0.0, _number(row.get("uncached_m_per_point")) * 1_000_000),
                "output": max(0.0, _number(row.get("output_m_per_point")) * 1_000_000),
            },
            "extensions": {
                "api_evidence": {
                    "quota_points": max(0.0, _number(row.get("api_quota_points"))),
                    "episodes": max(0, _integer(row.get("api_episodes"))),
                }
            },
        })

    # ----- Guardian periods and approval episodes -----
    guardian_requested = not bool(getattr(args, "no_guardian_audit", False))
    periods: list[Dict[str, object]] = []
    period_bounds: list[tuple[datetime, Optional[datetime], str]] = []
    for i, row in enumerate(sorted(period_cost_rows, key=lambda r: r.get("period_start") or datetime.min.replace(tzinfo=timezone.utc)), 1):
        pid = f"guardian-period-{i:03d}"
        start_obj = row.get("period_start")
        end_obj = row.get("period_end")
        start_iso = _iso_utc(start_obj)
        if start_iso is None:
            continue
        if isinstance(start_obj, datetime):
            start_dt = start_obj
        else:
            start_dt = datetime.fromisoformat(str(start_obj).replace("Z", "+00:00"))
        if isinstance(end_obj, datetime):
            end_dt: Optional[datetime] = end_obj
        elif end_obj:
            end_dt = datetime.fromisoformat(str(end_obj).replace("Z", "+00:00"))
        else:
            end_dt = None
        period_bounds.append((start_dt, end_dt, pid))
        est = _estimate(
            row.get("estimated_guardian_points"),
            _interval(
                "guardian_parent_session_bootstrap_coefficient",
                0.80,
                row.get("estimated_guardian_points_lo"),
                row.get("estimated_guardian_points_hi"),
                getattr(args, "guardian_fit_bootstraps", None),
            ),
        )
        share = _estimate(
            (_number(row.get("estimated_share_of_used_percent")) / 100.0
             if _finite(row.get("estimated_share_of_used_percent")) is not None else None),
            _interval(
                "guardian_parent_session_bootstrap_coefficient",
                0.80,
                (_number(row.get("estimated_share_of_used_percent_lo")) / 100.0
                 if _finite(row.get("estimated_share_of_used_percent_lo")) is not None else None),
                (_number(row.get("estimated_share_of_used_percent_hi")) / 100.0
                 if _finite(row.get("estimated_share_of_used_percent_hi")) is not None else None),
                getattr(args, "guardian_fit_bootstraps", None),
            ),
        )
        rate_usd = _finite(row.get("guardian_ratecard_usd"))
        periods.append({
            "id": pid,
            "start": start_iso,
            "end": _iso_utc(end_obj),
            "quota_points_used": max(0.0, _number(row.get("period_used_points"))),
            "approvals": max(0, _integer(row.get("approvals"))),
            "paired_approvals": max(0, _integer(row.get("paired_approvals"))),
            "guardian_tokens": max(0, _integer(row.get("guardian_tokens"))),
            "estimated_quota_overhead": est,
            "estimated_share_of_used_quota": share,
            "ratecard": {
                "model": str(row.get("guardian_ratecard_model") or "gpt-5.4"),
                "api_list_equivalent_usd": max(0.0, rate_usd or 0.0),
                "priced_approvals": max(0, _integer(row.get("guardian_ratecard_priced_approvals"))),
                "long_context_events": max(0, _integer(row.get("guardian_long_context_events"))),
            },
            "approval_episode_ids": [],
            "status": str(row.get("estimate_status") or ("supported" if est else "not-identifiable")),
            "extensions": {"auto_review_quota_policy": review_policy.normalize(row.get("quota_policy"))}
                if row.get("quota_policy") else {},
        })

    period_by_id = {p[2]: next(x for x in periods if x["id"] == p[2]) for p in period_bounds}
    guardian_episodes: list[Dict[str, object]] = []
    unmapped_approvals = 0
    for row in sorted(approval_episodes, key=lambda r: r.get("start") or datetime.min.replace(tzinfo=timezone.utc)):
        start_obj = row.get("start")
        if not isinstance(start_obj, datetime):
            try:
                start_obj = datetime.fromisoformat(str(start_obj).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                unmapped_approvals += 1
                continue
        pid = None
        for lo, hi, candidate in period_bounds:
            if start_obj >= lo and (hi is None or start_obj < hi):
                pid = candidate
                break
        if pid is None:
            unmapped_approvals += 1
            continue
        aid = f"approval-{len(guardian_episodes) + 1:04d}"
        period_by_id[pid]["approval_episode_ids"].append(aid)
        episode = {
            "id": aid,
            "period_id": pid,
            "start": _iso_utc(start_obj),
            "end": _iso_utc(row.get("end")),
            "duration_seconds": max(0.0, _number(row.get("duration_seconds"))),
            "pairing": {
                "confidence": str(row.get("pair_confidence") or "unknown"),
                "method": str(row.get("pair_method") or "unknown"),
                "ambiguous": _boolean(row.get("pair_ambiguous")),
            },
            "guardian_events": max(0, _integer(row.get("guardian_events"))),
            "guardian_tokens": max(0, _integer(row.get("guardian_tokens"))),
            "guardian_token_breakdown": {
                "cached_input": max(0, _integer(row.get("guardian_cached"))),
                "uncached_input": max(0, _integer(row.get("guardian_uncached"))),
                "output": max(0, _integer(row.get("guardian_output"))),
                "reasoning_output": max(0, _integer(row.get("guardian_reasoning_output"))),
            },
            "parent": {
                "model": str(row.get("parent_model")) if row.get("parent_model") else None,
                "effort": str(row.get("parent_effort")) if row.get("parent_effort") else None,
                "policy_regime": str(row.get("policy_regime")) if row.get("policy_regime") else None,
            },
            "ratecard": {
                "model": str(row.get("guardian_ratecard_model") or "gpt-5.4"),
                "api_list_equivalent_usd": max(0.0, _number(row.get("guardian_ratecard_usd"))),
                "base_usd": max(0.0, _number(row.get("guardian_ratecard_base_usd"))),
                "long_context_events": max(0, _integer(row.get("guardian_long_context_events"))),
            },
            "extensions": {"auto_review_quota_policy": review_policy.normalize(row.get("quota_policy"))}
                if row.get("quota_policy") else {},
        }
        guardian_episodes.append(episode)

    if not guardian_requested:
        guardian_status = "not_requested"
        guardian_summary_out = None
        periods = []
        guardian_episodes = []
    else:
        guardian_status = "complete"
        if guardian_episodes and any(p.get("estimated_quota_overhead") is None and
                                    (p.get("extensions", {}).get("auto_review_quota_policy") or {}).get("status") != "free"
                                    for p in periods):
            guardian_status = "partial"
        gs_est = _estimate(
            guardian_summary_data.get("estimated_points"),
            _interval(
                "guardian_parent_session_bootstrap_coefficient",
                0.80,
                guardian_summary_data.get("estimated_points_lo"),
                guardian_summary_data.get("estimated_points_hi"),
                getattr(args, "guardian_fit_bootstraps", None),
            ),
        )
        summary_policy = review_policy.normalize(guardian_summary_data.get("quota_policy"))
        if summary_policy and summary_policy["status"] != "historical":
            gs_est = None
        guardian_summary_out = {
            "approval_episodes": len(guardian_episodes),
            "inference_events": sum(int(x["guardian_events"]) for x in guardian_episodes),
            "guardian_tokens": max(0, _integer(guardian_summary_data.get("guardian_tokens"),
                                                sum(int(x["guardian_tokens"]) for x in guardian_episodes))),
            "estimated_quota_overhead": gs_est,
        }

    # ----- banked-reset comparison -----
    supplied_markers = len(getattr(args, "banked_reset", []) or [])
    matched_markers = max(0, _integer(banked_summary.get("matched_markers")))
    banked_status = "not_requested" if supplied_markers == 0 else "complete"
    if supplied_markers and not banked_summary.get("matched_strata"):
        banked_status = "insufficient_data"
    elif supplied_markers and matched_markers < supplied_markers:
        banked_status = "partial"

    comparisons: list[Dict[str, object]] = []
    for i, (metric, fallback_label) in enumerate(BANKED_METRICS, 1):
        m = (banked_summary.get("metrics") or {}).get(metric, {})
        ratio = _finite(m.get("ratio"))
        if ratio is None:
            continue
        comparisons.append({
            "id": f"banked-comparison-{i:03d}",
            "metric": metric,
            "label": str(m.get("label") or fallback_label),
            "capacity_ratio": {
                "estimate": ratio,
                "interval": _interval(
                    "whole_reset_period_bootstrap_percentile",
                    _finite(getattr(args, "banked_capacity_interval", 0.80)),
                    m.get("lo"), m.get("hi"), m.get("bootstrap_n"),
                ),
            },
            "confirmed_banked_periods": max(0, _integer(banked_summary.get("matched_banked_periods"))),
            "comparison_periods": max(0, _integer(banked_summary.get("matched_comparison_periods"))),
            "extensions": {},
        })

    # Evidence rows are the same matched strata used by the whole-period estimator.
    eligible_rows = [r for r in banked_rows if _boolean(r.get("eligible"))]
    banked_strata = {(str(r.get("model")), str(r.get("effort")), str(r.get("regime")))
                     for r in eligible_rows if _boolean(r.get("confirmed_banked"))}
    comparison_strata = {(str(r.get("model")), str(r.get("effort")), str(r.get("regime")))
                         for r in eligible_rows if not _boolean(r.get("confirmed_banked"))}
    matched_strata = banked_strata & comparison_strata
    matched_rows = [r for r in eligible_rows
                    if (str(r.get("model")), str(r.get("effort")), str(r.get("regime"))) in matched_strata]
    matched_rows.sort(key=lambda r: r.get("period_start") or datetime.min.replace(tzinfo=timezone.utc))
    capacity_periods: list[Dict[str, object]] = []
    for i, row in enumerate(matched_rows, 1):
        marker = None
        if row.get("banked_marker"):
            marker = {
                "supplied": str(row.get("banked_marker")),
                "precision": str(row.get("banked_marker_precision") or "unknown"),
                "match_offset_minutes": _finite(row.get("banked_match_offset_minutes")),
            }
        start = _iso_utc(row.get("period_start"))
        if start is None:
            continue
        capacity_periods.append({
            "id": f"banked-period-{i:03d}",
            "start": start,
            "end": _iso_utc(row.get("period_end")),
            "reset_kind": str(row.get("reset_kind") or "unknown"),
            "confirmed_banked": _boolean(row.get("confirmed_banked")),
            "marker": marker,
            "quota_points": max(0.0, _number(row.get("quota_points"))),
            "model": str(row.get("model") or "unknown"),
            "model_token_share": min(1.0, max(0.0, _number(row.get("model_share")))),
            "effort": str(row.get("effort") or "unknown"),
            "effort_token_share": min(1.0, max(0.0, _number(row.get("effort_share")))),
            "regime": str(row.get("regime") or "unknown"),
            "complete_period": _boolean(row.get("complete_period")),
            "all_work": _banked_work(row, "all"),
            "core_work": _banked_work(row, "core"),
            "guardian_tokens": max(0, _integer(row.get("guardian_tokens"))),
            "extensions": {},
        })

    strongest_metric = str(banked_slice_summary.get("strongest_metric") or banked_summary.get("strongest_metric") or "core_api")
    if strongest_metric not in {x[0] for x in BANKED_METRICS}:
        strongest_metric = "core_api"
    slices: list[Dict[str, object]] = []
    for points, sr in sorted((banked_slice_summary.get("slices") or {}).items(), key=lambda kv: float(kv[0])):
        p = float(points)
        metrics: Dict[str, object] = {}
        for metric, fallback_label in BANKED_METRICS:
            m = (sr.get("metrics") or {}).get(metric, {})
            ratio = _finite(m.get("ratio"))
            metrics[metric] = {
                "label": str(m.get("label") or fallback_label),
                "after_before_ratio": (
                    {
                        "estimate": ratio,
                        "interval": _interval(
                            "whole_reset_pair_bootstrap_percentile",
                            _finite(getattr(args, "banked_slice_interval", 0.80)),
                            m.get("lo"), m.get("hi"), m.get("bootstrap_n"),
                        ),
                    }
                    if ratio is not None else None
                ),
            }
        slices.append({
            "id": f"banked-slice-{int(round(p)):03d}",
            "quota_points": p,
            "pairs": max(0, _integer(sr.get("pairs"))),
            "preferred_metric": strongest_metric,
            "metrics": metrics,
            "extensions": {},
        })

    boundary_observations: list[Dict[str, object]] = []
    eligible_slice_rows = [r for r in banked_slice_rows if _boolean(r.get("eligible"))]
    eligible_slice_rows.sort(key=lambda r: (r.get("reset_time") or datetime.min.replace(tzinfo=timezone.utc),
                                            _number(r.get("slice_points"))))
    for i, row in enumerate(eligible_slice_rows, 1):
        reset_time = _iso_utc(row.get("reset_time"))
        if reset_time is None or not row.get("banked_marker"):
            continue
        boundary_observations.append({
            "id": f"banked-boundary-{i:03d}",
            "reset_time": reset_time,
            "marker": {
                "supplied": str(row.get("banked_marker")),
                "precision": str(row.get("banked_marker_precision") or "unknown"),
            },
            "slice_points": max(0.0, _number(row.get("slice_points"))),
            "model": str(row.get("model") or "unknown"),
            "effort": str(row.get("effort") or "unknown"),
            "regime": str(row.get("regime") or "unknown"),
            "controls": {
                "same_model": _boolean(row.get("same_model")),
                "same_effort": _boolean(row.get("same_effort")),
                "same_regime": _boolean(row.get("same_regime")),
            },
            "before": _boundary_side(row, "before"),
            "after": _boundary_side(row, "after"),
            "ratios": {
                "all_raw": _finite(row.get("ratio_all_raw")),
                "all_api": _finite(row.get("ratio_all_api")),
                "core_raw": _finite(row.get("ratio_core_raw")),
                "core_api": _finite(row.get("ratio_core_api")),
            },
            "extensions": {},
        })

    banked_summary_out = None
    if supplied_markers:
        banked_summary_out = {
            "confirmed_resets": matched_markers,
            "matched_comparison_periods": max(0, _integer(banked_summary.get("matched_comparison_periods"))),
            "preferred_boundary_points": _finite(banked_slice_summary.get("preferred_points")),
            "strongest_metric": (str(banked_slice_summary.get("strongest_metric"))
                                 if banked_slice_summary.get("strongest_metric") else
                                 (str(banked_summary.get("strongest_metric")) if banked_summary.get("strongest_metric") else None)),
        }

    # ----- deterministic findings -----
    findings: list[Dict[str, object]] = []
    warnings: list[Dict[str, object]] = []
    by_model: dict[str, int] = {}
    for rg in regime_records:
        model = str(rg["model"])
        by_model[model] = by_model.get(model, 0) + 1
    changed_models = sorted(m for m, n in by_model.items() if n > 1)
    visible_regime_labels = {next((str(r["label"]) for r in regime_records if r["id"] == c["regime_id"]), "") for c in cohorts}
    if changed_models or len(visible_regime_labels) > 1:
        refs = [str(r["id"]) for r in regime_records]
        findings.append({
            "id": f"finding-{len(findings)+1:03d}",
            "code": "CROSS_REGIME_COMPARISON",
            "category": "quota",
            "level": "caution",
            "title": "Visible cohorts span detected policy regimes",
            "summary": "Model/effort efficiency differences can reflect quota-policy changes as well as model or reasoning-effort differences.",
            "evidence_refs": refs,
            "extensions": {"models_with_detected_changes": changed_models},
        })
        warnings.append({
            "code": "CROSS_REGIME_COMPARISON",
            "level": "caution",
            "message": "Visible quota cohorts span model-specific detected policy regimes; do not treat the chart as a controlled model leaderboard.",
            "refs": refs,
        })

    supported_periods = [p for p in periods if p.get("estimated_quota_overhead")]
    if supported_periods:
        worst = max(supported_periods, key=lambda p: float(p["estimated_quota_overhead"]["estimate"]))
        findings.append({
            "id": f"finding-{len(findings)+1:03d}",
            "code": "GUARDIAN_LONG_TAIL_PERIOD",
            "category": "guardian",
            "level": "info",
            "title": "Guardian overhead varies by reset period",
            "summary": f"The highest observed supported period is estimated at {float(worst['estimated_quota_overhead']['estimate']):.2f} quota points.",
            "evidence_refs": [str(worst["id"])],
            "extensions": {},
        })

    usable_slices = []
    for s in slices:
        metric = s["metrics"].get(strongest_metric, {})
        est = metric.get("after_before_ratio") if isinstance(metric, dict) else None
        if isinstance(est, dict) and _finite(est.get("estimate")) is not None:
            usable_slices.append(s)
    if len(usable_slices) >= 2:
        first, last = usable_slices[0], usable_slices[-1]
        a = first["metrics"][strongest_metric]["after_before_ratio"]["estimate"]
        b = last["metrics"][strongest_metric]["after_before_ratio"]["estimate"]
        findings.append({
            "id": f"finding-{len(findings)+1:03d}",
            "code": "BANKED_BOUNDARY_SLICE_PATTERN",
            "category": "banked_reset",
            "level": "info",
            "title": "Banked-reset boundary ratios vary with slice size",
            "summary": f"The observed {strongest_metric} after/before ratio is {float(a):.2f}x at {first['quota_points']:g} points and {float(b):.2f}x at {last['quota_points']:g} points.",
            "evidence_refs": [str(first["id"]), str(last["id"])],
            "extensions": {},
        })

    priced_cohorts = sum(c["efficiency"]["api_equivalent_usd_per_quota_point"] is not None for c in cohorts)
    if priced_cohorts < len(cohorts):
        warnings.append({
            "code": "API_EQ_PARTIAL_COHORT_COVERAGE",
            "level": "info",
            "message": "Some quota cohorts do not have enough price coverage for an API-list-price-equivalent estimate.",
            "refs": [str(c["id"]) for c in cohorts if c["efficiency"]["api_equivalent_usd_per_quota_point"] is None],
        })
    if unmapped_approvals:
        warnings.append({
            "code": "GUARDIAN_APPROVALS_OUTSIDE_PERIODS",
            "level": "caution",
            "message": f"{unmapped_approvals} Guardian approval episodes could not be mapped to a reconstructed reset period and are omitted from report drill-downs.",
            "refs": [],
        })
    excluded_buckets = max(0, _integer(getattr(args, "auto_review_excluded_buckets", 0)))
    if excluded_buckets:
        warnings.append({
            "code": "AUTO_REVIEW_POLICY_FIT_EXCLUSIONS",
            "level": "info",
            "message": f"Quota-value fits exclude {excluded_buckets} buckets containing free or unresolved Auto-review; observed work and meter readings are retained.",
            "refs": [],
        })
    if supplied_markers and matched_markers < supplied_markers:
        warnings.append({
            "code": "BANKED_MARKERS_PARTIALLY_MATCHED",
            "level": "caution",
            "message": f"Matched {matched_markers} of {supplied_markers} user-supplied banked-reset markers.",
            "refs": [],
        })

    pairing_den = sum(int(p["approvals"]) for p in periods)
    pairing_num = sum(int(p["paired_approvals"]) for p in periods)
    pairing_coverage = pairing_num / pairing_den if pairing_den else None
    quality = "good" if not any(w["level"] == "caution" for w in warnings) else "caution"

    observed_start = _iso_utc(coverage.get("log_start"))
    observed_end = _iso_utc(coverage.get("log_end"))
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    report: Dict[str, object] = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "generator": {
            "name": "codex-quota-audit",
            "version": generator_version,
            "components": [],
        },
        "report": {
            "generated_at": generated_at,
            "kind": report_kind if report_kind in {"dashboard", "export"} else "export",
            "observed_range": {"start": observed_start, "end": observed_end},
            "extensions": {
                "target_window_minutes": int(getattr(args, "window_minutes", 0)),
                "sessions_scanned": max(0, _integer(coverage.get("files"))),
                "records_scanned": max(0, _integer(coverage.get("records"))),
            },
        },
        "privacy": {
            "profile": PRIVACY_PROFILE,
            "contains_prompt_text": False,
            "contains_response_text": False,
            "contains_tool_output": False,
            "contains_file_contents": False,
            "contains_auth_data": False,
            "contains_account_identity": False,
            "contains_source_paths": False,
            "contains_raw_session_ids": False,
        },
        "analysis": {
            "profile": "dashboard-v1",
            "methods": {
                "quota_analysis": f"codex-quota-audit-v{generator_version}",
                "quota_efficiency_interval": "whole_episode_bootstrap_percentile",
                "guardian_estimation": "guardian_parent_session_bootstrap_coefficient",
                "banked_reset_capacity": "same_model_effort_regime_matched_periods",
                "banked_reset_boundary": "equal_quota_before_after_slices",
            },
            "parameters": {
                "quota_chart_interval": getattr(args, "chart_interval", None),
                "quota_chart_bootstraps": getattr(args, "chart_bootstraps", None),
                "banked_capacity_interval": getattr(args, "banked_capacity_interval", None),
                "banked_capacity_bootstraps": getattr(args, "banked_capacity_bootstraps", None),
                "banked_boundary_interval": getattr(args, "banked_slice_interval", None),
                "banked_boundary_bootstraps": getattr(args, "banked_slice_bootstraps", None),
                "banked_boundary_points": [float(x) for x in (getattr(args, "banked_slice_points", None) or [])],
                "chart_model_purity": getattr(args, "chart_model_purity", None),
                "chart_effort_purity": getattr(args, "chart_effort_purity", None),
            },
            "extensions": {},
        },
        "quota": {
            "status": "complete" if cohorts else "insufficient_data",
            "summary": {
                "models": len({str(c["model"]) for c in cohorts}),
                "cohorts": len(cohorts),
                "quota_points_observed": sum(float(c["evidence"]["quota_points"]) for c in cohorts),
                "episodes": sum(int(c["evidence"]["episodes"]) for c in cohorts),
            },
            "regimes": regime_records,
            "cohorts": cohorts,
            "extensions": {"auto_review_policy_excluded_buckets": max(0, _integer(getattr(args, "auto_review_excluded_buckets", 0))),
                           "auto_review_policy_exclusion_reason": "Buckets with free, transitional, unknown or API Auto-review are excluded from quota-value fits; work and meter observations are retained."},
        },
        "guardian": {
            "status": guardian_status,
            "summary": guardian_summary_out,
            "periods": periods,
            "approval_episodes": guardian_episodes,
            "extensions": {"auto_review_quota_policy": review_policy.normalize(guardian_summary_data.get("quota_policy"))}
                if guardian_requested and guardian_summary_data.get("quota_policy") else {},
        },
        "banked_resets": {
            "status": banked_status,
            "summary": banked_summary_out,
            "whole_period_comparisons": comparisons,
            "boundary_slices": slices,
            "capacity_periods": capacity_periods,
            "boundary_observations": boundary_observations,
            "extensions": {
                "whole_period_interpretation": str(banked_summary.get("interpretation") or "not tested"),
                "boundary_interpretation": str(banked_slice_summary.get("interpretation") or "not tested"),
            },
        },
        "workflow": {
            "status": "not_requested",
            "profiles": [],
            "extensions": {},
        },
        "findings": findings,
        "data_quality": {
            "overall": quality,
            "metrics": {
                "quota_cohorts_total": len(cohorts),
                "quota_cohorts_priced": priced_cohorts,
                "guardian_pairing_coverage": pairing_coverage,
                "workflow_role_attribution_sessions": None,
                "workflow_sessions": None,
                "workflow_lifetime_windows_status": None,
            },
            "warnings": warnings,
            "extensions": {},
        },
    }
    return report



def build_usage_only_report_v1(usage_result: Mapping[str, object], *, generator_version: str,
                               report_kind: str = "export") -> Dict[str, object]:
    """Package an already-computed calendar usage result as an additive v1 export.

    No quota fits or workflow analyses run. Older renderers safely ignore the
    versioned usage extension; HTML, terminal and CSV consumers use the same values.
    """
    coverage = usage_result["coverage"]
    history = coverage["history"]
    report = build_cqa_report_v1(
        generator_version=generator_version,
        args=SimpleNamespace(no_guardian_audit=True),
        coverage={"files": history["files_checked"], "records": usage_result["summary"]["requests"],
                  "log_start": coverage["first_observed"], "log_end": coverage["last_observed"]},
        chart_rows=[], regimes=[], approval_episodes=[], period_cost_rows=[],
        guardian_summary_data={}, banked_rows=[], banked_summary={},
        banked_slice_rows=[], banked_slice_summary={},
    )
    report["report"]["extensions"] = {"usage": copy.deepcopy(dict(usage_result))}
    report["report"]["kind"] = report_kind
    report["analysis"]["methods"] = {
        "usage": "calendar_token_rate_equivalent_v1",
        "duplicates": "global_usage_identity_and_immediate_cumulative_duplicates",
        "replay": "evidence_based_full_file_prefix_before_calendar_selection",
    }
    report["analysis"]["parameters"] = {
        "usage_period": copy.deepcopy(usage_result["period"]),
        "usage_filters": copy.deepcopy(usage_result["filters"]),
    }
    report["quota"]["status"] = "not_requested"
    report["quota"]["summary"] = None
    report["quota"]["extensions"] = {}
    report["data_quality"]["warnings"] = copy.deepcopy(usage_result["warnings"])
    report["data_quality"]["overall"] = ("unknown" if usage_result["status"] == "not_available"
                                          else "caution" if usage_result["warnings"] else "good")
    return report


def _semantic_workflow_role(value: object) -> str:
    """Return the responsibility label without topology encoded into it.

    Historical workflow-profiler exports used ``/root`` as a suffix on the
    workflow root's role for some aggregate buckets.  cqa-report-v1 already
    carries topology separately through ``parent_id`` and timeline agent IDs,
    so presentation roles must stay semantic and workflow-agnostic.
    """
    role = str(value or "unknown").strip() or "unknown"
    return role[:-5] if role.endswith("/root") else role


def _merge_workflow_usage(rows: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    normalized = [_workflow_usage(row) for row in rows]
    total_tokens = sum(row["total_tokens"] for row in normalized)
    coverage_weight = 0.0
    coverage_known = True
    for row in normalized:
        cov = row.get("price_coverage")
        if row["total_tokens"] and cov is None:
            coverage_known = False
            break
        if cov is not None:
            coverage_weight += float(cov) * row["total_tokens"]
    api_values = [row.get("api_list_equivalent_usd") for row in normalized]
    api_list_equivalent_usd = (
        sum(float(value) for value in api_values if value is not None)
        if any(value is not None for value in api_values)
        else None
    )
    return {
        "requests": sum(row["requests"] for row in normalized),
        "input_tokens": sum(row["input_tokens"] for row in normalized),
        "cached_input_tokens": sum(row["cached_input_tokens"] for row in normalized),
        "uncached_input_tokens": sum(row["uncached_input_tokens"] for row in normalized),
        "output_tokens": sum(row["output_tokens"] for row in normalized),
        "reasoning_output_tokens": sum(row["reasoning_output_tokens"] for row in normalized),
        "total_tokens": total_tokens,
        "api_list_equivalent_usd": api_list_equivalent_usd,
        "price_coverage": (coverage_weight / total_tokens if total_tokens and coverage_known else None),
    }


def _merge_workflow_throughput(rows: Sequence[Mapping[str, object]]) -> Dict[str, object] | None:
    stats = [_workflow_throughput(row) for row in rows]
    stats = [row for row in stats if row is not None]
    if not stats:
        return None
    turns = sum(row["turns"] for row in stats)
    transitions = sum(row["observed_turn_transitions"] for row in stats)
    seconds = sum(row["observed_turn_interval_seconds"] for row in stats)
    raw_tokens = sum(row["raw_tokens"] for row in stats)
    output_tokens = sum(row["output_tokens"] for row in stats)
    observed_raw_tokens = sum(row["observed_raw_tokens"] for row in stats)
    workflow_turn_rates = [row.get("turns_per_workflow_second") for row in stats]
    workflow_output_rates = [row.get("output_tokens_per_workflow_second") for row in stats]
    return {
        "turns": turns,
        "contributors": sum(row["contributors"] for row in stats),
        "observed_turn_transitions": transitions,
        "observed_turn_interval_seconds": seconds,
        "turns_per_observed_second": (transitions / seconds if seconds > 0 else None),
        "turns_per_workflow_second": (sum(float(v) for v in workflow_turn_rates) if all(v is not None for v in workflow_turn_rates) else None),
        "raw_tokens": raw_tokens,
        "output_tokens": output_tokens,
        "tokens_per_turn": (raw_tokens / turns if turns else None),
        "output_tokens_per_workflow_second": (sum(float(v) for v in workflow_output_rates) if all(v is not None for v in workflow_output_rates) else None),
        "observed_raw_tokens": observed_raw_tokens,
        "raw_tokens_per_observed_second": (observed_raw_tokens / seconds if seconds > 0 else None),
    }


def _merge_workflow_performance(rows: Sequence[Mapping[str, object]]) -> Dict[str, object] | None:
    stats = [_workflow_performance(row) for row in rows]
    stats = [row for row in stats if row is not None]
    if not stats:
        return None
    turns = sum(row["turns"] for row in stats)
    task_turns = sum(row["task_turns"] for row in stats)
    qualified = sum(row["qualified_visible_responses"] for row in stats)
    visible = sum(row["visible_responses"] for row in stats)
    visible_timed = sum(row["visible_timed_responses"] for row in stats)
    visible_tokens = sum(row["visible_output_tokens"] for row in stats)
    generation_seconds = sum(row["visible_generation_seconds"] for row in stats)
    reasoning_rates = [row.get("reasoning_tokens_per_turn") for row in stats]
    reasoning_known = all(rate is not None or row["turns"] == 0 for row, rate in zip(stats, reasoning_rates))
    reasoning_total = (
        sum(float(rate) * row["turns"] for row, rate in zip(stats, reasoning_rates) if rate is not None)
        if reasoning_known
        else None
    )
    ttft_samples = sum(row["ttft_samples"] for row in stats)
    duration_samples = sum(row["turn_duration_samples"] for row in stats)
    visible_cov = qualified / visible if visible else None
    if qualified < 5 or (visible_cov is not None and visible_cov < 0.10):
        quality = "insufficient"
    elif qualified < 20 or (visible_cov is not None and visible_cov < 0.50):
        quality = "low"
    else:
        quality = "good"
    reasons: dict[str, int] = {}
    for row in stats:
        for key, value in row.get("qualification_reasons", {}).items():
            reasons[str(key)] = reasons.get(str(key), 0) + int(value)
    # Percentiles cannot be reconstructed exactly from already-aggregated role
    # summaries. Preserve them only when no role buckets had to be merged.
    single = len(stats) == 1
    return {
        "turns": turns,
        "task_turns": task_turns,
        "contributors": sum(row["contributors"] for row in stats),
        "generation_timed_turns": qualified,
        "qualified_visible_responses": qualified,
        "visible_responses": visible,
        "visible_timed_responses": visible_timed,
        "generation_coverage": (qualified / turns if turns else None),
        "visible_generation_coverage": visible_cov,
        "visible_timing_coverage": (visible_timed / visible if visible else None),
        "generation_quality": quality,
        "visible_output_tokens": visible_tokens,
        "visible_generation_seconds": generation_seconds,
        "visible_output_tokens_per_second": (visible_tokens / generation_seconds if qualified and generation_seconds > 0 else None),
        "visible_output_tokens_per_turn": (visible_tokens / qualified if qualified else None),
        "reasoning_tokens_per_turn": (reasoning_total / turns if turns and reasoning_total is not None else None),
        "reasoning_timed_turns": sum(row["reasoning_timed_turns"] for row in stats),
        "reasoning_seconds": sum(row["reasoning_seconds"] for row in stats),
        "reasoning_tokens_per_second": None,
        "ttft_samples": ttft_samples,
        "ttft_coverage": (ttft_samples / task_turns if task_turns else None),
        "ttft_p50_seconds": stats[0].get("ttft_p50_seconds") if single else None,
        "ttft_p90_seconds": stats[0].get("ttft_p90_seconds") if single else None,
        "turn_duration_samples": duration_samples,
        "turn_duration_coverage": (duration_samples / task_turns if task_turns else None),
        "turn_duration_p50_seconds": stats[0].get("turn_duration_p50_seconds") if single else None,
        "turn_duration_p90_seconds": stats[0].get("turn_duration_p90_seconds") if single else None,
        "qualification_reasons": dict(sorted(reasons.items())),
    }


def _workflow_usage(value: Mapping[str, object] | None) -> Dict[str, object]:
    """Normalize one already-computed workflow usage record for cqa-report-v1."""
    src = value or {}
    input_tokens = max(0, _integer(src.get("input_tokens")))
    output_tokens = max(0, _integer(src.get("output_tokens")))
    total = _integer(src.get("total_tokens"), input_tokens + output_tokens)
    if total <= 0 and (input_tokens or output_tokens):
        total = input_tokens + output_tokens
    coverage = _finite(src.get("price_coverage"))
    if coverage is not None:
        coverage = min(1.0, max(0.0, coverage))
    api_eq = _finite(src.get("api_list_equivalent_usd"))
    # Historical workflow profiles may encode an unpriced non-empty usage bucket
    # as 0.0 USD with zero price coverage. In cqa-report-v1, unavailable is null;
    # zero must remain a real measured value, never a stand-in for missing price data.
    if total > 0 and coverage is not None and coverage <= 0:
        api_eq = None
    return {
        "requests": max(0, _integer(src.get("requests"))),
        "input_tokens": input_tokens,
        "cached_input_tokens": max(0, _integer(src.get("cached_input_tokens"))),
        "uncached_input_tokens": max(0, _integer(src.get("uncached_input_tokens"))),
        "output_tokens": output_tokens,
        "reasoning_output_tokens": max(0, _integer(src.get("reasoning_output_tokens"))),
        "total_tokens": max(0, total),
        "api_list_equivalent_usd": api_eq,
        "price_coverage": coverage,
    }


def _workflow_throughput(value: Mapping[str, object] | None) -> Dict[str, object] | None:
    """Normalize optional observed workflow turn-cadence metrics."""
    if not isinstance(value, Mapping):
        return None
    turns = max(0, _integer(value.get("turns")))
    contributors = max(0, _integer(value.get("contributors")))
    transitions = max(0, _integer(value.get("observed_turn_transitions")))
    seconds = max(0.0, _number(value.get("observed_turn_interval_seconds")))
    raw_tokens = max(0, _integer(value.get("raw_tokens")))
    output_tokens = max(0, _integer(value.get("output_tokens")))
    observed_raw_tokens = max(0, _integer(value.get("observed_raw_tokens")))
    observed_rate = _finite(value.get("turns_per_observed_second"))
    workflow_rate = _finite(value.get("turns_per_workflow_second"))
    tokens_per_turn = _finite(value.get("tokens_per_turn"))
    work_rate = _finite(value.get("raw_tokens_per_observed_second"))
    workflow_output_rate = _finite(value.get("output_tokens_per_workflow_second"))
    return {
        "turns": turns,
        "contributors": contributors,
        "observed_turn_transitions": transitions,
        "observed_turn_interval_seconds": seconds,
        "turns_per_observed_second": max(0.0, observed_rate) if observed_rate is not None else None,
        "turns_per_workflow_second": max(0.0, workflow_rate) if workflow_rate is not None else None,
        "raw_tokens": raw_tokens,
        "output_tokens": output_tokens,
        "tokens_per_turn": max(0.0, tokens_per_turn) if tokens_per_turn is not None else None,
        "output_tokens_per_workflow_second": max(0.0, workflow_output_rate) if workflow_output_rate is not None else None,
        "observed_raw_tokens": observed_raw_tokens,
        "raw_tokens_per_observed_second": max(0.0, work_rate) if work_rate is not None else None,
    }


def _workflow_performance(value: Mapping[str, object] | None) -> Dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    def nn(key):
        x = _finite(value.get(key))
        return max(0.0, x) if x is not None else None
    turns = max(0, _integer(value.get("turns")))
    generation_timed = max(0, _integer(value.get("generation_timed_turns")))
    legacy_generation_coverage = nn("generation_coverage")
    # Older v1 workflow profiles predate explicit visible-response denominators.
    # Fall back to the old all-response semantics only when the new fields are
    # absent, so legacy reports remain browseable without inventing new evidence.
    visible_responses = max(0, _integer(value.get("visible_responses", turns)))
    visible_timed = max(0, _integer(value.get("visible_timed_responses", generation_timed)))
    visible_generation_coverage = nn("visible_generation_coverage")
    if visible_generation_coverage is None:
        visible_generation_coverage = legacy_generation_coverage
    visible_timing_coverage = nn("visible_timing_coverage")
    if visible_timing_coverage is None:
        visible_timing_coverage = legacy_generation_coverage
    return {
        "turns": turns,
        "task_turns": max(0, _integer(value.get("task_turns"))),
        "contributors": max(0, _integer(value.get("contributors"))),
        "generation_timed_turns": generation_timed,
        "qualified_visible_responses": max(0, _integer(value.get("qualified_visible_responses", generation_timed))),
        "visible_responses": visible_responses,
        "visible_timed_responses": visible_timed,
        "generation_coverage": legacy_generation_coverage,
        "visible_generation_coverage": visible_generation_coverage,
        "visible_timing_coverage": visible_timing_coverage,
        "generation_quality": (str(value.get("generation_quality")) if value.get("generation_quality") in {"good", "low", "insufficient"} else None),
        "visible_output_tokens": max(0, _integer(value.get("visible_output_tokens"))),
        "visible_generation_seconds": max(0.0, _number(value.get("visible_generation_seconds"))),
        "visible_output_tokens_per_second": nn("visible_output_tokens_per_second"),
        "visible_output_tokens_per_turn": nn("visible_output_tokens_per_turn"),
        "reasoning_tokens_per_turn": nn("reasoning_tokens_per_turn"),
        "reasoning_timed_turns": max(0, _integer(value.get("reasoning_timed_turns"))),
        "reasoning_seconds": max(0.0, _number(value.get("reasoning_seconds"))),
        "reasoning_tokens_per_second": nn("reasoning_tokens_per_second"),
        "ttft_samples": max(0, _integer(value.get("ttft_samples"))),
        "ttft_coverage": nn("ttft_coverage"),
        "ttft_p50_seconds": nn("ttft_p50_seconds"),
        "ttft_p90_seconds": nn("ttft_p90_seconds"),
        "turn_duration_samples": max(0, _integer(value.get("turn_duration_samples"))),
        "turn_duration_coverage": nn("turn_duration_coverage"),
        "turn_duration_p50_seconds": nn("turn_duration_p50_seconds"),
        "turn_duration_p90_seconds": nn("turn_duration_p90_seconds"),
        "qualification_reasons": {str(k): max(0, _integer(v)) for k, v in (value.get("qualification_reasons") or {}).items()} if isinstance(value.get("qualification_reasons"), Mapping) else {},
    }



def _workflow_response_efficiency_stats(value: Mapping[str, object] | None) -> Dict[str, object] | None:
    """Normalize optional tool-excluded response-efficiency evidence for extensions.

    cqa-report-v1.0 stays frozen: these fields live only under existing free-form
    ``extensions`` objects and are ignored safely by older renderers.
    """
    if not isinstance(value, Mapping):
        return None

    def nn(key):
        x = _finite(value.get(key))
        return max(0.0, x) if x is not None else None

    quality = str(value.get("evidence_quality") or "unavailable")
    if quality not in {"exact", "partial", "unavailable"}:
        quality = "unavailable"
    return {
        "tasks_seen": max(0, _integer(value.get("tasks_seen"))),
        "complete_tasks": max(0, _integer(value.get("complete_tasks"))),
        "qualified_tasks": max(0, _integer(value.get("qualified_tasks"))),
        "task_coverage": nn("task_coverage"),
        "evidence_quality": quality,
        "nonreasoning_output_tokens": max(0, _integer(value.get("nonreasoning_output_tokens"))),
        "output_tokens_per_second": nn("output_tokens_per_second"),
        "task_elapsed_seconds": max(0.0, _number(value.get("task_elapsed_seconds"))),
        "tool_wait_seconds": max(0.0, _number(value.get("tool_wait_seconds"))),
        "tool_excluded_seconds": max(0.0, _number(value.get("tool_excluded_seconds"))),
        "timed_reasoning_seconds": max(0.0, _number(value.get("timed_reasoning_seconds"))),
        "timed_visible_generation_seconds": max(0.0, _number(value.get("timed_visible_generation_seconds"))),
        "other_tool_excluded_seconds": max(0.0, _number(value.get("other_tool_excluded_seconds"))),
        "reasoning_share": nn("reasoning_share"),
        "tool_wait_share": nn("tool_wait_share"),
        "tool_pairing_coverage": nn("tool_pairing_coverage"),
        "reasoning_timing_coverage": nn("reasoning_timing_coverage"),
        "agent_message_timing_coverage": nn("agent_message_timing_coverage"),
        "ttft_samples": max(0, _integer(value.get("ttft_samples"))),
        "ttft_p50_seconds": nn("ttft_p50_seconds"),
        "ttft_p90_seconds": nn("ttft_p90_seconds"),
        "qualification_reasons": {
            str(k): max(0, _integer(v))
            for k, v in (value.get("qualification_reasons") or {}).items()
        } if isinstance(value.get("qualification_reasons"), Mapping) else {},
    }


def _workflow_response_efficiency_method(value: Mapping[str, object] | None) -> Dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    keys = (
        "status", "task_interval", "token_basis", "tool_exclusion",
        "reasoning_semantics", "residual_semantics", "qualification",
    )
    return {key: str(value.get(key)) for key in keys if value.get(key) is not None}

def _workflow_performance_method(value: Mapping[str, object] | None) -> Dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    method = value.get("method")
    if not isinstance(method, Mapping):
        return None
    return {
        "turn_definition": str(method.get("turn_definition") or "turn timing record"),
        "visible_output_definition": str(method.get("visible_output_definition") or "visible output tokens"),
        "generation_qualification": str(method.get("generation_qualification") or "unambiguous timed AgentMessage"),
        "aggregation": str(method.get("aggregation") or "weighted totals"),
        "caveat": str(method.get("caveat") or "Timing coverage is partial unless stated otherwise."),
    }


def _workflow_throughput_method(value: Mapping[str, object] | None) -> Dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    method = value.get("method")
    if not isinstance(method, Mapping):
        return None
    return {
        "turn_definition": str(method.get("turn_definition") or "observed model inference usage record"),
        "cadence_basis": str(method.get("cadence_basis") or "consecutive observed turns"),
        "observed_work_basis": str(method.get("observed_work_basis") or "raw tokens attributed to qualifying cadence intervals"),
        "workflow_output_basis": str(method.get("workflow_output_basis") or "model output tokens divided by workflow wall time"),
        "idle_gap_seconds": max(0.0, _number(method.get("idle_gap_seconds"))),
        "caveat": str(method.get("caveat") or "Observed cadence is not server inference latency."),
    }


def _workflow_local_ids(profile_source: Mapping[str, object], profile_index: int) -> tuple[dict[str, str], list[Mapping[str, object]]]:
    nested = profile_source.get("nested_attribution")
    sessions = nested.get("sessions") if isinstance(nested, Mapping) else None
    rows = [row for row in (sessions or []) if isinstance(row, Mapping)]
    labels: list[str] = []
    for row in rows:
        label = str(row.get("agent") or "")
        if label and label not in labels:
            labels.append(label)
    prefix = f"agent-{profile_index:03d}-"
    return {label: f"{prefix}{i:03d}" for i, label in enumerate(labels, 1)}, rows


def workflow_profile_to_cqa(profile_source: Mapping[str, object], profile_index: int = 1) -> Dict[str, object]:
    """Translate a privacy-safe workflow-profiler export into one cqa-report-v1 profile.

    The workflow profiler remains authoritative for attribution and lifecycle analysis.
    This function only renames identifiers, normalizes units/timestamps, and selects
    privacy-safe fields for the shared presentation contract.
    """
    schema = str(profile_source.get("schema") or "")
    if not schema.startswith("codex-workflow-cost-profile-"):
        raise ValueError(f"unsupported workflow profile schema: {schema or 'missing'}")

    local_ids, agent_rows = _workflow_local_ids(profile_source, profile_index)
    throughput_source = profile_source.get("turn_throughput")
    throughput_source = throughput_source if isinstance(throughput_source, Mapping) else {}
    throughput_by_agent = throughput_source.get("by_agent") if isinstance(throughput_source.get("by_agent"), Mapping) else {}
    throughput_by_role = throughput_source.get("by_role") if isinstance(throughput_source.get("by_role"), Mapping) else {}
    throughput_by_model = throughput_source.get("by_model") if isinstance(throughput_source.get("by_model"), Mapping) else {}
    performance_source = profile_source.get("turn_performance")
    performance_source = performance_source if isinstance(performance_source, Mapping) else {}
    performance_by_agent = performance_source.get("by_agent") if isinstance(performance_source.get("by_agent"), Mapping) else {}
    performance_by_role = performance_source.get("by_role") if isinstance(performance_source.get("by_role"), Mapping) else {}
    performance_by_model = performance_source.get("by_model") if isinstance(performance_source.get("by_model"), Mapping) else {}
    performance_by_model_effort = performance_source.get("by_model_effort") if isinstance(performance_source.get("by_model_effort"), Sequence) else []
    response_source = profile_source.get("response_efficiency")
    response_source = response_source if isinstance(response_source, Mapping) else {}
    response_by_model = response_source.get("by_model") if isinstance(response_source.get("by_model"), Mapping) else {}
    response_by_model_effort = {
        (str(row.get("model") or "unknown"), str(row.get("effort") or "unknown")): row.get("stats")
        for row in (response_source.get("by_model_effort") or [])
        if isinstance(row, Mapping) and isinstance(row.get("stats"), Mapping)
    }
    active_windows = [row for row in (profile_source.get("active_windows") or []) if isinstance(row, Mapping)]
    windows_by_agent: dict[str, list[Mapping[str, object]]] = {}
    for row in active_windows:
        label = str(row.get("target") or "")
        if label:
            windows_by_agent.setdefault(label, []).append(row)

    timeline_agent_windows: list[Dict[str, object]] = []
    for i, row in enumerate(active_windows, 1):
        label = str(row.get("target") or "")
        agent_id = local_ids.get(label)
        start_ts = _iso_utc(row.get("start"))
        end_ts = _iso_utc(row.get("end"))
        if agent_id is None or start_ts is None or end_ts is None:
            continue
        timeline_agent_windows.append({
            "id": f"agent-window-{profile_index:03d}-{i:03d}",
            "agent_id": agent_id,
            "role": _semantic_workflow_role(row.get("role")),
            "start": start_ts,
            "end": end_ts,
            "chunk": (str(row.get("chunk")) if row.get("chunk") is not None else None),
            "confidence": str(row.get("assignment_confidence") or row.get("spawn_confidence") or "unknown"),
            "extensions": {
                "spawn_method": row.get("spawn_method"),
                "assignment_source": row.get("assignment_source"),
            },
        })

    timeline_guardian: list[Dict[str, object]] = []
    for i, row in enumerate((profile_source.get("bursts") or []), 1):
        if not isinstance(row, Mapping) or str(row.get("role") or "") != "guardian/auto-review":
            continue
        start_ts = _iso_utc(row.get("start"))
        end_ts = _iso_utc(row.get("end")) or start_ts
        if start_ts is None or end_ts is None:
            continue
        label = str(row.get("agent") or "")
        timeline_guardian.append({
            "id": f"guardian-activity-{profile_index:03d}-{len(timeline_guardian)+1:03d}",
            "agent_id": local_ids.get(label),
            "start": start_ts,
            "end": end_ts,
            "usage": _workflow_usage(row.get("cost") if isinstance(row.get("cost"), Mapping) else {}),
            "extensions": {"burst_index": _integer(row.get("index")),
                           "auto_review_quota_policy": review_policy.normalize(row.get("quota_policy"))},
        })

    timeline_handoffs: list[Dict[str, object]] = []
    for row in (profile_source.get("cycles") or []):
        if not isinstance(row, Mapping):
            continue
        from_label = str(row.get("first_agent") or "")
        to_label = str(row.get("second_agent") or "")
        from_id, to_id = local_ids.get(from_label), local_ids.get(to_label)
        if from_id is None or to_id is None:
            continue
        candidate_starts = [_iso_utc(w.get("start")) for w in windows_by_agent.get(to_label, [])]
        candidate_starts = [x for x in candidate_starts if x is not None]
        timestamp = min(candidate_starts) if candidate_starts else _iso_utc(row.get("end"))
        if timestamp is None:
            continue
        timeline_handoffs.append({
            "id": f"handoff-{profile_index:03d}-{len(timeline_handoffs)+1:03d}",
            "from_agent_id": from_id,
            "to_agent_id": to_id,
            "from_role": _semantic_workflow_role(row.get("first_role")),
            "to_role": _semantic_workflow_role(row.get("second_role")),
            "timestamp": timestamp,
            "gap_seconds": _number(row.get("handoff_gap_seconds")),
            "quality": str(row.get("quality") or "unknown"),
            "extensions": {
                "isolated_from_other_observed_descendants": bool(row.get("isolated_from_other_observed_descendants")),
            },
        })

    timeline_concurrency = _workflow_concurrency_windows(timeline_agent_windows, profile_index)

    agents: list[Dict[str, object]] = []
    for row in agent_rows:
        label = str(row.get("agent") or "")
        if not label or label not in local_ids:
            continue
        windows = windows_by_agent.get(label, [])
        active = None
        chunks: set[str] = set()
        if windows:
            starts = [_iso_utc(w.get("start")) for w in windows]
            ends = [_iso_utc(w.get("end")) for w in windows]
            starts = [x for x in starts if x is not None]
            ends = [x for x in ends if x is not None]
            if starts or ends:
                active = {
                    "start": min(starts) if starts else None,
                    "end": max(ends) if ends else None,
                }
            chunks = {str(w.get("chunk")) for w in windows if w.get("chunk")}
        parent_label = str(row.get("parent") or "")
        agents.append({
            "id": local_ids[label],
            "parent_id": local_ids.get(parent_label) if parent_label else None,
            "role": _semantic_workflow_role(row.get("role")),
            "role_confidence": str(row.get("role_confidence") or "unknown"),
            "active": active,
            "usage": _workflow_usage(row.get("direct") if isinstance(row.get("direct"), Mapping) else {}),
            "throughput": _workflow_throughput(throughput_by_agent.get(label) if isinstance(throughput_by_agent, Mapping) else None),
            "performance": _workflow_performance(performance_by_agent.get(label) if isinstance(performance_by_agent, Mapping) else None),
            "unit": (str(row.get("unit")) if row.get("unit") is not None else None),
            "chunk": (next(iter(chunks)) if len(chunks) == 1 else None),
            "issues": [str(x) for x in (row.get("issues") or [])],
            "extensions": {
                "lifecycle_windows": len(windows),
                "chunks_observed": sorted(chunks),
                "is_root": label == "ROOT",
            },
        })

    role_costs = profile_source.get("role_costs")
    roles: list[Dict[str, object]] = []
    if isinstance(role_costs, Mapping):
        role_groups: dict[str, list[tuple[str, Mapping[str, object]]]] = {}
        for source_role, values in role_costs.items():
            if not isinstance(values, Mapping):
                continue
            role_groups.setdefault(_semantic_workflow_role(source_role), []).append((str(source_role), values))
        for role, grouped in sorted(role_groups.items()):
            source_roles = [source_role for source_role, _ in grouped]
            throughput_rows = [throughput_by_role[source_role] for source_role in source_roles
                               if isinstance(throughput_by_role, Mapping) and isinstance(throughput_by_role.get(source_role), Mapping)]
            performance_rows = [performance_by_role[source_role] for source_role in source_roles
                                if isinstance(performance_by_role, Mapping) and isinstance(performance_by_role.get(source_role), Mapping)]
            # A semantic role can absorb more than one legacy topology-bearing
            # bucket.  Do not publish a merged timing/cadence statistic when
            # only some of those source buckets have evidence; partial input
            # would otherwise look like a complete role aggregate.
            merged_throughput = (_merge_workflow_throughput(throughput_rows)
                                 if len(throughput_rows) == len(source_roles) else None)
            merged_performance = (_merge_workflow_performance(performance_rows)
                                  if len(performance_rows) == len(source_roles) else None)
            roles.append({
                "role": role,
                "usage": _merge_workflow_usage([values for _, values in grouped]),
                "throughput": merged_throughput,
                "performance": merged_performance,
                "extensions": ({"topology_merged_from": source_roles} if len(source_roles) > 1 else {}),
            })

    compaction_src = profile_source.get("compaction_audit")
    compaction_src = compaction_src if isinstance(compaction_src, Mapping) else {}
    compactions: list[Dict[str, object]] = []
    for i, event in enumerate((compaction_src.get("events") or []), 1):
        if not isinstance(event, Mapping):
            continue
        ts = _iso_utc(event.get("timestamp"))
        if ts is None:
            continue
        agent_label = str(event.get("agent") or "")
        shrink = _finite(event.get("observed_shrink_fraction"))
        if shrink is not None:
            shrink = min(1.0, max(0.0, shrink))
        compactions.append({
            "id": f"compaction-{profile_index:03d}-{i:03d}",
            "agent_id": local_ids.get(agent_label),
            "role": _semantic_workflow_role(event.get("role")),
            "timestamp": ts,
            "context_before_input_tokens": (_integer(event.get("context_before_input_tokens"))
                                              if event.get("context_before_input_tokens") is not None else None),
            "context_after_first_request_input_tokens": (_integer(event.get("context_after_first_request_input_tokens"))
                                                            if event.get("context_after_first_request_input_tokens") is not None else None),
            "observed_shrink_fraction": shrink,
            "recovery": {
                "end": _iso_utc(event.get("recovery_end")),
                "end_reason": (str(event.get("recovery_end_reason")) if event.get("recovery_end_reason") is not None else None),
                "duration_seconds": _finite(event.get("recovery_duration_seconds")),
                "usage": _workflow_usage(event.get("recovery") if isinstance(event.get("recovery"), Mapping) else {}),
            },
            "extensions": {
                "refill_reached": bool(event.get("refill_reached")),
                "refill_target_tokens": (_integer(event.get("refill_target_tokens"))
                                         if event.get("refill_target_tokens") is not None else None),
                "direct_usage_observed": bool(event.get("direct_usage_observed")),
                "direct_usage_method": event.get("direct_usage_method"),
                "direct_usage_confidence": event.get("direct_usage_confidence"),
            },
        })

    comparison = profile_source.get("comparison_metrics")
    comparison = comparison if isinstance(comparison, Mapping) else {}
    signals: list[Dict[str, object]] = []

    def add_signal(code: str, label: str, value: object, unit: str, summary: str,
                   extensions: Optional[Dict[str, object]] = None) -> None:
        x = _finite(value)
        if x is None:
            return
        signals.append({
            "id": f"signal-{profile_index:03d}-{len(signals)+1:03d}",
            "code": code,
            "label": label,
            "value": x,
            "unit": unit,
            "classification": "observational",
            "summary": summary,
            "evidence_refs": [f"workflow-{profile_index:03d}"],
            "extensions": extensions or {},
        })

    add_signal(
        "POST_COMPACTION_RECOVERY_SHARE", "Post-compaction recovery share",
        comparison.get("post_compaction_recovery_api_eq_share"), "share",
        "API-list-equivalent work observed during defined post-compaction recovery windows; not automatically caused by compaction.",
        {
            "api_list_equivalent_usd": _finite(comparison.get("post_compaction_recovery_api_eq")),
            "requests": max(0, _integer(comparison.get("post_compaction_recovery_requests"))),
        },
    )
    add_signal(
        "LARGE_CONTEXT_SMALL_OUTPUT_SHARE", "Large context / small output share",
        comparison.get("large_context_small_output_api_eq_share"), "share",
        "Share of API-list-equivalent work matching the large-context/small-output heuristic; not automatically waste.",
    )
    add_signal(
        "EXTRA_CONCURRENCY_HOURS", "Extra concurrent agent-hours",
        comparison.get("extra_concurrency_hours"), "hours",
        "Additive recognized child-agent time above one active child; it can exceed workflow wall-clock duration and is not proof of duplicate work.",
        {"peak_concurrent_children": max(0, _integer(comparison.get("peak_concurrent_children")))},
    )
    add_signal(
        "LINGERING_AGENT_CANDIDATES", "Lingering-agent candidates",
        len(profile_source.get("lingering_candidates") or []), "count",
        "Candidate overlaps requiring contextual review; cross-chunk activity and successor overlap can be legitimate.",
    )

    pause_src = profile_source.get("pause_analysis")
    pause_src = pause_src if isinstance(pause_src, Mapping) else {}
    decisions: dict[tuple[str, str], Mapping[str, object]] = {}
    for decision in (pause_src.get("decisions") or []):
        if isinstance(decision, Mapping):
            key = (str(decision.get("start") or ""), str(decision.get("end") or ""))
            decisions[key] = decision
    pauses: list[Dict[str, object]] = []
    for i, row in enumerate((pause_src.get("quiet_intervals") or []), 1):
        if not isinstance(row, Mapping):
            continue
        start = _iso_utc(row.get("start"))
        end = _iso_utc(row.get("end"))
        if start is None or end is None:
            continue
        decision = decisions.get((str(row.get("start") or ""), str(row.get("end") or "")), {})
        pauses.append({
            "id": f"pause-{profile_index:03d}-{i:03d}",
            "start": start,
            "end": end,
            "seconds": max(0.0, _number(row.get("seconds"))),
            "status": str(row.get("status") or "unclassified"),
            "boundary_source": str(decision.get("boundary_source") or "unknown"),
        })

    nested = profile_source.get("nested_attribution")
    nested = nested if isinstance(nested, Mapping) else {}
    nested_total = nested.get("total") if isinstance(nested.get("total"), Mapping) else {}
    coverage = nested.get("coverage") if isinstance(nested.get("coverage"), Mapping) else {}
    analysis_window = profile_source.get("analysis_window")
    analysis_window = analysis_window if isinstance(analysis_window, Mapping) else {}
    source_span = profile_source.get("source_family_span")
    source_span = source_span if isinstance(source_span, Mapping) else {}
    start = _iso_utc(analysis_window.get("analysis_start") or source_span.get("start"))
    end = _iso_utc(analysis_window.get("analysis_end") or source_span.get("end"))

    concurrency_src = profile_source.get("concurrency")
    concurrency_src = concurrency_src if isinstance(concurrency_src, Mapping) else {}
    raw_tokens = _integer(comparison.get("workflow_raw_tokens"), _integer(nested_total.get("total_tokens")))
    nested_price_coverage = _finite(nested_total.get("price_coverage"))
    if nested_price_coverage is not None:
        nested_price_coverage = min(1.0, max(0.0, nested_price_coverage))
    api_eq = _finite(comparison.get("workflow_api_eq"))
    if api_eq is None:
        api_eq = _finite(nested_total.get("api_list_equivalent_usd"))
        if raw_tokens > 0 and nested_price_coverage is not None and nested_price_coverage <= 0:
            api_eq = None

    pricing_src = profile_source.get("pricing")
    pricing_src = pricing_src if isinstance(pricing_src, Mapping) else {}
    pricing_rows = []
    for raw_row in (pricing_src.get("by_model") or []):
        if not isinstance(raw_row, Mapping):
            continue
        row_cost = _finite(raw_row.get("api_list_equivalent_usd"))
        row_base = _finite(raw_row.get("base_api_list_equivalent_usd"))
        row_uplift = _finite(raw_row.get("long_context_price_uplift_usd"))
        row_cov = _finite(raw_row.get("price_coverage"))
        if row_cov is not None:
            row_cov = min(1.0, max(0.0, row_cov))
        rates = raw_row.get("rates_per_million")
        rates = rates if isinstance(rates, Mapping) else {}
        pricing_rows.append({
            "observed_model": str(raw_row.get("observed_model") or "unknown"),
            "ratecard_model": str(raw_row.get("ratecard_model") or raw_row.get("observed_model") or "unknown"),
            "requests": max(0, _integer(raw_row.get("requests"))),
            "priced_requests": max(0, _integer(raw_row.get("priced_requests"))),
            "input_tokens": max(0, _integer(raw_row.get("input_tokens"))),
            "cached_input_tokens": max(0, _integer(raw_row.get("cached_input_tokens"))),
            "uncached_input_tokens": max(0, _integer(raw_row.get("uncached_input_tokens"))),
            "output_tokens": max(0, _integer(raw_row.get("output_tokens"))),
            "total_tokens": max(0, _integer(raw_row.get("total_tokens"))),
            "api_list_equivalent_usd": row_cost,
            "base_api_list_equivalent_usd": row_base,
            "price_coverage": row_cov,
            "long_context_priced_requests": max(0, _integer(raw_row.get("long_context_priced_requests"))),
            "long_context_price_uplift_usd": row_uplift,
            "rates_per_million": {
                "uncached_input": _finite(rates.get("uncached_input")),
                "cached_input": _finite(rates.get("cached_input")),
                "output": _finite(rates.get("output")),
            } if rates else None,
            "auto_review_quota_policy": review_policy.normalize(raw_row.get("quota_policy")),
        })
    if nested_price_coverage is None:
        pricing_status = "unknown"
    elif nested_price_coverage <= 0:
        pricing_status = "unavailable"
    elif nested_price_coverage >= 1.0 - 1e-9:
        pricing_status = "complete"
    else:
        pricing_status = "partial"
    pricing_extension = {
        "status": pricing_status,
        "price_coverage": nested_price_coverage,
        "basis": str(pricing_src.get("basis") or "public token-rate equivalent"),
        "source": str(pricing_src.get("source") or "unknown"),
        "source_ref": str(pricing_src.get("source_ref") or ""),
        "as_of": str(pricing_src.get("as_of") or "unknown"),
        "auto_review_transition_date": pricing_src.get("auto_review_transition_date"),
        "auto_review_before": pricing_src.get("auto_review_before"),
        "auto_review_on_or_after": pricing_src.get("auto_review_on_or_after"),
        "long_context_threshold_input_tokens": pricing_src.get("long_context_threshold_input_tokens"),
        "long_context_input_multiplier": pricing_src.get("long_context_input_multiplier"),
        "long_context_cached_multiplier": pricing_src.get("long_context_cached_multiplier"),
        "long_context_output_multiplier": pricing_src.get("long_context_output_multiplier"),
        "long_context_exempt_models": list(pricing_src.get("long_context_exempt_models") or []),
        "fast_mode_inferred": bool(pricing_src.get("fast_mode_inferred", False)),
        "regional_multiplier_inferred": bool(pricing_src.get("regional_multiplier_inferred", False)),
        "by_model": pricing_rows,
        "priced_requests": max(0, _integer(pricing_src.get("priced_requests"))),
        "unpriced_requests": max(0, _integer(pricing_src.get("unpriced_requests"))),
        "long_context_priced_requests": max(0, _integer(pricing_src.get("long_context_priced_requests"))),
        "long_context_price_uplift_usd": _finite(pricing_src.get("long_context_price_uplift_usd")),
        "custom_price_override": bool(pricing_src.get("custom_price_override", False)),
    }
    session_count = _integer(analysis_window.get("primary_sessions"), _integer(coverage.get("sessions"), len(agent_rows)))
    request_count = _integer(nested_total.get("requests"))
    compaction_count = _integer(compaction_src.get("explicit_compactions"), len(compactions))
    peak = _integer(concurrency_src.get("peak_concurrent_children"))

    workflow_analysis = profile_source.get("workflow_analysis")
    workflow_analysis = workflow_analysis if isinstance(workflow_analysis, Mapping) else {}
    lifetime_status = workflow_analysis.get("lifetime_windows_status")
    status = "partial" if str(lifetime_status or "").lower() == "partial" else "complete"
    model_throughput = []
    if isinstance(throughput_by_model, Mapping):
        for model, values in sorted(throughput_by_model.items(), key=lambda kv: str(kv[0])):
            stats = _workflow_throughput(values if isinstance(values, Mapping) else None)
            if stats is not None:
                model_throughput.append({"model": str(model), "throughput": stats, "extensions": {}})
    model_performance = []
    if performance_by_model_effort:
        for row in performance_by_model_effort:
            if not isinstance(row, Mapping):
                continue
            stats = _workflow_performance(row.get("stats") if isinstance(row.get("stats"), Mapping) else None)
            if stats is not None:
                model = str(row.get("model") or "unknown")
                effort = str(row.get("effort") or "unknown")
                efficiency = _workflow_response_efficiency_stats(
                    response_by_model_effort.get((model, effort))
                )
                model_performance.append({
                    "model": model,
                    "effort": effort,
                    "performance": stats,
                    "extensions": ({"response_efficiency": efficiency} if efficiency is not None else {}),
                })
    elif isinstance(performance_by_model, Mapping):
        for model, values in sorted(performance_by_model.items(), key=lambda kv: str(kv[0])):
            stats = _workflow_performance(values if isinstance(values, Mapping) else None)
            if stats is not None:
                efficiency = _workflow_response_efficiency_stats(
                    response_by_model.get(str(model)) if isinstance(response_by_model.get(str(model)), Mapping) else None
                )
                model_performance.append({
                    "model": str(model), "effort": None, "performance": stats,
                    "extensions": ({"response_efficiency": efficiency} if efficiency is not None else {}),
                })

    return {
        "id": f"workflow-{profile_index:03d}",
        "analysis_window": {"start": start, "end": end},
        "summary": {
            "raw_tokens": max(0, raw_tokens),
            "api_list_equivalent_usd": (max(0.0, api_eq) if api_eq is not None else None),
            "sessions": max(0, session_count),
            "requests": max(0, request_count),
            "compactions": max(0, compaction_count),
            "peak_concurrent_children": max(0, peak),
        },
        "throughput_method": _workflow_throughput_method(throughput_source),
        "throughput": _workflow_throughput(throughput_source.get("overall") if isinstance(throughput_source.get("overall"), Mapping) else None),
        "throughput_by_model": model_throughput,
        "performance_method": _workflow_performance_method(performance_source),
        "performance": _workflow_performance(performance_source.get("overall") if isinstance(performance_source.get("overall"), Mapping) else None),
        "performance_by_model": model_performance,
        "roles": roles,
        "agents": agents,
        "compactions": compactions,
        "concurrency": {
            "recognized_child_agent_hours": max(0.0, _number(concurrency_src.get("recognized_child_agent_hours"))),
            "active_wall_hours": max(0.0, _number(concurrency_src.get("recognized_child_active_wall_minutes"))) / 60.0,
            "overlap_wall_hours": max(0.0, _number(concurrency_src.get("overlap_wall_minutes"))) / 60.0,
            "extra_concurrency_hours": max(0.0, _number(concurrency_src.get("extra_concurrency_hours"))),
            "peak_concurrent_children": max(0, peak),
            "wall_minutes_by_count": {
                str(k): max(0.0, _number(v)) for k, v in
                (concurrency_src.get("wall_minutes_by_count") or {}).items()
            } if isinstance(concurrency_src.get("wall_minutes_by_count"), Mapping) else {},
        },
        "timeline": {
            "agent_windows": timeline_agent_windows,
            "guardian_activity": timeline_guardian,
            "concurrency_windows": timeline_concurrency,
            "handoffs": timeline_handoffs,
        },
        "signals": signals,
        "pauses": pauses,
        "extensions": {
            "source_profile_schema": schema,
            "source_profile_version": str(profile_source.get("version") or "unknown"),
            "section_status": status,
            "lifetime_windows_status": lifetime_status,
            "trusted_active_windows": max(0, _integer(comparison.get("trusted_active_windows"), len(active_windows))),
            "chunk_correlation_coverage": _finite(comparison.get("chunk_correlation_coverage")),
            "role_attribution_sessions": max(0, _integer(coverage.get("assigned_or_inherited_sessions"))),
            "workflow_sessions": max(0, _integer(coverage.get("sessions"), session_count)),
            "response_efficiency": {
                "method": _workflow_response_efficiency_method(
                    response_source.get("method") if isinstance(response_source.get("method"), Mapping) else None
                ),
                "overall": _workflow_response_efficiency_stats(
                    response_source.get("overall") if isinstance(response_source.get("overall"), Mapping) else None
                ),
            } if response_source else None,
            "pricing": pricing_extension,
            "auto_review_quota_policy": review_policy.normalize(profile_source.get("auto_review_quota_policy")),
        },
    }


def _workflow_quality(profile: Mapping[str, object]) -> tuple[str, list[Dict[str, object]]]:
    status = str(profile.get("extensions", {}).get("section_status") if isinstance(profile.get("extensions"), Mapping) else "complete")
    warnings: list[Dict[str, object]] = []
    if status == "partial":
        warnings.append({
            "code": "WORKFLOW_LIFETIME_WINDOWS_PARTIAL",
            "level": "caution",
            "message": "The workflow profile reports partial lifetime-window coverage; concurrency and lifecycle views should preserve that caveat.",
            "refs": [str(profile.get("id"))],
        })
        return "caution", warnings
    return "good", warnings


def build_workflow_only_report_v1(profile_source: Mapping[str, object], *,
                                  generator_version: str,
                                  report_kind: str = "export") -> Dict[str, object]:
    """Build a complete cqa-report-v1 whose only requested analysis is workflow profiling."""
    profile = workflow_profile_to_cqa(profile_source, 1)
    quality, warnings = _workflow_quality(profile)
    ext = profile.get("extensions") if isinstance(profile.get("extensions"), Mapping) else {}
    workflow_status = str(ext.get("section_status") or "complete")
    start = profile["analysis_window"]["start"]  # type: ignore[index]
    end = profile["analysis_window"]["end"]  # type: ignore[index]
    profiler_version = str(profile_source.get("version") or "unknown")
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "generator": {
            "name": "codex-quota-audit",
            "version": generator_version,
            "components": [{"name": "codex-workflow-cost-profile", "version": profiler_version}],
        },
        "report": {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "kind": report_kind if report_kind in {"dashboard", "export"} else "export",
            "observed_range": {"start": start, "end": end},
            "extensions": {"workflow_only": True},
        },
        "privacy": {
            "profile": PRIVACY_PROFILE,
            "contains_prompt_text": False,
            "contains_response_text": False,
            "contains_tool_output": False,
            "contains_file_contents": False,
            "contains_auth_data": False,
            "contains_account_identity": False,
            "contains_source_paths": False,
            "contains_raw_session_ids": False,
        },
        "analysis": {
            "profile": "dashboard-v1",
            "methods": {"workflow_attribution": str(profile_source.get("schema") or "codex-workflow-cost-profile")},
            "parameters": {},
            "extensions": {},
        },
        "quota": {"status": "not_requested", "summary": None, "regimes": [], "cohorts": [], "extensions": {}},
        "guardian": {"status": "not_requested", "summary": None, "periods": [], "approval_episodes": [], "extensions": {}},
        "banked_resets": {
            "status": "not_requested", "summary": None, "whole_period_comparisons": [],
            "boundary_slices": [], "capacity_periods": [], "boundary_observations": [], "extensions": {},
        },
        "workflow": {"status": workflow_status, "profiles": [profile], "extensions": {}},
        "findings": [],
        "data_quality": {
            "overall": quality,
            "metrics": {
                "quota_cohorts_total": 0,
                "quota_cohorts_priced": 0,
                "guardian_pairing_coverage": None,
                "workflow_role_attribution_sessions": ext.get("role_attribution_sessions"),
                "workflow_sessions": ext.get("workflow_sessions"),
                "workflow_lifetime_windows_status": ext.get("lifetime_windows_status"),
            },
            "warnings": warnings,
            "extensions": {},
        },
    }


def attach_workflow_profile_sources(report: Mapping[str, object],
                                    profile_sources: Sequence[Mapping[str, object]]) -> Dict[str, object]:
    """Attach one or more privacy-safe workflow-profiler exports to an existing report."""
    out: Dict[str, object] = copy.deepcopy(dict(report))
    profiles: list[Dict[str, object]] = []
    warnings: list[Dict[str, object]] = []
    overall = str(out.get("data_quality", {}).get("overall", "unknown")) if isinstance(out.get("data_quality"), Mapping) else "unknown"
    components = out.get("generator", {}).get("components") if isinstance(out.get("generator"), Mapping) else None
    if not isinstance(components, list):
        components = []
        out["generator"]["components"] = components  # type: ignore[index]

    role_sessions = 0
    workflow_sessions = 0
    lifetime_statuses: list[str] = []
    starts: list[str] = []
    ends: list[str] = []
    for i, source in enumerate(profile_sources, 1):
        profile = workflow_profile_to_cqa(source, i)
        profiles.append(profile)
        q, ws = _workflow_quality(profile)
        warnings.extend(ws)
        if q == "caution" and overall == "good":
            overall = "caution"
        ext = profile.get("extensions") if isinstance(profile.get("extensions"), Mapping) else {}
        role_sessions += max(0, _integer(ext.get("role_attribution_sessions")))
        workflow_sessions += max(0, _integer(ext.get("workflow_sessions")))
        if ext.get("lifetime_windows_status") is not None:
            lifetime_statuses.append(str(ext.get("lifetime_windows_status")))
        window = profile.get("analysis_window") if isinstance(profile.get("analysis_window"), Mapping) else {}
        if window.get("start"):
            starts.append(str(window.get("start")))
        if window.get("end"):
            ends.append(str(window.get("end")))
        component = {"name": "codex-workflow-cost-profile", "version": str(source.get("version") or "unknown")}
        if component not in components:
            components.append(component)

    workflow_status = "partial" if any(
        str(p.get("extensions", {}).get("section_status")) == "partial"
        for p in profiles if isinstance(p.get("extensions"), Mapping)
    ) else ("complete" if profiles else "not_requested")
    out["workflow"] = {"status": workflow_status, "profiles": profiles, "extensions": {}}

    analysis = out.get("analysis")
    if isinstance(analysis, dict):
        methods = analysis.setdefault("methods", {})
        if isinstance(methods, dict) and profile_sources:
            methods["workflow_attribution"] = str(profile_sources[0].get("schema") or "codex-workflow-cost-profile")

    report_info = out.get("report")
    if isinstance(report_info, dict):
        observed = report_info.get("observed_range")
        if isinstance(observed, dict):
            if observed.get("start"):
                starts.append(str(observed.get("start")))
            if observed.get("end"):
                ends.append(str(observed.get("end")))
            observed["start"] = min(starts) if starts else None
            observed["end"] = max(ends) if ends else None

    quality_obj = out.get("data_quality")
    if isinstance(quality_obj, dict):
        quality_obj["overall"] = overall
        metrics = quality_obj.get("metrics")
        if isinstance(metrics, dict):
            metrics["workflow_role_attribution_sessions"] = role_sessions
            metrics["workflow_sessions"] = workflow_sessions
            metrics["workflow_lifetime_windows_status"] = (
                lifetime_statuses[0] if len(set(lifetime_statuses)) == 1 and lifetime_statuses else
                "mixed" if lifetime_statuses else None
            )
        qwarnings = quality_obj.get("warnings")
        if isinstance(qwarnings, list):
            qwarnings.extend(warnings)
    return out


def load_workflow_profile_json(path: str) -> Dict[str, object]:
    payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"workflow profile must be a JSON object: {path}")
    schema = str(payload.get("schema") or "")
    if not schema.startswith("codex-workflow-cost-profile-"):
        raise ValueError(f"expected a codex-workflow-cost-profile JSON export, got {schema or 'no schema'}: {path}")
    return payload

def write_cqa_report_json(path: str, report: Mapping[str, object]) -> str:
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return str(out)


def render_dashboard_html(report: Mapping[str, object], output_path: str,
                          template_path: Optional[str] = None) -> str:
    if template_path is None:
        template = Path(__file__).resolve().parents[1] / "assets" / "dashboard" / "cqa-dashboard-v1.template.html"
    else:
        template = Path(template_path).expanduser()
    text = template.read_text(encoding="utf-8")
    if DASHBOARD_PLACEHOLDER not in text:
        raise RuntimeError(f"dashboard template is missing {DASHBOARD_PLACEHOLDER}")
    payload = json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    out = Path(output_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text.replace(DASHBOARD_PLACEHOLDER, payload), encoding="utf-8")
    return str(out)
