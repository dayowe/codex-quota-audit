"""Date-aware Auto-review quota policy, separate from observed work and pricing.

The announcement timestamp is a reporting reference, not evidence of the exact
server activation time. Authentication facts come only from structured rollout
metadata; a declaration fills missing evidence without overriding known facts.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional

ANNOUNCED_AT = datetime(2026, 10, 6, 7, 13, 54, 94000, tzinfo=timezone.utc)
TRANSITION_START = datetime(2026, 10, 6, tzinfo=timezone.utc)
SOURCE_REF = "x.com/thsottiaux/status/2107368734981517634"
STATUSES = ("historical", "free", "transition", "unknown", "outside_scope")
AUTH_MODES = ("unknown", "chatgpt", "api")
AUTH_SOURCES = ("unknown", "rollout_auth_mode", "rate_limit_plan_type", "conflicting_metadata", "declared")
NOTE = (
    "Auto-review is free for ChatGPT-account sign-in under the October 6, 2026 announcement. "
    "Publication time is the policy reference; exact activation and retroactivity are unspecified. "
    "Earlier October 6 UTC activity is transitional. API-key billing is outside the announcement. "
    "Observed meter movement may include concurrent work. Tokens and API-list equivalents remain work measures."
)


def auth_mode(value: object) -> str:
    text = str(value or "").lower().replace("-", "_")
    return {"chatgpt": "chatgpt", "api": "api", "api_key": "api", "apikey": "api"}.get(text, "unknown")


def auth_evidence(payload: Mapping, record_type: object) -> Optional[tuple[str, str]]:
    """Read only allowlisted metadata, never credentials or tool/message content."""
    if record_type not in {"session_meta", "turn_context", "event_msg"}:
        return None
    if record_type == "event_msg" and payload.get("type") not in {
        "token_count", "token_usage_record", "thread_settings_applied",
    }:
        return None
    explicit = [auth_mode(payload[k]) for k in ("auth_mode", "authentication_mode") if k in payload]
    if explicit:
        if len(set(explicit)) != 1:
            return "unknown", "conflicting_metadata"
        # Explicit authentication takes precedence over a possibly stale meter snapshot.
        return explicit[0], "rollout_auth_mode"
    limits = payload.get("rate_limits")
    if isinstance(limits, Mapping) and limits.get("plan_type") in {
        "free", "go", "plus", "pro", "team", "business", "enterprise", "edu",
    }:
        return "chatgpt", "rate_limit_plan_type"
    return None


def classify(ts: object, mode: str = "unknown", source: str = "unknown",
             declaration: str = "unknown") -> dict:
    mode = auth_mode(mode)
    source = source if source in AUTH_SOURCES else "unknown"
    # A conflict is not merely missing metadata and cannot be resolved by assumption.
    if mode == "unknown" and source != "conflicting_metadata" and declaration in {"chatgpt", "api"}:
        mode, source = declaration, "declared"
    if mode == "api":
        status = "outside_scope"
    elif not isinstance(ts, datetime) or ts.tzinfo is None:
        status = "unknown"
    elif ts < TRANSITION_START:
        # Historical fits remain observational and require meter/pairing evidence.
        status = "historical"
    elif ts < ANNOUNCED_AT:
        status = "transition"
    elif mode == "chatgpt":
        status = "free"
    else:
        status = "unknown"
    return {"status": status, "auth_mode": mode, "auth_source": source,
            "policy_quota_points": 0.0 if status == "free" else None}


def update_auth(mode: str, source: str, evidence: Optional[tuple[str, str]]) -> tuple[str, str]:
    if evidence is None:
        return mode, source
    # A subscription label in a meter snapshot cannot override explicit sign-in
    # metadata from this rollout. A later explicit auth record can update it.
    if evidence[1] == "rate_limit_plan_type" and source in {"rollout_auth_mode", "conflicting_metadata"}:
        return mode, source
    return evidence


def observation(item: object, declaration: str = "unknown") -> dict:
    return classify(getattr(item, "ts", None), getattr(item, "auth_mode", "unknown"),
                    getattr(item, "auth_source", "unknown"), declaration)


def summarize(items: Iterable[tuple[Mapping, int]], declaration: str = "unknown") -> dict:
    tokens, requests = Counter(), Counter()
    bases, modes = set(), set()
    for policy, count in items:
        status = policy.get("status", "unknown")
        status = status if status in STATUSES else "unknown"
        if isinstance(policy.get("tokens_by_status"), Mapping):
            for part in STATUSES:
                tokens[part] += max(0, int(policy["tokens_by_status"].get(part, 0)))
                requests[part] += max(0, int(policy.get("requests_by_status", {}).get(part, 0)))
        else:
            tokens[status] += max(0, int(count))
            requests[status] += 1
        evidence = policy.get("auth_bases") or [policy.get("auth_source", "unknown")]
        for basis in evidence:
            bases.add(basis if isinstance(basis, str) and basis in AUTH_SOURCES else "unknown")
        for mode in policy.get("auth_modes") or [policy.get("auth_mode", "unknown")]:
            modes.add(mode if isinstance(mode, str) and mode in AUTH_MODES else "unknown")
    present = [s for s in STATUSES if requests[s]]
    status = present[0] if len(present) == 1 else ("mixed" if present else "none")
    return {"version": 1, "status": status, "announced_at": ANNOUNCED_AT.isoformat().replace("+00:00", "Z"),
            "source_ref": SOURCE_REF, "note": NOTE,
            "auth_declaration": declaration if declaration in AUTH_MODES else "unknown",
            "auth_bases": sorted(bases),
            "auth_modes": sorted(modes),
            "tokens_by_status": {s: tokens[s] for s in STATUSES},
            "requests_by_status": {s: requests[s] for s in STATUSES},
            "policy_quota_points": 0.0 if status == "free" else None}


def episode_policy(row: Mapping) -> Mapping:
    policy = row.get("quota_policy")
    return policy if isinstance(policy, Mapping) else classify(row.get("start"))


def historical_fit_eligible(row: Mapping) -> bool:
    return episode_policy(row).get("status") == "historical" and row.get("historical_fit_eligible", True)


def normalize(value: object) -> Optional[dict]:
    """Privacy-safe additive extension for imported profiler/quota data."""
    if not isinstance(value, Mapping) or value.get("status") not in (*STATUSES, "mixed", "none"):
        return None
    out = summarize([], str(value.get("auth_declaration", "unknown")))
    out["status"] = value["status"]
    for field in ("tokens_by_status", "requests_by_status"):
        raw = value.get(field)
        if isinstance(raw, Mapping):
            for status in STATUSES:
                try:
                    out[field][status] = max(0, int(raw.get(status, 0)))
                except (ValueError, TypeError, OverflowError):
                    pass
    bases = value.get("auth_bases")
    out["auth_bases"] = sorted({x for x in bases if isinstance(x, str) and x in AUTH_SOURCES}) if isinstance(bases, list) else []
    modes = value.get("auth_modes")
    out["auth_modes"] = sorted({x for x in modes if isinstance(x, str) and x in AUTH_MODES}) if isinstance(modes, list) else []
    out["policy_quota_points"] = 0.0 if out["status"] == "free" else None
    estimate = value.get("historical_estimate")
    if isinstance(estimate, Mapping):
        import math
        cooked = {}
        for key in ("value", "lo", "hi"):
            number = estimate.get(key)
            if isinstance(number, (int, float)) and math.isfinite(number) and number >= 0:
                cooked[key] = number
        if "value" in cooked:
            out["historical_estimate"] = cooked
    return out


def add_csv_fields(row: Mapping) -> dict:
    policy = episode_policy(row)
    tokens = policy.get("tokens_by_status", {})
    historical = policy.get("historical_estimate", {})
    return {"auto_review_policy_status": policy.get("status", "unknown"),
            "policy_quota_points": policy.get("policy_quota_points"),
            "historical_guardian_tokens": tokens.get("historical", 0),
            "free_guardian_tokens": tokens.get("free", 0),
            "uncertain_guardian_tokens": sum(tokens.get(s, 0) for s in ("transition", "unknown", "outside_scope")),
            "auto_review_policy_source": SOURCE_REF,
            "auto_review_policy_announced_at": ANNOUNCED_AT.isoformat().replace("+00:00", "Z"),
            "historical_estimated_guardian_points": historical.get("value"),
            "historical_estimated_guardian_points_lo": historical.get("lo"),
            "historical_estimated_guardian_points_hi": historical.get("hi"),
            "auto_review_auth_bases": ",".join(policy.get("auth_bases", [])),
            "auto_review_auth_modes": ",".join(policy.get("auth_modes", [])),
            "auto_review_auth_declaration": policy.get("auth_declaration", "unknown")}


CSV_FIELDS = tuple(add_csv_fields({}))


def add_arguments(parser) -> None:
    parser.add_argument("--auto-review-auth-mode", choices=AUTH_MODES, default="unknown",
                        help="declare historical sign-in mode for Auto-review where rollout evidence is missing; "
                             "chatgpt enables the announced free policy, api is outside its scope (default: unknown)")
