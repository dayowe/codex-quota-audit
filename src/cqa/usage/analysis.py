"""Calendar selection and auditable token-rate-equivalent aggregates."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import auto_review_policy
from ..quota import audit


def iso_utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def calendar_zone(name):
    if name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Timezone {name!r} is unavailable; use UTC or an installed IANA timezone such as Europe/Berlin.") from exc


def period(month=None, after=None, before=None, timezone_name="UTC", *, now=None):
    """Return inclusive/exclusive UTC bounds and explicit calendar provenance."""
    zone = calendar_zone(timezone_name)
    if month and (after or before):
        raise ValueError("--month cannot be combined with --after or --before")
    if after or before:
        if not (after and before):
            raise ValueError("Provide both --after and --before for a timestamp range")
        def parse(value, option):
            try:
                result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{option} requires an ISO-8601 timestamp with a timezone offset") from exc
            if result.tzinfo is None or result.utcoffset() is None:
                raise ValueError(f"{option} requires an explicit timezone offset")
            return result.astimezone(timezone.utc)
        start, end = parse(after, "--after"), parse(before, "--before")
        label = "timestamp range"
    else:
        month = month or (now or datetime.now(timezone.utc)).astimezone(zone).strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("--month must be YYYY-MM")
        year, number = map(int, month.split("-"))
        try:
            start = datetime(year, number, 1, tzinfo=zone).astimezone(timezone.utc)
            end = datetime(year + int(number == 12), number % 12 + 1, 1, tzinfo=zone).astimezone(timezone.utc)
        except ValueError as exc:
            raise ValueError("--month must identify a valid calendar month") from exc
        label = month
    if end <= start:
        raise ValueError("--before must be later than --after")
    return start, end, {
        "label": label, "timezone": timezone_name,
        "start_inclusive": iso_utc(start), "end_exclusive": iso_utc(end),
        "calendar_start": start.astimezone(zone).isoformat(),
        "calendar_end": end.astimezone(zone).isoformat(),
    }


def aggregate(events, prices):
    """Keep unpriced work, and distinguish a subtotal from a complete equivalent."""
    uncached = cached = output = reasoning = priced_tokens = priced_requests = 0
    base_values, values = [], []
    long_context_requests = 0
    auth_sources = Counter()
    for event in events:
        uncached += event.uncached
        cached += event.cached
        output += event.output
        reasoning += event.reasoning_output  # A subset of output, never added to total.
        auth_sources[event.auth_source if event.auth_source in auto_review_policy.AUTH_SOURCES else "unknown"] += 1
        base, adjusted, long_context, _ = audit.ratecard_event_cost(
            event.model, event.uncached, event.cached, event.output, prices, ts=event.ts
        )
        if adjusted is not None:
            priced_requests += 1
            priced_tokens += event.tokens
            base_values.append(base)
            values.append(adjusted)
            long_context_requests += int(long_context)
    requests = len(events)
    total = uncached + cached + output
    subtotal = math.fsum(values)
    unpriced_requests = requests - priced_requests
    pricing_status = ("not_available" if not requests else "unavailable" if not priced_requests
                      else "partial" if unpriced_requests else "complete")
    return {
        "requests": requests,
        "uncached_input_tokens": uncached, "cached_input_tokens": cached,
        "output_tokens": output, "reasoning_output_tokens": reasoning,
        "total_tokens": total,
        "cached_fraction_of_input": cached / (uncached + cached) if uncached + cached else None,
        "cached_fraction_of_all_tokens": cached / total if total else None,
        "priced_requests": priced_requests, "unpriced_requests": unpriced_requests,
        "priced_tokens": priced_tokens, "unpriced_tokens": total - priced_tokens,
        "price_coverage": priced_tokens / total if total else None,
        "pricing_status": pricing_status,
        "priced_subtotal_usd": subtotal,
        "api_list_equivalent_usd": subtotal if pricing_status == "complete" else None,
        "long_context_requests": long_context_requests,
        "long_context_uplift_usd": subtotal - math.fsum(base_values),
        "auth_evidence_sources": dict(sorted(auth_sources.items())),
    }


def analyze(events, stats, prices, selected_period, *, models=(), custom_prices=False):
    """Select only after full-file duplicate/replay classification has completed."""
    start = datetime.fromisoformat(selected_period["start_inclusive"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(selected_period["end_exclusive"].replace("Z", "+00:00"))
    zone = calendar_zone(selected_period["timezone"])
    models = sorted(set(models))
    in_period = [e for e in events if start <= e.ts < end and (not models or e.model in models)]
    selected = [e for e in in_period if not e.probable_replay]
    groups = defaultdict(list)
    model_groups = defaultdict(list)
    model_rate_groups = defaultdict(set)
    auth_groups = defaultdict(list)
    activity_groups = defaultdict(list)
    day_groups = defaultdict(list)
    resolved_models = set()
    for event in selected:
        auth = event.auth_mode if event.auth_mode in auto_review_policy.AUTH_MODES else "unknown"
        activity = "auto_review" if event.is_auto_review_inference else "agent"
        groups[(event.model, auth, activity)].append(event)
        model_groups[event.model].append(event)
        auth_groups[auth].append(event)
        activity_groups[activity].append(event)
        day_groups[event.ts.astimezone(zone).date().isoformat()].append(event)
        rate_model = audit.ratecard_model_at(event.model, event.ts, prices)
        resolved_models.add(rate_model)
        if rate_model in prices:
            model_rate_groups[event.model].add(rate_model)
    summary = aggregate(selected, prices)
    coverage = {
        "scope": "available local Codex telemetry; other devices and missing logs are outside coverage",
        "first_observed": iso_utc(min((e.ts for e in selected), default=None)),
        "last_observed": iso_utc(max((e.ts for e in selected), default=None)),
        "observed_days": len({e.ts.astimezone(zone).date() for e in selected}),
        "without_quota_snapshot_requests": sum(not e.rate_windows for e in selected),
        "excluded_probable_replay_requests": sum(e.probable_replay for e in in_period),
        "uncertain_dense_requests": sum(e.uncertain_dense for e in selected),
        # These counters cover extraction of the entire available history.
        "history": {"files_checked": stats.files, "global_duplicates_removed": stats.global_duplicates,
                    "immediate_duplicates_removed": stats.immediate_duplicate_totals,
                    "missing_or_invalid_usage_records": stats.missing_usage,
                    "parse_errors": stats.json_errors,
                    "unreadable_files": getattr(stats, "unreadable_files", 0)},
        "cache": getattr(stats, "cache_stats", {}),
    }
    warnings = []
    def warn(code, message):
        warnings.append({"code": code, "level": "caution", "message": message, "refs": []})
    if summary["unpriced_requests"]:
        warn("USAGE_PARTIAL_PRICING", "Some selected usage has no known price; dollars are a priced subtotal.")
    if coverage["uncertain_dense_requests"]:
        warn("USAGE_UNCERTAIN_DENSE", "Dense records without sufficient replay evidence are retained; inspect counting uncertainty.")
    if stats.missing_usage or stats.json_errors or getattr(stats, "unreadable_files", 0):
        warn("USAGE_INCOMPLETE_HISTORY", "Available history contains missing/invalid usage, parse errors or unreadable files; selected-period completeness cannot be guaranteed.")
    if not selected:
        warn("USAGE_NO_OBSERVATIONS", "No retained usage matches this range and model selection; this does not establish zero actual usage.")
    status = "not_available" if not selected else "partial" if warnings else "complete"
    return {
        "schema_version": "1.0.0", "status": status,
        "period": selected_period, "filters": {"models": models},
        "summary": summary,
        # Observed calendar days only: gaps do not establish inactivity.
        "by_day": [dict(date=day, **aggregate(group, prices)) for day, group in sorted(day_groups.items())],
        "by_model": [dict(model=model, rate_models=sorted(model_rate_groups[model]), **aggregate(group, prices))
                     for model, group in sorted(model_groups.items())],
        "by_auth_mode": [dict(auth_mode=mode, **aggregate(auth_groups[mode], prices))
                         for mode in auto_review_policy.AUTH_MODES],
        "by_activity": [dict(activity=activity, **aggregate(activity_groups[activity], prices))
                        for activity in ("agent", "auto_review")],
        # Disjoint rows are the CSV source; their counts/tokens/subtotals reconcile.
        "rows": [dict(model=model, auth_mode=mode, activity=activity, **aggregate(group, prices))
                 for (model, mode, activity), group in sorted(groups.items())],
        "coverage": coverage,
        "pricing": {
            "basis": "CQA Standard token-rate equivalent",
            "source": audit.RATECARD_SOURCE, "source_ref": audit.RATECARD_SOURCE_REF,
            "as_of": audit.RATECARD_AS_OF, "custom_price_override": custom_prices,
            "rates_per_million": {model: {"uncached_input": prices[model][0], "cached_input": prices[model][1],
                                           "output": prices[model][2]} for model in sorted(resolved_models) if model in prices},
            "auto_review_mapping": {"transition_at": iso_utc(audit.AUTO_REVIEW_TRANSITION_AT),
                                    "before": audit.AUTO_REVIEW_LEGACY_MODEL, "on_or_after": audit.AUTO_REVIEW_CURRENT_MODEL},
            "long_context": {"threshold_input_tokens": audit.LONG_CONTEXT_THRESHOLD_INPUT_TOKENS,
                             "input_multiplier": audit.LONG_CONTEXT_INPUT_MULTIPLIER,
                             "cached_multiplier": audit.LONG_CONTEXT_CACHED_MULTIPLIER,
                             "output_multiplier": audit.LONG_CONTEXT_OUTPUT_MULTIPLIER,
                             "exempt_models": sorted(audit.LONG_CONTEXT_EXEMPT_MODELS)},
            "limitations": ["API equivalent is observed token work, not billing or maximum plan capacity.",
                            "Cache-write, tool, fast-mode and regional charges are not modeled or inferred.",
                            "ChatGPT sign-in evidence does not distinguish included allowance from purchased credits.",
                            "Unknown sign-in evidence remains separate; current credentials are never used to label history."],
        },
        "warnings": warnings,
    }
